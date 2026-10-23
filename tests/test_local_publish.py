"""`tftlab local-publish` (tftlab.local_publish): gates, the GitHub Release,
Render and the live-site checks, all offline (git/gh and HTTP are faked)."""

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
from tftlab.local_collector import LocalSnapshotError
from tftlab.storage import Database

SITE = "https://theorylabs.example.onrender.com"
RENDER_KEY = "rnd_" + "A1b2C3d4E5f6G7h8I9j0"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPO = "owner/tft-theorylab"
SERVICE = "srv-abc123"
OLD_URL = "https://github.com/owner/tft-theorylab/releases/download/public-snapshot-18.4-20261017/x.gz"
OLD_SHA = "f" * 64
NEW_EXPORTED = "2026-10-24T09:00:00.000000Z"


@dataclass
class World:
    """Everything outside this computer: git, gh, Render and the website."""

    head: str = COMMIT
    main: str = COMMIT
    dirty: str = ""
    releases: list[dict] = field(default_factory=list)
    live_windows: list[str] = field(default_factory=lambda: ["18.4"])
    live_matches: int = 3416
    deploy_statuses: list[str] = field(default_factory=lambda: ["build_in_progress", "live"])
    discovery_status: str = "current"
    env: dict[str, str] = field(default_factory=lambda: {"TFT_SNAPSHOT_URL": OLD_URL, "TFT_SNAPSHOT_SHA256": OLD_SHA,
                                                         "TFT_DATA_SOURCE": "snapshot"})
    commands: list[list[str]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    deployed: bool = False
    asset_sizes: dict[str, int] = field(default_factory=dict)

    # -- subprocess
    def run(self, cmd):
        cmd = [str(c) for c in cmd]
        self.commands.append(cmd)

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
            self.asset_sizes = {p.name: p.stat().st_size for p in files}
            notes = gh[gh.index("--notes") + 1]
            self.releases.append({"tag": tag, "body": notes, "created_at": "2026-10-24T10:00:00Z"})
            return ok()
        if gh[:2] == ["release", "view"]:
            return ok(json.dumps({"assets": [{"name": n, "size": s} for n, s in self.asset_sizes.items()]}))
        if gh[:2] == ["release", "delete"]:
            self.releases = [r for r in self.releases if r["tag"] != gh[2]]
            return ok()
        raise AssertionError(f"unexpected command {cmd}")

    # -- HTTP
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(SITE):
            assert "authorization" not in request.headers  # the Render key never goes to the website
            if request.url.path == "/api/source":
                exported = NEW_EXPORTED if self.deployed else "2026-10-18T15:31:10.374000Z"
                matches = 1500 if self.deployed else self.live_matches
                windows = ["18.5"] if self.deployed else self.live_windows
                return httpx.Response(200, json={"mode": "snapshot", "snapshot": {
                    "balance_windows": windows, "matches": matches, "exported_at": exported}})
            if request.url.path == "/api/discovery":
                return httpx.Response(200, json={"prepared": {"status": self.discovery_status}})
        assert url.startswith(lp.RENDER_API), url
        assert request.headers["authorization"] == f"Bearer {RENDER_KEY}"
        path = request.url.path.removeprefix("/v1")
        if path == "/services":
            return httpx.Response(200, json=[
                {"service": {"id": "srv-other", "serviceDetails": {"url": "https://other.onrender.com"}}, "cursor": "a"},
                {"service": {"id": SERVICE, "serviceDetails": {"url": SITE}}, "cursor": "b"}])
        if path == f"/services/{SERVICE}/env-vars" and request.method == "GET":
            return httpx.Response(200, json=[{"envVar": {"key": k, "value": v}, "cursor": k} for k, v in self.env.items()])
        if path.startswith(f"/services/{SERVICE}/env-vars/") and request.method == "PUT":
            key = path.rsplit("/", 1)[1]
            self.env[key] = json.loads(request.content)["value"]
            return httpx.Response(200, json={"key": key, "value": self.env[key]})
        if path == f"/services/{SERVICE}/deploys" and request.method == "POST":
            return httpx.Response(201, json={"id": "dep-1", "status": "created"})
        if path == f"/services/{SERVICE}/deploys/dep-1":
            status = self.deploy_statuses.pop(0) if len(self.deploy_statuses) > 1 else self.deploy_statuses[0]
            self.deployed = status == "live"
            return httpx.Response(200, json={"id": "dep-1", "status": status})
        raise AssertionError(f"unexpected request {request.method} {url}")

    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.commands if c[0] == "gh"]

    def render_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).startswith(lp.RENDER_API)]


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
        self.report("20261024T080000Z", "success")
        self.current = list(current)
        self.exports: list[list[str]] = []
        stored = windows if windows is not None else [("18.5", 1500, 2), ("18.4", 3416, 1)]
        monkeypatch.setattr(lp, "available_balance_windows", lambda db: list(stored))

    def report(self, stamp: str, outcome: str) -> None:
        (self.reports / f"local-collect-{stamp}.json").write_text(json.dumps({"outcome": outcome}))

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


