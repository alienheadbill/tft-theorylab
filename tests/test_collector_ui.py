"""The TheoryLabs Collector control panel (tftlab.collector_ui): a local-only
app over the existing local collector. Riot is always a fake here."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tftlab.collector_ui as ui
import tftlab.local_collector as lc
from tftlab import cli
from tftlab.riot import RiotApiError
from tftlab.storage import Database

from _helpers import build_live_ingest_like_source
from test_local_collector import AT_MS, FAKE_KEY, StubRiot, _metadata, _payloads_18_4

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
LOCAL = ("127.0.0.1", 50123)
OTHER_KEY = "RGAPI-fedcba98-7654-3210-fedc-ba9876543210"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A clean TheoryLabs folder as cwd, a fixed 18.4 clock and fake Riot."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", "")  # restored after the test even though the app changes it
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TFT_LOCAL_DB_PATH", raising=False)
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)
    stub = {"riot": StubRiot(_payloads_18_4(30))}
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: stub["riot"])
    monkeypatch.setattr(cli, "_fetch_current_metadata", _metadata)
    stopped = []
    app = ui.create_collector_app(root=tmp_path, port=PORT, token="test-token", on_shutdown=lambda: stopped.append(1))
    client = TestClient(app, base_url=BASE, client=LOCAL)
    return {"root": tmp_path, "app": app, "client": client, "stub": stub, "stopped": stopped}


def _post(client: TestClient, path: str, body: dict | None = None, **headers):
    return client.post(path, json=body or {}, headers={ui.TOKEN_HEADER: "test-token", **headers})


def _wait(app, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while app.state.jobs.busy:
        if time.time() > deadline:
            raise AssertionError("job did not finish")
        time.sleep(0.02)
    return app.state.jobs.snapshot()


def _save_key(client, key: str = FAKE_KEY):
    response = _post(client, "/api/key", {"api_key": key})
    assert response.status_code == 200, response.text
    return response


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------- starting, binding, isolation


def test_the_ui_starts_and_reports_without_modifying_anything(env) -> None:
    client, root = env["client"], env["root"]
    page = client.get("/")
    assert page.status_code == 200 and "TheoryLabs Collector" in page.text and "Collect Data" in page.text
    state = client.get("/api/state").json()
    assert state["collector"]["exists"] is False and state["key"]["state"] == "not_configured"
    assert state["jobs"] == {"current": None, "last": {}}
    assert state["collector"]["trusted_window"]["patch"] == "18.4" and state["collector"]["trusted_window"]["inside"]
    for asset in ("/ui/collector.js", "/ui/collector.css", "/static/app.css"):
        assert client.get(asset).status_code == 200, asset
    assert _files(root) == set()  # no database, .env, backup or folder was created


def test_bind_defaults_are_localhost_only(monkeypatch, tmp_path: Path) -> None:
    import uvicorn

    assert ui.HOST == "127.0.0.1"
    captured = {}

    class FakeServer:
        def __init__(self, config):
            captured["host"], captured["port"] = config.host, config.port
            self.started, self.should_exit = True, False

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(ui, "_ping", lambda *a, **k: False)
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", opened.append)
    monkeypatch.setenv("RIOT_API_KEY", "")
    assert ui.serve(open_browser=False, root=tmp_path) == 0
    assert captured["host"] == "127.0.0.1" and captured["ran"] and 8765 <= captured["port"] < 8775
    assert not (tmp_path / "data/local/.collector-ui.json").exists()  # removed again on stop
    from typer.testing import CliRunner
    help_text = CliRunner().invoke(cli.app, ["collector-ui", "--help"]).output
    assert "--host" not in help_text  # no way to bind anything else


def test_a_second_launch_only_reopens_the_browser(monkeypatch, tmp_path: Path) -> None:
    import uvicorn

    (tmp_path / "data/local").mkdir(parents=True)
    (tmp_path / "data/local" / ui.STATE_FILE_NAME).write_text(json.dumps({"port": 8770, "token": "t"}))
    monkeypatch.setattr(ui, "_ping", lambda port, timeout=1.5: port == 8770)
    monkeypatch.setattr(uvicorn, "Server", lambda *a, **k: pytest.fail("a second server was started"))
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", opened.append)
    monkeypatch.setenv("RIOT_API_KEY", "")
    assert ui.serve(root=tmp_path) == 0 and opened == ["http://127.0.0.1:8770/"]


def test_requests_from_other_machines_or_hosts_are_refused(env) -> None:
    app = env["app"]
    remote = TestClient(app, base_url=BASE, client=("192.168.1.20", 50000))
    assert remote.get("/api/state").status_code == 403 and remote.get("/").status_code == 403
    assert env["client"].get("/api/state", headers={"Host": "evil.example:8765"}).status_code == 403  # DNS rebinding
    assert env["client"].get("/api/state", headers={"Host": "localhost:8765"}).status_code == 200


def test_state_changing_requests_need_the_page_token_and_same_origin(env) -> None:
    client = env["client"]
    for path in ("/api/key", "/api/key/validate", "/api/collect", "/api/snapshot", "/api/shutdown"):
        assert client.post(path, json={"api_key": FAKE_KEY}).status_code == 403, path
        assert _post(client, path, {"api_key": FAKE_KEY}, Origin="https://evil.example").status_code == 403, path
        assert client.get(path).status_code in (404, 405), path  # never a GET action
    assert not (env["root"] / ".env").exists() and env["app"].state.jobs.snapshot()["current"] is None


def test_security_headers_and_no_api_docs(env) -> None:
    response = env["client"].get("/")
    assert response.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in \
        response.headers["Content-Security-Policy"] and response.headers["Cache-Control"] == "no-store"
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert env["client"].get(path).status_code == 404


def test_the_public_website_gains_no_local_control_routes() -> None:
    from tftlab import webapp

    paths = {getattr(route, "path", "") for route in webapp.app.routes}
    for local in ("/api/key", "/api/key/validate", "/api/collect", "/api/snapshot", "/api/shutdown", "/api/ping",
                  "/ui"):
        assert local not in paths, local
    source = Path(webapp.__file__).read_text()
    assert "collector_ui" not in source and "collector_web" not in source
    public_static = Path(webapp.__file__).parent / "web" / "static"
    assert not list(public_static.glob("collector*"))  # the panel's assets are not served by the public site


# ---------------------------------------------------------------- status


def test_status_with_an_existing_database(env) -> None:
    root = env["root"]
    build_live_ingest_like_source(root / "data/local/theorylabs.sqlite3", 12)  # 18.3 history
    state = env["client"].get("/api/state").json()["collector"]
    assert state["exists"] is True and state["size_bytes"] > 0
    assert state["store"]["matches"] == 12 and state["store"]["boards"] == 96
    assert state["current"]["matches"] == 0  # nothing from 18.4 yet
    assert {w["balance_window"] for w in state["windows"]} == {"18.3"}
    assert state["latest_backup"] is None and state["last_report"] is None


def test_status_survives_an_unreadable_database(env) -> None:
    db = env["root"] / "data/local/theorylabs.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    response = env["client"].get("/api/state")
    assert response.status_code == 200 and response.json()["collector"]["exists"] is True


# ---------------------------------------------------------------- the Riot key


def test_saving_a_key_uses_the_existing_writer_and_never_echoes_it(env, monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    calls = []
    real = lc.set_env_key
    monkeypatch.setattr(lc, "set_env_key", lambda path, key: calls.append(path) or real(path, key))
    client, root = env["client"], env["root"]
    (root / ".env.example").write_text("RIOT_API_KEY=RGAPI-your-key-here\nTFT_PLATFORM=na1\nTFT_REGION=americas\n")
    response = _save_key(client)
    assert calls == [root / ".env"]
    assert (root / ".env").read_text() == f"RIOT_API_KEY={FAKE_KEY}\nTFT_PLATFORM=na1\nTFT_REGION=americas\n"
    assert response.json()["key"]["state"] == "unchecked"
    _save_key(client, OTHER_KEY)  # replacing keeps the other lines
    assert (root / ".env").read_text().count("RGAPI-") == 1 and OTHER_KEY in (root / ".env").read_text()
    seen = [response.text, client.get("/").text, client.get("/api/state").text, caplog.text]
    for text in seen:
        assert FAKE_KEY not in text and OTHER_KEY not in text


def test_a_malformed_key_is_refused_without_echo_or_change(env) -> None:
    client, root = env["client"], env["root"]
    bad = _post(client, "/api/key", {"api_key": "RGAPI-not-a-real-key-" + "x" * 10})
    assert bad.status_code == 422 and "does not look like a Riot API key" in bad.json()["error"]
    assert "RGAPI-not" not in bad.text and not (root / ".env").exists()
    wrong_type = _post(client, "/api/key", {"api_key": {"nested": FAKE_KEY}})
    assert wrong_type.status_code == 422 and FAKE_KEY not in wrong_type.text  # FastAPI would echo the input
    assert client.post("/api/key?api_key=" + FAKE_KEY, headers={ui.TOKEN_HEADER: "test-token"}).status_code == 422


def test_key_check_valid_expired_and_unavailable(env, monkeypatch) -> None:
    client = env["client"]
    assert _post(client, "/api/key/validate").status_code == 409  # nothing saved yet
    _save_key(client)
    ok = _post(client, "/api/key/validate").json()
    assert ok["ok"] is True and ok["key"]["state"] == "valid"
    env["stub"]["riot"] = StubRiot([], key_error="401")
    expired = _post(client, "/api/key/validate").json()
    assert expired["ok"] is False and expired["key"]["state"] == "invalid"
    assert "Your Riot development key has expired" in expired["key"]["detail"]

    class Offline(StubRiot):
        def challenger(self):
            raise RiotApiError("Network error contacting Riot API (ConnectError) for https://na1.api.riotgames.com/x")

    env["stub"]["riot"] = Offline([])
    offline = _post(client, "/api/key/validate").json()
    assert offline["key"]["state"] == "error" and "network" in offline["key"]["detail"]
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    crashed = _post(client, "/api/key/validate")
    assert crashed.status_code == 200 and crashed.json()["key"]["state"] == "error"
    assert FAKE_KEY not in json.dumps([ok, expired, offline, crashed.json()])


def test_the_log_file_redacts_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    previous = logging.getLogger().handlers[:]
    try:
        path = ui._configure_logging(tmp_path)
        logging.getLogger("tftlab.collector_ui").info("oops %s and %s", FAKE_KEY, "RGAPI-abcdefab-0000")
        try:
            raise RuntimeError(f"failure mentioning {FAKE_KEY}")
        except RuntimeError:
            logging.getLogger("tftlab.collector_ui").exception("crash")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = path.read_text()
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.getLogger().handlers[:] = previous
    assert "RGAPI-" not in text and "[redacted" in text and "failure mentioning" in text


# ---------------------------------------------------------------- collection jobs


def test_collect_runs_in_the_background_and_reports_results(env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = env["client"]
    _save_key(client)
    started = _post(client, "/api/collect")
    assert started.status_code == 200 and started.json()["job"]["state"] == "running"
    jobs = _wait(env["app"])
    job = jobs["last"]["collect"]
    assert jobs["current"] is None and job["state"] == "succeeded" and job["phase"] == "Complete"
    r = job["result"]
    assert r["new_matches"] == 30 and r["patch_matches"] == 30 and r["boards"] == 240 and r["patch"] == "18.4"
    assert r["riot_requests"] > 100 and r["rate_limited"] == 0 and r["validation"]["18.4"]["is_severe"] is False
    assert "18.4: prepared" in r["prepared"] and r["ready_for_snapshot"] is True
    assert "Create Public Snapshot" in job["message"] and "tftlab" not in job["message"]
    assert Path(r["report_path"]).is_file() and "no pre-run backup" in r["backup"]
    names = [s["name"] for s in job["steps"]]
    assert names[:2] == ["Local database", "Disk space"] and "Validation" in names and "Discovery preparation" in names
    state = client.get("/api/state").json()
    assert state["collector"]["current"]["matches"] == 30 and state["collector"]["last_report"]["outcome"] == "success"
    blob = json.dumps(state) + caplog.text + Path(r["report_path"]).read_text()
    assert FAKE_KEY not in blob and "PUUID-" not in blob and "NA1_" not in blob


def test_the_cli_and_the_ui_share_one_collection_code_path(env, monkeypatch) -> None:
    calls = []
    real = cli.run_local_collection
    monkeypatch.setattr(cli, "run_local_collection", lambda *a, **k: calls.append(k) or real(*a, **k))
    _save_key(env["client"])
    _post(env["client"], "/api/collect")
    _wait(env["app"])
    assert len(calls) == 1 and callable(calls[0]["client_factory"]) and callable(calls[0]["progress"])
    source = Path(ui.__file__).read_text()
    # Orchestration only: no collector, backup, validation, Discovery or export logic of its own.
    for reimplemented in ("ingest_ladder", "run_local_collect(", "backup_local_db", "validate_live_data",
                          "prepare_window", "export_public_snapshot", "client.match("):
        assert reimplemented not in source, reimplemented


class _Blocking(StubRiot):
    """Holds the collection inside its first match fetch until released."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.entered, self.release = threading.Event(), threading.Event()

    def match(self, match_id):
        self.entered.set()
        assert self.release.wait(20)
        return super().match(match_id)

    @property
    def telemetry(self):  # the real RiotClient's live counters (read by the progress display)
        from types import SimpleNamespace
        return SimpleNamespace(requests=len(self.calls), rate_limited=0)


