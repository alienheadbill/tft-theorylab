"""Publish the local collector's public snapshot to the website, unattended.

`tftlab local-publish` chains the steps the owner did by hand for the 18.3
and 18.4 snapshots (README, "Publishing a snapshot to the website"), with
checks before anything leaves this computer:

  1. gates -- this checkout is exactly `origin/main` with no local changes
     to tracked files (so the snapshot's analytics version is the deployed
     code's), the latest collection that changed data succeeded (validation
     and Discovery preparation passed), there is a current trusted patch,
     and there is enough new data since what the site serves;
  2. the snapshot -- `tftlab local-snapshot`'s exporter (sanitized, opaque
     ids, prepared Discovery, verified twice) for the current patch, plus
     the previous patch while the current one is thin;
  3. a new, immutable GitHub Release with exactly the `.gz` and its
     manifest (`gh release create`, as the owner's GitHub login);
  4. Render: `TFT_SNAPSHOT_URL` / `TFT_SNAPSHOT_SHA256` updated and a deploy
     started through the Render API; if the deploy fails, both variables are
     put back, so the next restart keeps serving the previous snapshot;
  5. verification of the live site: `/api/source` reports the new snapshot
     and `/api/discovery` serves it from prepared (not live-computed) data;
  6. retention: only releases this command published are ever deleted,
     keeping the newest `KEEP_PUBLISHED_RELEASES`.

Nothing here contacts Riot or any database server. The Render API key is
read from `RENDER_API_KEY` (`.env`, git-ignored), sent only to
api.render.com, and never printed. Every external call goes through the
injected `run` (subprocess) and `http` (httpx) so tests run offline.
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
from .local_collector import LocalSnapshotError
from .storage import Database

RENDER_API = "https://api.render.com/v1"
RENDER_KEY_ENV = "RENDER_API_KEY"
SITE_URL_ENV = "TFT_PUBLIC_SITE_URL"
RENDER_KEY_RE = re.compile(r"^rnd_[A-Za-z0-9]{16,}$")

SNAPSHOT_ASSET = "theorylabs-public-snapshot.sqlite3.gz"
MANIFEST_ASSET = "theorylabs-public-snapshot.sqlite3.manifest.json"
TAG_PREFIX = "public-snapshot-"
#: In every release body this command writes; retention only ever deletes those.
PUBLISH_MARKER = "Published automatically by `tftlab local-publish`."

#: New matches (in the exported windows) needed before a new snapshot is worth publishing.
MIN_NEW_MATCHES = 200
#: Below this, the previous patch is exported alongside the current one.
MIN_CURRENT_MATCHES_ALONE = 1000
KEEP_PUBLISHED_RELEASES = 5

DEPLOY_TIMEOUT_S = 30 * 60
SITE_TIMEOUT_S = 10 * 60
POLL_INTERVAL_S = 15.0
DEPLOY_LIVE = "live"
DEPLOY_FAILED = frozenset({"build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated"})

PUBLISHED = "published"
SKIPPED = "skipped"
DRY_RUN = "dry_run"
FAILED = "failed"


class PublishError(RuntimeError):
    """A step failed; the message says what was (not) changed."""


@dataclass
class Step:
    name: str
    status: str  # ok | skip | fail | info
    detail: str


@dataclass
class PublishReport:
    outcome: str = FAILED
    message: str = ""
    steps: list[Step] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    matches: int | None = None
    live: dict[str, Any] = field(default_factory=dict)
    tag: str | None = None
    url: str | None = None
    sha256: str | None = None
    deploy_id: str | None = None
    deleted_releases: list[str] = field(default_factory=list)

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
    """The newest `local-collect` report that changed data must be a
    success. Runs stopped by preflight (an expired key, a patch pause)
    changed nothing and are passed over."""
    reports = sorted(reports_dir.glob("local-collect-*.json"), reverse=True) if reports_dir.exists() else []
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        outcome = data.get("outcome")
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


# ---------------------------------------------------------------- the live site


def live_snapshot(http: httpx.Client, site_url: str) -> dict[str, Any]:
    """What the website serves now (`/api/source`): its snapshot provenance."""
    try:
        response = http.get(f"{site_url}/api/source", timeout=120)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PublishError(f"Could not read {site_url}/api/source ({type(exc).__name__}). Nothing was published.") from None
    snap = body.get("snapshot") or {}
    return {"mode": body.get("mode"), "balance_windows": list(snap.get("balance_windows") or []),
            "matches": snap.get("matches"), "exported_at": snap.get("exported_at")}


def enough_new_data(live: dict[str, Any], windows: Sequence[str], counts: dict[str, int], *, minimum: int) -> str | None:
    """None when worth publishing; otherwise why not."""
    if live.get("mode") != "snapshot" or list(live.get("balance_windows") or []) != list(windows):
        return None  # the site serves something else (another patch, demo): publish
    new = sum(counts.values()) - int(live.get("matches") or 0)
    if new >= minimum:
        return None
    return (f"only {max(new, 0)} new matches since the snapshot the site serves (at least {minimum} are needed); "
            "the site keeps its current snapshot")


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
                               "not changed; delete that release before trying again.")


def prune_releases(run: Runner, gh: str, repo: str, keep: int, protect: set[str]) -> list[str]:
    """Delete this command's own releases beyond the newest `keep`. Releases
    without `PUBLISH_MARKER` (published by hand or by a workflow) are never
    touched."""
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
        return response.json() if response.content else None

    def service_for(self, site_url: str) -> str:
        """The id of the web service whose public URL is `site_url`."""
        cursor = None
        while True:
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            page = self._call("GET", "/services", "listing services", params=params) or []
            for item in page:
                service = item.get("service") or {}
                if (service.get("serviceDetails") or {}).get("url", "").rstrip("/") == site_url:
                    return service["id"]
            if len(page) < 100:
                raise PublishError(f"Render API: no service serves {site_url}. Check {SITE_URL_ENV} in .env.")
            cursor = page[-1].get("cursor")

    def env_values(self, service_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        found: dict[str, str | None] = {k: None for k in keys}
        cursor = None
        while True:
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            page = self._call("GET", f"/services/{service_id}/env-vars", "reading environment variables",
                              params=params) or []
            for item in page:
                var = item.get("envVar") or {}
                if var.get("key") in found:
                    found[var["key"]] = var.get("value")
            if len(page) < 100:
                return found
            cursor = page[-1].get("cursor")

    def set_env(self, service_id: str, key: str, value: str) -> None:
        self._call("PUT", f"/services/{service_id}/env-vars/{key}", f"setting {key}", json={"value": value})

    def deploy(self, service_id: str) -> str:
        body = self._call("POST", f"/services/{service_id}/deploys", "starting a deploy", json={})
        if not body or not body.get("id"):
            raise PublishError("Render API: the deploy was queued without an id; check the Render dashboard.")
        return body["id"]

    def deploy_status(self, service_id: str, deploy_id: str) -> str:
        body = self._call("GET", f"/services/{service_id}/deploys/{deploy_id}", "reading the deploy") or {}
        return str(body.get("status") or "")


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
    """Gates, snapshot, release, Render, verification, retention (see the
    module docstring). Never raises for an expected failure: the report's
    outcome and message say what happened and what was (not) changed.

    `current_windows(db)` is the current trusted patch's stored balance
    windows (`local_collector.default_snapshot_windows`); `make_snapshot(windows)`
    returns the exporter's `ExportResult`."""
    report = PublishReport()

    def step(name: str, status: str, detail: str) -> None:
        item = Step(name, status, detail)
        report.steps.append(item)
        progress(item)

    def finish(outcome: str, message: str) -> PublishReport:
        report.outcome, report.message = outcome, message
        return report

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
        report.live = live_snapshot(http, site)
        step("Website now", "info", f"{', '.join(report.live['balance_windows']) or report.live['mode']}: "
             f"{report.live['matches']} matches, exported {report.live['exported_at']}")
        reason = enough_new_data(report.live, windows, counts, minimum=config.min_new_matches)
        if reason and not config.force:
            step("New data", "skip", reason)
            return finish(SKIPPED, f"Not published: {reason}.")
        step("New data", "ok", reason or "enough to publish")

        # 2. the snapshot (the verified exporter; holds the collection lock).
        result = make_snapshot(windows)
        prov = result.provenance
        gz = result.gzip_path or result.path.with_name(result.path.name + ".gz")
        report.matches, report.sha256 = prov["matches"], result.files[gz.name]["sha256"]
        step("Snapshot", "ok", f"{prov['matches']} matches, {prov['boards']} boards, analytics "
             f"{prov['code_version']['analytics_version']}, sha256 {report.sha256[:12]}")
        if config.dry_run:
            return finish(DRY_RUN, "Dry run: the snapshot was made and verified locally. Nothing was uploaded or "
                                   "deployed.")

        # 3. the GitHub Release.
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

        # 4. Render: point the service at the new asset, deploy, put it back on failure.
        render = Render(http, config.render_api_key or "")
        service = render.service_for(site)
        previous = render.env_values(service, ("TFT_SNAPSHOT_URL", "TFT_SNAPSHOT_SHA256"))
        render.set_env(service, "TFT_SNAPSHOT_URL", report.url)
        render.set_env(service, "TFT_SNAPSHOT_SHA256", report.sha256)
        report.deploy_id = render.deploy(service)
        step("Render deploy", "info", f"started ({report.deploy_id})")

        def deployed() -> bool | None:
            status = render.deploy_status(service, report.deploy_id or "")
            return True if status == DEPLOY_LIVE else False if status in DEPLOY_FAILED else None

        if not wait_for(deployed, timeout_s=DEPLOY_TIMEOUT_S, interval_s=POLL_INTERVAL_S, sleep=sleep, clock=clock):
            for key, value in previous.items():
                if value is not None:
                    render.set_env(service, key, value)
            step("Render deploy", "fail", "did not go live; the previous snapshot settings were restored")
            return finish(FAILED, f"The Render deploy did not go live, so the website still serves the previous "
                                  f"snapshot and its settings were put back. The release {report.tag} exists; check the "
                                  "deploy log in the Render dashboard.")
        step("Render deploy", "ok", "live")

        # 5. the live site serves it, from prepared Discovery.
        def serving() -> bool | None:
            try:
                return True if live_snapshot(http, site)["exported_at"] == prov["exported_at"] else None
            except PublishError:
                return None

        if not wait_for(serving, timeout_s=SITE_TIMEOUT_S, interval_s=POLL_INTERVAL_S, sleep=sleep, clock=clock):
            step("Website", "fail", "does not report the new snapshot yet")
            return finish(FAILED, "Render reports the deploy live, but the website does not report the new snapshot "
                                  "yet. Check /api/source in a few minutes.")
        step("Website", "ok", f"serves the new snapshot ({prov['matches']} matches)")
        latest = windows[-1]
        try:
            discovery = http.get(f"{site}/api/discovery", params={"balance_window": latest}, timeout=180).json()
            prepared = (discovery.get("prepared") or {}).get("status")
        except (httpx.HTTPError, ValueError):
            prepared = None
        if prepared == "current":
            step("Discovery", "ok", f"{latest} served from prepared data")
        else:
            step("Discovery", "fail", f"{latest} prepared status is {prepared or 'unknown'} (computed live: slow)")

        # 6. retention: only this command's own releases.
        report.deleted_releases = prune_releases(run, gh_path, repo, config.keep_releases, {report.tag})
        if report.deleted_releases:
            step("Old releases", "ok", f"deleted {', '.join(report.deleted_releases)}")
        if prepared != "current":
            return finish(FAILED, f"Published {report.tag}, but the website computes Discovery live (prepared status "
                                  f"{prepared or 'unknown'}): the deployed code and this checkout may differ.")
        return finish(PUBLISHED, f"Published {report.tag}: the website now serves {', '.join(windows)} "
                                 f"({prov['matches']} matches).")
    except PublishError as exc:
        step("Stopped", "fail", str(exc))
        return finish(FAILED, str(exc))


def write_publish_report(report: PublishReport, reports_dir: Path, *, at_ms: int) -> Path:
    """The run's report as JSON (no keys)."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = reports_dir / f"local-publish-{stamp}.json"
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str) + "\n")
    return path