def publish(local: Local, world: World, **overrides) -> lp.PublishReport:
    config = lp.PublishConfig(db_path=local.db, repo_root=local.root, site_url=SITE, render_api_key=RENDER_KEY,
                              **overrides)
    with httpx.Client(transport=httpx.MockTransport(world.handler)) as http:
        return lp.run_local_publish(
            config, at_ms=0, current_windows=local.current_windows, make_snapshot=local.make_snapshot,
            run=world.run, http=http, gh=lambda: "gh", reports_dir=local.reports,
            sleep=lambda _s: None, clock=iter(range(10_000)).__next__,
        )


# ---------------------------------------------------------------- the whole path


def test_publish_releases_points_render_at_it_deploys_and_checks_the_site(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    report = publish(local, world)
    assert report.outcome == lp.PUBLISHED and report.exit_code == 0, report.message
    assert report.tag == "public-snapshot-18.5-20261024" and local.exports == [["18.5"]]
    create = next(c for c in world.gh_calls() if c[1:3] == ["release", "create"])
    assert create[create.index("--target") + 1] == COMMIT
    assert [Path(p).name for p in create[4:create.index("--repo")]] == [lp.SNAPSHOT_ASSET, lp.MANIFEST_ASSET]
    assert lp.PUBLISH_MARKER in create[create.index("--notes") + 1]
    url = f"https://github.com/{REPO}/releases/download/{report.tag}/{lp.SNAPSHOT_ASSET}"
    assert world.env["TFT_SNAPSHOT_URL"] == url == report.url and world.env["TFT_SNAPSHOT_SHA256"] == "a" * 64
    assert world.env["TFT_DATA_SOURCE"] == "snapshot"  # nothing else on the service is touched
    puts = [r for r in world.render_calls() if r.method == "PUT"]
    assert [r.url.path.rsplit("/", 1)[1] for r in puts] == ["TFT_SNAPSHOT_URL", "TFT_SNAPSHOT_SHA256"]
    assert any(r.method == "POST" and r.url.path.endswith("/deploys") for r in world.render_calls())
    assert [s.name for s in report.steps if s.status == "ok"][-3:] == ["Render deploy", "Website", "Discovery"]
    assert RENDER_KEY not in json.dumps(report.as_dict())


def test_the_published_report_and_saved_file_never_hold_the_render_key(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    report = publish(local, world)
    path = lp.write_publish_report(report, local.reports, at_ms=0)
    assert RENDER_KEY not in path.read_text() and "Bearer" not in path.read_text()


# ---------------------------------------------------------------- gates


@pytest.mark.parametrize("change", [{"head": "1" * 40}, {"dirty": " M src/tftlab/carry.py\n"}])
def test_a_checkout_that_is_not_origin_main_publishes_nothing(change, tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(**change)
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "Nothing was published" in report.message
    assert world.gh_calls() == [] and world.requests == [] and local.exports == []


def test_a_failed_collection_blocks_publishing_but_a_preflight_stop_does_not(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    local.report("20261024T100000Z", "preflight_failed")  # e.g. an expired key: nothing changed
    assert publish(local, World()).outcome == lp.PUBLISHED
    local.report("20261024T120000Z", "collection_failed")
    local.report("20261024T140000Z", "preflight_failed")
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "collection_failed" in report.message
    assert world.gh_calls() == [] and local.exports[-1:] != [] and len(local.exports) == 1


def test_too_few_new_matches_is_a_clean_skip_unless_forced(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch)
    world = World(live_windows=["18.5"], live_matches=1400)
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and report.exit_code == 0 and "only 100 new matches" in report.message
    assert local.exports == [] and world.gh_calls() == []
    assert publish(local, World(live_windows=["18.5"], live_matches=1400), force=True).outcome == lp.PUBLISHED


def test_no_current_patch_is_a_skip_and_the_site_keeps_its_snapshot(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch, current=()), World()
    report = publish(local, world)
    assert report.outcome == lp.SKIPPED and "keeps its current snapshot" in report.message
    assert world.gh_calls() == [] and world.render_calls() == []


def test_a_thin_current_patch_keeps_the_previous_patch_alongside(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch, windows=[("18.5", 600, 2), ("18.4", 3416, 1)])
    report = publish(local, World())
    assert local.exports == [["18.4", "18.5"]] and report.tag == "public-snapshot-18.4_18.5-20261024"
    with Database(local.db) as db:
        assert lp.choose_windows(db, ["18.5"], min_current_alone=500)[0] == ["18.5"]


def test_a_missing_render_key_or_site_url_stops_before_anything(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    config = lp.PublishConfig(db_path=local.db, repo_root=tmp_path, site_url=SITE, render_api_key=None)
    with httpx.Client(transport=httpx.MockTransport(world.handler)) as http:
        report = lp.run_local_publish(config, at_ms=0, current_windows=local.current_windows,
                                      make_snapshot=local.make_snapshot, run=world.run, http=http, gh=lambda: "gh",
                                      reports_dir=local.reports)
        assert report.outcome == lp.FAILED and "local-set-render-key" in report.message
        bad_url = lp.PublishConfig(db_path=local.db, repo_root=tmp_path, site_url="", render_api_key=RENDER_KEY)
        report = lp.run_local_publish(bad_url, at_ms=0, current_windows=local.current_windows,
                                      make_snapshot=local.make_snapshot, run=world.run, http=http, gh=lambda: "gh",
                                      reports_dir=local.reports)
        assert report.outcome == lp.FAILED and lp.SITE_URL_ENV in report.message
    assert world.commands == [] and world.requests == []


def test_a_dry_run_makes_the_snapshot_and_sends_nothing(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    config = lp.PublishConfig(db_path=local.db, repo_root=tmp_path, site_url=SITE, render_api_key=None, dry_run=True)
    with httpx.Client(transport=httpx.MockTransport(world.handler)) as http:
        report = lp.run_local_publish(config, at_ms=0, current_windows=local.current_windows,
                                      make_snapshot=local.make_snapshot, run=world.run, http=http, gh=lambda: "gh",
                                      reports_dir=local.reports)
    assert report.outcome == lp.DRY_RUN and report.exit_code == 0 and local.exports == [["18.5"]]
    assert world.gh_calls() == [] and world.render_calls() == []


# ---------------------------------------------------------------- failure handling


def test_a_failed_deploy_puts_the_previous_snapshot_settings_back(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(deploy_statuses=["build_in_progress", "build_failed"])
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "still serves the previous snapshot" in report.message
    assert world.env["TFT_SNAPSHOT_URL"] == OLD_URL and world.env["TFT_SNAPSHOT_SHA256"] == OLD_SHA
    assert not any(c[1:3] == ["release", "delete"] for c in world.gh_calls())


def test_a_deploy_that_never_finishes_times_out_and_restores(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(deploy_statuses=["build_in_progress"])
    report = publish(local, world)
    assert report.outcome == lp.FAILED and world.env["TFT_SNAPSHOT_URL"] == OLD_URL


def test_discovery_computed_live_after_publishing_is_reported_as_a_failure(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World(discovery_status="stale")
    report = publish(local, world)
    assert report.outcome == lp.FAILED and report.tag and "computes Discovery live" in report.message


def test_an_incomplete_upload_never_reaches_render(tmp_path, monkeypatch) -> None:
    local, world = Local(tmp_path, monkeypatch), World()
    real = world.run

    def short_upload(cmd):
        done = real(cmd)
        if [str(c) for c in cmd][1:3] == ["release", "create"]:
            world.asset_sizes[lp.SNAPSHOT_ASSET] -= 1
        return done

    world.run = short_upload
    report = publish(local, world)
    assert report.outcome == lp.FAILED and "full size" in report.message and world.render_calls() == []


# ---------------------------------------------------------------- tags and retention


def test_release_tags_are_never_reused() -> None:
    latest = "2026-10-24T08:59:00Z"
    assert lp.release_tag(["18.5"], latest, set()) == "public-snapshot-18.5-20261024"
    taken = {"public-snapshot-18.5-20261024", "public-snapshot-18.5-20261024-r2"}
    assert lp.release_tag(["18.5"], latest, taken) == "public-snapshot-18.5-20261024-r3"


def test_retention_deletes_only_this_commands_own_old_releases(tmp_path, monkeypatch) -> None:
    local = Local(tmp_path, monkeypatch)
    manual = [{"tag": "public-snapshot-18.3-20261005", "body": "by hand", "created_at": "2026-10-08T00:00:00Z"},
              {"tag": "public-snapshot-18.4-20261017", "body": "Patch 18.4", "created_at": "2026-10-19T00:00:00Z"}]
    own = [{"tag": f"public-snapshot-18.5-202610{d:02d}", "body": lp.PUBLISH_MARKER,
            "created_at": f"2026-10-{d:02d}T09:00:00Z"} for d in range(15, 21)]
    world = World(releases=manual + own)
    report = publish(local, world)
    assert report.outcome == lp.PUBLISHED, report.message
    tags = {r["tag"] for r in world.releases}
    assert {r["tag"] for r in manual} <= tags  # never touched
    assert report.tag in tags and len([t for t in tags if t not in {r["tag"] for r in manual}]) == lp.KEEP_PUBLISHED_RELEASES
    assert report.deleted_releases == ["public-snapshot-18.5-20261016", "public-snapshot-18.5-20261015"]


# ---------------------------------------------------------------- CLI


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
