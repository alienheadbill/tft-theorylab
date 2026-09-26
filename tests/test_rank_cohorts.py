"""Rank cohorts, seed rotation, the sampling ledger and discovery provenance."""

from __future__ import annotations

import os
import random
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tftlab import cli
from tftlab.ingest import fetch_cohort_entries, ingest_ladder
from tftlab.riot import RiotClient
from tftlab.sampling import COHORTS, evenly_spaced, rank_entries, rotate, select_cohort_seeds
from tftlab.storage import Database

from _helpers import make_match, make_unit
from test_ingest_riot import _FakeResponse, _StubRiotClient

START_S, END_S = 1_790_233_200, 1_791_244_800
IN_WINDOW_MS = 1_790_233_200_000 + 60_000


def _apex(prefix: str, n: int, top_lp: int) -> list[dict]:
    return [{"puuid": f"{prefix}{i:03d}", "leaguePoints": top_lp - i, "rank": "I"} for i in range(n)]


def _divisions(prefix: str, per_division: int, page_size: int = 1000) -> dict[tuple[str, str], list[list[dict]]]:
    """{(TIER, DIVISION): [page1, page2, ...]} for one divisional tier."""
    tier = {"d": "DIAMOND", "p": "PLATINUM"}[prefix]
    out = {}
    for division in ("I", "II", "III", "IV"):
        entries = [
            {"puuid": f"{prefix}{division}-{i:03d}", "leaguePoints": 99 - i, "rank": division, "tier": tier}
            for i in range(per_division)
        ]
        out[(tier, division)] = [entries[i : i + page_size] for i in range(0, len(entries), page_size)]
    return out


class _CohortStub(_StubRiotClient):
    """Adds the TFT-LEAGUE-V1 entries endpoint to the ingest stub."""

    def __init__(self, *, pages=None, **kwargs):
        super().__init__(**kwargs)
        self._pages = pages or {}
        self.league_calls: list[tuple[str, str, int]] = []

    def league_entries(self, tier, division, *, page=1, queue="RANKED_TFT"):
        self.league_calls.append((tier, division, page))
        self.ladder_calls.append(f"{tier.lower()}-{division}-{page}")
        pages = self._pages.get((tier, division), [])
        return list(pages[page - 1]) if page <= len(pages) else []


def _full_client(**kwargs) -> _CohortStub:
    tiers = {"challenger": _apex("c", 30, 2000), "grandmaster": _apex("g", 30, 900), "master": _apex("m", 60, 400)}
    pages = {**_divisions("d", 20), **_divisions("p", 20)}
    kwargs.setdefault("match_ids_by_puuid", {})
    kwargs.setdefault("matches", {})
    return _CohortStub(tiers=tiers, pages=pages, **kwargs)


def _entries_fetcher(client: _CohortStub, max_pages: int = 3):
    return lambda cohort: fetch_cohort_entries(client, cohort, max_pages=max_pages)[0]


def _m(match_id: str, **kwargs) -> dict:
    kwargs.setdefault("game_datetime", IN_WINDOW_MS)
    return make_match(match_id, units=[make_unit("TFT14_Foo", tier=2, items=[])], **kwargs)


# ---------------------------------------------------------------- cohorts


def test_five_distinct_cohorts_and_no_elite() -> None:
    assert COHORTS == ("challenger", "grandmaster", "master", "diamond", "platinum")
    assert "elite" not in COHORTS
    with pytest.raises(ValueError, match="Unknown seed cohort"):
        select_cohort_seeds(lambda c: [], {"elite": 5})
    with pytest.raises(ValueError):
        select_cohort_seeds(lambda c: [], {"challenger": -1})


def test_each_apex_cohort_stays_separate() -> None:
    client = _full_client()
    sel = select_cohort_seeds(_entries_fetcher(client), {"challenger": 5, "grandmaster": 4, "master": 3})
    assert list(sel.reports) == ["challenger", "grandmaster", "master"]
    assert {c: r.selected for c, r in sel.reports.items()} == {"challenger": 5, "grandmaster": 4, "master": 3}
    assert {c: r.available for c, r in sel.reports.items()} == {"challenger": 30, "grandmaster": 30, "master": 60}
    assert sel.cohorts == ("challenger",) * 5 + ("grandmaster",) * 4 + ("master",) * 3
    for puuid, cohort in zip(sel.puuids, sel.cohorts):
        assert puuid[0] == cohort[0]  # each seed is labelled with the ladder it came from