def test_only_one_job_at_a_time_and_polling_never_starts_one(env) -> None:
    client, app = env["client"], env["app"]
    _save_key(client)
    blocking = _Blocking(_payloads_18_4(10))
    env["stub"]["riot"] = blocking
    first = _post(client, "/api/collect").json()["job"]
    assert blocking.entered.wait(10)
    for _ in range(5):
        state = client.get("/api/state").json()
        assert state["jobs"]["current"]["id"] == first["id"]  # polling/refresh: same job, nothing new
        assert client.get("/").status_code == 200
    live = state["jobs"]["current"]
    assert live["phase"] == "Collecting matches from Riot" and live["live"]["riot_requests"] > 0
    for path in ("/api/collect", "/api/snapshot", "/api/key/validate", "/api/shutdown"):
        refused = _post(client, path)
        assert refused.status_code == 409, path
        assert "already running" in refused.json()["error"] or "running" in refused.json()["error"]
    assert not env["stopped"]
    threads = [t.name for t in threading.enumerate() if t.name.startswith("collector-ui-")]
    assert threads == ["collector-ui-collect"]
    blocking.release.set()
    jobs = _wait(app)
    assert jobs["current"] is None and jobs["last"]["collect"]["id"] == first["id"]
    assert jobs["last"]["collect"]["state"] == "succeeded"
    assert _post(client, "/api/snapshot").status_code == 200  # the slot is free again
    _wait(app)


