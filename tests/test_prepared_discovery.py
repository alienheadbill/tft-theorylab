"""Prepared Discovery analytics: equivalence with live computation, the
freshness/staleness rules, atomic publishing and failure handling, on SQLite
and (with TFTLAB_TEST_DATABASE_URL) Postgres."""

from __future__ import annotations

import json
import os
import random
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tftlab.prepared_discovery as prepared
from tftlab.analytics import available_balance_windows, discovery_population
from tftlab.prepared_discovery import (
    PreparedLookup,
    PreparedSourceChanged,
    lookup_prepared,
    prepare_window,
)
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
V_A = "Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>"
V_B = "Version 14.7.580.4321 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
T0 = 1_790_000_000_000

_INTENT = json.loads((Path(__file__).parents[1] / "src/tftlab/data/item_intent.json").read_text())["items"]
DAMAGE = sorted(k for k, v in _INTENT.items() if v.get("intent") == "damage" and k.startswith("DA_") and "Emblem" not in k)
TANK = sorted(k for k, v in _INTENT.items() if v.get("intent") == "tank" and k.startswith("DA_"))
TRAITS = [f"TFT14_Trait{i}" for i in range(10)]
CHAMPS = [(f"TFT14_C{i:02d}", 1 + i % 5) for i in range(30)]


def _board(rng: random.Random) -> tuple[float, list, list]:
    cid, cost = rng.choice(CHAMPS[:18]) if rng.random() < 0.85 else rng.choice(CHAMPS)
    committed = rng.random() < 0.75
    items = rng.sample(DAMAGE[:12], rng.choice([2, 3])) if committed else [rng.choice(DAMAGE)]
    hit = committed and rng.random() < (0.4 if cost <= 3 else 0.1)
    units = [{"character_id": cid, "rarity": cost - 1, "tier": 3 if hit else 2, "itemNames": items}]
    if rng.random() < 0.05:  # a duplicate instance of the carry on the same board
        units.append({"character_id": cid, "rarity": cost - 1, "tier": 2, "itemNames": rng.sample(DAMAGE[:12], 2)})
    for i, (p, pc) in enumerate(rng.sample([c for c in CHAMPS if c[0] != cid], 6)):
        units.append({"character_id": p, "rarity": pc - 1, "tier": 2, "itemNames": rng.sample(TANK, 2) if i == 0 else []})
    traits = [
        {"name": t, "num_units": n, "style": 1 + (n >= 4), "tier_current": 1 + (n >= 4), "tier_total": 3}
        for t, n in ((t, rng.choice([2, 3, 4, 5, 6])) for t in rng.sample(TRAITS, rng.randint(2, 4)))
    ]
    edge = (0.2 if hit else 0.0) + (0.1 if items[0] in DAMAGE[:3] else 0.0)
    return rng.random() + edge, units, traits


def _match(match_id: str, version: str, when: int, rng: random.Random) -> dict:
    boards = sorted((_board(rng) for _ in range(8)), key=lambda b: -b[0])
    return {"metadata": {"match_id": match_id}, "info": {
        "game_version": version, "tft_game_type": "standard", "queue_id": 1100, "tft_set_number": 14,
        "tft_set_core_name": "TFTSet14", "game_datetime": when,
        "participants": [{"placement": r + 1, "level": 8, "augments": [], "units": u, "traits": t}
                         for r, (_, u, t) in enumerate(boards)]}}


