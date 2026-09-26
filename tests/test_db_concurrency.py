"""Safe database concurrency and resumable ingest.

- Web requests open production read-only and never run schema setup.
- A PostgreSQL deadlock (40P01) on one match is retried, nothing else is.
- Only completed ingest runs advance seed rotation; finalization is atomic.
- A partial run followed by a new run resumes without duplicating matches.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tftlab.demo import generate_demo_matches
from tftlab.ingest import ingest_ladder, is_deadlock
from tftlab.storage import Database, SchemaUnavailable

from test_ingest_riot import _StubRiotClient

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
NO_WAIT = (0.0, 0.0)


class FakeDeadlock(Exception):
    """Stands in for psycopg.errors.DeadlockDetected (same SQLSTATE)."""

    sqlstate = "40P01"


def _matches(n: int, prefix: str = "M") -> dict[str, dict]:
    out = {}
    for i, payload in enumerate(generate_demo_matches(n, seed=11)):
        payload = copy.deepcopy(payload)
        payload["metadata"]["match_id"] = f"{prefix}{i}"
        out[f"{prefix}{i}"] = payload
    return out


def _client(seeds: int, matches: dict[str, dict]) -> _StubRiotClient:
    puuids = [f"p{i}" for i in range(seeds)]
    ids = list(matches)
    return _StubRiotClient(puuids=puuids, match_ids_by_puuid={p: ids for p in puuids}, matches=matches)


def _counts(db: Database) -> tuple[int, int, int]:
    return tuple(db.query_one(f"SELECT COUNT(*) FROM {t}")[0] for t in ("matches", "participants", "units"))


def _inject_units_failures(db: Database, monkeypatch: pytest.MonkeyPatch, failures: list[Exception]) -> list[str]:
    """Raise the queued exceptions from the units INSERT -- i.e. mid-way
    through a match's transaction, after matches/participants rows exist."""
    original = db.executemany
    attempts: list[str] = []

    def flaky(sql, rows):
        if "INSERT INTO units" in sql:
            attempts.append(rows[0][0])
            if failures:
                raise failures.pop(0)
        return original(sql, rows)

    monkeypatch.setattr(db, "executemany", flaky)
    return attempts


def _run_status(db: Database, run_id: str) -> str | None:
    row = db.query_one("SELECT status FROM ingest_runs WHERE run_id = ?", (run_id,))
    return row[0] if row else None


# ---------------------------------------------------------------- web: no schema setup


def _live_sqlite(tmp_path: Path) -> Path:
    path = tmp_path / "prod.sqlite3"
    with Database(path) as db:  # the CLI/ingest side initializes the schema
        for payload in _matches(3).values():
            db.ingest_match(payload)
    return path


def _web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "unused.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "unused-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


WEB_PATHS = ("/api/health", "/api/balance-windows", "/api/carries", "/api/discovery", "/api/experiments")


def test_production_web_access_never_calls_init_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _live_sqlite(tmp_path)

    def forbidden(self):
        raise AssertionError("a web request ran schema setup")

    monkeypatch.setattr(Database, "_init_schema", forbidden)
    client = _web(monkeypatch, tmp_path, str(path))
    for url in WEB_PATHS:
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
    assert client.get("/api/health").json()["demo"] is False


def test_web_requests_execute_no_ddl_backfill_or_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _live_sqlite(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    statements: list[str] = []
    original = Database._connect_sqlite

    def traced(p, *, read_only=False):
        conn = original(p, read_only=read_only)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "_connect_sqlite", staticmethod(traced))
    client = _web(monkeypatch, tmp_path, str(path))
    for url in WEB_PATHS:
        assert client.get(url).status_code == 200, url
    assert statements  # the requests really did query
    mutating = re.compile(r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|REPLACE|VACUUM|PRAGMA\s+journal_mode)\b", re.I)
    assert [s for s in statements if mutating.search(s)] == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_open_existing_refuses_writes_and_missing_schema(tmp_path: Path) -> None:
    path = _live_sqlite(tmp_path)
    with Database.open_existing(path) as db:
        assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 3
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM matches")
    bare = tmp_path / "bare.sqlite3"
    sqlite3.connect(bare).execute("CREATE TABLE matches (match_id TEXT)").connection.close()
    with pytest.raises(SchemaUnavailable):
        Database.open_existing(bare)
    with pytest.raises(Exception):
        Database.open_existing(tmp_path / "absent.sqlite3")
    assert not (tmp_path / "absent.sqlite3").exists()