def test_diamond_and_platinum_divisions_collapse_into_one_cohort_each() -> None:
    client = _full_client()
    sel = select_cohort_seeds(_entries_fetcher(client), {"diamond": 8, "platinum": 8})
    assert list(sel.reports) == ["diamond", "platinum"]
    assert sel.reports["diamond"].available == 80 and sel.reports["platinum"].available == 80
    assert set(sel.cohorts) == {"diamond", "platinum"}  # never "diamond_ii" etc.
    # ...but the division spreads the seeds across all four divisions.
    for prefix in ("d", "p"):
        divisions = [p.split("-")[0][1:] for p in sel.puuids if p[0] == prefix]
        assert divisions == ["I", "I", "II", "II", "III", "III", "IV", "IV"]


def test_zero_count_cohorts_are_never_fetched() -> None:
    client = _full_client()
    select_cohort_seeds(_entries_fetcher(client), {"challenger": 0, "grandmaster": 2, "diamond": 0, "platinum": 0})
    assert client.ladder_calls == ["grandmaster"]


def test_cohort_short_of_players_is_not_backfilled_from_another() -> None:
    client = _full_client()
    sel = select_cohort_seeds(_entries_fetcher(client), {"challenger": 50, "grandmaster": 5})
    assert (sel.reports["challenger"].requested, sel.reports["challenger"].selected) == (50, 30)
    assert sel.reports["grandmaster"].selected == 5
    assert sel.requested == 55 and len(sel.puuids) == 35


def test_duplicate_puuids_never_give_duplicate_seeds() -> None:
    entries = {
        "master": [{"puuid": "x", "leaguePoints": 10}, {"puuid": "x", "leaguePoints": 30}, {"puuid": "y", "leaguePoints": 5}],
        # "x" promoted between requests / listed twice across pages: counts once, in the higher cohort.
        "diamond": [
            {"puuid": "x", "leaguePoints": 50, "rank": "I"},
            {"puuid": "z", "leaguePoints": 50, "rank": "II"},
            {"puuid": "z", "leaguePoints": 70, "rank": "II"},
        ],
    }
    sel = select_cohort_seeds(lambda c: entries[c], {"master": 5, "diamond": 5})
    assert sorted(sel.puuids) == ["x", "y", "z"] and len(set(sel.puuids)) == len(sel.puuids)
    assert dict(zip(sel.puuids, sel.cohorts)) == {"x": "master", "y": "master", "z": "diamond"}


def test_ranking_uses_only_division_lp_and_puuid() -> None:
    entries = [
        {"puuid": "b", "leaguePoints": 10, "rank": "II", "wins": 400, "hotStreak": True},
        {"puuid": "a", "leaguePoints": 90, "rank": "III"},
        {"puuid": "c", "leaguePoints": 10, "rank": "II"},
        {"puuid": "d", "leaguePoints": 0, "rank": "I"},
    ]
    assert rank_entries(entries) == ["d", "b", "c", "a"]
    assert rank_entries(entries, exclude={"b"}) == ["d", "c", "a"]


# ---------------------------------------------------------------- rotation


RANKED = [f"p{i:02d}" for i in range(20)]


def test_unseen_players_are_preferred() -> None:
    ledger = {p: 1_000 for p in RANKED[:15]}  # only p15..p19 never sampled
    assert rotate(RANKED, 5, ledger) == RANKED[15:]
    picked = rotate(RANKED, 8, ledger)
    assert set(RANKED[15:]) <= set(picked) and len(picked) == 8


def test_least_recently_sampled_preferred_once_everyone_has_been_seen() -> None:
    ledger = {p: (100 if i % 2 else 200) for i, p in enumerate(RANKED)}  # odd ranks sampled longer ago
    assert rotate(RANKED, 10, ledger) == RANKED[1::2]
    ledger[RANKED[4]] = 50  # the oldest of all comes first
    assert RANKED[4] in rotate(RANKED, 1, ledger)