def test_failures_release_the_slot_with_readable_messages(env, monkeypatch) -> None:
    client, app = env["client"], env["app"]
    _save_key(client)
    env["stub"]["riot"] = StubRiot([], key_error="401")
    _post(client, "/api/collect")
    job = _wait(app)["last"]["collect"]
    assert job["state"] == "failed" and job["phase"] == "Failed"
    assert job["message"].startswith("Your Riot development key has expired") and "Nothing was collected" in job["message"]
    assert not (env["root"] / "data/local/theorylabs.sqlite3").exists()

    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS + 30 * 86_400_000)  # after 18.4 ends
    _post(client, "/api/collect")
    job = _wait(app)["last"]["collect"]
    assert job["state"] == "failed" and "does not yet have a trusted window" in job["message"]

    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)

    def crash(*a, **k):
        raise RuntimeError(f"disk exploded near {FAKE_KEY}")

    monkeypatch.setattr(cli, "run_local_collection", crash)
    _post(client, "/api/collect")
    job = _wait(app)["last"]["collect"]
    assert job["state"] == "failed" and job["message"] == ui.UNEXPECTED_MESSAGE
    assert "disk exploded" in job["detail"] and FAKE_KEY not in json.dumps(job)
    assert app.state.jobs.snapshot()["current"] is None and _post(client, "/api/collect").status_code == 200
    _wait(app)