def test_local_demo_sqlite_still_initializes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "none.sqlite3"))
    demo = tmp_path / "demo.sqlite3"
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(demo))
    from tftlab.webapp import create_app

    body = TestClient(create_app()).get("/api/health").json()
    assert body["demo"] is True and body["matches"] > 0
    tables = {r[0] for r in sqlite3.connect(demo).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"matches", "participants", "units", "traits", "ingest_runs"} <= tables


def test_ingest_side_initialization_creates_the_full_schema(tmp_path: Path) -> None:
    with Database(tmp_path / "new.sqlite3") as db:
        tables = {r[0] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
        run_columns = [r[1] for r in db.query_all("PRAGMA table_info(ingest_runs)")]
    assert {"matches", "participants", "units", "traits", "seed_samples", "match_discoveries", "ingest_runs"} <= tables
    assert run_columns == ["run_id", "started_at", "completed_at", "status", "failure"]


# ---------------------------------------------------------------- deadlock retry


def test_deadlock_detection_is_sqlstate_40p01_only() -> None:
    assert is_deadlock(FakeDeadlock())
    assert not is_deadlock(sqlite3.OperationalError("database is locked"))
    other = Exception()
    other.sqlstate = "40001"  # serialization failure: not retried here
    assert not is_deadlock(other)


def test_first_deadlock_rolls_back_retries_and_stores_one_complete_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    matches = _matches(1)
    with Database(tmp_path / "dl1.sqlite3") as db:
        attempts = _inject_units_failures(db, monkeypatch, [FakeDeadlock()])
        result = ingest_ladder(_client(1, matches), db, seed_allocation={"challenger": 1}, retry_delays=NO_WAIT, run_id="r")
        counts = _counts(db)
    assert attempts == ["M0", "M0"]  # the same match, retried once
    assert (result.deadlock_retries, result.matches_with_deadlock_retry, result.deadlocks_recovered) == (1, 1, 1)
    assert counts == (1, 8, 32)  # exactly one complete match: the failed attempt left nothing behind
    assert result.run_status == "completed"


def test_two_transient_deadlocks_then_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    matches = _matches(2)
    with Database(tmp_path / "dl2.sqlite3") as db:
        attempts = _inject_units_failures(db, monkeypatch, [FakeDeadlock(), FakeDeadlock()])
        result = ingest_ladder(_client(1, matches), db, seed_allocation={"challenger": 1}, retry_delays=NO_WAIT, run_id="r")
        counts = _counts(db)
    assert attempts == ["M0", "M0", "M0", "M1"]
    assert (result.deadlock_retries, result.matches_with_deadlock_retry, result.deadlocks_recovered) == (2, 1, 1)
    assert counts == (2, 16, 64)


def test_retry_exhaustion_fails_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    matches = _matches(1)
    with Database(tmp_path / "dl3.sqlite3") as db:
        attempts = _inject_units_failures(db, monkeypatch, [FakeDeadlock(), FakeDeadlock(), FakeDeadlock()])
        with pytest.raises(FakeDeadlock) as info:
            ingest_ladder(_client(1, matches), db, seed_allocation={"challenger": 1}, retry_delays=NO_WAIT, run_id="r")
        assert _counts(db) == (0, 0, 0)
        assert _run_status(db, "r") == "failed"
        assert db.seed_last_sampled() == {}
    assert len(attempts) == 3  # original + 2 retries, then give up
    assert any("retries exhausted" in note for note in info.value.__notes__)


def test_non_deadlock_database_error_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    matches = _matches(1)
    with Database(tmp_path / "other.sqlite3") as db:
        attempts = _inject_units_failures(db, monkeypatch, [sqlite3.OperationalError("disk I/O error")])
        with pytest.raises(sqlite3.OperationalError):
            ingest_ladder(_client(1, matches), db, seed_allocation={"challenger": 1}, retry_delays=NO_WAIT, run_id="r")
        assert _counts(db) == (0, 0, 0) and _run_status(db, "r") == "failed"
    assert attempts == ["M0"]


# ---------------------------------------------------------------- run completion + ledger


def test_incomplete_run_does_not_count_for_rotation(tmp_path: Path) -> None:
    with Database(tmp_path / "inc.sqlite3") as db:
        db.start_ingest_run("started-only", 10)
        db.record_seed_samples("started-only", [("a", "master")], 10)
        db.start_ingest_run("failed", 20)
        db.record_seed_samples("failed", [("b", "master")], 20)
        db.mark_ingest_run_failed("failed", "RuntimeError")
        assert db.seed_last_sampled() == {}
        assert db.query_one("SELECT COUNT(*) FROM seed_samples")[0] == 2  # kept as evidence


def test_completed_run_counts_for_rotation(tmp_path: Path) -> None:
    with Database(tmp_path / "done.sqlite3") as db:
        result = ingest_ladder(_client(2, _matches(1)), db, seed_allocation={"challenger": 2}, run_id="ok", now_ms=500)
        assert db.seed_last_sampled() == {"p0": 500, "p1": 500}
        assert _run_status(db, "ok") == "completed"
    assert result.seed_ledger_rows == 2 and result.discovery_rows == 2


def test_seed_samples_without_a_completed_run_are_ignored_not_deleted(tmp_path: Path) -> None:
    """E.g. rows written by a pre-`ingest_runs` build of the ingest, which
    has no run record at all."""
    with Database(tmp_path / "legacy.sqlite3") as db:
        db.record_seed_samples("legacy-run", [("p0", "challenger"), ("p1", "challenger")], 99)
        assert db.seed_last_sampled() == {}
        client = _client(2, {})
        result = ingest_ladder(client, db, seed_allocation={"challenger": 1}, run_id="new", now_ms=100)
        assert result.cohort_reports["challenger"].never_sampled_selected == 1
        assert db.query_one("SELECT COUNT(*) FROM seed_samples WHERE run_id = 'legacy-run'")[0] == 2


def test_finalization_is_atomic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with Database(tmp_path / "atomic.sqlite3") as db:
        def broken(*_a, **_k):
            raise sqlite3.OperationalError("simulated failure while writing provenance")

        monkeypatch.setattr(db, "_insert_match_discoveries", broken)
        with pytest.raises(sqlite3.OperationalError):
            ingest_ladder(_client(2, _matches(1)), db, seed_allocation={"challenger": 2}, run_id="r", now_ms=1)
        # The ledger insert that ran first in the same transaction was rolled back too.
        assert db.query_one("SELECT COUNT(*) FROM seed_samples")[0] == 0
        assert db.seed_last_sampled() == {} and _run_status(db, "r") == "failed"
        assert _counts(db)[0] == 1  # the match itself (its own transaction) stays stored


def test_a_run_id_is_used_once(tmp_path: Path) -> None:
    with Database(tmp_path / "once.sqlite3") as db:
        ingest_ladder(_client(1, {}), db, seed_allocation={"challenger": 1}, run_id="same")
        with pytest.raises(ValueError, match="already exists"):
            ingest_ladder(_client(1, {}), db, seed_allocation={"challenger": 1}, run_id="same")


# ---------------------------------------------------------------- partial run, then resume


def _partial_then_resume(db: Database, monkeypatch: pytest.MonkeyPatch, error: Exception) -> dict:
    matches = _matches(5)
    first = _client(3, matches)
    original = db.ingest_match
    calls = {"n": 0}

    def dies_on_fourth(payload, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise error
        return original(payload, **kwargs)

    monkeypatch.setattr(db, "ingest_match", dies_on_fourth)
    with pytest.raises(type(error)):
        ingest_ladder(first, db, seed_allocation={"challenger": 3}, run_id="run-a", now_ms=1_000, retry_delays=NO_WAIT)
    monkeypatch.setattr(db, "ingest_match", original)
    after_a = {
        "stored": _counts(db)[0],
        "status": _run_status(db, "run-a"),
        "ledger": db.seed_last_sampled(),
        "provenance": db.query_one("SELECT COUNT(*) FROM match_discoveries")[0],
    }

    second = _client(3, matches)
    result = ingest_ladder(second, db, seed_allocation={"challenger": 3}, run_id="run-b", now_ms=2_000, retry_delays=NO_WAIT)
    return {
        "after_a": after_a,
        "seeds_a": first.history_calls,
        "seeds_b": second.history_calls,
        "fetched_b": second.match_calls,
        "result": result,
        "counts": _counts(db),
        "provenance": db.query_all("SELECT match_id, run_id, COUNT(*) FROM match_discoveries GROUP BY match_id, run_id ORDER BY match_id"),
        "ledger": db.seed_last_sampled(),
        "status_b": _run_status(db, "run-b"),
        "status_a": _run_status(db, "run-a"),
    }


def _assert_resumed(out: dict) -> None:
    assert out["after_a"] == {"stored": 3, "status": "failed", "ledger": {}, "provenance": 0}
    assert out["seeds_b"] == out["seeds_a"] == ["p0", "p1", "p2"]  # run A never advanced rotation
    result = out["result"]
    assert result.cohort_reports["challenger"].never_sampled_selected == 3
    assert (result.duplicates_skipped, result.matches_inserted) == (3, 2)
    assert out["fetched_b"] == ["M3", "M4"]  # only the missing matches are fetched
    assert out["counts"] == (5, 40, 160)  # no canonical match duplicated
    # Run B records provenance for already-stored and newly inserted matches alike.
    assert out["provenance"] == [(f"M{i}", "run-b", 3) for i in range(5)]
    assert out["ledger"] == {"p0": 2_000, "p1": 2_000, "p2": 2_000}
    assert (out["status_a"], out["status_b"]) == ("failed", "completed")


def test_partial_run_then_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with Database(tmp_path / "resume.sqlite3") as db:
        out = _partial_then_resume(db, monkeypatch, sqlite3.OperationalError("connection lost"))
    _assert_resumed(out)


# ---------------------------------------------------------------- real (test) Postgres


def _fresh_pg() -> Database:
    db = Database(POSTGRES_TEST_URL)
    for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db


@requires_postgres
def test_postgres_ledger_counts_completed_runs_only() -> None:
    db = _fresh_pg()
    try:
        db.record_seed_samples("pg-legacy", [("p0", "challenger")], 5)
        db.start_ingest_run("pg-started", 6)
        db.record_seed_samples("pg-started", [("p1", "challenger")], 6)
        assert db.seed_last_sampled() == {}
        ingest_ladder(_client(2, _matches(1, "PGL")), db, seed_allocation={"challenger": 2}, run_id="pg-done", now_ms=7)
        ledger = db.seed_last_sampled()
        kept = db.query_one("SELECT COUNT(*) FROM seed_samples")[0]
    finally:
        db.close()
    assert ledger == {"p0": 7, "p1": 7} and kept == 4


@requires_postgres
def test_postgres_partial_run_then_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    db = _fresh_pg()
    try:
        out = _partial_then_resume(db, monkeypatch, psycopg.errors.AdminShutdown("terminating connection"))
    finally:
        db.close()
    _assert_resumed(out)


@requires_postgres
def test_postgres_web_open_is_read_only_and_skips_schema_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import psycopg

    db = _fresh_pg()
    for payload in _matches(2, "PGW").values():
        db.ingest_match(payload)
    db.close()

    with Database.open_existing(POSTGRES_TEST_URL) as ro:
        assert ro.query_one("SELECT COUNT(*) FROM matches")[0] == 2
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("DELETE FROM matches")

    def forbidden(self):
        raise AssertionError("a web request ran schema setup")

    monkeypatch.setattr(Database, "_init_schema", forbidden)
    client = _web(monkeypatch, tmp_path, POSTGRES_TEST_URL)
    for url in WEB_PATHS:
        assert client.get(url).status_code == 200, url


@requires_postgres
def test_postgres_real_deadlock_is_retried_and_the_match_stored_once() -> None:
    """A genuine 40P01: another session holds SHARE on `units` (as a
    CREATE INDEX would) and then asks for SHARE on `participants` while the
    ingest -- holding its participants insert -- waits on `units`. Postgres
    aborts the ingest's transaction; the retry then stores the match once."""
    import psycopg

    db = _fresh_pg()
    matches = _matches(1, "PGD")
    blocker = psycopg.connect(POSTGRES_TEST_URL)
    monitor = psycopg.connect(POSTGRES_TEST_URL, autocommit=True)
    locked = threading.Event()
    errors: list[BaseException] = []

    def other_session() -> None:
        try:
            with blocker.cursor() as cur:
                cur.execute("LOCK TABLE units IN SHARE MODE")
                locked.set()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:  # until the ingest waits on units
                    waiting = monitor.execute(
                        "SELECT COUNT(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                        "WHERE c.relname = 'units' AND NOT l.granted"
                    ).fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.02)
                cur.execute("LOCK TABLE participants IN SHARE MODE")  # closes the cycle
        except BaseException as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)
        finally:
            blocker.rollback()

    thread = threading.Thread(target=other_session)
    thread.start()
    assert locked.wait(10)
    try:
        result = ingest_ladder(_client(1, matches), db, seed_allocation={"challenger": 1}, run_id="pg-deadlock")
        counts = _counts(db)
        status = _run_status(db, "pg-deadlock")
    finally:
        thread.join(20)
        blocker.close()
        monitor.close()
        db.close()
    assert errors == []
    assert (result.deadlock_retries, result.matches_with_deadlock_retry, result.deadlocks_recovered) == (1, 1, 1)
    assert counts == (1, 8, 32) and status == "completed"