def test_partial_group_is_spread_across_the_tier_not_its_top() -> None:
    assert rotate(RANKED, 4, {}) == evenly_spaced(RANKED, 4) == ["p02", "p07", "p12", "p17"]
    assert rotate(RANKED, 0, {}) == []


def test_rotation_is_deterministic_for_identical_ladder_and_ledger() -> None:
    client = _full_client()
    ledger = {f"d{d}-{i:03d}": 10 + i for d in ("I", "II") for i in range(20)}
    allocation = {"challenger": 3, "master": 7, "diamond": 9, "platinum": 2}
    first = select_cohort_seeds(_entries_fetcher(client), allocation, last_sampled=ledger)

    shuffled = {k: [random.Random(7).sample(page, len(page)) for page in pages] for k, pages in client._pages.items()}
    tiers = {t: random.Random(3).sample(e, len(e)) for t, e in client._tiers.items()}
    again = _CohortStub(tiers=tiers, pages=shuffled, match_ids_by_puuid={}, matches={})
    second = select_cohort_seeds(_entries_fetcher(again), allocation, last_sampled=dict(reversed(list(ledger.items()))))
    assert first == second
    assert first.reports["diamond"].never_sampled_selected == 9  # divisions III/IV are unseen


def test_rotation_reports_never_and_previously_sampled() -> None:
    client = _full_client()
    ledger = {f"c{i:03d}": 5 for i in range(28)}  # only two Challenger players unseen
    sel = select_cohort_seeds(_entries_fetcher(client), {"challenger": 5}, last_sampled=ledger)
    report = sel.reports["challenger"]
    assert (report.never_sampled_selected, report.previously_sampled_selected) == (2, 3)
    assert {"c028", "c029"} <= set(sel.puuids)


# ---------------------------------------------------------------- Riot client + pagination


def test_league_entries_uses_the_documented_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(self, url, params=None):
        captured.update(url=url, params=dict(params or {}))
        return _FakeResponse([{"puuid": "a", "leaguePoints": 1, "rank": "II", "tier": "DIAMOND"}])

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with RiotClient("fake-key") as client:
        page = client.league_entries("DIAMOND", "II", page=2)
        assert page[0]["puuid"] == "a"
        assert captured["url"] == "https://na1.api.riotgames.com/tft/league/v1/entries/DIAMOND/II"
        assert captured["params"] == {"queue": "RANKED_TFT", "page": 2}
        for bad in (("Diamond", "II", 1), ("DIAMOND", "V", 1), ("DIAMOND", "2", 1), ("MASTER", "I", 1), ("DIAMOND", "I", 0)):
            with pytest.raises(ValueError):
                client.league_entries(bad[0], bad[1], page=bad[2])


def test_divisional_pagination_reads_all_divisions_and_stops_at_empty_page_or_cap() -> None:
    pages = _divisions("d", 25, page_size=10)  # 3 pages per division: 10, 10, 5
    client = _CohortStub(pages=pages, match_ids_by_puuid={}, matches={})
    entries, requests, complete = fetch_cohort_entries(client, "diamond", max_pages=5)
    assert len(entries) == 100 and requests == 16  # 3 pages + the empty 4th, per division
    assert [c[1] for c in client.league_calls[::4]] == ["I", "II", "III", "IV"]

    capped = _CohortStub(pages=pages, match_ids_by_puuid={}, matches={})
    entries, requests, complete = fetch_cohort_entries(capped, "diamond", max_pages=2)
    assert len(entries) == 80 and requests == 8
    assert {c[2] for c in capped.league_calls} == {1, 2}

    apex = _full_client()
    assert fetch_cohort_entries(apex, "grandmaster")[1] == 1 and apex.league_calls == []


# ---------------------------------------------------------------- pagination completeness