def test_validation_failure_says_not_to_snapshot(env, monkeypatch) -> None:
    import dataclasses

    from tftlab.validate import validate_live_data

    def broken(db, *, balance_window, metadata):
        return dataclasses.replace(validate_live_data(db, balance_window=balance_window, metadata=metadata),
                                   malformed_placements=2)

    monkeypatch.setattr(lc.run_local_collect, "__kwdefaults__", {**lc.run_local_collect.__kwdefaults__,
                                                                 "validate": broken})
    _save_key(env["client"])
    _post(env["client"], "/api/collect")
    job = _wait(env["app"])["last"]["collect"]
    assert job["state"] == "failed" and job["message"] == ui.VALIDATION_FAILED_MESSAGE
    assert job["result"]["ready_for_snapshot"] is False


def test_a_terminal_collection_holding_the_lock_is_respected(env) -> None:
    client, app = env["client"], env["app"]
    _save_key(client)
    db = env["root"] / "data/local/theorylabs.sqlite3"
    build_live_ingest_like_source(db, 4)
    with lc.collector_lock(db):  # e.g. `tftlab local-collect` running in a terminal
        _post(client, "/api/collect")
        collect = _wait(app)["last"]["collect"]
        _post(client, "/api/snapshot")
        snap = _wait(app)["last"]["snapshot"]
    assert collect["message"] == snap["message"] == ui.CLI_BUSY_MESSAGE