# ---------------------------------------------------------------- CLI reporting


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client, args: list[str]):
    from typer.testing import CliRunner

    from tftlab import cli

    class _Ctx:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "RiotClient", _Ctx)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", lambda **_: (None, False, 18))
    monkeypatch.setenv("RIOT_API_KEY", "fake-key")
    monkeypatch.setenv("GITHUB_RUN_ID", "777")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.chdir(tmp_path)
    return CliRunner().invoke(cli.app, ["ingest-riot", *args])


def test_cli_reports_run_status_retries_and_finalized_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _cli(monkeypatch, tmp_path, _client(2, _matches(2)), ["--challenger-seeds", "2"])
    assert result.exit_code == 0, result.output
    for line in (
        "Run id: gh-777-2",
        "Ingest run status: completed",
        "Database deadlock retries: 0 (matches needing a retry: 0, recovered: 0)",
        "Seed ledger rows finalized: 2",
        "Provenance rows finalized: 4 (for 2 stored matches, each stored once)",
    ):
        assert line in result.output


def test_cli_fatal_error_names_the_incomplete_run_and_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(1, _matches(1))

    def boom(match_id):
        raise RuntimeError("simulated crash")

    client.match = boom
    result = _cli(monkeypatch, tmp_path, client, ["--challenger-seeds", "1"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)  # the original exception propagates
    text = " ".join(result.output.split())
    assert "Ingest run gh-777-2 failed (RuntimeError); the run remains incomplete" in text
    assert "seed rotation will ignore it on the next run" in text
    with Database(tmp_path / "cli.sqlite3") as db:
        assert _run_status(db, "gh-777-2") == "failed" and db.seed_last_sampled() == {}
