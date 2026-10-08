"""`tftlab fetch-snapshot` (tftlab.snapshot_fetch): installing the published
public snapshot on the website host. No real network: every download goes
through an httpx.MockTransport."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import tftlab.snapshot_fetch as sf
from tftlab import cli, webapp
from tftlab.public_snapshot import export_public_snapshot
from tftlab.storage import Database

from _helpers import build_live_ingest_like_source

URL = "https://github.com/alienheadbill/tft-theorylab/releases/download/public-snapshot-18.3-20261005/" \
      "theorylabs-public-snapshot.sqlite3.gz"
ASSET_HOST = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> dict:
    """A real exported, verified public snapshot (.gz bytes + its facts)."""
    work = tmp_path_factory.mktemp("snapshot")
    build_live_ingest_like_source(work / "source.sqlite3", 30)
    with Database.open_existing(work / "source.sqlite3") as db:
        result = export_public_snapshot(db, work / "out" / "theorylabs-public-snapshot.sqlite3", compress=True)
    data = result.gzip_path.read_bytes()
    return {"bytes": data, "sha256": hashlib.sha256(data).hexdigest(), "provenance": result.provenance}


def _env(tmp_path: Path, snapshot: dict, **overrides) -> dict:
    env = {
        "TFT_DATA_SOURCE": "snapshot",
        "TFT_SNAPSHOT_URL": URL,
        "TFT_SNAPSHOT_SHA256": snapshot["sha256"],
        "TFT_SNAPSHOT_PATH": str(tmp_path / "data/snapshot/theorylabs-public-snapshot.sqlite3.gz"),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


class Server:
    """GitHub-like release download: a 302 to the asset host, then the bytes."""

    def __init__(self, body: bytes, *, status: int = 200, failures: int = 0, truncate: bool = False) -> None:
        self.body, self.status, self.failures, self.truncate = body, status, failures, truncate
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if "Authorization" in request.headers:
            raise AssertionError("no credentials are ever sent")
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"Location": ASSET_HOST + "?sig=SECRET-TOKEN"})
        if self.failures:
            self.failures -= 1
            raise httpx.ConnectError("connection reset")
        if self.status != 200:
            return httpx.Response(self.status, content=b"nope")
        if self.truncate:
            return httpx.Response(200, headers={"Content-Length": str(len(self.body))},
                                  content=self.body[: len(self.body) // 2])
        return httpx.Response(200, content=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _fetch(env, server: Server | None = None, **kwargs):
    lines: list[str] = []
    result = sf.fetch_configured_snapshot(env, transport=server.transport if server else _no_network(),
                                          progress=lines.append, sleep=lambda _s: None, **kwargs)
    return result, lines


def _no_network() -> httpx.MockTransport:
    def handler(request):
        raise AssertionError(f"network used: {request.url}")
    return httpx.MockTransport(handler)


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# ---------------------------------------------------------------- 1-2: download, sha256 accepted


def test_downloads_verifies_and_installs_the_snapshot(tmp_path: Path, snapshot: dict) -> None:
    server = Server(snapshot["bytes"])
    env = _env(tmp_path, snapshot)
    result, lines = _fetch(env, server)
    target = Path(env["TFT_SNAPSHOT_PATH"])
    assert result.status == "installed" and result.path == target
    assert target.read_bytes() == snapshot["bytes"] and result.sha256 == snapshot["sha256"]
    assert server.requests[0] == URL and server.requests[1].startswith(ASSET_HOST)  # redirect followed
    assert result.summary["matches"] == 30 and result.summary["boards"] == 240
    assert result.summary["balance_windows"] == ["18.3"] and result.summary["checks"] >= 10
    assert _leftovers(target.parent) == [target.name]  # no .part or scratch files left
    assert "SECRET-TOKEN" not in "\n".join(lines)  # query strings are never printed


def test_an_installed_matching_file_needs_no_network(tmp_path: Path, snapshot: dict) -> None:
    env = _env(tmp_path, snapshot)
    _fetch(env, Server(snapshot["bytes"]))
    result, lines = _fetch(env)  # _no_network() would fail on any request
    assert result.status == "already-installed" and "nothing downloaded" in lines[0]


def test_uppercase_sha256_is_accepted(tmp_path: Path, snapshot: dict) -> None:
    result, _ = _fetch(_env(tmp_path, snapshot, TFT_SNAPSHOT_SHA256=snapshot["sha256"].upper()),
                       Server(snapshot["bytes"]))
    assert result.status == "installed"


# ---------------------------------------------------------------- 3-4: wrong sha256, partial download


def test_a_wrong_sha256_is_rejected_and_nothing_is_installed(tmp_path: Path, snapshot: dict) -> None:
    env = _env(tmp_path, snapshot, TFT_SNAPSHOT_SHA256="0" * 64)
    with pytest.raises(sf.SnapshotFetchError, match="sha256 mismatch.*NOT installed"):
        _fetch(env, Server(snapshot["bytes"]))
    target = Path(env["TFT_SNAPSHOT_PATH"])
    assert not target.exists() and _leftovers(target.parent) == []


def test_a_bad_download_never_replaces_the_active_snapshot(tmp_path: Path, snapshot: dict) -> None:
    env = _env(tmp_path, snapshot)
    target = Path(env["TFT_SNAPSHOT_PATH"])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"the previously active snapshot")  # e.g. an older verified file
    for server, expected in ((Server(snapshot["bytes"], truncate=True), "incomplete download"),
                             (Server(snapshot["bytes"], status=404), "HTTP 404"),
                             (Server(snapshot["bytes"], failures=99), "after 3 attempts")):
        with pytest.raises(sf.SnapshotFetchError, match=expected):
            _fetch(env, server)
        assert target.read_bytes() == b"the previously active snapshot"
        assert _leftovers(target.parent) == [target.name]
    with pytest.raises(sf.SnapshotFetchError, match="sha256 mismatch"):
        _fetch(_env(tmp_path, snapshot, TFT_SNAPSHOT_SHA256="f" * 64), Server(snapshot["bytes"]))
    assert target.read_bytes() == b"the previously active snapshot"


def test_transient_failures_are_retried_a_bounded_number_of_times(tmp_path: Path, snapshot: dict) -> None:
    server = Server(snapshot["bytes"], failures=2)
    result, lines = _fetch(_env(tmp_path, snapshot), server)
    assert result.status == "installed" and sum("attempt" in line for line in lines) == 3
    not_found = Server(snapshot["bytes"], status=404)
    with pytest.raises(sf.SnapshotFetchError):
        _fetch(_env(tmp_path / "b", snapshot), not_found)
    assert sum(1 for u in not_found.requests if u.startswith(ASSET_HOST)) == 1  # a 404 is final, not retried


def test_an_oversized_download_is_refused(tmp_path: Path, snapshot: dict) -> None:
    with pytest.raises(sf.SnapshotFetchError, match="limit"):
        _fetch(_env(tmp_path, snapshot), Server(snapshot["bytes"]), max_bytes=1000)


# ---------------------------------------------------------------- 5: corrupt / uncertified archives


def _matching(tmp_path: Path, body: bytes) -> tuple[dict, Server]:
    env = _env(tmp_path, {"sha256": hashlib.sha256(body).hexdigest()})
    return env, Server(body)


def test_a_corrupt_archive_with_a_matching_sha256_is_rejected(tmp_path: Path, snapshot: dict) -> None:
    corrupt = bytearray(snapshot["bytes"])
    corrupt[len(corrupt) // 2] ^= 0xFF
    env, server = _matching(tmp_path, bytes(corrupt))
    with pytest.raises(sf.SnapshotFetchError, match="corrupt|verification|not a valid"):
        _fetch(env, server)
    assert not Path(env["TFT_SNAPSHOT_PATH"]).exists()
    env, server = _matching(tmp_path, b"not gzip at all")
    with pytest.raises(sf.SnapshotFetchError, match="corrupt"):
        _fetch(env, server)


def test_a_demo_or_tampered_database_is_rejected_by_the_snapshot_checks(tmp_path: Path, snapshot: dict) -> None:
    from tftlab.demo import generate_demo_matches

    demo = tmp_path / "demo.sqlite3"
    with Database(demo) as db:
        db.ingest_many(generate_demo_matches(20))
    env, server = _matching(tmp_path, gzip.compress(demo.read_bytes()))
    with pytest.raises(sf.SnapshotFetchError, match="verification|not a valid"):
        _fetch(env, server)

    plain = tmp_path / "tampered.sqlite3"
    plain.write_bytes(gzip.decompress(snapshot["bytes"]))
    conn = sqlite3.connect(plain)
    prov = json.loads(conn.execute("SELECT value FROM public_snapshot_metadata").fetchone()[0])
    prov["synthetic"] = True
    conn.execute("UPDATE public_snapshot_metadata SET value = ?", (json.dumps(prov),))
    conn.commit()
    conn.close()
    env, server = _matching(tmp_path, gzip.compress(plain.read_bytes()))
    with pytest.raises(sf.SnapshotFetchError, match="verification"):
        _fetch(env, server)
    assert not Path(env["TFT_SNAPSHOT_PATH"]).exists()


# ---------------------------------------------------------------- 6-8: demo mode, unavailable, no database


@pytest.mark.parametrize("mode", [None, "", "demo", "database"])
def test_other_modes_need_no_snapshot_and_no_network(mode, tmp_path: Path) -> None:
    result, _ = _fetch({"TFT_DATA_SOURCE": mode} if mode is not None else {})
    assert result.status == "not-snapshot-mode"
    assert not (tmp_path / "data").exists()


def test_demo_site_still_serves_with_the_new_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TFT_DATA_SOURCE", "demo")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "demo.sqlite3"))
    assert CliRunner().invoke(cli.app, ["fetch-snapshot"]).exit_code == 0
    body = TestClient(webapp.create_app()).get("/api/source").json()
    assert body["mode"] == "demo" and body["synthetic"] is True and body["observed"] is False


@pytest.mark.parametrize("overrides,message", [
    ({"TFT_SNAPSHOT_URL": None, "TFT_SNAPSHOT_SHA256": None, "TFT_SNAPSHOT_PATH": None}, "needs TFT_SNAPSHOT_URL"),
    ({"TFT_SNAPSHOT_URL": None}, "no snapshot file"),
    ({"TFT_SNAPSHOT_SHA256": None}, "64-character hex"),
    ({"TFT_SNAPSHOT_SHA256": "abc"}, "64-character hex"),
    ({"TFT_SNAPSHOT_URL": "http://example.com/s.sqlite3.gz"}, "https://"),
    ({"TFT_SNAPSHOT_PATH": None}, "TFT_SNAPSHOT_PATH must be set"),
    ({"TFT_SNAPSHOT_PATH": "data/snapshot/theorylabs.sqlite3"}, "must end in .gz"),
])
def test_snapshot_mode_fails_closed_when_misconfigured(overrides, message, tmp_path: Path, snapshot: dict) -> None:
    with pytest.raises(sf.SnapshotFetchError, match=message):
        _fetch(_env(tmp_path, snapshot, **overrides))  # no network either


def test_an_existing_local_snapshot_without_a_url_is_verified(tmp_path: Path, snapshot: dict) -> None:
    path = tmp_path / "shipped.sqlite3.gz"
    path.write_bytes(snapshot["bytes"])
    result, _ = _fetch(_env(tmp_path, snapshot, TFT_SNAPSHOT_URL=None, TFT_SNAPSHOT_PATH=str(path)))
    assert result.status == "verified-local" and result.summary["matches"] == 30
    path.write_bytes(b"garbage")
    with pytest.raises(sf.SnapshotFetchError):
        _fetch(_env(tmp_path, snapshot, TFT_SNAPSHOT_URL=None, TFT_SNAPSHOT_PATH=str(path)))


def test_snapshot_mode_never_uses_or_contacts_a_database(tmp_path: Path, snapshot: dict, monkeypatch) -> None:
    monkeypatch.setattr(Database, "_connect_postgres", staticmethod(lambda *a, **k: pytest.fail("Postgres opened")))
    with pytest.raises(sf.SnapshotFetchError, match="cannot be combined with DATABASE_URL") as exc:
        _fetch(_env(tmp_path, snapshot, DATABASE_URL="postgresql://u:hunter2@ep-x.neon.tech/db"))
    assert "hunter2" not in str(exc.value) and "neon" not in str(exc.value)
    result, _ = _fetch(_env(tmp_path, snapshot), Server(snapshot["bytes"]))  # and the normal path never does
    assert result.status == "installed"
    source = Path(sf.__file__).read_text()
    assert "NEON" not in source and "psycopg" not in source


# ---------------------------------------------------------------- CLI + the website end to end


def test_cli_installs_then_the_website_serves_the_snapshot_as_observed(tmp_path: Path, snapshot: dict,
                                                                       monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key, value in _env(tmp_path, snapshot).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_SNAPSHOT_CACHE_DIR", str(tmp_path / "cache"))
    server = Server(snapshot["bytes"])
    real_fetch = sf.fetch_configured_snapshot
    monkeypatch.setattr(sf, "fetch_configured_snapshot",
                        lambda **kw: real_fetch(transport=server.transport, sleep=lambda _s: None, **kw))
    result = CliRunner().invoke(cli.app, ["fetch-snapshot"])
    assert result.exit_code == 0, result.output
    assert "Snapshot installed and verified" in result.output and "matches 30" in result.output
    again = CliRunner().invoke(cli.app, ["fetch-snapshot"])
    assert again.exit_code == 0 and "already installed" in again.output

    client = TestClient(webapp.create_app())
    body = client.get("/api/source").json()
    prov = snapshot["provenance"]
    assert body["mode"] == "snapshot" and body["observed"] is True and body["synthetic"] is False
    snap = body["snapshot"]
    assert snap["balance_windows"] == prov["balance_windows"] == ["18.3"]
    assert snap["matches"] == prov["matches"] and snap["boards"] == prov["boards"]
    assert snap["latest_game"] == prov["latest_game"]  # straight from the file, never hardcoded
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["source"] == "snapshot"


def test_cli_failure_exits_nonzero_and_the_website_stays_closed(tmp_path: Path, snapshot: dict, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key, value in _env(tmp_path, snapshot, TFT_SNAPSHOT_SHA256="0" * 64).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    server = Server(snapshot["bytes"])
    real_fetch = sf.fetch_configured_snapshot
    monkeypatch.setattr(sf, "fetch_configured_snapshot",
                        lambda **kw: real_fetch(transport=server.transport, sleep=lambda _s: None, **kw))
    result = CliRunner().invoke(cli.app, ["fetch-snapshot"])
    assert result.exit_code == 1 and "Snapshot NOT installed" in result.output
    client = TestClient(webapp.create_app())
    for path in ("/api/health", "/api/source", "/api/carries"):
        response = client.get(path)
        assert response.status_code == 503, path  # never demo data in snapshot mode
        assert "demo" not in response.text.lower() or response.json().get("demo") is not True


# ---------------------------------------------------------------- render.yaml


def test_render_runs_the_fetch_at_build_and_before_start() -> None:
    text = (Path(__file__).resolve().parent.parent / "render.yaml").read_text()
    assert 'buildCommand: pip install -e ".[postgres]" && tftlab fetch-snapshot' in text
    assert "startCommand: tftlab fetch-snapshot && uvicorn tftlab.webapp:app --host 0.0.0.0 --port $PORT" in text
    for key in ("TFT_SNAPSHOT_URL", "TFT_SNAPSHOT_SHA256", "TFT_SNAPSHOT_PATH", "TFT_DATA_SOURCE", "DATABASE_URL"):
        block = text[text.index(f"- key: {key}"):]
        assert block.splitlines()[1].strip() == "sync: false", key  # set on the service, never committed
    assert "healthCheckPath: /api/health" in text
