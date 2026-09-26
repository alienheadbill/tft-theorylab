"""Maximum collection mode: ladder and history exhaustion with guards,
breadth-first ordering, dedupe, budgets, resumability and the CLI surface.
Mocked Riot only -- no network, no key."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import tftlab.cli as cli
from tftlab.collection import (
    DUPLICATE_ONLY_PAGES,
    EMPTY_PAGE,
    HISTORY_PAGE_SIZE,
    PAGE_CAP,
    REPEATED_PAGE,
    STOP_COMPLETE,
    STOP_DURATION,
    STOP_MATCH_FETCHES,
    STOP_REQUESTS,
    CollectionBudgets,
    breadth_first_queue,
    collect_maximum,
    collection_plan,
    enumerate_division,
    spread_order,
)
from tftlab.riot import RiotApiError, RiotClient
from tftlab.riot_limits import RateWindow
from tftlab.sampling import COHORTS
from tftlab.storage import Database

from _helpers import make_match, make_unit
from conftest import FakeClock

START_S = 1_790_233_200
BIG = CollectionBudgets(max_duration_s=10_000, max_requests=100_000, max_match_fetches=100_000)


def _m(match_id: str, **kwargs) -> dict:
    return make_match(match_id, units=[make_unit("TFT14_Foo", tier=2, items=[])], game_datetime=START_S * 1000 + 1, **kwargs)


class MaxStub:
    """Duck-typed RiotClient for maximum mode: apex lists, paginated
    divisional pages, and paginated (start/count) histories."""

    def __init__(self, *, apex=None, divisions=None, histories=None, matches=None, failing_histories=(),
                 failing_matches=(), clock: FakeClock | None = None, request_cost_s: float = 0.0):
        self.apex = apex or {}
        self.divisions = divisions or {}
        self.histories = histories or {}
        self.matches = matches or {}
        self.failing_histories = set(failing_histories)
        self.failing_matches = set(failing_matches)
        self.clock = clock
        self.request_cost_s = request_cost_s
        self.calls: list[tuple] = []

    def _tick(self):
        if self.clock is not None:
            self.clock.now += self.request_cost_s

    def _apex(self, tier):
        self._tick()
        self.calls.append(("apex", tier))
        return {"entries": list(self.apex.get(tier, []))}

    def challenger(self):
        return self._apex("challenger")

    def grandmaster(self):
        return self._apex("grandmaster")

    def master(self):
        return self._apex("master")

    def league_entries(self, tier, division, *, page=1, queue="RANKED_TFT"):
        self._tick()
        self.calls.append(("league", tier, division, page))
        source = self.divisions.get((tier, division), [])
        if callable(source):
            return source(page)
        return list(source[page - 1]) if page <= len(source) else []

    def match_ids(self, puuid, *, count=20, start=0, start_time=None, end_time=None):
        self._tick()
        self.calls.append(("history", puuid, start, count, start_time, end_time))
        if puuid in self.failing_histories:
            raise RiotApiError(f"Riot API returned 503 for fake/{puuid}")
        source = self.histories.get(puuid, [])
        if callable(source):
            return source(start, count)
        return list(source[start : start + count])

    def match(self, match_id):
        self._tick()
        self.calls.append(("match", match_id))
        if match_id in self.failing_matches:
            raise RiotApiError(f"Riot API returned 500 for fake/{match_id}")
        return self.matches[match_id]

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def _apex(prefix, n, top_lp=1000):
    return [{"puuid": f"{prefix}{i}", "leaguePoints": top_lp - i} for i in range(n)]


def _pages(prefix, tier, division, sizes):
    pages, k = [], 0
    for size in sizes:
        pages.append([{"puuid": f"{prefix}{division}-{k + i}", "rank": division, "tier": tier, "leaguePoints": 50}
                      for i in range(size)])
        k += size
    return pages


def _collect(client, db, **kwargs):
    kwargs.setdefault("budgets", BIG)
    kwargs.setdefault("history_start_time", START_S)
    kwargs.setdefault("run_id", "max")
    kwargs.setdefault("now_ms", iter(range(10_000, 10_000_000, 1000)).__next__)
    return collect_maximum(client, db, **kwargs)


# ---------------------------------------------------------------- ladder enumeration


def test_enumerates_all_five_cohorts_and_nothing_else(tmp_path: Path) -> None:
    divisions = {("DIAMOND", d): _pages("d", "DIAMOND", d, [3, 2]) for d in ("I", "II", "III", "IV")}
    divisions.update({("PLATINUM", d): _pages("p", "PLATINUM", d, [1]) for d in ("I", "II", "III", "IV")})
    client = MaxStub(apex={"challenger": _apex("c", 2), "grandmaster": _apex("g", 2), "master": _apex("m", 2)},
                     divisions=divisions)
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db)
    assert list(result.cohorts) == list(COHORTS)
    assert {c[1] for c in client.of("apex")} == {"challenger", "grandmaster", "master"}
    assert {c[1] for c in client.of("league")} == {"DIAMOND", "PLATINUM"}  # no Emerald or lower
    assert result.cohorts["diamond"].candidates == 20 and result.cohorts["platinum"].candidates == 4
    assert result.cohorts["diamond"].ladder_complete is True
    assert all(d.stop_reason == EMPTY_PAGE and d.pages == 3 for d in result.cohorts["diamond"].divisions)
    assert result.ladder_requests == 3 + 4 * 3 + 4 * 2
    assert result.candidates == 30


def test_division_paginates_past_short_pages_until_an_empty_page() -> None:
    """Page size is undocumented: a short page is not treated as the last."""
    client = MaxStub(divisions={("DIAMOND", "I"): _pages("d", "DIAMOND", "I", [5, 2, 5])})
    entries, report = enumerate_division(client, "DIAMOND", "I", max_pages=50)
    assert len(entries) == 12 and report.pages == 4 and report.stop_reason == EMPTY_PAGE and report.complete


def test_division_guard_repeated_page() -> None:
    page = [{"puuid": "x1"}, {"puuid": "x2"}]
    client = MaxStub(divisions={("DIAMOND", "I"): lambda n: page})
    entries, report = enumerate_division(client, "DIAMOND", "I", max_pages=50)
    assert report.stop_reason == REPEATED_PAGE and report.pages == 2 and len(entries) == 2 and not report.complete


def test_division_guard_duplicate_only_pages() -> None:
    """Pages that differ from each other but bring no new PUUID."""
    pages = {1: [{"puuid": "a"}, {"puuid": "b"}], 2: [{"puuid": "a"}], 3: [{"puuid": "b"}], 4: [{"puuid": "zz"}]}
    client = MaxStub(divisions={("DIAMOND", "I"): lambda n: pages.get(n, [])})
    entries, report = enumerate_division(client, "DIAMOND", "I", max_pages=50)
    assert report.stop_reason == DUPLICATE_ONLY_PAGES and report.pages == 3 and not report.complete


def test_division_guard_non_terminating_pagination_hits_the_cap() -> None:
    client = MaxStub(divisions={("DIAMOND", "I"): lambda n: [{"puuid": f"new{n}"}]})
    entries, report = enumerate_division(client, "DIAMOND", "I", max_pages=7)
    assert report.stop_reason == PAGE_CAP and report.pages == 7 and len(entries) == 7 and not report.complete


def test_cross_cohort_puuids_seed_once_in_the_highest_cohort(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": [{"puuid": "dup", "leaguePoints": 1}], "master": [{"puuid": "dup", "leaguePoints": 1}]},
                     histories={"dup": ["A"]}, matches={"A": _m("A")})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db)
        assert db.query_all("SELECT cohort FROM seed_samples") == [("challenger",)]
    assert result.cross_listed_puuids == 1 and result.candidates == 1
    assert len(client.of("history")) == 1


# ---------------------------------------------------------------- history exhaustion


def test_history_is_read_to_exhaustion_inside_the_time_bounds(tmp_path: Path) -> None:
    ids = [f"M{i}" for i in range(45)]
    client = MaxStub(apex={"challenger": _apex("c", 1)}, histories={"c0": ids}, matches={m: _m(m) for m in ids})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db, history_end_time=START_S + 999)
    history = client.of("history")
    assert [(c[2], c[3]) for c in history] == [(0, 20), (20, 20), (40, 20)]
    assert all(c[4] == START_S and c[5] == START_S + 999 for c in history)
    assert result.seeds_exhausted == 1 and result.unique_match_ids == 45 and result.matches_inserted == 45


def test_history_exact_multiple_of_page_size_ends_on_an_empty_page(tmp_path: Path) -> None:
    ids = [f"M{i}" for i in range(2 * HISTORY_PAGE_SIZE)]
    client = MaxStub(apex={"challenger": _apex("c", 1)}, histories={"c0": ids}, matches={m: _m(m) for m in ids})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db)
    assert len(client.of("history")) == 3 and result.seeds_exhausted == 1


def test_history_repeated_page_guard(tmp_path: Path) -> None:
    """A history that keeps returning the same IDs whatever `start` is."""
    same = [f"S{i}" for i in range(HISTORY_PAGE_SIZE)]
    client = MaxStub(apex={"challenger": _apex("c", 1)}, histories={"c0": lambda start, count: same},
                     matches={m: _m(m) for m in same})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db)
        assert db.query_one("SELECT COUNT(*) FROM seed_samples")[0] == 1
    assert len(client.of("history")) == 2 and result.seeds_repeated_page_guard == 1
    assert result.unique_match_ids == HISTORY_PAGE_SIZE


def test_history_page_cap_is_reported_separately(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 1)},
                     histories={"c0": lambda start, count: [f"H{start + i}" for i in range(count)]},
                     matches={f"H{i}": _m(f"H{i}") for i in range(200)})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db, max_history_pages=3)
    assert len(client.of("history")) == 3 and result.seeds_page_capped == 1 and result.seeds_ledgered == 1


def test_history_pages_are_round_robin_across_a_wave(tmp_path: Path) -> None:
    histories = {f"c{i}": [f"M{i}-{k}" for k in range(25)] for i in range(3)}
    matches = {m: _m(m) for ids in histories.values() for m in ids}
    client = MaxStub(apex={"challenger": _apex("c", 3)}, histories=histories, matches=matches)
    with Database(tmp_path / "db.sqlite3") as db:
        _collect(client, db, wave_size=3)
    starts = [c[2] for c in client.of("history")]
    assert starts == [0, 0, 0, 20, 20, 20]


# ---------------------------------------------------------------- dedupe and provenance


def test_dedupes_in_run_and_against_the_store_before_any_match_fetch(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 3)},
                     histories={"c0": ["A", "B"], "c1": ["B", "C"], "c2": ["C", "OLD"]},
                     matches={m: _m(m) for m in ("A", "B", "C")})
    with Database(tmp_path / "db.sqlite3") as db:
        db.ingest_match(_m("OLD"))
        result = _collect(client, db)
        assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 4  # one row per lobby
        provenance = db.query_all("SELECT match_id, puuid FROM match_discoveries ORDER BY match_id, puuid")
    assert sorted(c[1] for c in client.of("match")) == ["A", "B", "C"]  # OLD never fetched, B/C once
    assert result.match_id_references == 6 and result.unique_match_ids == 4
    assert result.already_stored_skipped == 1
    # Every seed that surfaced a stored match gets a provenance row -- raw observations kept.
    assert provenance == [("A", "c0"), ("B", "c0"), ("B", "c1"), ("C", "c1"), ("C", "c2"), ("OLD", "c2")]


def test_provenance_across_waves_without_refetch(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 2)}, histories={"c0": ["A"], "c1": ["A"]}, matches={"A": _m("A")})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db, wave_size=1)
        rows = db.query_all("SELECT match_id, run_id, puuid FROM match_discoveries ORDER BY run_id")
    assert len(client.of("match")) == 1
    assert rows == [("A", "max-w001", "c0"), ("A", "max-w002", "c1")]
    assert result.wave_run_ids == ["max-w001", "max-w002"]


def test_cohort_is_provenance_not_a_match_label(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 1), "master": _apex("m", 1)},
                     histories={"c0": ["A"], "m0": ["A"]}, matches={"A": _m("A")})
    with Database(tmp_path / "db.sqlite3") as db:
        _collect(client, db)
        cohorts = db.query_all("SELECT DISTINCT cohort FROM match_discoveries ORDER BY cohort")
        match_columns = [r[1] for r in db.query_all("PRAGMA table_info(matches)")]
    assert cohorts == [("challenger",), ("master",)]
    assert "cohort" not in match_columns


def test_non_ranked_matches_are_not_stored(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 1)}, histories={"c0": ["N"]}, matches={"N": _m("N", queue_id=1090)})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db)
        assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 0
    assert result.non_target_matches_skipped == 1 and result.seeds_ledgered == 1


# ---------------------------------------------------------------- breadth-first


def test_spread_order_is_a_spread_permutation() -> None:
    for n in (0, 1, 2, 3, 10, 97, 100):
        order = spread_order(n)
        assert sorted(order) == list(range(n))
    first = spread_order(100)[:4]
    assert max(first) - min(first) > 50  # the first few already span the ladder


def test_queue_puts_never_sampled_first_then_least_recent_interleaving_cohorts() -> None:
    ranked = {"challenger": ["c0", "c1"], "diamond": ["d0", "d1", "d2"]}
    queue = breadth_first_queue(ranked, {"c0": 500, "d1": 100})
    # Never sampled first, alternating cohorts; then the oldest sample first.
    assert queue == [("d0", "diamond"), ("c1", "challenger"), ("d2", "diamond"), ("d1", "diamond"), ("c0", "challenger")]


def test_seed_rotation_prefers_never_sampled_players(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 4)}, histories={}, matches={})
    with Database(tmp_path / "db.sqlite3") as db:
        db.start_ingest_run("old", 1)
        db.finalize_ingest_run("old", [("c0", "challenger"), ("c1", "challenger")], [], sampled_at=5, completed_at=6)
        _collect(client, db, wave_size=1, budgets=CollectionBudgets(10_000, 100_000, 100_000))
    order = [c[1] for c in client.of("history")]
    assert order == ["c3", "c2", "c0", "c1"]  # never-sampled (spread across the ladder) first


# ---------------------------------------------------------------- budgets


def test_match_fetch_budget_stops_cleanly_and_ledgers_only_complete_seeds(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 2)}, histories={"c0": ["A"], "c1": ["B", "C"]},
                     matches={m: _m(m) for m in "ABC"})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db, budgets=CollectionBudgets(10_000, 100_000, 2))
        ledger = db.query_all("SELECT puuid FROM seed_samples")
        runs = db.query_all("SELECT run_id, status FROM ingest_runs")
    assert result.stop_reason == STOP_MATCH_FETCHES
    assert result.match_fetches == 2 and result.matches_inserted == 2 and result.unhandled_match_ids == 1
    assert ledger == [("c0",)]  # c1 still has an unfetched match: not ledgered
    assert result.seeds_with_unhandled_matches == 1
    assert runs == [("max-w001", "completed")]


def test_request_budget_is_enforced_by_the_real_client(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        headers = {"X-App-Rate-Limit": "100:1", "X-App-Rate-Limit-Count": "1:1",
                   "X-Method-Rate-Limit": "100:1", "X-Method-Rate-Limit-Count": "1:1"}
        if path.endswith("/challenger"):
            body = {"entries": _apex("c", 3)}
        elif "/league/v1/" in path:
            body = [] if "/entries/" in path else {"entries": []}
        elif path.endswith("/ids"):
            body = [f"{path.split('/')[-2]}-A"]
        else:
            body = _m(path.rsplit("/", 1)[1])
        return httpx.Response(200, headers=headers, content=json.dumps(body).encode())

    with Database(tmp_path / "db.sqlite3") as db, RiotClient(
        "fake-key", clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handler)
    ) as client:
        # 3 apex + 8 division pages = 11 ladder requests; 4 more allowed.
        result = _collect(client, db, budgets=CollectionBudgets(10_000, 15, 100), clock=clock)
        assert client.telemetry.requests == 15
        ledger = db.query_one("SELECT COUNT(*) FROM seed_samples")[0]
    assert result.stop_reason == STOP_REQUESTS
    assert result.ladder_requests == 11 and result.history_requests == 3 and result.match_fetches == 1
    # c0's only match was fetched before the budget ran out; c1/c2's were not.
    assert ledger == 1 == result.seeds_ledgered and result.seeds_with_unhandled_matches == 2


def test_duration_budget_stops_cleanly(tmp_path: Path) -> None:
    clock = FakeClock()
    client = MaxStub(apex={"challenger": _apex("c", 50)}, histories={f"c{i}": [f"M{i}"] for i in range(50)},
                     matches={f"M{i}": _m(f"M{i}") for i in range(50)}, clock=clock, request_cost_s=1.0)
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db, budgets=CollectionBudgets(30, 100_000, 100_000), clock=clock, wave_size=5)
        statuses = {s for (s,) in db.query_all("SELECT status FROM ingest_runs")}
        ledgered = db.query_one("SELECT COUNT(*) FROM seed_samples")[0]
    assert result.stop_reason == STOP_DURATION
    assert statuses == {"completed"}  # every started wave finalized, none left dangling
    assert 0 < result.seeds_ledgered == ledgered < 50
    assert result.elapsed_s <= 31


def test_budgets_are_required_and_validated() -> None:
    with pytest.raises(ValueError):
        CollectionBudgets(0, 10, 10)
    with pytest.raises(ValueError):
        CollectionBudgets(10, 0, 10)
    with pytest.raises(ValueError):
        CollectionBudgets(10, 10, -1)


# ---------------------------------------------------------------- failures and resumability


def test_failed_history_is_not_ledgered_but_its_matches_are_kept(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 2)}, histories={"c0": ["A"], "c1": ["B"]},
                     matches={m: _m(m) for m in "AB"}, failing_histories={"c1"})
    with Database(tmp_path / "db.sqlite3") as db:
        result = _collect(client, db)
        assert db.query_all("SELECT puuid FROM seed_samples") == [("c0",)]
    assert result.seeds_failed == 1 and result.stop_reason == STOP_COMPLETE


def test_database_error_marks_the_wave_failed_and_propagates(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 1)}, histories={"c0": ["A"]}, matches={"A": _m("A")})
    with Database(tmp_path / "db.sqlite3") as db:
        original = db.ingest_match

        def boom(*a, **k):
            raise RuntimeError("disk full")

        db.ingest_match = boom
        with pytest.raises(RuntimeError):
            _collect(client, db)
        db.ingest_match = original
        assert db.query_all("SELECT run_id, status FROM ingest_runs") == [("max-w001", "failed")]
        assert db.seed_last_sampled() == {}


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TFTLAB_TEST_DATABASE_URL not set")


def _fresh_pg() -> Database:
    db = Database(POSTGRES_TEST_URL)
    for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db


def _rerun_scenario(db: Database) -> None:
    histories = {f"c{i}": [f"M{i}", "SHARED"] for i in range(4)}
    matches = {m: _m(m) for ids in histories.values() for m in ids}
    first = MaxStub(apex={"challenger": _apex("c", 4)}, histories=histories, matches=matches)
    r1 = _collect(first, db, run_id="run1", budgets=CollectionBudgets(10_000, 100_000, 3), wave_size=2)
    # Wave 1 (c0, c3) completes; wave 2 (c2, c1) stops on the fetch budget.
    assert r1.stop_reason == STOP_MATCH_FETCHES
    assert sorted(p for (p,) in db.query_all("SELECT puuid FROM seed_samples")) == ["c0", "c3"]
    assert db.query_all("SELECT run_id, status FROM ingest_runs ORDER BY run_id") == [
        ("run1-w001", "completed"), ("run1-w002", "completed")]

    second = MaxStub(apex={"challenger": _apex("c", 4)}, histories=histories, matches=matches)
    r2 = _collect(second, db, run_id="run2")
    assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 5  # each lobby once
    assert db.query_one("SELECT COUNT(DISTINCT puuid) FROM seed_samples")[0] == 4
    assert r2.stop_reason == STOP_COMPLETE
    assert [c[1] for c in second.of("history")][:2] == ["c2", "c1"]  # unfinished seeds first
    assert sorted(c[1] for c in first.of("match")) == ["M0", "M3", "SHARED"]
    assert sorted(c[1] for c in second.of("match")) == ["M1", "M2"]  # nothing refetched
    # Provenance: every (match, seed) observation once per run that saw it.
    assert db.query_one(
        "SELECT COUNT(*) FROM (SELECT match_id, run_id, puuid FROM match_discoveries "
        "GROUP BY match_id, run_id, puuid HAVING COUNT(*) > 1) d")[0] == 0


def test_rerun_continues_without_refetching_or_duplicating(tmp_path: Path) -> None:
    with Database(tmp_path / "db.sqlite3") as db:
        _rerun_scenario(db)


@requires_postgres
def test_postgres_rerun_continues_without_refetching_or_duplicating() -> None:
    db = _fresh_pg()
    try:
        _rerun_scenario(db)
    finally:
        db.close()


@requires_postgres
def test_postgres_failed_wave_is_marked_failed_and_earlier_waves_stay_completed() -> None:
    db = _fresh_pg()
    try:
        client = MaxStub(apex={"challenger": _apex("c", 2)}, histories={"c0": ["A"], "c1": ["B"]},
                         matches={"A": _m("A"), "B": _m("B")})
        original = db.ingest_match

        def fail_on_b(payload, **kwargs):
            if payload["metadata"]["match_id"] == "B":
                raise RuntimeError("connection lost")
            return original(payload, **kwargs)

        db.ingest_match = fail_on_b
        with pytest.raises(RuntimeError):
            _collect(client, db, run_id="pgmax", wave_size=1)
        db.ingest_match = original
        runs = db.query_all("SELECT run_id, status FROM ingest_runs ORDER BY run_id")
        ledger = db.seed_last_sampled()
    finally:
        db.close()
    assert runs == [("pgmax-w001", "completed"), ("pgmax-w002", "failed")]
    assert list(ledger) == ["c0"]


def test_requires_a_history_lower_bound(tmp_path: Path) -> None:
    with Database(tmp_path / "db.sqlite3") as db, pytest.raises(ValueError):
        collect_maximum(MaxStub(), db, budgets=BIG, history_start_time=None)


def test_result_json_has_aggregates_only(tmp_path: Path) -> None:
    client = MaxStub(apex={"challenger": _apex("secretpuuid", 1)}, histories={"secretpuuid0": ["MATCHID1"]},
                     matches={"MATCHID1": _m("MATCHID1")})
    with Database(tmp_path / "db.sqlite3") as db:
        text = json.dumps(_collect(client, db).as_dict())
    assert "secretpuuid" not in text and "MATCHID1" not in text


# ---------------------------------------------------------------- planning


def test_plan_is_network_free_and_bounded_by_the_ceiling() -> None:
    plan = collection_plan(budgets=CollectionBudgets(3600, 10_000, 5000), rate_ceilings=(RateWindow(10, 10),))
    assert plan["network"].startswith("none")
    assert plan["max_requests_by_time_at_ceiling"] == 3600
    assert plan["request_upper_bound"] == 3600
    no_ceiling = collection_plan(budgets=CollectionBudgets(3600, 10_000, 5000))
    assert no_ceiling["request_upper_bound"] == 10_000


# ---------------------------------------------------------------- CLI


def _cli(monkeypatch, tmp_path, args, client=None):
    monkeypatch.setenv("RIOT_API_KEY", "fake-key")
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", lambda **_: (None, False, None))

    class _Ctx:
        def __init__(self, *_a, **_k):
            _Ctx.kwargs = _k

        def __enter__(self):
            return client

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "RiotClient", _Ctx)
    return CliRunner().invoke(cli.app, ["ingest-riot", *args]), _Ctx


MAX_ARGS = ["--collection-mode", "maximum", "--start-time", "2026-09-24T07:00:00Z",
            "--max-duration-minutes", "5", "--max-requests", "100", "--max-match-fetches", "50"]


@pytest.mark.parametrize(
    "args, message",
    [
        (["--collection-mode", "huge"], "must be one of"),
        (["--collection-mode", "maximum", "--start-time", "2026-09-24T07:00:00Z"], "explicit budgets"),
        ([*MAX_ARGS, "--challenger-seeds", "5"], "do not pass"),
        ([*MAX_ARGS, "--matches-per-player", "5"], "do not pass"),
        ([*MAX_ARGS, "--max-ladder-pages", "5"], "--max-division-pages"),
        (["--collection-mode", "maximum", "--max-duration-minutes", "5", "--max-requests", "1",
          "--max-match-fetches", "1"], "--current-trusted-window or --start-time"),
        (["--max-requests", "10"], "need --collection-mode maximum"),
        (["--plan"], "need --collection-mode maximum"),
        ([*MAX_ARGS, "--rate-ceiling", "fast"], "10:10"),
    ],
)
def test_cli_rejects_invalid_mode_combinations(monkeypatch, tmp_path, args, message) -> None:
    result, _ = _cli(monkeypatch, tmp_path, args)
    assert result.exit_code != 0
    assert message in " ".join(re.sub("[│╭╮╰╯─]", " ", result.output).split())


def test_cli_plan_makes_no_network_call(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("RIOT_API_KEY", raising=False)

    def no_network(*a, **k):
        raise AssertionError("network used during --plan")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", no_network)
    monkeypatch.setattr(cli, "RiotClient", no_network)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "absent.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = CliRunner().invoke(cli.app, ["ingest-riot", *MAX_ARGS, "--plan", "--rate-ceiling", "10:10"])
    assert result.exit_code == 0, result.output
    assert "NETWORK-FREE" in result.output
    assert "Upper bound on Riot requests this run: 100" in result.output
    assert not (tmp_path / "absent.sqlite3").exists()  # never created


def test_cli_maximum_run_reports_and_writes_telemetry(monkeypatch, tmp_path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 2)}, histories={"c0": ["A"], "c1": ["A", "B"]},
                     matches={m: _m(m) for m in "AB"})
    out = tmp_path / "telemetry.json"
    result, ctx = _cli(monkeypatch, tmp_path,
                       [*MAX_ARGS, "--rate-ceiling", "10:10", "--safety-utilization", "0.8", "--telemetry-out", str(out)],
                       client=client)
    assert result.exit_code == 0, result.output
    assert "Maximum collection report" in result.output
    assert "Stop reason: complete" in result.output
    assert ctx.kwargs["rate_ceilings"] == (RateWindow(10, 10),) and ctx.kwargs["safety_utilization"] == 0.8
    data = json.loads(out.read_text())
    assert data["mode"] == "maximum" and data["outcome"] == "completed"
    assert data["collection"]["matches_inserted"] == 2 and data["rate_ceilings"] == ["10:10"]
    assert "fake-key" not in out.read_text() and '"c0"' not in out.read_text()


def test_cli_bounded_mode_is_unchanged_and_writes_telemetry(monkeypatch, tmp_path) -> None:
    client = MaxStub(apex={"challenger": _apex("c", 2)}, histories={"c0": ["A"]}, matches={"A": _m("A")})
    out = tmp_path / "t.json"
    result, _ = _cli(monkeypatch, tmp_path, ["--challenger-seeds", "2", "--matches-per-player", "3",
                                             "--telemetry-out", str(out)], client=client)
    assert result.exit_code == 0, result.output
    assert "Ingest report" in result.output
    assert [c[3] for c in client.of("history")] == [3, 3]  # count = matches per player, one page each
    assert json.loads(out.read_text())["collection"]["matches_inserted"] == 1
