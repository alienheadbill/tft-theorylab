"""Publish the local collector's public snapshot to the website.

`tftlab local-publish` chains the steps the owner did by hand for the 18.3
and 18.4 snapshots (README, "Publishing a snapshot to the website"). The
guarantee it is built around: a failed or ambiguous publication leaves
production on the previously verified snapshot, never deletes a release that
could be needed for recovery, and fails closed when collection provenance is
uncertain.

Before anything is changed:
  1. gates -- this checkout is exactly `origin/main` with no local changes
     to tracked files (so the snapshot's analytics version is the deployed
     code's); the newest data-changing collection report is readable and a
     success; there is a current trusted patch; a patch the site does not
     serve yet has at least `MIN_FIRST_PUBLISH_MATCHES` current-patch
     matches; otherwise at least `MIN_NEW_MATCHES` new matches compared
     patch by patch with the live site;
  2. a read-only Render preflight -- the key works, exactly one web service
     serves `TFT_PUBLIC_SITE_URL`, and its current `TFT_SNAPSHOT_URL` /
     `TFT_SNAPSHOT_SHA256` are a usable rollback target, recorded together
     with what the live site reports (`/api/source`);
  3. the snapshot -- `tftlab local-snapshot`'s exporter (sanitized, opaque
     ids, full completed-ingest provenance, prepared Discovery, verified
     twice). If a collection holds the collector lock, publication is
     skipped cleanly;
  4. a new, immutable GitHub Release with exactly the `.gz` and its
     manifest, with its uploaded sizes checked.

Then, as one unit (`Production`): the two Render variables, a deploy, the
live site serving the new snapshot (`/api/source`) and prepared Discovery
(`/api/discovery`). Any failure after the first variable changed restores
the previous values; if a new deploy went live (or might still), the
previous snapshot is actively redeployed, and the rollback counts only once
the live site reports the previous snapshot again. A rollback that cannot be
verified is reported as a high-severity failure needing manual inspection.

Retention runs only after complete success, deletes only releases this
command marked, never the new release or the one production served before,
and a retention problem is a warning, never a reason to roll back.

Nothing here contacts Riot or any database server. The Render API key is
read from `RENDER_API_KEY` (`.env`, git-ignored), sent only to
api.render.com, and never printed or reported. Every external call goes
through the injected `run` (subprocess) and `http` (httpx) so tests run
offline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from .analytics import available_balance_windows
from .local_collector import CollectorBusy, LocalSnapshotError
from .public_snapshot import SnapshotExportError
from .storage import Database

RENDER_API = "https://api.render.com/v1"
RENDER_KEY_ENV = "RENDER_API_KEY"
SITE_URL_ENV = "TFT_PUBLIC_SITE_URL"
RENDER_KEY_RE = re.compile(r"^rnd_[A-Za-z0-9]{16,}$")
SNAPSHOT_URL_VAR = "TFT_SNAPSHOT_URL"
SNAPSHOT_SHA_VAR = "TFT_SNAPSHOT_SHA256"
SNAPSHOT_VARS = (SNAPSHOT_URL_VAR, SNAPSHOT_SHA_VAR)
RELEASE_URL_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/releases/download/([^/]+)/[^/]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SNAPSHOT_ASSET = "theorylabs-public-snapshot.sqlite3.gz"
MANIFEST_ASSET = "theorylabs-public-snapshot.sqlite3.manifest.json"
TAG_PREFIX = "public-snapshot-"
#: In every release body this command writes; retention only ever deletes those.
PUBLISH_MARKER = "Published automatically by `tftlab local-publish`."

#: New matches (compared patch by patch with the live site) needed before a new snapshot is worth publishing.
MIN_NEW_MATCHES = 200
#: Current-patch matches needed before a patch the site does not serve yet is published (previous-patch
#: matches never count towards it).
MIN_FIRST_PUBLISH_MATCHES = MIN_NEW_MATCHES
#: Below this, the previous patch is exported alongside the current one.
MIN_CURRENT_MATCHES_ALONE = 1000
KEEP_PUBLISHED_RELEASES = 5

DEPLOY_TIMEOUT_S = 30 * 60
SITE_TIMEOUT_S = 10 * 60
POLL_INTERVAL_S = 15.0
#: Guard against a Render listing that never ends.
MAX_RENDER_PAGES = 50
DEPLOY_LIVE = "live"
DEPLOY_FAILED = frozenset({"build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated"})

PUBLISHED = "published"
SKIPPED = "skipped"
DRY_RUN = "dry_run"
FAILED = "failed"

#: `PublishReport.rollback` values.
ROLLBACK_NOT_NEEDED = "not_needed"
ROLLBACK_VERIFIED = "verified"
ROLLBACK_UNVERIFIED = "unverified"

COLLECTION_RUNNING_MESSAGE = "Collection is still running; publication was skipped."


class PublishError(RuntimeError):
    """A step failed; the message says what was (not) changed."""


@dataclass
class Step:
    name: str
    status: str  # ok | skip | fail | info | warn
    detail: str


@dataclass
class PublishReport:
    outcome: str = FAILED
    message: str = ""
    steps: list[Step] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    matches: int | None = None
    live: dict[str, Any] = field(default_factory=dict)
    previous: dict[str, Any] = field(default_factory=dict)
    service_id: str | None = None
    tag: str | None = None
    url: str | None = None
    sha256: str | None = None
    deploy_id: str | None = None
    rollback: str | None = None
    rollback_deploy_id: str | None = None
    deleted_releases: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == FAILED else 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishConfig:
    db_path: Path
    repo_root: Path
    site_url: str
    render_api_key: str | None
    dry_run: bool = False
    force: bool = False
    min_new_matches: int = MIN_NEW_MATCHES
    min_first_publish_matches: int = MIN_FIRST_PUBLISH_MATCHES
    min_current_alone: int = MIN_CURRENT_MATCHES_ALONE
    keep_releases: int = KEEP_PUBLISHED_RELEASES


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(repo_root: Path) -> Runner:
    def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(cmd), cwd=repo_root, capture_output=True, text=True, encoding="utf-8")
    return run


def gh_executable() -> str:
    """`gh` on PATH, else the GitHub CLI's default Windows install location."""
    found = shutil.which("gh")
    if found:
        return found
    default = Path(r"C:\Program Files\GitHub CLI\gh.exe")
    if default.exists():
        return str(default)
    raise PublishError("The GitHub CLI (gh) is not installed. Install it and run `gh auth login` once.")


