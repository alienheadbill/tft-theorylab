"""`tftlab local-publish` (tftlab.local_publish): gates, the Render preflight,
the GitHub Release, the production change with its verified rollback, and
retention -- all offline (git/gh and every HTTP call are faked).

The fake Render behaves like Render: a deploy serves the snapshot its
service's `TFT_SNAPSHOT_URL` named when the deploy was created, and only
once that deploy reaches `live`."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import tftlab.local_publish as lp
from tftlab import cli
from tftlab.local_collector import CollectorBusy, LocalSnapshotError
from tftlab.public_snapshot import SnapshotExportError
from tftlab.storage import Database

SITE = "https://theorylabs.example.onrender.com"
RENDER_KEY = "rnd_" + "A1b2C3d4E5f6G7h8I9j0"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPO = "owner/tft-theorylab"
SERVICE = "srv-abc123"
OLD_TAG = "public-snapshot-18.4-20261017"
OLD_URL = f"https://github.com/{REPO}/releases/download/{OLD_TAG}/{lp.SNAPSHOT_ASSET}"
OLD_SHA = "f" * 64
OLD = {"exported_at": "2026-10-18T15:31:10.374000Z", "windows": {"18.4": 3416}}
NEW_EXPORTED = "2026-10-24T09:00:00.000000Z"


@dataclass
class World:
    """Everything outside this computer: git, gh, Render and the website."""

    head: str = COMMIT
    main: str = COMMIT
    dirty: str = ""
    releases: list[dict] = field(default_factory=list)
    env: dict[str, str | None] = field(default_factory=lambda: {
        "TFT_SNAPSHOT_URL": OLD_URL, "TFT_SNAPSHOT_SHA256": OLD_SHA, "TFT_DATA_SOURCE": "snapshot"})
    #: Status scripts, one per deploy in creation order (the last status repeats).
    deploy_scripts: list[list[str]] = field(default_factory=lambda: [["build_in_progress", "live"], ["live"]])
    put_failures: set[str] = field(default_factory=set)
    deploy_post_fails: bool = False
    source_stuck: bool = False
    #: Prepared status for every window, a per-window dict, or an HTTP status code.
    discovery: str | int | dict = "current"
    discovery_windows: list[str] = field(default_factory=list)
    #: Discovery status (or HTTP code) while the site still serves a snapshot other than the new one.
    discovery_before_publish: str | int | None = None
    render_status: int = 200
    services: list[tuple[str, str]] = field(default_factory=lambda: [("srv-other", "https://other.onrender.com"),
                                                                      (SERVICE, SITE)])
    prune_fails: bool = False
    upload_short: bool = False
    #: What each snapshot URL serves; the new release's URL is added when it is created.
    snapshots: dict[str, dict] = field(default_factory=lambda: {OLD_URL: OLD})
    serving: dict = field(default_factory=lambda: OLD)
    events: list[tuple] = field(default_factory=list)
    deploys: list[dict] = field(default_factory=list)
    asset_sizes: dict[str, int] = field(default_factory=dict)

    # -- subprocess
    def run(self, cmd):
        cmd = [str(c) for c in cmd]
        self.events.append(("cmd", *cmd[:4]))

        def ok(out: str = "") -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd[:2] == ["git", "fetch"]:
            return ok()
        if cmd[:2] == ["git", "rev-parse"]:
            return ok((self.head if cmd[2] == "HEAD" else self.main) + "\n")
        if cmd[:2] == ["git", "status"]:
            return ok(self.dirty)
        gh = cmd[1:]
        if gh[:2] == ["repo", "view"]:
            return ok(REPO + "\n")
        if gh[:1] == ["api"]:
            return ok(json.dumps([{"tag_name": r["tag"], "body": r["body"], "created_at": r["created_at"]}
                                  for r in self.releases]))
        if gh[:2] == ["release", "create"]:
            tag = gh[2]
            files = [Path(p) for p in gh[3:gh.index("--repo")]]
            self.asset_sizes = {p.name: p.stat().st_size - (1 if self.upload_short else 0) for p in files}
            self.releases.append({"tag": tag, "body": gh[gh.index("--notes") + 1], "created_at": "2026-10-24T10:00:00Z"})
            self.snapshots[f"https://github.com/{REPO}/releases/download/{tag}/{lp.SNAPSHOT_ASSET}"] = {
                "exported_at": NEW_EXPORTED, "windows": {"18.5": 1500}}
            return ok()
        if gh[:2] == ["release", "view"]:
            return ok(json.dumps({"assets": [{"name": n, "size": s} for n, s in self.asset_sizes.items()]}))
        if gh[:2] == ["release", "delete"]:
            if self.prune_fails:
                return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502: release delete failed")
            self.releases = [r for r in self.releases if r["tag"] != gh[2]]
            return ok()
        raise AssertionError(f"unexpected command {cmd}")

    # -- HTTP
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.events.append(("http", request.method, request.url.path))
        if url.startswith(SITE):
            assert "authorization" not in request.headers  # the Render key never goes to the website
            shown = OLD if self.source_stuck else self.serving
            if request.url.path == "/api/source":
                return httpx.Response(200, json={"mode": "snapshot", "snapshot": {
                    "balance_windows": list(shown["windows"]), "matches": sum(shown["windows"].values()),
                    "exported_at": shown["exported_at"]}})
            if request.url.path == "/api/balance-windows":
                return httpx.Response(200, json={"windows": [{"balance_window": w, "matches": n}
                                                             for w, n in shown["windows"].items()]})
            if request.url.path == "/api/discovery":
                window = request.url.params.get("balance_window")
                self.discovery_windows.append(window)
                status = self.discovery.get(window, "current") if isinstance(self.discovery, dict) else self.discovery
                if self.discovery_before_publish is not None and shown["exported_at"] != NEW_EXPORTED:
                    status = self.discovery_before_publish  # what the old snapshot gets from the deployed code
                if isinstance(status, int):
                    return httpx.Response(status, text="<html>Internal Server Error</html>")
                return httpx.Response(200, json={"prepared": {"status": status}})
        assert url.startswith(lp.RENDER_API), url
        assert request.headers["authorization"] == f"Bearer {RENDER_KEY}"
        if self.render_status != 200:
            return httpx.Response(self.render_status, json={"message": "nope"})
        path = request.url.path.removeprefix("/v1")
        if path == "/services":
            return httpx.Response(200, json=[{"service": {"id": sid, "serviceDetails": {"url": u}}, "cursor": sid}
                                             for sid, u in self.services])
        if path == f"/services/{SERVICE}/env-vars" and request.method == "GET":
            return httpx.Response(200, json=[{"envVar": {"key": k, "value": v}, "cursor": k}
                                             for k, v in self.env.items() if v is not None])
        if path.startswith(f"/services/{SERVICE}/env-vars/") and request.method == "PUT":
            key = path.rsplit("/", 1)[1]
            if key in self.put_failures:
                return httpx.Response(500, json={"message": "boom"})
            self.env[key] = json.loads(request.content)["value"]
            return httpx.Response(200, json={"key": key, "value": self.env[key]})
        if path == f"/services/{SERVICE}/deploys" and request.method == "POST":
            if self.deploy_post_fails:
                return httpx.Response(503, json={"message": "unavailable"})
            script = list(self.deploy_scripts[len(self.deploys)] if len(self.deploys) < len(self.deploy_scripts)
                          else ["live"])
            deploy = {"id": f"dep-{len(self.deploys) + 1}", "url": self.env["TFT_SNAPSHOT_URL"], "script": script,
                      "cancelled": False}
            self.deploys.append(deploy)
            return httpx.Response(201, json={"id": deploy["id"], "status": "created"})
        if path.endswith("/cancel") and request.method == "POST":
            deploy = next(d for d in self.deploys if path == f"/services/{SERVICE}/deploys/{d['id']}/cancel")
            deploy["cancelled"] = True
            return httpx.Response(200, json={"id": deploy["id"], "status": "canceled"})
        for deploy in self.deploys:
            if path == f"/services/{SERVICE}/deploys/{deploy['id']}":
                status = "canceled" if deploy["cancelled"] else (
                    deploy["script"].pop(0) if len(deploy["script"]) > 1 else deploy["script"][0])
                if status == "live":
                    self.serving = self.snapshots.get(deploy["url"], {"exported_at": "unknown", "windows": {}})
                return httpx.Response(200, json={"id": deploy["id"], "status": status})
        raise AssertionError(f"unexpected request {request.method} {url}")

    def gh_calls(self) -> list[tuple]:
        return [e for e in self.events if e[0] == "cmd" and e[1] == "gh"]

    def render(self, method: str | None = None) -> list[tuple]:
        return [e for e in self.events if e[0] == "http" and e[2].startswith("/v1")
                and (method is None or e[1] == method)]

    def puts(self) -> list[str]:
        return [e[2].rsplit("/", 1)[1] for e in self.render("PUT")]


def _verbs(world: World) -> list[str]:
    """Every gh call as "<command> <subcommand> <first argument>"."""
    return [" ".join(e[2:]) for e in world.events if e[0] == "cmd" and e[1] == "gh"]


@dataclass
class FakeExport:
    path: Path
    manifest_path: Path
    gzip_path: Path
    provenance: dict
    files: dict


class Local:
    """This computer: the database, the collection reports and the exporter."""

    def __init__(self, tmp_path: Path, monkeypatch, *, windows=None, current=("18.5",)) -> None:
        self.root = tmp_path
        self.db = tmp_path / "data" / "local" / "theorylabs.sqlite3"
        self.db.parent.mkdir(parents=True)
        Database(self.db).close()
        self.reports = tmp_path / "data" / "local" / "reports"
        self.reports.mkdir()
        self.report("20261024T080000Z", {"outcome": "success"})
        self.current = list(current)
        self.exports: list[list[str]] = []
        stored = windows if windows is not None else [("18.5", 1500, 2), ("18.4", 3416, 1)]
        monkeypatch.setattr(lp, "available_balance_windows", lambda db: list(stored))

    def report(self, stamp: str, data, raw: str | None = None) -> None:
        path = self.reports / f"local-collect-{stamp}.json"
        path.write_text(raw if raw is not None else json.dumps(data))

    def current_windows(self, db):
        if not self.current:
            raise LocalSnapshotError("There is no current trusted patch window, so there is no default to export.")
        return list(self.current)

    def make_snapshot(self, windows):
        self.exports.append(list(windows))
        public = self.root / "public"
        public.mkdir(exist_ok=True)
        gz, manifest = public / lp.SNAPSHOT_ASSET, public / lp.MANIFEST_ASSET
        gz.write_bytes(b"gz" * 100)
        manifest.write_text("{}")
        prov = {"matches": 1500, "boards": 12000, "latest_game": "2026-10-24T08:59:00.000000Z",
                "exported_at": NEW_EXPORTED, "code_version": {"analytics_version": "v1-test"}}
        return FakeExport(public / "theorylabs-public-snapshot.sqlite3", manifest, gz, prov,
                          {gz.name: {"sha256": "a" * 64, "bytes": gz.stat().st_size}})


def publish(local: Local, world: World, *, key: str | None = RENDER_KEY, site: str = SITE, **overrides) -> lp.PublishReport:
    config = lp.PublishConfig(db_path=local.db, repo_root=local.root, site_url=site, render_api_key=key, **overrides)
    ticks = iter(range(100_000))
    with httpx.Client(transport=httpx.MockTransport(world.handler)) as http:
        return lp.run_local_publish(
            config, at_ms=0, current_windows=local.current_windows, make_snapshot=local.make_snapshot,
            run=world.run, http=http, gh=lambda: "gh", reports_dir=local.reports,
            sleep=lambda _s: None, clock=lambda: next(ticks) * 60.0,
        )


def _no_release_created(world: World) -> bool:
    return not any(v.startswith("release create") for v in _verbs(world))


def _deleted(world: World) -> list[str]:
    return [v.split()[2] for v in _verbs(world) if v.startswith("release delete")]


# ---------------------------------------------------------------- the whole path


def test_publish_releases_points_render_at_it_deploys_and_checks_the_site(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    report = publish(local, world)
    assert report.outcome == lp.PUBLISHED and report.exit_code == 0, report.message
    assert report.tag == "public-snapshot-18.5-20261024" and local.exports == [["18.5"]]
    assert report.rollback == lp.ROLLBACK_NOT_NEEDED and report.warnings == []
    url = f"https://github.com/{REPO}/releases/download/{report.tag}/{lp.SNAPSHOT_ASSET}"
    assert world.env["TFT_SNAPSHOT_URL"] == url == report.url and world.env["TFT_SNAPSHOT_SHA256"] == "a" * 64
    assert world.env["TFT_DATA_SOURCE"] == "snapshot"  # nothing else on the service is touched
    assert world.puts() == ["TFT_SNAPSHOT_URL", "TFT_SNAPSHOT_SHA256"] and len(world.deploys) == 1
    assert report.previous == {"url": OLD_URL, "sha256": OLD_SHA, "tag": OLD_TAG, "exported_at": OLD["exported_at"],
                               "balance_windows": ["18.4"], "matches": 3416}
    assert [s.name for s in report.steps if s.status == "ok"][-3:] == ["Render deploy", "Website", "Discovery"]


def test_the_render_key_never_reaches_the_report_or_its_file(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch)
    for world in (World(), World(discovery="stale"), World(deploy_scripts=[["build_failed"]])):
        report = publish(local, world)
        text = lp.write_publish_report(report, local.reports, at_ms=0).read_text()
        assert RENDER_KEY not in text and "Bearer" not in text


# ---------------------------------------------------------------- A-D: the current-patch floor


def test_a_new_patch_below_its_floor_is_skipped(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 199, 2), ("18.4", 3416, 1)])
    world = World()
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and report.exit_code == 0
    assert "first publication of balance window 18.5: 18.5: 199 matches of its own, at least 200" in report.message
    assert local.exports == [] and world.gh_calls() == [] and world.render() == []


def test_a_new_patch_at_its_floor_is_published_with_the_previous_patch_alongside(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 200, 2), ("18.4", 3416, 1)])
    report = publish(local, World())
    assert report.outcome == lp.PUBLISHED, report.message
    assert local.exports == [["18.4", "18.5"]] and report.tag == "public-snapshot-18.4_18.5-20261024"
    assert any(s.name == "New data" and "first publication of 18.5: 18.5: 200 matches of its own" in s.detail
               for s in report.steps)


def test_force_overrides_the_new_patch_floor(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 199, 2), ("18.4", 3416, 1)])
    report = publish(local, World(), force=True)
    assert report.outcome == lp.PUBLISHED, report.message
    assert any(s.name == "New data" and "published anyway (--force)" in s.detail for s in report.steps)


def test_previous_patch_matches_never_count_towards_the_new_patch_floor(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 150, 2), ("18.4", 9000, 1)])  # 5,584 new 18.4 matches
    report = publish(local, World())
    assert report.outcome == lp.SKIPPED and "18.5: 150 matches of its own" in report.message
    assert "previous-patch matches and older balance windows of the same patch do not count" in report.message


# Two balance windows inside one client patch (a registered mid-patch cutover), latest first, as
# `local_collector._window_balance_windows` returns them. The site already serves the older one.
LIVE_18_2A = {"exported_at": OLD["exported_at"], "windows": {"18.2a": 1000}}


def _two_windows(tmp_path, monkeypatch, new_count: int) -> Local:
    return Local(tmp_path, monkeypatch, current=("18.2b", "18.2a"),
                 windows=[("18.2b", new_count, 3), ("18.2a", 1000, 2), ("18.1", 3000, 1)])


def test_a_new_balance_window_cannot_borrow_its_older_siblings_matches(tmp_path, monkeypatch) -> None:
    local = _two_windows(tmp_path, monkeypatch, 50)
    world = World(serving=LIVE_18_2A, snapshots={OLD_URL: LIVE_18_2A})
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED, report.message  # 50 + 1,000 = 1,050 must not count
    assert "first publication of balance window 18.2b: 18.2b: 50 matches of its own" in report.message
    assert local.exports == [] and world.gh_calls() == [] and world.render("PUT") == []


def test_every_new_balance_window_needs_its_own_floor(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, current=("18.2b", "18.2a"), windows=[("18.2b", 500, 3), ("18.2a", 150, 2)])
    report = publish(local, World())  # the site serves 18.4 only: both windows are new, 18.2a is short
    assert report.outcome == lp.SKIPPED and "first publication of balance window 18.2a" in report.message
    assert "18.2b: 500, 18.2a: 150 matches of its own" in report.message


def test_discovery_is_verified_for_the_newest_current_balance_window(tmp_path, monkeypatch) -> None:
    local = _two_windows(tmp_path, monkeypatch, 200)
    world = World(serving=LIVE_18_2A, snapshots={OLD_URL: LIVE_18_2A})
    report = publish(local, world)
    assert report.outcome == lp.PUBLISHED, report.message
    assert local.exports == [["18.2b", "18.2a"]] and world.discovery_windows == ["18.2b"]
    assert any(s.name == "Discovery" and s.detail.startswith("18.2b ") for s in report.steps)


def test_stale_discovery_for_the_newest_window_fails_even_if_the_older_one_is_current(tmp_path, monkeypatch) -> None:
    local = _two_windows(tmp_path, monkeypatch, 200)
    world = World(serving=LIVE_18_2A, snapshots={OLD_URL: LIVE_18_2A}, discovery={"18.2b": "stale", "18.2a": "current"})
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "Discovery for 18.2b is not served from prepared data (stale)" in report.message
    assert report.rollback == lp.ROLLBACK_VERIFIED and world.serving == LIVE_18_2A


def test_discovery_is_verified_for_the_current_patch_when_the_previous_one_is_included(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 600, 2), ("18.4", 3416, 1)])
    world = World()
    assert publish(local, world).outcome == lp.PUBLISHED
    assert local.exports == [["18.4", "18.5"]] and world.discovery_windows == ["18.5"]


def test_new_data_is_compared_window_by_window_with_the_live_site(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 1500, 2), ("18.4", 3416, 1)])
    live_18_5 = {"exported_at": OLD["exported_at"], "windows": {"18.5": 1400}}
    report = publish(local, World(serving=live_18_5, snapshots={OLD_URL: live_18_5}))
    assert report.outcome == lp.SKIPPED and "only 100 new matches in 18.5" in report.message and local.exports == []
    world = World(serving=live_18_5, snapshots={OLD_URL: live_18_5})
    assert publish(local, world, force=True).outcome == lp.PUBLISHED


# The site already serves 18.5 (1,400 matches) and the local store has only 100 more.
LIVE_18_5 = {"exported_at": OLD["exported_at"], "windows": {"18.5": 1400}}


def _thin_new_data(tmp_path, monkeypatch) -> Local:
    return Local(tmp_path, monkeypatch, windows=[("18.5", 1500, 2), ("18.4", 3416, 1)])


def test_stale_live_discovery_republishes_without_new_matches(tmp_path, monkeypatch) -> None:
    """After a code change moves the analytics version, the deployed site
    reports its snapshot's prepared Discovery as stale and computes it live.
    That alone is a reason to publish: the new snapshot is prepared by the
    deployed code."""
    local = _thin_new_data(tmp_path, monkeypatch)
    world = World(serving=LIVE_18_5, snapshots={OLD_URL: LIVE_18_5}, discovery_before_publish="stale")
    report = publish(local, world)
    assert report.outcome == lp.PUBLISHED, report.message
    assert report.live["discovery"] == "stale" and world.discovery_windows[0] == "18.5"
    assert any(s.name == "New data" and "prepared data is stale" in s.detail for s in report.steps)


@pytest.mark.parametrize("before", ["current", "missing", 500])
def test_only_a_stale_live_discovery_waives_the_new_matches_minimum(before, tmp_path, monkeypatch) -> None:
    local = _thin_new_data(tmp_path, monkeypatch)
    world = World(serving=LIVE_18_5, snapshots={OLD_URL: LIVE_18_5}, discovery_before_publish=before)
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and "only 100 new matches in 18.5" in report.message
    assert local.exports == [] and world.render("PUT") == []


def test_a_stale_live_discovery_never_waives_a_new_windows_own_floor(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, current=("18.2b", "18.2a"),
                  windows=[("18.2b", 50, 3), ("18.2a", 1000, 2)])
    live_18_2a = {"exported_at": OLD["exported_at"], "windows": {"18.2a": 1000}}
    world = World(serving=live_18_2a, snapshots={OLD_URL: live_18_2a}, discovery_before_publish="stale")
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and "18.2b: 50 matches of its own" in report.message
    assert world.discovery_windows == []  # a new window: the live site has nothing to check for it


def test_no_current_patch_is_a_skip_and_the_site_keeps_its_snapshot(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch, current=()), World()
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and "keeps its current snapshot" in report.message
    assert world.gh_calls() == [] and world.render() == []


# ---------------------------------------------------------------- E-G: a partial Render change


def test_a_failed_first_variable_change_changes_nothing_further(tmp_path, monkeypatch) -> None:
    world = World(put_failures={"TFT_SNAPSHOT_URL"})
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert world.puts() == ["TFT_SNAPSHOT_URL"]  # the one failed attempt; nothing to restore
    assert world.env["TFT_SNAPSHOT_URL"] == OLD_URL and world.deploys == []
    assert "Rollback verified" in report.message and report.tag in report.message  # the release is kept


def test_a_failed_second_variable_change_restores_the_first(tmp_path, monkeypatch) -> None:
    world = World(put_failures={"TFT_SNAPSHOT_SHA256"})
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert world.puts() == ["TFT_SNAPSHOT_URL", "TFT_SNAPSHOT_SHA256", "TFT_SNAPSHOT_URL"]
    assert (world.env["TFT_SNAPSHOT_URL"], world.env["TFT_SNAPSHOT_SHA256"]) == (OLD_URL, OLD_SHA)
    assert world.deploys == []


def test_a_deploy_that_cannot_be_started_restores_both_variables(tmp_path, monkeypatch) -> None:
    world = World(deploy_post_fails=True)
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert (world.env["TFT_SNAPSHOT_URL"], world.env["TFT_SNAPSHOT_SHA256"]) == (OLD_URL, OLD_SHA)
    assert world.puts() == ["TFT_SNAPSHOT_URL", "TFT_SNAPSHOT_SHA256"] * 2


# ---------------------------------------------------------------- H-I: a deploy that never goes live


def test_a_failed_deploy_restores_the_variables_and_verifies_the_site(tmp_path, monkeypatch) -> None:
    world = World(deploy_scripts=[["build_in_progress", "build_failed"]])
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert (world.env["TFT_SNAPSHOT_URL"], world.env["TFT_SNAPSHOT_SHA256"]) == (OLD_URL, OLD_SHA)
    assert len(world.deploys) == 1 and report.rollback_deploy_id is None  # the old instance never stopped serving
    assert OLD["exported_at"] in report.message and _deleted(world) == []


def test_a_deploy_timeout_restores_cancels_and_redeploys_the_previous_snapshot(tmp_path, monkeypatch) -> None:
    world = World(deploy_scripts=[["build_in_progress"], ["live"]])
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert world.env["TFT_SNAPSHOT_URL"] == OLD_URL
    assert world.deploys[0]["cancelled"] is True and world.deploys[1]["url"] == OLD_URL
    assert report.rollback_deploy_id == "dep-2" and world.serving == OLD


# ---------------------------------------------------------------- J-L: a failure after the new deploy went live


def test_a_live_deploy_the_site_never_reports_is_actively_rolled_back(tmp_path, monkeypatch) -> None:
    world = World(source_stuck=True)
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert world.env["TFT_SNAPSHOT_URL"] == OLD_URL and len(world.deploys) == 2
    assert world.deploys[1]["url"] == OLD_URL and report.rollback_deploy_id == "dep-2"
    assert _deleted(world) == []


def test_stale_discovery_after_going_live_rolls_back_and_keeps_every_release(tmp_path, monkeypatch) -> None:
    own = [{"tag": f"public-snapshot-18.5-202610{d:02d}", "body": lp.PUBLISH_MARKER,
            "created_at": f"2026-10-{d:02d}T09:00:00Z"} for d in range(10, 20)]
    world = World(discovery="stale", releases=list(own))
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_VERIFIED
    assert "not served from prepared data (stale)" in report.message
    assert world.serving == OLD and world.deploys[1]["url"] == OLD_URL
    assert _deleted(world) == [] and report.tag in {r["tag"] for r in world.releases}


def test_a_discovery_http_error_is_a_classified_failure_and_rolls_back(tmp_path, monkeypatch) -> None:
    world = World(discovery=500)
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and "(HTTP 500)" in report.message
    assert report.rollback == lp.ROLLBACK_VERIFIED and world.serving == OLD


def test_a_rollback_deploy_that_fails_is_a_high_severity_unverified_rollback(tmp_path, monkeypatch) -> None:
    world = World(discovery="stale", deploy_scripts=[["live"], ["build_failed"]])
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.FAILED and report.rollback == lp.ROLLBACK_UNVERIFIED and report.exit_code == 1
    assert report.message.startswith("HIGH SEVERITY") and "Manual Render inspection is required" in report.message
    assert "Rollback verified" not in report.message and "still serves" not in report.message
    assert _deleted(world) == []


def test_an_unexpected_error_after_the_change_is_rolled_back(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    original = lp.Production.serves_new

    def broken(self, exported_at):
        raise RuntimeError(f"socket closed while holding {RENDER_KEY}")

    monkeypatch.setattr(lp.Production, "serves_new", broken)
    report = publish(local, world)
    monkeypatch.setattr(lp.Production, "serves_new", original)
    assert report.outcome == lp.FAILED and "unexpected error (RuntimeError" in report.message
    assert report.rollback == lp.ROLLBACK_VERIFIED and world.serving == OLD
    assert RENDER_KEY not in json.dumps(report.as_dict())


# ---------------------------------------------------------------- M-Q: retention


def test_retention_runs_only_after_full_verification(tmp_path, monkeypatch) -> None:
    own = [{"tag": f"public-snapshot-18.5-202610{d:02d}", "body": lp.PUBLISH_MARKER,
            "created_at": f"2026-10-{d:02d}T09:00:00Z"} for d in range(10, 20)]
    world = World(releases=list(own))
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.PUBLISHED and report.deleted_releases
    discovery_at = max(i for i, e in enumerate(world.events) if e[:2] == ("http", "GET") and e[2] == "/api/discovery")
    first_delete = min(i for i, e in enumerate(world.events) if e[:4] == ("cmd", "gh", "release", "delete"))
    assert first_delete > discovery_at


def test_retention_deletes_only_this_commands_old_releases_and_protects_the_previous_live_one(tmp_path, monkeypatch) -> None:
    manual = [{"tag": "public-snapshot-18.3-20261005", "body": "by hand", "created_at": "2026-10-08T00:00:00Z"}]
    previous = {"tag": OLD_TAG, "body": lp.PUBLISH_MARKER, "created_at": "2026-10-01T09:00:00Z"}  # oldest marked
    own = [{"tag": f"public-snapshot-18.5-202610{d:02d}", "body": lp.PUBLISH_MARKER,
            "created_at": f"2026-10-{d:02d}T09:00:00Z"} for d in range(15, 21)]
    world = World(releases=manual + [previous] + own)
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.PUBLISHED, report.message
    tags = {r["tag"] for r in world.releases}
    assert manual[0]["tag"] in tags and OLD_TAG in tags and report.tag in tags
    assert report.deleted_releases == ["public-snapshot-18.5-20261016", "public-snapshot-18.5-20261015"]


def test_unmarked_releases_are_never_touched(tmp_path, monkeypatch) -> None:
    manual = [{"tag": f"public-snapshot-18.{n}-manual", "body": "Patch notes", "created_at": f"2026-09-{n:02d}T00:00:00Z"}
              for n in range(1, 12)]
    world = World(releases=list(manual))
    assert publish(Local(tmp_path, monkeypatch), world).outcome == lp.PUBLISHED
    assert _deleted(world) == [] and {r["tag"] for r in manual} <= {r["tag"] for r in world.releases}


def test_a_retention_failure_is_a_warning_never_a_rollback(tmp_path, monkeypatch) -> None:
    own = [{"tag": f"public-snapshot-18.5-202610{d:02d}", "body": lp.PUBLISH_MARKER,
            "created_at": f"2026-10-{d:02d}T09:00:00Z"} for d in range(10, 20)]
    world = World(releases=list(own), prune_fails=True)
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.PUBLISHED and report.exit_code == 0
    assert report.warnings and "old releases were not pruned" in report.warnings[0]
    assert len(world.deploys) == 1 and report.rollback == lp.ROLLBACK_NOT_NEEDED


def test_an_unidentifiable_previous_release_means_no_pruning(tmp_path, monkeypatch) -> None:
    own = [{"tag": f"public-snapshot-18.5-202610{d:02d}", "body": lp.PUBLISH_MARKER,
            "created_at": f"2026-10-{d:02d}T09:00:00Z"} for d in range(10, 20)]
    elsewhere = "https://cdn.example.com/theorylabs/snapshot.sqlite3.gz"
    world = World(releases=list(own), env={"TFT_SNAPSHOT_URL": elsewhere, "TFT_SNAPSHOT_SHA256": OLD_SHA},
                  snapshots={elsewhere: OLD})
    report = publish(Local(tmp_path, monkeypatch), world)
    assert report.outcome == lp.PUBLISHED and _deleted(world) == []
    assert "could not be identified" in report.warnings[0]


# ---------------------------------------------------------------- R-U: the collection report gate


def test_the_newest_successful_collection_passes(tmp_path) -> None:
    reports = tmp_path / "r"
    reports.mkdir()
    (reports / "local-collect-20261024T080000Z.json").write_text(json.dumps({"outcome": "success"}))
    assert lp.latest_collection_gate(reports) == "local-collect-20261024T080000Z.json"


@pytest.mark.parametrize(("raw", "why"), [
    ("{\"outcome\": \"succ", "cannot be read"),                       # truncated JSON
    ("\udcff", "cannot be read"),                                      # unreadable bytes (written below)
    (json.dumps({"message": "Collection complete"}), "has no outcome"),
    (json.dumps(["success"]), "has no outcome"),
    (json.dumps({"outcome": ""}), "has no outcome"),
])
def test_a_malformed_newest_report_blocks_publication(raw, why, tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    newest = local.reports / "local-collect-20261024T100000Z.json"
    if raw == "\udcff":
        newest.write_bytes(b"\xff\xfe\x00garbage")
    else:
        newest.write_text(raw)
    report = publish(local, world)
    assert report.outcome == lp.FAILED and why in report.message and newest.name in report.message
    assert local.exports == [] and world.gh_calls() == [] and world.render() == []


def test_a_newer_preflight_stop_is_passed_over_but_a_failed_collection_blocks(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch)
    local.report("20261024T100000Z", {"outcome": "preflight_failed"})  # e.g. an expired key: nothing changed
    assert lp.latest_collection_gate(local.reports) == "local-collect-20261024T080000Z.json"
    local.report("20261024T120000Z", {"outcome": "collection_failed"})
    local.report("20261024T140000Z", {"outcome": "preflight_failed"})
    with pytest.raises(lp.PublishError, match="collection_failed"):
        lp.latest_collection_gate(local.reports)
    report = publish(local, World())
    assert report.outcome == lp.FAILED and local.exports == []


def test_a_malformed_report_behind_a_preflight_stop_still_blocks(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch)
    local.report("20261024T100000Z", None, raw="not json")
    local.report("20261024T120000Z", {"outcome": "preflight_failed"})
    with pytest.raises(lp.PublishError, match="20261024T100000Z.json cannot be read"):
        lp.latest_collection_gate(local.reports)


# ---------------------------------------------------------------- Y: a running collection


def test_a_running_collection_is_a_clean_skip_that_changes_nothing(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()

    def busy(windows):
        raise CollectorBusy("Another collection is already running.")

    local.make_snapshot = busy
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and report.exit_code == 0
    assert report.message.startswith(lp.COLLECTION_RUNNING_MESSAGE)
    assert _no_release_created(world) and world.render("PUT") == [] and world.render("POST") == []
    assert lp.write_publish_report(report, local.reports, at_ms=0).exists()


@pytest.mark.parametrize("error", [SnapshotExportError, LocalSnapshotError])
def test_a_failed_snapshot_export_is_a_reported_failure(error, tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()

    def failing(windows):
        raise error("verification failed: boards")

    local.make_snapshot = failing
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "verification failed: boards" in report.message
    assert "Nothing was published" in report.message and _no_release_created(world) and world.render("PUT") == []


def test_an_unexpected_error_before_any_change_is_reported_without_the_key(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()

    def broken(windows):
        raise RuntimeError(f"boom with {RENDER_KEY}")

    local.make_snapshot = broken
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "unexpected error (RuntimeError" in report.message
    assert "Nothing was published" in report.message and RENDER_KEY not in json.dumps(report.as_dict())


# ---------------------------------------------------------------- Z-AB: the Render preflight


@pytest.mark.parametrize("env", [
    {"TFT_SNAPSHOT_SHA256": OLD_SHA},                                         # no URL
    {"TFT_SNAPSHOT_URL": OLD_URL},                                            # no SHA
    {"TFT_SNAPSHOT_URL": OLD_URL, "TFT_SNAPSHOT_SHA256": "not-a-sha"},       # malformed SHA
    {"TFT_SNAPSHOT_URL": "http://insecure/x.gz", "TFT_SNAPSHOT_SHA256": OLD_SHA},
])
def test_no_usable_rollback_target_stops_before_any_release(env, tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(env=dict(env))
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "no known snapshot to roll back to" in report.message
    assert _no_release_created(world) and local.exports == [] and world.render("PUT") == []


def test_a_refused_render_key_stops_before_any_release(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(render_status=401)
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "the key was refused (401)" in report.message
    assert _no_release_created(world) and local.exports == []


@pytest.mark.parametrize(("services", "why"), [
    ([("srv-other", "https://other.onrender.com")], "no service serves"),
    ([(SERVICE, SITE), ("srv-twin", SITE + "/")], "2 services claim"),
])
def test_a_missing_or_ambiguous_service_stops_before_any_release(services, why, tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(services=services)
    report = publish(local, world)
    assert report.outcome == lp.FAILED and why in report.message and _no_release_created(world)


# ---------------------------------------------------------------- AD-AF: existing guarantees


def test_a_dry_run_makes_the_snapshot_and_changes_nothing(tmp_path, monkeypatch) -> None:
    for key in (None, RENDER_KEY):
        local, world = Local(tmp_path / str(bool(key)), monkeypatch), World()
        report = publish(local, world, key=key, dry_run=True)
        assert report.outcome == lp.DRY_RUN and report.exit_code == 0 and local.exports == [["18.5"]]
        assert world.gh_calls() == [] and world.render("PUT") == [] and world.render("POST") == []
        assert world.env["TFT_SNAPSHOT_URL"] == OLD_URL


def test_an_incomplete_upload_never_reaches_render(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(upload_short=True)
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "full size" in report.message
    assert world.render("PUT") == [] and world.render("POST") == [] and world.env["TFT_SNAPSHOT_URL"] == OLD_URL


@pytest.mark.parametrize("change", [{"head": "1" * 40}, {"dirty": " M src/tftlab/carry.py\n"}])
def test_a_checkout_that_is_not_origin_main_publishes_nothing(change, tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(**change)
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "Nothing was published" in report.message
    assert world.gh_calls() == [] and [e for e in world.events if e[0] == "http"] == [] and local.exports == []


def test_a_missing_render_key_or_site_url_stops_before_anything(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    report = publish(local, world, key=None)
    assert report.outcome == lp.FAILED and "local-set-render-key" in report.message
    report = publish(local, world, site="")
    assert report.outcome == lp.FAILED and lp.SITE_URL_ENV in report.message
    assert world.events == []


# ---------------------------------------------------------------- tags and CLI


def test_release_tags_are_never_reused() -> None:
    latest = "2026-10-24T08:59:00Z"
    assert lp.release_tag(["18.5"], latest, set()) == "public-snapshot-18.5-20261024"
    taken = {"public-snapshot-18.5-20261024", "public-snapshot-18.5-20261024-r2"}
    assert lp.release_tag(["18.5"], latest, taken) == "public-snapshot-18.5-20261024-r3"


def test_local_set_render_key_saves_it_hidden_and_keeps_other_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-x\nTFT_PLATFORM=na1\n")
    result = CliRunner().invoke(cli.app, ["local-set-render-key"], input=RENDER_KEY + "\n")
    assert result.exit_code == 0 and RENDER_KEY not in result.output
    assert (tmp_path / ".env").read_text().splitlines() == ["RIOT_API_KEY=RGAPI-x", "TFT_PLATFORM=na1",
                                                            f"RENDER_API_KEY={RENDER_KEY}"]
    bad = CliRunner().invoke(cli.app, ["local-set-render-key"], input="not-a-key\n")
    assert bad.exit_code == 1 and "RENDER_API_KEY=not-a-key" not in (tmp_path / ".env").read_text()


def test_local_publish_cli_without_configuration_fails_plainly(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(lp.SITE_URL_ENV, raising=False)
    monkeypatch.delenv(lp.RENDER_KEY_ENV, raising=False)
    monkeypatch.setattr(lp, "default_runner", lambda root: lambda cmd: pytest.fail(f"ran {cmd}"))
    result = CliRunner().invoke(cli.app, ["local-publish"])
    assert result.exit_code == 1 and lp.SITE_URL_ENV in result.output
    assert list((tmp_path / "data" / "local" / "reports").glob("local-publish-*.json"))