def _uneven_pages(prefix: str, pages_per_division: dict[str, int], page_size: int = 5):
    """Divisional pages where each division has its own number of full pages."""
    tier = {"d": "DIAMOND", "p": "PLATINUM"}[prefix]
    out = {}
    for division, n_pages in pages_per_division.items():
        out[(tier, division)] = [
            [{"puuid": f"{prefix}{division}-{pg}-{i}", "leaguePoints": 99 - i, "rank": division} for i in range(page_size)]
            for pg in range(n_pages)
        ]
    return out


@pytest.mark.parametrize("prefix, cohort", [("d", "diamond"), ("p", "platinum")])
def test_pagination_complete_when_every_division_reaches_an_empty_page(prefix, cohort) -> None:
    pages = _uneven_pages(prefix, {"I": 3, "II": 1, "III": 2, "IV": 0})
    client = _CohortStub(pages=pages, match_ids_by_puuid={}, matches={})
    fetched = fetch_cohort_entries(client, cohort, max_pages=4)
    assert fetched.pagination_complete is True
    assert len(fetched.entries) == 30 and fetched.requests == 4 + 2 + 3 + 1


@pytest.mark.parametrize("prefix, cohort", [("d", "diamond"), ("p", "platinum")])
@pytest.mark.parametrize(
    "pages_per_division",
    [
        {"I": 1, "II": 1, "III": 1, "IV": 5},  # one division still had entries on the last allowed page
        {"I": 3, "II": 3, "III": 3, "IV": 3},  # exactly max_pages full pages: no empty page seen, can't know
    ],
)
def test_pagination_capped_when_any_division_hits_the_cap_with_entries(prefix, cohort, pages_per_division) -> None:
    client = _CohortStub(pages=_uneven_pages(prefix, pages_per_division), match_ids_by_puuid={}, matches={})
    fetched = fetch_cohort_entries(client, cohort, max_pages=3)
    assert fetched.pagination_complete is False
    assert max(page for _, _, page in client.league_calls) == 3  # the default cap is never exceeded


def test_apex_cohorts_have_no_pagination_status(tmp_path: Path) -> None:
    client = _full_client()
    for cohort in ("challenger", "grandmaster", "master"):
        assert fetch_cohort_entries(client, cohort).pagination_complete is None
    with Database(tmp_path / "apex.sqlite3") as db:
        result = ingest_ladder(
            client, db, seed_allocation={"challenger": 2, "grandmaster": 2, "master": 2}, run_id="a", now_ms=1
        )
        legacy = ingest_ladder(_full_client(), db, player_limit=6, sampling_mode="high_elo", run_id="b", now_ms=2)
    for report in (*result.cohort_reports.values(), *legacy.cohort_reports.values()):
        assert report.pagination_complete is None and report.max_ladder_pages is None
        assert report.ladder_requests == 1


def test_capped_and_complete_reports_leave_sampling_unchanged(tmp_path: Path) -> None:
    """Completeness is reporting only: the seeds are exactly what
    select_cohort_seeds picks from the same fetched entries."""
    pages = {**_uneven_pages("d", {"I": 4, "II": 4, "III": 4, "IV": 4}), **_uneven_pages("p", {"I": 1, "II": 1, "III": 1, "IV": 1})}
    allocation = {"diamond": 4, "platinum": 4}
    client = _CohortStub(pages=pages, match_ids_by_puuid={}, matches={})
    with Database(tmp_path / "same.sqlite3") as db:
        result = ingest_ladder(client, db, seed_allocation=allocation, max_ladder_pages=3, run_id="s", now_ms=1)
    probe = _CohortStub(pages=pages, match_ids_by_puuid={}, matches={})
    expected = select_cohort_seeds(_entries_fetcher(probe, max_pages=3), allocation).puuids
    assert tuple(client.history_calls) == expected
    # Evenly spaced over the fetched pools (60 -> ranks 7,22,37,52; 20 -> 2,7,12,17): one per division.
    assert expected == ("dI-1-2", "dII-1-2", "dIII-1-2", "dIV-1-2", "pI-0-2", "pII-0-2", "pIII-0-2", "pIV-0-2")
    diamond, platinum = result.cohort_reports["diamond"], result.cohort_reports["platinum"]
    assert (diamond.pagination_complete, diamond.fetched_candidates, diamond.max_ladder_pages) == (False, 60, 3)
    assert (platinum.pagination_complete, platinum.fetched_candidates) == (True, 20)