def _ok(run: Runner, cmd: Sequence[str], what: str) -> str:
    done = run(cmd)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise PublishError(f"{what} failed: {detail[-1] if detail else f'exit {done.returncode}'}")
    return done.stdout


# ---------------------------------------------------------------- gates


def git_gate(run: Runner) -> str:
    """This checkout must be exactly `origin/main` with no changes to tracked
    files, so the snapshot is prepared by the code Render deploys. Returns
    the commit."""
    _ok(run, ["git", "fetch", "--quiet", "origin", "main"], "git fetch")
    head = _ok(run, ["git", "rev-parse", "HEAD"], "git rev-parse").strip()
    main = _ok(run, ["git", "rev-parse", "origin/main"], "git rev-parse").strip()
    if head != main:
        raise PublishError(f"This checkout is at {head[:7]}, not origin/main ({main[:7]}). Update it (`git pull` on "
                           "main) so the snapshot is prepared by the code the website runs. Nothing was published.")
    dirty = _ok(run, ["git", "status", "--porcelain", "--untracked-files=no"], "git status").strip()
    if dirty:
        raise PublishError("This checkout has local changes to tracked files, so its analytics may differ from the "
                           "website's. Commit or discard them first. Nothing was published.")
    return head


def latest_collection_gate(reports_dir: Path) -> str:
    """The newest data-changing `local-collect` report must be a readable success.

    Reports are read newest first (their names carry the UTC run time). A
    `preflight_failed` report changed nothing in the database (an expired
    key, a patch pause, ...) and is passed over to the one before it. Any
    other outcome blocks, and so does a report that cannot be read or has no
    outcome: it may be a collection that changed data and then crashed, so
    the state of the database is uncertain. Fails closed."""
    reports = sorted(reports_dir.glob("local-collect-*.json"), key=lambda p: p.name, reverse=True) \
        if reports_dir.exists() else []
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise PublishError(f"The collection report {path.name} cannot be read, so it is unknown whether that "
                               "collection changed data and finished. Check it (and run `tftlab local-collect` "
                               "again). Nothing was published.") from None
        outcome = data.get("outcome") if isinstance(data, dict) else None
        if not isinstance(outcome, str) or not outcome:
            raise PublishError(f"The collection report {path.name} has no outcome, so it is unknown whether that "
                               "collection finished. Run `tftlab local-collect` again. Nothing was published.")
        if outcome == "preflight_failed":
            continue
        if outcome != "success":
            raise PublishError(f"The latest collection that changed data did not succeed ({outcome}; {path.name}). "
                               "Run `tftlab local-collect` again and check its report. Nothing was published.")
        return path.name
    raise PublishError("No successful collection report was found. Run `tftlab local-collect` first. "
                       "Nothing was published.")


def choose_windows(db: Database, current: Sequence[str], *, min_current_alone: int) -> tuple[list[str], dict[str, int]]:
    """The current patch's balance window(s), plus the most recent other
    stored window while the current patch has fewer than
    `min_current_alone` matches. Returns (windows, match count per window)."""
    stored = available_balance_windows(db)
    counts = {w: n for w, n, _ in stored}
    windows = list(current)
    if sum(counts.get(w, 0) for w in windows) < min_current_alone:
        previous = next((w for w, _, _ in stored if w not in windows), None)
        if previous is not None:
            windows = [previous, *windows]
    return windows, {w: counts.get(w, 0) for w in windows}