def _seed(db: Database, per_window: int = 70) -> None:
    rng = random.Random(4242)
    for i in range(per_window):
        db.ingest_match(_match(f"A_{i:04d}", V_A, T0 + i * 60_000, rng))
        db.ingest_match(_match(f"B_{i:04d}", V_B, T0 + 86_400_000 * 20 + i * 60_000, rng))


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def target(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        path = str(tmp_path / "prepared.sqlite3")
        with Database(path) as db:
            _seed(db)
        return path
    with Database(POSTGRES_TEST_URL) as db:
        for table in ("discovery_prepared_candidates", "discovery_prepared_runs", "traits", "units",
                      "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        _seed(db)
    return POSTGRES_TEST_URL


@pytest.fixture()
def client(target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The production-style web app: DATABASE_URL set, opened read-only."""
    import tftlab.webapp as webapp

    monkeypatch.setenv("DATABASE_URL", target)
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "unused-demo.sqlite3"))
    webapp._POPULATION_CACHE.clear()
    return TestClient(webapp.create_app())


def _windows(target: str) -> list[str]:
    with Database(target) as db:
        return [w for w, _n, _t in available_balance_windows(db)]


def _prepare(target: str, *windows: str, force: bool = False) -> list:
    with Database(target) as db:
        return [prepare_window(db, w, force=force) for w in (windows or _windows(target))]


def _live(client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, params: dict) -> dict:
    """The same request answered by the live computation (prepared runs ignored)."""
    import tftlab.webapp as webapp

    with monkeypatch.context() as m:
        m.setattr(webapp, "lookup_prepared", lambda db, window: PreparedLookup("missing"))
        body = client.get(path, params=params).json()
    body.pop("prepared")
    return body


def _get(client: TestClient, path: str, params: dict, expect: str) -> dict:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.pop("prepared")["status"] == expect
    return body


FILTERS = [
    {"costs": "1,2,3", "min_samples": 10, "top_n": 5, "limit": 200},  # the production page request
    {"costs": "4", "min_samples": 1, "top_n": 5, "limit": 200},
    {"costs": "5", "min_samples": 1, "top_n": 5, "limit": 200},
    {"costs": "1,3,5", "min_samples": 1, "top_n": 20, "limit": 200},
    {"costs": "3,1", "min_samples": 3, "top_n": 2, "limit": 200},
    {"max_cost": 2, "min_samples": 1, "top_n": 5, "limit": 200},  # costs omitted: max_cost range
    {"min_samples": 10},  # every API default
    {"costs": "1,2,3,4,5", "min_samples": 1, "top_n": 1, "limit": 3},
    {"costs": "1,2,3,4,5", "min_samples": 30, "top_n": 8, "limit": 200},
    {"costs": "2", "min_samples": 100000, "top_n": 5, "limit": 200},  # filters match nothing
]


# ---------------------------------------------------------------- equivalence


def test_prepared_responses_equal_live_computation_for_every_filter(
    target: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = _windows(target)
    assert len(windows) == 2
    _prepare(target)
    seen_costs: set[int] = set()
    for window in [None, *windows]:
        for params in FILTERS:
            params = {**params, **({"balance_window": window} if window else {})}
            expected = _live(client, monkeypatch, "/api/discovery", params)
            got = _get(client, "/api/discovery", params, "current")
            assert got == expected, params
            seen_costs |= {c["cost"] for c in got["candidates"]}
    assert seen_costs == {1, 2, 3, 4, 5}


def test_prepared_detail_equals_live_for_every_carry(
    target: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(target)
    for window in _windows(target):
        with Database(target) as db:
            ids = [s.character_id for s in discovery_population(db, window)]
        for cid in ids:
            for top_n in (1, 8, 20):
                params = {"balance_window": window, "top_n": top_n}
                expected = _live(client, monkeypatch, f"/api/discovery/{cid}", params)
                assert _get(client, f"/api/discovery/{cid}", params, "current") == expected
        assert client.get("/api/discovery/TFT14_Nobody", params={"balance_window": window}).status_code == 404


def test_exact_costs_limit_top_n_and_ordering(target: str, client: TestClient) -> None:
    _prepare(target)
    base = {"min_samples": 1, "limit": 200}
    four = _get(client, "/api/discovery", {**base, "costs": "4"}, "current")
    assert four["costs"] == [4] and {c["cost"] for c in four["candidates"]} == {4}
    odd = _get(client, "/api/discovery", {**base, "costs": "3,1"}, "current")
    assert odd["costs"] == [1, 3] and odd == _get(client, "/api/discovery", {**base, "costs": "1,3"}, "current")
    ranged = _get(client, "/api/discovery", {**base, "max_cost": 2}, "current")
    assert ranged["costs"] == [1, 2] and {c["cost"] for c in ranged["candidates"]} <= {1, 2}
    scores = [c["opportunity_score"] for c in odd["candidates"]]
    assert scores == sorted(scores, reverse=True)
    top3 = _get(client, "/api/discovery", {**base, "costs": "1,3", "limit": 3}, "current")
    assert top3["candidates"] == odd["candidates"][:3]
    one = _get(client, "/api/discovery", {**base, "costs": "1,3", "top_n": 1}, "current")
    for c in one["candidates"]:
        assert all(len(c[k]) <= 1 for k in ("best_partners", "best_item_packages", "best_trait_breakpoints"))
    strict = _get(client, "/api/discovery", {**base, "costs": "1,2,3,4,5", "min_samples": 25}, "current")
    assert all(c["commitment_games"] >= 25 for c in strict["candidates"])


def test_window_carries_keeps_its_meaning(target: str, client: TestClient) -> None:
    _prepare(target)
    for window in _windows(target):
        with Database(target) as db:
            population = len(discovery_population(db, window))
        none_match = _get(client, "/api/discovery",
                          {"balance_window": window, "costs": "2", "min_samples": 100000}, "current")
        # All carries of any cost before the cost/min filters, even when nothing passes them.
        assert none_match["window_carries"] == population > 0 and none_match["candidates"] == []
    unknown = _get(client, "/api/discovery", {"balance_window": "99.9"}, "missing")
    assert unknown["window_carries"] == 0 and unknown["candidates"] == []


# ---------------------------------------------------------------- windows and freshness


def _add_match(target: str, match_id: str, version: str, when: int) -> None:
    with Database(target) as db:
        db.ingest_match(_match(match_id, version, when, random.Random(match_id)))


def test_balance_windows_never_leak(target: str, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    window_a, window_b = sorted(_windows(target))
    _prepare(target, window_a)
    a = _get(client, "/api/discovery", {"balance_window": window_a, "costs": "1,2,3,4,5", "min_samples": 1}, "current")
    assert {c["balance_window"] for c in a["candidates"]} == {window_a}
    # B was never prepared: it is computed live, never answered from A's run.
    b = _get(client, "/api/discovery", {"balance_window": window_b, "costs": "1,2,3,4,5", "min_samples": 1}, "missing")
    assert {c["balance_window"] for c in b["candidates"]} == {window_b}

    _prepare(target, window_b)
    # A new match in B makes only B stale.
    _add_match(target, "B_NEW", V_B, T0 + 86_400_000 * 21)
    with Database(target) as db:
        assert lookup_prepared(db, window_a).status == "current"
        assert lookup_prepared(db, window_b).status == "stale"


def test_new_match_makes_the_prepared_run_stale_until_prepared_again(
    target: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    window_a = sorted(_windows(target))[0]
    params = {"balance_window": window_a, "costs": "1,2,3,4,5", "min_samples": 1}
    _prepare(target, window_a)
    before = _get(client, "/api/discovery", params, "current")

    _add_match(target, "A_NEW", V_A, T0 + 3_600_000 * 24)
    stale = client.get("/api/discovery", params=params).json()
    assert stale["prepared"]["status"] == "stale"  # never presented as current
    stale.pop("prepared")
    assert stale == _live(client, monkeypatch, "/api/discovery", params)  # served live, includes the new match
    assert sum(c["commitment_games"] for c in stale["candidates"]) >= sum(
        c["commitment_games"] for c in before["candidates"])

    result = _prepare(target, window_a)[0]
    assert result.status == "published" and result.source_matches == 71
    assert _get(client, "/api/discovery", params, "current") == stale


def test_a_data_migration_or_new_analytics_code_makes_the_run_stale(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    window_a = sorted(_windows(target))[0]
    _prepare(target, window_a)
    with Database(target) as db:
        assert lookup_prepared(db, window_a).status == "current"
        db.execute("INSERT INTO schema_migrations (migration_key, applied_at, rows_changed) VALUES (?, ?, ?)",
                   ("test:in-place-recount", 1_900_000_000_000, 3))
        db.commit()
        assert lookup_prepared(db, window_a).status == "stale"
        db.execute("DELETE FROM schema_migrations WHERE migration_key = ?", ("test:in-place-recount",))
        db.commit()
        assert lookup_prepared(db, window_a).status == "current"
        monkeypatch.setattr(prepared, "ANALYTICS_VERSION", "v1-somethingnewer")
        assert lookup_prepared(db, window_a).status == "stale"


def test_analytics_version_tracks_the_source_files(tmp_path: Path) -> None:
    one, two = tmp_path / "a.py", tmp_path / "b.json"
    one.write_text("x = 1\n")
    two.write_text("{}")
    first = prepared.analytics_version([one, two])
    assert prepared.analytics_version([one, two]) == first
    one.write_text("x = 2\n")
    assert prepared.analytics_version([one, two]) != first
    assert prepared.ANALYTICS_VERSION == prepared.analytics_version()
    assert all(p.exists() for p in prepared.ANALYTICS_SOURCES)


# ---------------------------------------------------------------- publishing and failures


def _runs(target: str, window: str) -> list[tuple]:
    with Database(target) as db:
        return db.query_all(
            "SELECT run_id, status FROM discovery_prepared_runs WHERE balance_window = ? ORDER BY started_at, run_id",
            (window,),
        )


def _candidate_runs(target: str) -> set[str]:
    with Database(target) as db:
        return {r[0] for r in db.query_all("SELECT DISTINCT run_id FROM discovery_prepared_candidates")}


def test_failed_publish_never_replaces_the_last_published_run(
    target: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    window_a = sorted(_windows(target))[0]
    params = {"balance_window": window_a, "costs": "1,2,3,4,5", "min_samples": 1}
    good = _prepare(target, window_a)[0]
    served = _get(client, "/api/discovery", params, "current")

    original = Database.executemany

    def explode(self, sql, rows):
        if "discovery_prepared_candidates" in sql:
            original(self, sql, list(rows)[:5])  # part of the rows are written...
            raise RuntimeError("connection lost mid-publish")  # ...then the publish dies
        return original(self, sql, rows)

    with monkeypatch.context() as m:
        m.setattr(Database, "executemany", explode)
        with pytest.raises(RuntimeError):
            _prepare(target, window_a, force=True)

    runs = _runs(target, window_a)
    assert [s for _r, s in runs] == ["published", "failed"]
    assert runs[0][0] == good.run_id and _candidate_runs(target) == {good.run_id}  # no partial rows survive
    assert _get(client, "/api/discovery", params, "current") == served


def test_failed_computation_keeps_the_previous_run(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    window_a = sorted(_windows(target))[0]
    good = _prepare(target, window_a)[0]

    def broken(*_a, **_k):
        raise ZeroDivisionError("analytics bug")

    with monkeypatch.context() as m:
        m.setattr(prepared, "population_candidates", broken)
        with pytest.raises(ZeroDivisionError):
            _prepare(target, window_a, force=True)
    with Database(target) as db:
        lookup = lookup_prepared(db, window_a)
        failure = db.query_one("SELECT failure FROM discovery_prepared_runs WHERE status = 'failed'")
    assert lookup.status == "current" and lookup.run.run_id == good.run_id
    assert failure == ("ZeroDivisionError",)  # the type only, never the message


def test_source_changing_during_preparation_is_never_published(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    window_a = sorted(_windows(target))[0]
    original = prepared._compute
    calls = {"n": 0}

    def compute_while_ingesting(db, window):
        out = original(db, window)
        db.commit()  # a concurrent ingest lands after the reads, as it would from another process
        calls["n"] += 1
        _add_match(target, f"A_DURING_{calls['n']}", V_A, T0 + 3_600_000 * (30 + calls["n"]))
        return out

    with monkeypatch.context() as m:
        m.setattr(prepared, "_compute", compute_while_ingesting)
        with pytest.raises(PreparedSourceChanged):
            _prepare(target, window_a)
    assert calls["n"] == prepared.MAX_ATTEMPTS
    assert [s for _r, s in _runs(target, window_a)] == ["failed"]

    # One change, then quiet: the retry publishes a run of the final data.
    calls["n"] = prepared.MAX_ATTEMPTS + 10

    def once(db, window):
        out = original(db, window)
        db.commit()
        if calls["n"] == prepared.MAX_ATTEMPTS + 10:
            calls["n"] += 1
            _add_match(target, "A_DURING_ONCE", V_A, T0 + 3_600_000 * 50)
        return out

    with monkeypatch.context() as m:
        m.setattr(prepared, "_compute", once)
        result = _prepare(target, window_a)[0]
    with Database(target) as db:
        assert result.status == "published" and lookup_prepared(db, window_a).status == "current"
        assert result.source_matches == db.query_one(
            "SELECT COUNT(*) FROM matches WHERE balance_window = ?", (window_a,))[0]


def test_current_runs_are_skipped_and_old_runs_pruned(target: str) -> None:
    window_a = sorted(_windows(target))[0]
    first = _prepare(target, window_a)[0]
    assert first.status == "published"
    again = _prepare(target, window_a)[0]
    assert again.status == "skipped" and again.run_id == first.run_id
    for _ in range(prepared.KEEP_PUBLISHED_RUNS + 2):
        _prepare(target, window_a, force=True)
    runs = _runs(target, window_a)
    assert len(runs) == prepared.KEEP_PUBLISHED_RUNS
    assert _candidate_runs(target) == {r for r, _s in runs}


def test_concurrent_preparations_publish_one_complete_run(target: str) -> None:
    window_a = sorted(_windows(target))[0]
    results, errors = [], []

    def run() -> None:
        try:
            with Database(target) as db:
                results.append(prepare_window(db, window_a))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if target.startswith("postgres") or not errors:
        assert errors == []
    with Database(target) as db:
        lookup = lookup_prepared(db, window_a)
        complete = db.query_one(
            "SELECT COUNT(*) FROM discovery_prepared_candidates WHERE run_id = ?", (lookup.run.run_id,))[0]
    assert lookup.status == "current"
    assert complete == lookup.run.window_carries  # the served run is whole
    if target.startswith("postgres"):
        # The advisory lock serializes them; the later ones see a current run and skip.
        assert sorted(r.status for r in results) == ["published", "skipped", "skipped"]


# ---------------------------------------------------------------- read-only web path


def test_prepared_request_runs_no_aggregation(target: str, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.analytics.discovery as discovery
    import tftlab.webapp as webapp

    _prepare(target)
    statements: list[str] = []
    original_all, original_one = Database.query_all, Database.query_one

    def spy_all(self, sql, params=()):
        statements.append(sql)
        return original_all(self, sql, params)

    def spy_one(self, sql, params=()):
        statements.append(sql)
        return original_one(self, sql, params)

    def forbidden(*_a, **_k):
        raise AssertionError("request-time Discovery aggregation on the prepared path")

    monkeypatch.setattr(Database, "query_all", spy_all)
    monkeypatch.setattr(Database, "query_one", spy_one)
    for module, name in ((webapp, "discovery_population"), (webapp, "discover_candidates"),
                         (webapp, "discovery_candidate_for"), (discovery, "_evidence_for")):
        monkeypatch.setattr(module, name, forbidden)
    body = _get(client, "/api/discovery", {"costs": "1,2,3", "min_samples": 10, "top_n": 5, "limit": 200}, "current")
    assert body["candidates"]
    # The schema probes `Database.open_existing` runs on connect (WHERE 1 = 0)
    # read no rows; everything else must avoid the match tables' joins.
    request = [" ".join(q.split()).lower() for q in statements if "where 1 = 0" not in q.lower()]
    for absent in ("from units", "from participants", "from traits", "join"):
        assert not any(absent in q for q in request), request
    assert len(request) <= 8, request


def test_database_without_prepared_tables_still_serves_discovery_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A production database initialized before this feature: the read-only
    web app must not 503 or create anything; it computes Discovery live."""
    import sqlite3

    import tftlab.webapp as webapp

    path = tmp_path / "older.sqlite3"
    with Database(path) as db:
        _seed(db, per_window=15)
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE discovery_prepared_candidates; DROP TABLE discovery_prepared_runs;")
    conn.close()
    monkeypatch.setenv("DATABASE_URL", str(path))
    webapp._POPULATION_CACHE.clear()
    client = TestClient(webapp.create_app())
    body = _get(client, "/api/discovery", {"costs": "1,2,3,4,5", "min_samples": 1}, "missing")
    assert body["candidates"] and body["window_carries"] > 0
    tables = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "discovery_prepared_runs" not in tables


def test_preparation_refuses_a_read_only_connection(target: str) -> None:
    window_a = sorted(_windows(target))[0]
    with Database.open_existing(target) as db:
        with pytest.raises(RuntimeError, match="writable"):
            prepare_window(db, window_a)


def test_cli_prepares_every_window_and_reports_failures(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from tftlab import cli

    runner = CliRunner()
    first = runner.invoke(cli.app, ["prepare-discovery", "--db", target])
    assert first.exit_code == 0, first.output
    assert first.output.count("published") == 2
    assert runner.invoke(cli.app, ["prepare-discovery", "--db", target]).output.count("already current") == 2

    def broken(*_a, **_k):
        raise ZeroDivisionError("analytics bug")

    monkeypatch.setattr(prepared, "population_candidates", broken)
    failed = runner.invoke(cli.app, ["prepare-discovery", "--db", target, "--force"])
    assert failed.exit_code == 1 and "FAILED" in failed.output and "previous run kept" in failed.output