def test_cli_never_calls_a_capped_pool_the_ladder_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pages = {**_uneven_pages("d", {"I": 5, "II": 1, "III": 1, "IV": 1}), **_uneven_pages("p", {"I": 1, "II": 1, "III": 1, "IV": 1})}
    client = _CohortStub(pages=pages, match_ids_by_puuid={}, matches={})
    result = _run(monkeypatch, tmp_path, client,
                  ["--diamond-seeds", "5", "--platinum-seeds", "2", "--max-ladder-pages", "3"])
    assert result.exit_code == 0, result.output
    out = result.output
    diamond = out[out.index("    diamond:") : out.index("    platinum:")]
    platinum = out[out.index("    platinum:") : out.index("Requested histories per seed")]
    assert "fetched candidate pool: 30" in diamond
    assert "pagination: capped at 3 pages/division; additional players may exist" in diamond
    assert "fetched candidate pool: 20" in platinum and "pagination: complete" in platinum
    assert "on the ladder" not in out and "available ladder entries" not in out


# ---------------------------------------------------------------- ingest, ledger, provenance


def test_shared_match_across_cohorts_is_stored_once_with_provenance_for_each(tmp_path: Path) -> None:
    probe = _full_client()
    seeds = select_cohort_seeds(_entries_fetcher(probe), {"challenger": 1, "master": 1, "diamond": 1}).puuids
    c, m, d = seeds
    client = _full_client(
        match_ids_by_puuid={c: ["SHARED", "C_ONLY"], m: ["SHARED"], d: ["SHARED", "D_ONLY"]},
        matches={k: _m(k) for k in ("SHARED", "C_ONLY", "D_ONLY")},
    )
    with Database(tmp_path / "shared.sqlite3") as db:
        result = ingest_ladder(
            client, db, seed_allocation={"challenger": 1, "master": 1, "diamond": 1}, run_id="r1", now_ms=1_000
        )
        stored = db.query_one("SELECT COUNT(*) FROM matches WHERE match_id = 'SHARED'")[0]
        provenance = db.query_all(
            "SELECT puuid, cohort FROM match_discoveries WHERE match_id = 'SHARED' ORDER BY cohort"
        )
    assert client.match_calls.count("SHARED") == 1 and stored == 1
    assert provenance == [(c, "challenger"), (d, "diamond"), (m, "master")]
    assert (result.match_id_references, result.match_ids_seen, result.cross_cohort_match_ids) == (5, 3, 1)
    assert {k: r.unique_match_ids for k, r in result.cohort_reports.items()} == {"challenger": 2, "master": 1, "diamond": 2}
    assert result.discovery_rows == 5 and result.matches_with_provenance == 3


def test_provenance_accumulates_seeds_and_runs_for_one_canonical_match(tmp_path: Path) -> None:
    with Database(tmp_path / "prov.sqlite3") as db:
        db.ingest_match(_m("M"))
        db.record_match_discoveries("r1", [("M", "a", "master"), ("M", "b", "diamond")], 1)
        db.record_match_discoveries("r2", [("M", "a", "grandmaster")], 2)  # "a" promoted since
        db.record_match_discoveries("r2", [("M", "a", "grandmaster")], 2)  # idempotent
        rows = db.query_all("SELECT run_id, puuid, cohort FROM match_discoveries ORDER BY run_id, puuid")
        matches = db.query_one("SELECT COUNT(*) FROM matches")[0]
    assert rows == [("r1", "a", "master"), ("r1", "b", "diamond"), ("r2", "a", "grandmaster")]
    assert matches == 1


def test_seed_rank_never_labels_matches_or_participants(tmp_path: Path) -> None:
    with Database(tmp_path / "schema.sqlite3") as db:
        for table in ("matches", "participants", "units", "traits"):
            columns = {r[1] for r in db.query_all(f"PRAGMA table_info({table})")}
            assert not columns & {"cohort", "rank", "division", "seed_rank", "ladder_tier", "league"}, table  # units.tier is star level
        ledger_columns = [r[1] for r in db.query_all("PRAGMA table_info(seed_samples)")]
    assert ledger_columns == ["run_id", "puuid", "cohort", "sampled_at"]  # PUUID only, no name/Riot ID