def publication_gate(live: dict[str, Any], windows: Sequence[str], counts: dict[str, int], current: Sequence[str], *,
                     min_new: int, min_first: int, live_discovery: str | None = None,
                     live_champions: str | None = None) -> tuple[str | None, str]:
    """(why not to publish, or None; what was compared).

    Each current balance window the live site does not serve yet is a first
    publication of that window: it needs `min_first` matches of its OWN.
    Neither the previous patch included alongside nor an older balance
    window of the same client patch (`18.2a` next to a new `18.2b`) ever
    counts towards it. Otherwise new data is compared window by window with
    the live site's own per-window counts (`/api/balance-windows`), never
    total against total, and needs `min_new` new matches -- unless the live
    site's Discovery for the current window is `stale` (`live_discovery`):
    the deployed code's analytics version no longer matches the snapshot
    (e.g. after a code change), so the site computes Discovery live until a
    snapshot prepared by that code is published. Stale prepared champion
    pages (`live_champions`, from `/api/champions`) waive it the same way.
    That waives only the new-matches minimum, never a new window's own floor."""
    live_counts: dict[str, int] = live.get("window_matches") or {} if live.get("mode") == "snapshot" else {}
    new_windows = [w for w in current if w not in live_counts]
    if new_windows:
        own = ", ".join(f"{w}: {counts.get(w, 0)}" for w in new_windows)
        short = [w for w in new_windows if counts.get(w, 0) < min_first]
        if short:
            return (f"first publication of balance window {', '.join(short)}: {own} matches of its own, at least "
                    f"{min_first} are needed per new window (previous-patch matches and older balance windows of "
                    "the same patch do not count); the site keeps its current snapshot",
                    f"first publication of {', '.join(new_windows)}")
        return None, f"first publication of {', '.join(new_windows)}: {own} matches of its own (at least {min_first} each)"
    comparable = [w for w in windows if w in live_counts]
    new = sum(max(0, counts.get(w, 0) - int(live_counts[w] or 0)) for w in comparable)
    label = ", ".join(comparable)
    stale = [what for what, status in (("Discovery", live_discovery), ("champion pages", live_champions))
             if status == "stale"]
    if new < min_new and stale:
        return None, (f"the site computes {' and '.join(stale)} for {current[0]} live (prepared data is stale: the "
                      f"deployed analytics version changed), so the snapshot is republished with {new} new matches "
                      f"in {label}")
    if new < min_new:
        return (f"only {new} new matches in {label} since the snapshot the site serves (at least {min_new} are "
                "needed); the site keeps its current snapshot", f"new matches in {label}")
    return None, f"{new} new matches in {label} since the snapshot the site serves (at least {min_new})"


# ---------------------------------------------------------------- the live site


