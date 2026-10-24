"""Prepared Champion Investigation payloads (`tftlab.prepared_champions`):
identical to the live computation, served only while current, never labelled
prepared when computed live, shipped and verified inside public snapshots --
on SQLite and (with TFTLAB_TEST_DATABASE_URL) Postgres."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import tftlab.prepared_champions as pc
from _helpers import REAL_18_3_START_MS, build_live_ingest_like_source, live_ingest_like_payloads
from tftlab import public_snapshot as ps
from tftlab.champion_investigation import champion_directory, champion_investigation, resolve_champion
from tftlab.cli import app
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
WINDOW = "18.3"
MATCHES = 60
WEB = Path(__file__).parent.parent / "src" / "tftlab" / "web"

BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def target(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        path = tmp_path / "champions.sqlite3"
        build_live_ingest_like_source(path, MATCHES)
        return str(path)
    with Database(POSTGRES_TEST_URL) as db:
        for table in ("champion_prepared_payloads", "champion_prepared_runs", "traits", "units", "participants",
                      "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.ingest_many(live_ingest_like_payloads(MATCHES))
    return POSTGRES_TEST_URL


def _live(db: Database, key: str, top_n: int = pc.PREPARED_TOP_N) -> dict:
    champion = resolve_champion(db, WINDOW, key)
    return jsonable_encoder(champion_investigation(db, champion, WINDOW, top_n=top_n))


def _carried(db: Database) -> list[dict]:
    return [c for c in champion_directory(db, WINDOW) if c["carry_games"]]


# ---------------------------------------------------------------- equivalence


def test_every_prepared_payload_and_the_directory_equal_the_live_computation(target: str) -> None:
    with Database(target) as db:
        result = pc.prepare_window(db, WINDOW)
        assert result.status == "published" and result.champions >= len(champion_directory(db, WINDOW))
    with Database.open_existing(target) as db:
        lookup = pc.lookup_prepared(db, WINDOW)
        assert lookup.status == "current" and lookup.run.top_n == pc.PREPARED_TOP_N
        directory = champion_directory(db, WINDOW)
        assert pc.read_prepared_directory(db, lookup.run) == jsonable_encoder(directory)
        carried = _carried(db)
        assert len(carried) >= 3 and any(c["carry_games"] >= 10 for c in carried)  # cores and boards have rows
        for entry in directory:  # carried or not, every champion the page can open
            champion = resolve_champion(db, WINDOW, entry["slug"])
            assert pc.read_prepared_champion(db, lookup.run, champion) == _live(db, entry["slug"]), entry["slug"]


def test_a_payload_built_for_another_champion_entry_is_never_served(target: str) -> None:
    with Database(target) as db:
        pc.prepare_window(db, WINDOW)
    with Database.open_existing(target) as db:
        run = pc.lookup_prepared(db, WINDOW).run
        champion = resolve_champion(db, WINDOW, _carried(db)[0]["slug"])
        assert pc.read_prepared_champion(db, run, {**champion, "slug": "renamed"}) is None
        assert pc.read_prepared_champion(db, run, {**champion, "character_id": "TFT18_Nobody"}) is None


# ---------------------------------------------------------------- freshness


def test_new_matches_make_the_run_stale_and_a_rerun_current(target: str) -> None:
    with Database(target) as db:
        first = pc.prepare_window(db, WINDOW)
        assert pc.prepare_window(db, WINDOW).status == "skipped"  # already current
        extra = live_ingest_like_payloads(1, seed=99)[0]
        extra["metadata"]["match_id"] = "NA1_5499999999"
        extra["info"]["game_datetime"] = REAL_18_3_START_MS + 600_000 * (MATCHES + 5)
        db.ingest_match(extra)
        db.commit()
        assert pc.lookup_prepared(db, WINDOW).status == "stale"
        second = pc.prepare_window(db, WINDOW)
        assert second.status == "published" and second.run_id != first.run_id
        assert pc.lookup_prepared(db, WINDOW).status == "current"


def test_a_new_analytics_version_makes_the_run_stale(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with Database(target) as db:
        pc.prepare_window(db, WINDOW)
    monkeypatch.setattr(pc, "ANALYTICS_VERSION", "c1-0000000000000000")
    with Database.open_existing(target) as db:
        assert pc.lookup_prepared(db, WINDOW).status == "stale"
        assert pc.lookup_prepared(db, "18.9").status == "missing"


def test_the_version_ignores_line_endings_and_covers_the_champion_page_code(tmp_path: Path) -> None:
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    lf.mkdir(), crlf.mkdir()
    (lf / "a.py").write_bytes(b"x = 1\ny = 2\n")
    (crlf / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    assert pc.analytics_version([lf / "a.py"]) == pc.analytics_version([crlf / "a.py"])
    assert pc.ANALYTICS_VERSION.startswith(f"c{pc.PREPARED_FORMAT}-")
    names = {p.name for p in pc.ANALYTICS_SOURCES}
    assert {"champion_investigation.py", "how_to_play.py", "boards.py", "cores.py", "game_art.py",
            "game_art_manifest.json", "set_roster.json", "prepared_champions.py"} <= names
    assert all(p.is_file() for p in pc.ANALYTICS_SOURCES)


def test_a_failed_preparation_keeps_the_previous_run(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with Database(target) as db:
        first = pc.prepare_window(db, WINDOW)

        def boom(*_a, **_k):
            raise RuntimeError("secret connection detail")

        monkeypatch.setattr(pc, "_compute", boom)
        with pytest.raises(RuntimeError):
            pc.prepare_window(db, WINDOW, force=True)
        assert pc.lookup_prepared(db, WINDOW).run.run_id == first.run_id
        failures = db.query_all("SELECT failure FROM champion_prepared_runs WHERE status = 'failed'")
        assert [r[0] for r in failures] == ["RuntimeError"]  # the type only, never the message


def test_publishing_keeps_only_the_newest_runs(target: str) -> None:
    with Database(target) as db:
        runs = [pc.prepare_window(db, WINDOW, force=True).run_id for _ in range(pc.KEEP_PUBLISHED_RUNS + 2)]
        kept = {r[0] for r in db.query_all("SELECT run_id FROM champion_prepared_runs")}
        assert kept == set(runs[-pc.KEEP_PUBLISHED_RUNS:])
        payload_runs = {r[0] for r in db.query_all("SELECT DISTINCT run_id FROM champion_prepared_payloads")}
        assert payload_runs == kept


# ---------------------------------------------------------------- the API


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "site.sqlite3"
    build_live_ingest_like_source(path, MATCHES)
    return path


def _client(monkeypatch: pytest.MonkeyPatch, path: Path) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(path))
    import tftlab.webapp as webapp

    webapp._WINDOW_AVERAGE_CACHE.clear()
    return TestClient(webapp.create_app())


def _without_prepared(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "prepared"}


def test_the_api_serves_prepared_payloads_identical_to_live_ones(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, source)
    with Database.open_existing(source) as db:
        slugs = [c["slug"] for c in _carried(db)[:4]] + ["ahri"]
    live = {s: client.get(f"/api/champions/{s}").json() for s in slugs}
    live_dir = client.get("/api/champions").json()
    assert all(b["prepared"]["status"] == "missing" for b in [*live.values(), live_dir])
    with Database(source) as db:
        run_id = pc.prepare_window(db, WINDOW).run_id
    for slug in slugs:
        body = client.get(f"/api/champions/{slug}").json()
        assert body["prepared"]["status"] == "current" and body["prepared"]["run_id"] == run_id
        assert body["prepared"]["analytics_version"] == pc.ANALYTICS_VERSION
        assert _without_prepared(body) == _without_prepared(live[slug]), slug
    directory = client.get("/api/champions").json()
    assert directory["prepared"]["status"] == "current"
    assert _without_prepared(directory) == _without_prepared(live_dir)


def test_another_top_n_or_a_stale_run_is_computed_live_and_says_so(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, source)
    with Database(source) as db:
        slug = _carried(db)[0]["slug"]
        live_three = client.get(f"/api/champions/{slug}", params={"top_n": 3}).json()
        pc.prepare_window(db, WINDOW)
    body = client.get(f"/api/champions/{slug}", params={"top_n": 3}).json()
    assert body["prepared"] == {"status": "missing", "reason": "top_n", "analytics_version": pc.ANALYTICS_VERSION}
    assert _without_prepared(body) == _without_prepared(live_three)
    monkeypatch.setattr(pc, "ANALYTICS_VERSION", "c1-0000000000000000")
    for url in (f"/api/champions/{slug}", "/api/champions"):
        prepared = client.get(url).json()["prepared"]
        assert prepared["status"] == "stale" and "run_id" not in prepared and "prepared_at" not in prepared


def test_the_route_default_top_n_is_the_prepared_one() -> None:
    import inspect

    import tftlab.webapp as webapp

    source = inspect.getsource(webapp)
    assert 'top_n: int = Query(6, ge=1, le=20, description="Rows kept per evidence list")' in source
    assert pc.PREPARED_TOP_N == 6
    js = (WEB / "static" / "champion.js").read_text(encoding="utf-8")
    assert "new URLSearchParams({ top_n: '6' })" in js


def test_the_evidence_tab_says_whether_the_page_was_prepared_or_computed_live() -> None:
    js = (WEB / "static" / "champion.js").read_text(encoding="utf-8")
    assert "prepared.status === 'current'" in js
    assert "Prepared ahead of time" in js and "Computed live" in js


# ---------------------------------------------------------------- snapshots and the CLI


def _site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot: Path) -> TestClient:
    for key in ("DATABASE_URL", "RIOT_API_KEY", "TFT_STALE_AFTER_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TFT_DATA_SOURCE", "snapshot")
    monkeypatch.setenv("TFT_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("TFT_SNAPSHOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


def test_a_public_snapshot_ships_current_prepared_champions(source: Path, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    with Database.open_existing(source) as db:
        result = ps.export_public_snapshot(db, tmp_path / "pub" / "public.sqlite3", compress=True)
        slug = _carried(db)[0]["slug"]
    assert result.verification["checks"]["prepared_champions_published"] == "passed"
    assert [(r["balance_window"], r["status"]) for r in result.provenance["prepared_champions"]] == [(WINDOW, "published")]
    assert result.provenance["code_version"]["champion_analytics_version"] == pc.ANALYTICS_VERSION
    with Database.open_existing(result.path) as db:  # stored against the finished file (its provenance included)
        assert pc.lookup_prepared(db, WINDOW).status == "current"
        assert pc.read_prepared_champion(db, pc.lookup_prepared(db, WINDOW).run,
                                         resolve_champion(db, WINDOW, slug)) == _live(db, slug)
    client = _site(monkeypatch, tmp_path, result.gzip_path)
    assert client.get(f"/api/champions/{slug}").json()["prepared"]["status"] == "current"
    assert client.get("/api/champions").json()["prepared"]["status"] == "current"


def test_verification_rejects_payloads_outside_a_published_run(source: Path, tmp_path: Path) -> None:
    with Database.open_existing(source) as db:
        result = ps.export_public_snapshot(db, tmp_path / "public.sqlite3")
    conn = sqlite3.connect(result.path)
    conn.execute("UPDATE champion_prepared_runs SET status = 'failed'")
    conn.commit()
    conn.close()
    with pytest.raises(ps.SnapshotExportError, match="prepared_champions_published"):
        ps.verify_public_snapshot(result.path)


def test_an_older_snapshot_without_the_tables_is_served_live(source: Path, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    with Database.open_existing(source) as db:
        result = ps.export_public_snapshot(db, tmp_path / "public.sqlite3")
        slug = _carried(db)[0]["slug"]
    conn = sqlite3.connect(result.path)
    conn.execute("DROP TABLE champion_prepared_payloads")
    conn.execute("DROP TABLE champion_prepared_runs")
    conn.commit()
    conn.close()
    client = _site(monkeypatch, tmp_path, result.path)
    body = client.get(f"/api/champions/{slug}").json()
    assert body["prepared"]["status"] == "missing" and body["carry"]["games"] > 0


def test_cli_prepare_champions(source: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(app, ["prepare-champions", "--db", str(source)])
    assert first.exit_code == 0, first.output
    assert f"analytics version {pc.ANALYTICS_VERSION}" in first.output and f"{WINDOW}: published" in first.output
    again = runner.invoke(app, ["prepare-champions", "--db", str(source)])
    assert again.exit_code == 0 and "already current, skipped" in again.output
    with Database.open_existing(source) as db:
        stored = db.query_one("SELECT payload_json FROM champion_prepared_payloads LIMIT 1")[0]
    assert json.loads(stored)["balance_window"] == WINDOW