def test_ledger_rotates_seeds_between_runs(tmp_path: Path) -> None:
    ladder = {"challenger": _apex("c", 6, 2000)}
    with Database(tmp_path / "rotate.sqlite3") as db:
        runs = []
        for i, now in enumerate((1_000, 2_000, 3_000)):
            client = _CohortStub(tiers=ladder, match_ids_by_puuid={}, matches={})
            result = ingest_ladder(client, db, seed_allocation={"challenger": 3}, run_id=f"r{i}", now_ms=now)
            runs.append((client.history_calls, result.cohort_reports["challenger"]))
    (first, r1), (second, r2), (third, r3) = runs
    assert set(first).isdisjoint(second) and len(set(first) | set(second)) == 6  # unseen first
    assert (r1.never_sampled_selected, r2.never_sampled_selected, r3.never_sampled_selected) == (3, 3, 0)
    assert third == first  # then least recently sampled
    assert r3.previously_sampled_selected == 3


def test_failed_history_is_not_recorded_so_the_player_stays_unseen(tmp_path: Path) -> None:
    client = _CohortStub(
        tiers={"challenger": _apex("c", 2, 2000)}, match_ids_by_puuid={}, matches={}, failing_histories={"c001"}
    )
    with Database(tmp_path / "failed.sqlite3") as db:
        result = ingest_ladder(client, db, seed_allocation={"challenger": 2}, run_id="r", now_ms=5)
        ledger = db.seed_last_sampled()
    assert ledger == {"c000": 5}
    assert result.cohort_reports["challenger"].failed_history_requests == 1 and result.seed_ledger_rows == 1


def test_current_window_bounds_and_empty_histories_with_cohorts(tmp_path: Path) -> None:
    probe = _full_client()
    seeds = select_cohort_seeds(_entries_fetcher(probe), {"grandmaster": 2, "platinum": 2}).puuids
    client = _full_client(match_ids_by_puuid={seeds[0]: ["A"], seeds[2]: ["A"]}, matches={"A": _m("A")})
    with Database(tmp_path / "window.sqlite3") as db:
        result = ingest_ladder(
            client, db, seed_allocation={"grandmaster": 2, "platinum": 2},
            history_start_time=START_S, history_end_time=END_S, run_id="w", now_ms=9,
        )
        ledger = db.seed_last_sampled()
    assert client.history_bounds == [(START_S, END_S)] * 4
    assert result.seeds_with_empty_history == 2 and result.failed_history_requests == 0
    assert {c: r.seeds_with_empty_history for c, r in result.cohort_reports.items()} == {"grandmaster": 1, "platinum": 1}
    assert len(ledger) == 4  # an empty in-window history still counts as sampled


def test_ranked_queue_filter_and_stored_dedupe_hold_with_cohorts(tmp_path: Path) -> None:
    probe = _full_client()
    c, d = select_cohort_seeds(_entries_fetcher(probe), {"challenger": 1, "diamond": 1}).puuids
    client = _full_client(
        match_ids_by_puuid={c: ["OLD", "RANKED"], d: ["HYPER", "RANKED"]},
        matches={"OLD": _m("OLD"), "RANKED": _m("RANKED"), "HYPER": _m("HYPER", queue_id=1130)},
    )
    with Database(tmp_path / "queue.sqlite3") as db:
        db.ingest_match(_m("OLD"))
        result = ingest_ladder(client, db, seed_allocation={"challenger": 1, "diamond": 1}, run_id="q", now_ms=1)
        assert (db.has_match("RANKED"), db.has_match("HYPER")) == (True, False)
        with_provenance = {r[0] for r in db.query_all("SELECT DISTINCT match_id FROM match_discoveries")}
    assert "OLD" not in client.match_calls
    assert result.non_target_matches_skipped == 1 and result.duplicates_skipped == 1 and result.matches_inserted == 1
    assert with_provenance == {"OLD", "RANKED"}  # stored matches only; the Hyper Roll game has none