def _get_json(http: httpx.Client, url: str, what: str, **kwargs: Any) -> Any:
    try:
        response = http.get(url, timeout=180, **kwargs)
    except httpx.HTTPError as exc:
        raise PublishError(f"Could not reach {what} ({type(exc).__name__}).") from None
    if response.status_code != 200:
        raise PublishError(f"{what} answered HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise PublishError(f"{what} did not answer with JSON.") from None


def live_snapshot(http: httpx.Client, site_url: str) -> dict[str, Any]:
    """What the website serves now (`/api/source`): its snapshot provenance."""
    body = _get_json(http, f"{site_url}/api/source", f"{site_url}/api/source")
    snap = (body.get("snapshot") if isinstance(body, dict) else None) or {}
    return {"mode": body.get("mode") if isinstance(body, dict) else None,
            "balance_windows": list(snap.get("balance_windows") or []),
            "matches": snap.get("matches"), "exported_at": snap.get("exported_at")}


def live_state(http: httpx.Client, site_url: str) -> dict[str, Any]:
    """`live_snapshot` plus the live per-window match counts (`/api/balance-windows`)."""
    state = live_snapshot(http, site_url)
    body = _get_json(http, f"{site_url}/api/balance-windows", f"{site_url}/api/balance-windows")
    rows = body.get("windows") if isinstance(body, dict) else None
    state["window_matches"] = {str(r["balance_window"]): int(r.get("matches") or 0)
                               for r in rows or [] if isinstance(r, dict) and r.get("balance_window")}
    return state


def discovery_status(http: httpx.Client, site_url: str, window: str) -> str:
    """The live `/api/discovery` prepared status for `window`, or a failure
    class ("HTTP 500", "unreachable (...)", "unreadable response")."""
    return _prepared_status(http, f"{site_url}/api/discovery", window)


def champions_status(http: httpx.Client, site_url: str, window: str) -> str:
    """The live `/api/champions` (champion picker) prepared status for
    `window`: champion pages come from the same prepared run."""
    return _prepared_status(http, f"{site_url}/api/champions", window)


def _prepared_status(http: httpx.Client, url: str, window: str) -> str:
    try:
        response = http.get(url, params={"balance_window": window}, timeout=180)
    except httpx.HTTPError as exc:
        return f"unreachable ({type(exc).__name__})"
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    try:
        prepared = (response.json() or {}).get("prepared") or {}
    except (ValueError, AttributeError):
        return "unreadable response"
    return str(prepared.get("status") or "missing")


# ---------------------------------------------------------------- GitHub Releases


def release_tag(windows: Sequence[str], latest_game_iso: str, existing: set[str]) -> str:
    """`public-snapshot-<windows>-<YYYYMMDD of the data>[-rN]`, never reusing a tag."""
    day = latest_game_iso[:10].replace("-", "")
    base = f"{TAG_PREFIX}{'_'.join(windows)}-{day}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-r{n}" in existing:
        n += 1
    return f"{base}-r{n}"


def _releases(run: Runner, gh: str, repo: str) -> list[dict[str, Any]]:
    out = _ok(run, [gh, "api", f"repos/{repo}/releases?per_page=100"], "listing GitHub releases")
    return [{"tag": r.get("tag_name"), "body": r.get("body") or "", "created_at": r.get("created_at") or ""}
            for r in json.loads(out or "[]")]


def create_release(run: Runner, gh: str, repo: str, tag: str, commit: str, files: Sequence[Path], notes: str) -> None:
    _ok(run, [gh, "release", "create", tag, *map(str, files), "--repo", repo, "--target", commit,
              "--title", f"Public snapshot {tag.removeprefix(TAG_PREFIX)}", "--notes", notes],
        "creating the GitHub release")
    out = _ok(run, [gh, "release", "view", tag, "--repo", repo, "--json", "assets"], "reading the GitHub release")
    uploaded = {a.get("name"): a.get("size") for a in json.loads(out).get("assets") or []}
    for path in files:
        if uploaded.get(path.name) != path.stat().st_size:
            raise PublishError(f"The release {tag} does not hold {path.name} at its full size. The website was "
                               "not changed; the release is kept for diagnosis (delete it before retrying the same day).")


def prune_releases(run: Runner, gh: str, repo: str, keep: int, protect: set[str]) -> list[str]:
    """Delete this command's own releases beyond the newest `keep`. Releases
    without `PUBLISH_MARKER` (published by hand or by a workflow) are never
    touched, and neither is any tag in `protect`."""
    own = sorted((r for r in _releases(run, gh, repo) if PUBLISH_MARKER in r["body"] and r["tag"]),
                 key=lambda r: r["created_at"], reverse=True)
    deleted = []
    for r in own[keep:]:
        if r["tag"] in protect:
            continue
        _ok(run, [gh, "release", "delete", r["tag"], "--repo", repo, "--yes", "--cleanup-tag"],
            f"deleting the old release {r['tag']}")
        deleted.append(r["tag"])
    return deleted


# ---------------------------------------------------------------- Render


class Render:
    """The few Render API calls publishing needs (bearer auth, api.render.com only)."""

    def __init__(self, http: httpx.Client, api_key: str) -> None:
        self.http = http
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def _call(self, method: str, path: str, what: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, f"{RENDER_API}{path}", headers=self._headers, timeout=60, **kwargs)
        except httpx.HTTPError as exc:
            raise PublishError(f"Render API: {what} failed ({type(exc).__name__}).") from None
        if response.status_code == 401:
            raise PublishError("Render API: the key was refused (401). Create a new API key in the Render dashboard "
                               "(Account Settings -> API Keys) and run `tftlab local-set-render-key`.")
        if response.status_code >= 400:
            raise PublishError(f"Render API: {what} failed (HTTP {response.status_code}).")
        try:
            return response.json() if response.content else None
        except ValueError:
            raise PublishError(f"Render API: {what} returned something that is not JSON.") from None

    def _pages(self, path: str, what: str) -> list[dict[str, Any]]:
        """Every item of a cursor-paginated listing; fails closed if it cannot be read to its end."""
        items: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_RENDER_PAGES):
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            page = self._call("GET", path, what, params=params)
            if not isinstance(page, list):
                raise PublishError(f"Render API: {what} returned an unexpected shape.")
            items.extend(i for i in page if isinstance(i, dict))
            if len(page) < 100:
                return items
            cursor = page[-1].get("cursor")
            if not cursor:
                raise PublishError(f"Render API: {what} could not be read to its end (no cursor).")
        raise PublishError(f"Render API: {what} did not end after {MAX_RENDER_PAGES} pages.")

    def service_for(self, site_url: str) -> str:
        """The id of the one web service whose public URL is `site_url`."""
        matches = {(i.get("service") or {}).get("id") for i in self._pages("/services", "listing services")
                   if ((i.get("service") or {}).get("serviceDetails") or {}).get("url", "").rstrip("/") == site_url}
        matches.discard(None)
        if not matches:
            raise PublishError(f"Render API: no service serves {site_url}. Check {SITE_URL_ENV} in .env.")
        if len(matches) > 1:
            raise PublishError(f"Render API: {len(matches)} services claim {site_url}; refusing to guess which one "
                               "to change.")
        return matches.pop()

    def env_values(self, service_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        found: dict[str, str | None] = {k: None for k in keys}
        for item in self._pages(f"/services/{service_id}/env-vars", "reading environment variables"):
            var = item.get("envVar") or {}
            if var.get("key") in found:
                found[var["key"]] = var.get("value")
        return found

    def set_env(self, service_id: str, key: str, value: str) -> None:
        self._call("PUT", f"/services/{service_id}/env-vars/{key}", f"setting {key}", json={"value": value})

    def deploy(self, service_id: str) -> str:
        body = self._call("POST", f"/services/{service_id}/deploys", "starting a deploy", json={})
        if not isinstance(body, dict) or not body.get("id"):
            raise PublishError("Render API: the deploy was queued without an id; check the Render dashboard.")
        return str(body["id"])

    def deploy_status(self, service_id: str, deploy_id: str) -> str:
        body = self._call("GET", f"/services/{service_id}/deploys/{deploy_id}", "reading the deploy") or {}
        return str(body.get("status") or "")

    def cancel_deploy(self, service_id: str, deploy_id: str) -> None:
        self._call("POST", f"/services/{service_id}/deploys/{deploy_id}/cancel", "cancelling a deploy")


@dataclass(frozen=True)
class RollbackTarget:
    """What production serves before anything changes: the values to put
    back, and what the live site must report again for a rollback to count."""

    service_id: str
    url: str
    sha256: str
    tag: str | None
    exported_at: str
    balance_windows: tuple[str, ...]
    matches: int | None

    def describe(self) -> dict[str, Any]:
        return {"url": self.url, "sha256": self.sha256, "tag": self.tag, "exported_at": self.exported_at,
                "balance_windows": list(self.balance_windows), "matches": self.matches}


def render_preflight(render: Render, site_url: str, live: dict[str, Any]) -> RollbackTarget:
    """Read-only: the service, its current snapshot variables, and the live
    snapshot identity. Refuses unless they form a usable rollback target."""
    service_id = render.service_for(site_url)
    values = render.env_values(service_id, SNAPSHOT_VARS)
    url, sha = values[SNAPSHOT_URL_VAR], values[SNAPSHOT_SHA_VAR]
    if not url or not url.startswith("https://") or not sha or not SHA256_RE.fullmatch(sha):
        raise PublishError(f"The website's current {SNAPSHOT_URL_VAR} / {SNAPSHOT_SHA_VAR} on Render are missing or "
                           "malformed, so there is no known snapshot to roll back to. Automatic publication refuses "
                           "to start without one. Nothing was published.")
    if live.get("mode") != "snapshot" or not live.get("exported_at"):
        raise PublishError("The website does not report a snapshot it serves now (/api/source), so a rollback "
                           "could not be verified. Nothing was published.")
    match = RELEASE_URL_RE.fullmatch(url)
    return RollbackTarget(service_id=service_id, url=url, sha256=sha, tag=match.group(1) if match else None,
                          exported_at=str(live["exported_at"]), balance_windows=tuple(live.get("balance_windows") or ()),
                          matches=live.get("matches"))


def wait_for(check: Callable[[], bool | None], *, timeout_s: float, interval_s: float,
             sleep: Callable[[float], None], clock: Callable[[], float]) -> bool:
    """Poll `check` until it returns True (done), False (failed) or the timeout."""
    deadline = clock() + timeout_s
    while True:
        result = check()
        if result is not None:
            return result
        if clock() >= deadline:
            return False
        sleep(interval_s)


class Production:
    """Every change to the production service, and how to undo it.

    `deploy_state` is None (no deploy started), "failed" (the new deploy
    reached a failed state, so the previous instance kept serving),
    "pending" (started, outcome unknown: it might still go live) or "live".
    `rollback()` puts back only the variables that differ from the target,
    and redeploys the previous snapshot whenever the new deploy went live or
    might still; it then verifies the live site serves the previous snapshot
    again. It returns (ROLLBACK_* value, detail) and never raises."""

    def __init__(self, render: Render, target: RollbackTarget, http: httpx.Client, site_url: str, *,
                 sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
        self.render, self.target, self.http, self.site = render, target, http, site_url
        self.sleep, self.clock = sleep, clock
        self.touched = False
        self.deploy_id: str | None = None
        self.deploy_state: str | None = None
        self.rollback_deploy_id: str | None = None

    def set_snapshot(self, url: str, sha256: str) -> None:
        self.touched = True  # from the first attempt on: a failed PUT may still have applied
        self.render.set_env(self.target.service_id, SNAPSHOT_URL_VAR, url)
        self.render.set_env(self.target.service_id, SNAPSHOT_SHA_VAR, sha256)

    def deploy_and_wait(self) -> bool:
        self.deploy_id = self.render.deploy(self.target.service_id)
        self.deploy_state = "pending"

        def done() -> bool | None:
            status = self.render.deploy_status(self.target.service_id, self.deploy_id or "")
            return True if status == DEPLOY_LIVE else False if status in DEPLOY_FAILED else None

        finished = wait_for(done, timeout_s=DEPLOY_TIMEOUT_S, interval_s=POLL_INTERVAL_S, sleep=self.sleep,
                            clock=self.clock)
        if finished:
            self.deploy_state = "live"
        elif self.render.deploy_status(self.target.service_id, self.deploy_id) in DEPLOY_FAILED:
            self.deploy_state = "failed"
        return finished

    def _serves(self, exported_at: str) -> bool:
        def check() -> bool | None:
            try:
                return True if live_snapshot(self.http, self.site)["exported_at"] == exported_at else None
            except PublishError:
                return None
        return wait_for(check, timeout_s=SITE_TIMEOUT_S, interval_s=POLL_INTERVAL_S, sleep=self.sleep, clock=self.clock)

    def serves_new(self, exported_at: str) -> bool:
        return self._serves(exported_at)

    def rollback(self) -> tuple[str, str]:
        if not self.touched:
            return ROLLBACK_NOT_NEEDED, "nothing on Render was changed"
        sid, problems = self.target.service_id, []
        wanted = {SNAPSHOT_URL_VAR: self.target.url, SNAPSHOT_SHA_VAR: self.target.sha256}
        try:
            current = self.render.env_values(sid, SNAPSHOT_VARS)
        except PublishError as exc:
            problems.append(f"could not read the variables back ({exc})")
            current = {k: None for k in SNAPSHOT_VARS}
        for key, value in wanted.items():
            if current.get(key) != value:
                try:
                    self.render.set_env(sid, key, value)
                except PublishError as exc:
                    problems.append(f"could not restore {key} ({exc})")
        if self.deploy_state in ("live", "pending"):
            if self.deploy_state == "pending" and self.deploy_id:
                try:
                    self.render.cancel_deploy(sid, self.deploy_id)
                except PublishError:
                    pass  # it may already have finished; the rollback deploy below supersedes it
            try:
                self.rollback_deploy_id = self.render.deploy(sid)

                def done() -> bool | None:
                    status = self.render.deploy_status(sid, self.rollback_deploy_id or "")
                    return True if status == DEPLOY_LIVE else False if status in DEPLOY_FAILED else None

                if not wait_for(done, timeout_s=DEPLOY_TIMEOUT_S, interval_s=POLL_INTERVAL_S, sleep=self.sleep,
                                clock=self.clock):
                    problems.append(f"the rollback deploy {self.rollback_deploy_id} did not go live")
            except PublishError as exc:
                problems.append(f"the rollback deploy could not be started ({exc})")
        try:
            restored = self.render.env_values(sid, SNAPSHOT_VARS)
            if restored != wanted:
                problems.append("Render's snapshot variables still differ from the previous values")
        except PublishError as exc:
            problems.append(f"could not read the variables back ({exc})")
        if not problems and not self._serves(self.target.exported_at):
            problems.append(f"/api/source does not report the previous snapshot (exported {self.target.exported_at})")
        if problems:
            return ROLLBACK_UNVERIFIED, "; ".join(problems)
        return ROLLBACK_VERIFIED, (f"the site serves the previous snapshot again ({', '.join(self.target.balance_windows)}, "
                                   f"exported {self.target.exported_at})")


# ---------------------------------------------------------------- orchestration


def run_local_publish(
    config: PublishConfig,
    *,
    at_ms: int,
    current_windows: Callable[[Database], list[str]],
    make_snapshot: Callable[[list[str]], Any],
    run: Runner,
    http: httpx.Client,
    gh: Callable[[], str] = gh_executable,
    reports_dir: Path,
    progress: Callable[[Step], None] = lambda _s: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> PublishReport:
    """Gates, Render preflight, snapshot, release, production change with
    rollback, retention (see the module docstring). Never raises: the
    report's outcome, message and `rollback` say what happened and what was
    (not) changed.

    `current_windows(db)` is the current trusted patch's stored balance
    windows (`local_collector.default_snapshot_windows`); `make_snapshot(windows)`
    returns the exporter's `ExportResult`."""
    report = PublishReport()
    production: Production | None = None

    def step(name: str, status: str, detail: str) -> None:
        item = Step(name, status, detail)
        report.steps.append(item)
        progress(item)

    def finish(outcome: str, message: str) -> PublishReport:
        report.outcome, report.message = outcome, message
        return report

    def scrub(text: str) -> str:
        return text.replace(config.render_api_key, "[redacted]") if config.render_api_key else text

    def fail_after_change(reason: str) -> PublishReport:
        """A failure once production may have changed: roll back, verify, report."""
        assert production is not None
        state, detail = production.rollback()
        report.rollback, report.rollback_deploy_id = state, production.rollback_deploy_id
        kept = f" The release {report.tag} was kept for diagnosis." if report.tag else ""
        if state == ROLLBACK_UNVERIFIED:
            step("Rollback", "fail", scrub(detail))
            return finish(FAILED, scrub(
                f"HIGH SEVERITY: publication failed ({reason}) and the rollback could not be verified: {detail}. "
                f"Manual Render inspection is required: check {SNAPSHOT_URL_VAR} / {SNAPSHOT_SHA_VAR} on the service "
                f"and what /api/source reports (the previous snapshot was exported {production.target.exported_at})."
                + kept))
        step("Rollback", "ok", detail)
        return finish(FAILED, scrub(f"Publication failed ({reason}). Rollback verified: {detail}." + kept))

    site = config.site_url.rstrip("/")
    try:
        # 1. gates, before anything is exported or sent.
        if not site.startswith("https://"):
            raise PublishError(f"{SITE_URL_ENV} must be the website's https:// address (set it in .env).")
        if not config.dry_run and not config.render_api_key:
            raise PublishError(f"{RENDER_KEY_ENV} is not set. Create an API key in the Render dashboard (Account "
                               "Settings -> API Keys) and run `tftlab local-set-render-key`. Nothing was published.")
        commit = git_gate(run)
        step("Code", "ok", f"this checkout is origin/main ({commit[:7]}) with no local changes")
        step("Latest collection", "ok", f"succeeded ({latest_collection_gate(reports_dir)})")
        if not config.db_path.exists():
            raise PublishError(f"There is no local database at {config.db_path}. Nothing was published.")
        with Database.open_existing(config.db_path) as db:
            try:
                current = current_windows(db)
            except LocalSnapshotError as exc:  # no trusted window, or no matches in it yet
                step("Patch", "skip", str(exc))
                return finish(SKIPPED, f"Not published: {exc} The site keeps its current snapshot.")
            windows, counts = choose_windows(db, current, min_current_alone=config.min_current_alone)
        report.windows = windows
        step("Windows", "ok", ", ".join(f"{w} ({counts[w]} matches)" for w in windows)
             + (" -- the previous patch is included while the current one is thin" if len(windows) > len(current) else ""))
        report.live = live_state(http, site)
        step("Website now", "info", f"{', '.join(report.live['balance_windows']) or report.live['mode']}: "
             f"{report.live['matches']} matches, exported {report.live['exported_at']}")
        live_discovery = discovery_status(http, site, current[0]) if current[0] in report.live["window_matches"] else None
        report.live["discovery"] = live_discovery
        live_champions = champions_status(http, site, current[0]) if current[0] in report.live["window_matches"] else None
        report.live["champions"] = live_champions
        reason, compared = publication_gate(report.live, windows, counts, current, min_new=config.min_new_matches,
                                            min_first=config.min_first_publish_matches, live_discovery=live_discovery,
                                            live_champions=live_champions)
        if reason and not config.force:
            step("New data", "skip", reason)
            return finish(SKIPPED, f"Not published: {reason}.")
        step("New data", "ok", compared if not reason else f"{reason} -- published anyway (--force)")

        # 2. read-only Render preflight: a known rollback target before any release exists.
        render = Render(http, config.render_api_key) if config.render_api_key else None
        target = None
        if render is not None:
            target = render_preflight(render, site, report.live)
            report.service_id, report.previous = target.service_id, target.describe()
            step("Render", "ok", f"service {target.service_id}; rollback target "
                 f"{target.tag or target.url} (exported {target.exported_at})")
        else:
            step("Render", "info", "not checked (no RENDER_API_KEY; dry run)")

        # 3. the snapshot (the verified exporter; holds the collection lock).
        try:
            result = make_snapshot(windows)
        except CollectorBusy:
            step("Snapshot", "skip", COLLECTION_RUNNING_MESSAGE)
            return finish(SKIPPED, f"{COLLECTION_RUNNING_MESSAGE} Nothing was uploaded or changed; the next run "
                                   "tries again.")
        except (SnapshotExportError, LocalSnapshotError) as exc:
            raise PublishError(f"The snapshot could not be made or did not pass verification: {exc} "
                               "Nothing was published.") from None
        prov = result.provenance
        gz = result.gzip_path or result.path.with_name(result.path.name + ".gz")
        report.matches, report.sha256 = prov["matches"], result.files[gz.name]["sha256"]
        step("Snapshot", "ok", f"{prov['matches']} matches, {prov['boards']} boards, analytics "
             f"{prov['code_version']['analytics_version']}, sha256 {report.sha256[:12]}")
        if config.dry_run:
            return finish(DRY_RUN, "Dry run: the snapshot was made and verified locally. Nothing was uploaded or "
                                   "deployed.")
        assert render is not None and target is not None

        # 4. the GitHub Release (kept whatever happens next).
        gh_path = gh()
        repo = _ok(run, [gh_path, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                   "reading the GitHub repository").strip()
        releases = _releases(run, gh_path, repo)
        report.tag = release_tag(windows, prov["latest_game"], {r["tag"] for r in releases})
        notes = (f"{PUBLISH_MARKER}\n\nBalance windows: {', '.join(windows)}; {prov['matches']} matches, "
                 f"{prov['boards']} boards; latest game {prov['latest_game']}.\n"
                 f"Analytics version {prov['code_version']['analytics_version']}, code {commit}.\n"
                 f"`{gz.name}` sha256 {report.sha256}.")
        create_release(run, gh_path, repo, report.tag, commit, [gz, result.manifest_path], notes)
        report.url = f"https://github.com/{repo}/releases/download/{report.tag}/{gz.name}"
        step("GitHub release", "ok", report.tag)

        # 5. production, as one unit: any failure from here on is rolled back and the rollback verified.
        production = Production(render, target, http, site, sleep=sleep, clock=clock)
        try:
            production.set_snapshot(report.url, report.sha256)
            step("Render", "info", f"{SNAPSHOT_URL_VAR} and {SNAPSHOT_SHA_VAR} set")
            live = production.deploy_and_wait()
            report.deploy_id = production.deploy_id
            if not live:
                step("Render deploy", "fail", f"{report.deploy_id}: did not go live")
                return fail_after_change(f"the Render deploy {report.deploy_id} did not go live")
            step("Render deploy", "ok", f"{report.deploy_id}: live")
            if not production.serves_new(prov["exported_at"]):
                step("Website", "fail", "/api/source does not report the new snapshot")
                return fail_after_change("the website did not report the new snapshot after the deploy")
            step("Website", "ok", f"serves the new snapshot ({prov['matches']} matches)")
            latest = current[0]  # the newest current balance window (`current` is latest first)
            prepared = discovery_status(http, site, latest)
            if prepared != "current":
                step("Discovery", "fail", f"{latest}: prepared status {prepared}")
                return fail_after_change(f"Discovery for {latest} is not served from prepared data ({prepared})")
            step("Discovery", "ok", f"{latest} served from prepared data")
            # Champion pages computed live are slower but still correct, so this
            # is a warning, never a reason to roll the site back.
            champions = champions_status(http, site, latest)
            if champions == "current":
                step("Champions", "ok", f"{latest} served from prepared data")
            else:
                step("Champions", "warn", f"{latest}: prepared status {champions}")
                report.warnings.append(f"champion pages for {latest} are computed live (prepared status {champions})")
        except Exception as exc:  # anything unforeseen once production may have changed
            report.deploy_id = production.deploy_id
            text = str(exc) if isinstance(exc, PublishError) else f"unexpected error ({type(exc).__name__}: {exc})"
            step("Production", "fail", scrub(text))
            return fail_after_change(scrub(text))

        # 6. retention: only after complete success, and never a reason to roll back.
        report.rollback = ROLLBACK_NOT_NEEDED
        if target.tag is None:
            report.warnings.append("old releases were not pruned: the release production served before could not be "
                                   "identified from its URL, so no release is deleted (fail safe)")
        else:
            try:
                report.deleted_releases = prune_releases(run, gh_path, repo, config.keep_releases,
                                                         {report.tag, target.tag})
            except Exception as exc:
                report.warnings.append(scrub(f"old releases were not pruned ({exc})"))
        if report.deleted_releases:
            step("Old releases", "ok", f"deleted {', '.join(report.deleted_releases)}")
        for warning in report.warnings:
            step("Old releases", "warn", warning)
        return finish(PUBLISHED, f"Published {report.tag}: the website now serves {', '.join(windows)} "
                                 f"({prov['matches']} matches).")
    except PublishError as exc:
        step("Stopped", "fail", scrub(str(exc)))
        return finish(FAILED, scrub(str(exc)))
    except Exception as exc:  # unforeseen, before production was touched: still a saved, reported failure
        step("Stopped", "fail", f"unexpected error ({type(exc).__name__})")
        return finish(FAILED, scrub(f"Publishing stopped on an unexpected error ({type(exc).__name__}: {exc}). "
                                    + (f"The release {report.tag} exists and was kept; the website was not changed."
                                       if report.tag else "Nothing was published.")))


def write_publish_report(report: PublishReport, reports_dir: Path, *, at_ms: int) -> Path:
    """The run's report as JSON (no keys)."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = reports_dir / f"local-publish-{stamp}.json"
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str) + "\n")
    return path
