"""The SELECT-only production diagnostics workflow and its script.

Static checks always run. The Postgres checks run the real script against a
disposable test database (TFTLAB_TEST_DATABASE_URL), never production.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "read-only-data-diagnostics.yml"
SCRIPT = REPO / "scripts" / "diagnostics" / "participants_without_units.py"

_spec = importlib.util.spec_from_file_location("participants_without_units", SCRIPT)
diag = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = diag  # dataclasses look their module up here
_spec.loader.exec_module(diag)

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")

FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "CALL")


def _workflow() -> str:
    return WORKFLOW.read_text()


def _workflow_code() -> str:
    return "\n".join(line for line in _workflow().splitlines() if not line.lstrip().startswith("#"))


# ---------------------------------------------------------------- workflow


def test_only_manually_triggered_without_inputs() -> None:
    text = _workflow()
    assert "workflow_dispatch: {}" in text  # no inputs at all, so no SQL can be passed in
    assert "inputs:" not in text
    for trigger in ("\npush:", "\n  push:", "pull_request", "schedule:"):
        assert trigger not in text


def test_main_only_and_database_url_validated_before_checkout() -> None:
    text = _workflow()
    preflight = text[text.index("Validate production configuration") : text.index("actions/checkout")]
    assert 'if [ "${GITHUB_REF}" != "refs/heads/main" ]' in preflight
    assert '-z "${DATABASE_URL}"' in preflight
    assert "postgres://*|postgresql://*" in preflight
    assert preflight.count("exit 1") >= 3
    assert text.index("Validate production configuration") < text.index("Participants without stored units")


def test_no_riot_key_and_read_only_permissions() -> None:
    text = _workflow()
    assert "RIOT_API_KEY" not in text
    assert "permissions:\n  contents: read" in text
    assert "contents: write" not in text
    assert "timeout-minutes:" in text


def test_database_url_is_never_echoed() -> None:
    for line in _workflow().splitlines():
        if "echo" in line or "printf" in line:
            assert "$DATABASE_URL" not in line and "${DATABASE_URL}" not in line, line


def test_runs_only_the_fixed_script_and_never_the_tftlab_package() -> None:
    code = _workflow_code()
    assert "python scripts/diagnostics/participants_without_units.py" in code
    assert "tftlab" not in code  # no CLI, no Database, no ingest, no patch diagnostics
    assert 'pip install "psycopg[binary]>=3.1,<4"' in code
    assert "pip install -e" not in code


@pytest.mark.parametrize("keyword", FORBIDDEN)
def test_workflow_has_no_write_statements(keyword: str) -> None:
    assert not re.search(rf"\b{keyword}\b", _workflow_code(), re.IGNORECASE)


# ---------------------------------------------------------------- script


def test_script_never_imports_tftlab_or_its_database() -> None:
    source = SCRIPT.read_text()
    assert not re.search(r"^\s*(from|import)\s+tftlab", source, re.MULTILINE)
    assert "Database(" not in source.replace("`Database(...)`", "")


def test_script_enters_read_only_transaction_and_ends_with_rollback() -> None:
    source = SCRIPT.read_text()
    assert 'cur.execute("BEGIN TRANSACTION READ ONLY")' in source
    assert '"SHOW transaction_read_only"' in source
    assert 'cur.execute("ROLLBACK")' in source
    assert "default_transaction_read_only=on" in source
    # ROLLBACK runs in `finally`, so it happens even when a query fails.
    assert source.index("finally:") < source.index('cur.execute("ROLLBACK")')


@pytest.mark.parametrize("keyword", FORBIDDEN)
def test_queries_are_select_only(keyword: str) -> None:
    for sql in diag.QUERIES:
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(rf"\b{keyword}\b", sql, re.IGNORECASE)


def test_queries_never_select_player_identity() -> None:
    for sql in diag.QUERIES:
        lowered = sql.lower()
        for field in ("puuid", "riotid", "gamename", "tagline", "summoner", "payload_json\n", "t.rp\n"):
            assert field not in lowered
        # The raw participant object itself is never selected whole.
        assert not re.search(r"\bt\.rp\s*(,|AS|$)", sql, re.IGNORECASE | re.MULTILINE)


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return []


@pytest.mark.parametrize(
    "sql",
    ["INSERT INTO matches VALUES (1)", "DELETE FROM units", "SELECT 1; DROP TABLE units", "UPDATE x SET y=1", "COMMIT"],
)
def test_statement_guard_refuses_writes(sql: str) -> None:
    cur = _FakeCursor()
    with pytest.raises(diag.NotReadOnly):
        diag._read(cur, sql)
    assert cur.executed == []


def test_main_refuses_a_missing_or_non_postgres_url(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def no_connect(url):
        raise AssertionError("must not connect")

    monkeypatch.setattr(diag, "connect", no_connect)
    for value in ("", "sqlite:///x.db", "mysql://user:secret@host/db"):
        monkeypatch.setenv("DATABASE_URL", value)
        assert diag.main() == 1
    assert "secret" not in capsys.readouterr().err


def _raw(index: int, *, units_type="array", raw_units=0, stored_units=0, has_key=True, placement=8, stored_placement=8):
    return diag.RawParticipant(
        "M1", index, str(placement), has_key, units_type, raw_units, stored_units, stored_placement,
        "3", None, None, None, None, None, 0,
    )


def _healthy_others() -> list:
    return [_raw(i, raw_units=7, stored_units=7, placement=i, stored_placement=i) for i in range(1, 8)]


@pytest.mark.parametrize(
    ("anomalous", "expected"),
    [
        (_raw(0, raw_units=0), diag.CASE_A),                                  # empty list
        (_raw(0, has_key=False, units_type=None, raw_units=None), diag.CASE_A),  # key missing
        (_raw(0, raw_units=5), diag.CASE_B),                                  # units lost
        (_raw(0, units_type="null", raw_units=None), diag.CASE_C),            # JSON null: ambiguous
        (_raw(0, stored_placement=3), diag.CASE_C),                           # doesn't line up
    ],
)
def test_classification(anomalous, expected) -> None:
    rows = [anomalous, *_healthy_others()]
    assert diag.classify([("M1", 0)], rows, {"M1": 8}) == expected


def test_classification_flags_broader_mismatch_and_count_mismatch() -> None:
    rows = [_raw(0), *_healthy_others()]
    rows[3] = _raw(3, raw_units=7, stored_units=6, placement=3, stored_placement=3)
    assert diag.classify([("M1", 0)], rows, {"M1": 8}) == diag.CASE_B
    assert diag.classify([("M1", 0)], [_raw(0), *_healthy_others()], {"M1": 9}) == diag.CASE_C
    assert diag.classify([("M1", 5)], [_raw(0)], {"M1": 1}) == diag.CASE_C  # no raw row at that index
    assert diag.classify([], [], {}) == diag.NO_ANOMALY


# ---------------------------------------------------------------- real (test) Postgres


def _prepare_test_db(mutate) -> None:
    """Build the fixture in the disposable TEST database with the normal
    tftlab code path -- setup only; the diagnostic itself never uses it."""
    from tftlab.demo import generate_demo_matches
    from tftlab.storage import Database

    with Database(POSTGRES_TEST_URL) as db:
        for table in ("traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        matches = generate_demo_matches(3, seed=5)
        mutate(db, matches)


def _run_diag() -> tuple[str, str]:
    out = io.StringIO()
    with diag.connect(POSTGRES_TEST_URL) as conn:
        verdict = diag.run(conn, out=out)
    return verdict, out.getvalue()


def _counts() -> tuple:
    from tftlab.storage import Database

    with Database(POSTGRES_TEST_URL) as db:
        return tuple(db.query_one(f"SELECT COUNT(*) FROM {t}")[0] for t in ("matches", "participants", "units", "traits"))


@requires_postgres
def test_postgres_source_empty_board_is_case_a_and_nothing_is_written() -> None:
    def mutate(db, matches):
        emptied = copy.deepcopy(matches[1])
        emptied["info"]["participants"][4]["units"] = []
        for m in (matches[0], emptied, matches[2]):
            db.ingest_match(m)

    _prepare_test_db(mutate)
    before = _counts()
    verdict, output = _run_diag()
    assert verdict == diag.CASE_A
    assert "Participants without stored units: 1" in output
    assert "raw participant count: 8" in output and "stored participant count: 8" in output
    assert "puuid" not in output.lower()
    assert _counts() == before


@requires_postgres
def test_postgres_lost_units_are_case_b() -> None:
    def mutate(db, matches):
        for m in matches:
            db.ingest_match(m)
        # Simulate a storage bug in the TEST database only.
        db.execute("DELETE FROM units WHERE match_id = ? AND participant_index = 2", (matches[0]["metadata"]["match_id"],))
        db.commit()

    _prepare_test_db(mutate)
    verdict, output = _run_diag()
    assert verdict == diag.CASE_B


@requires_postgres
def test_postgres_healthy_store_reports_no_anomaly() -> None:
    _prepare_test_db(lambda db, matches: [db.ingest_match(m) for m in matches])
    verdict, _ = _run_diag()
    assert verdict == diag.NO_ANOMALY


@requires_postgres
def test_postgres_session_really_rejects_writes() -> None:
    """The server, not just the script, refuses writes on this connection."""
    import psycopg

    _prepare_test_db(lambda db, matches: [db.ingest_match(m) for m in matches])
    before = _counts()
    with diag.connect(POSTGRES_TEST_URL) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            cur.execute("DELETE FROM units")  # default_transaction_read_only=on
        cur.execute("BEGIN TRANSACTION READ ONLY")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            cur.execute("UPDATE participants SET level = 0")
        cur.execute("ROLLBACK")
    assert _counts() == before