# ---------------------------------------------------------------- CLI


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client, args: list[str]):
    class _Ctx:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "RiotClient", _Ctx)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", lambda **_: (None, False, 18))
    monkeypatch.setenv("RIOT_API_KEY", "super-secret-fake-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.chdir(tmp_path)
    return CliRunner().invoke(cli.app, ["ingest-riot", *args])


def test_cli_reports_every_cohort_separately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _full_client()
    args = ["--challenger-seeds", "4", "--grandmaster-seeds", "3", "--master-seeds", "2", "--diamond-seeds", "5",
            "--platinum-seeds", "0", "--matches-per-player", "5", "--max-ladder-pages", "2"]
    result = _run(monkeypatch, tmp_path, client, args)
    assert result.exit_code == 0, result.output
    for line in (
        "Sampling mode: cohorts",
        "Requested seed players: 14",
        "challenger: 4 selected (requested 4)",
        "grandmaster: 3 selected (requested 3)",
        "master: 2 selected (requested 2)",
        "available ladder entries: 60",
        "diamond: 5 selected (requested 5)",
        "fetched candidate pool: 80",
        "pagination: complete (every division reached an empty page)",
        "never sampled before: 5, previously sampled: 0",
        "Unique match IDs found by more than one cohort: 0",
        "Seeds recorded in the sampling ledger: 14",
        "Run id: local-",
    ):
        assert line in result.output
    assert "platinum" not in client.ladder_calls and not any(c.startswith("platinum") for c in client.ladder_calls)
    assert "elite" not in result.output.lower()
    assert "super-secret-fake-key" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--challenger-seeds", "0", "--diamond-seeds", "0"],  # total 0
        ["--challenger-seeds", "5", "--players", "5"],  # legacy and explicit mixed
        ["--diamond-seeds", "5", "--sampling", "high_elo"],
        ["--master-seeds", "5", "--include-master"],
        ["--platinum-seeds", "-1"],
        ["--diamond-seeds", "5", "--max-ladder-pages", "11"],
    ],
)
def test_cli_rejects_bad_cohort_options_before_any_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args) -> None:
    client = _full_client()
    result = _run(monkeypatch, tmp_path, client, args)
    assert result.exit_code != 0
    assert client.ladder_calls == [] and client.history_calls == []


# ---------------------------------------------------------------- Postgres


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_postgres_ledger_rotation_and_multi_cohort_provenance() -> None:
    db = Database(POSTGRES_TEST_URL)
    try:
        for table in ("match_discoveries", "seed_samples", "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        probe = _full_client()
        allocation = {"challenger": 1, "grandmaster": 1, "platinum": 1}
        c, g, p = select_cohort_seeds(_entries_fetcher(probe), allocation).puuids
        client = _full_client(
            match_ids_by_puuid={c: ["PG_SHARED"], g: ["PG_SHARED", "PG_G"], p: ["PG_SHARED"]},
            matches={k: _m(k) for k in ("PG_SHARED", "PG_G")},
        )
        first = ingest_ladder(client, db, seed_allocation=allocation, run_id="pg1", now_ms=100)
        shared = db.query_one("SELECT COUNT(*) FROM matches WHERE match_id = 'PG_SHARED'")[0]
        cohorts = [r[0] for r in db.query_all(
            "SELECT cohort FROM match_discoveries WHERE match_id = 'PG_SHARED' ORDER BY cohort"
        )]
        again = _full_client()
        second = ingest_ladder(again, db, seed_allocation=allocation, run_id="pg2", now_ms=200)
        ledger = db.seed_last_sampled()
    finally:
        db.close()
    assert client.match_calls.count("PG_SHARED") == 1 and shared == 1
    assert cohorts == ["challenger", "grandmaster", "platinum"]
    assert first.cross_cohort_match_ids == 1 and first.discovery_rows == 4
    assert set(again.history_calls).isdisjoint({c, g, p})  # rotated to unseen players
    assert all(r.never_sampled_selected == 1 for r in second.cohort_reports.values())
    assert len(ledger) == 6 and ledger[c] == 100