# ---------------------------------------------------------------- snapshots


def test_snapshot_reuses_local_snapshot_and_never_uploads(env, monkeypatch) -> None:
    client, app = env["client"], env["app"]
    _save_key(client)
    _post(client, "/api/collect")
    _wait(app)
    calls = []
    real = lc.local_snapshot
    monkeypatch.setattr(lc, "local_snapshot", lambda *a, **k: calls.append(a) or real(*a, **k))
    _post(client, "/api/snapshot")
    job = _wait(app)["last"]["snapshot"]
    assert job["state"] == "succeeded" and len(calls) == 1
    r = job["result"]
    assert r["balance_windows"] == ["18.4"] and r["matches"] == 30 and r["boards"] == 240
    assert Path(r["gz_path"]).is_file() and len(r["gz_sha256"]) == 64 and r["checks_passed"] >= 10
    assert "Nothing was uploaded or deployed" in job["message"]
    assert FAKE_KEY.encode() not in Path(r["gz_path"]).read_bytes()


def test_snapshot_refusals_are_readable(env) -> None:
    client, app = env["client"], env["app"]
    _post(client, "/api/snapshot")  # no database yet
    job = _wait(app)["last"]["snapshot"]
    assert job["state"] == "failed" and job["message"].startswith("The public snapshot was NOT created")
    from tftlab.demo import generate_demo_matches
    db = env["root"] / "data/local/theorylabs.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    with Database(db) as database:
        database.ingest_many(generate_demo_matches(10))
    _post(client, "/api/snapshot")
    job = _wait(app)["last"]["snapshot"]
    assert job["state"] == "failed" and job["detail"]  # the exporter's reason, kept for diagnosis


# ---------------------------------------------------------------- stopping, launchers


def test_shutdown_stops_only_when_idle(env) -> None:
    response = _post(env["client"], "/api/shutdown")
    assert response.status_code == 200 and "stopping" in response.json()["message"]
    deadline = time.time() + 3
    while not env["stopped"] and time.time() < deadline:
        time.sleep(0.05)
    assert env["stopped"] == [1]
    assert "not running" in ui.stop_running(env["root"])


ROOT = Path(__file__).resolve().parent.parent


def test_windows_launchers_start_the_panel_detached_and_stop_it() -> None:
    start = (ROOT / "TheoryLabs Collector.cmd").read_text()
    assert 'cd /d "%~dp0"' in start and '.venv\\Scripts\\pythonw.exe' in start
    assert 'start "" ".venv\\Scripts\\pythonw.exe" -m tftlab.collector_ui' in start and "exit /b 0" in start
    stop = (ROOT / "Stop TheoryLabs Collector.cmd").read_text()
    assert "-m tftlab.collector_ui --stop" in stop
    for text in (start, stop):
        for forbidden in ("--host", "0.0.0.0", "DATABASE_URL", "RIOT_API_KEY=", "ingest-riot"):
            assert forbidden not in text, forbidden
    sh = (ROOT / "scripts" / "collector-ui.sh").read_text()
    assert "collector-ui" in sh and "0.0.0.0" not in sh
