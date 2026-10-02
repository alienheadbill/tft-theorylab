"""Recurring 2-4 unit cores: units directly observed together on the same
committed carry boards (`tftlab.analytics.cores`), as Champion Investigation
shows them.

A core is counted only where every member was on the board; its numbers are
the whole core's own `compute_associations` row, never a sum of pair
statistics; cores are selected and ordered by recurrence, never by results.

Riot-shaped boards with real Set 18 ids; SQLite and (with
TFTLAB_TEST_DATABASE_URL) Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _helpers import make_match, make_unit
from tftlab import how_to_play as htp
from tftlab.analytics import carry_partner_associations
from tftlab.analytics.cores import recurring_cores
from tftlab.champion_investigation import _partner_row, champion_investigation, champion_slug
from tftlab.roster import load_roster
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
WINDOW, OTHER_WINDOW = "14.6", "14.7"
OTHER_VERSION = "Version 14.7.581.1234 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"

ROSTER = load_roster()
KOGMAW, = ROSTER.champion_ids("Kog'Maw")
DRAVEN, = ROSTER.champion_ids("Draven")
KARMA, = ROSTER.champion_ids("Karma")
GROMP, = ROSTER.champion_ids("Gromp")
NIDALEE, = ROSTER.champion_ids("Nidalee")
VI, = ROSTER.champion_ids("Vi")
SUMMON = "TFT_Voidspawn"  # in the roster with a cost but no trait: not a shop champion
KOG = {"character_id": KOGMAW, "name": "Kog'Maw", "slug": "kogmaw", "cost": 3,
       "art_url": "/static/game/champions/x.png"}
ITEMS = ["DA_GuinsoosRageblade", "DA_InfinityEdge"]


def ids(core) -> tuple[str, ...]:
    return core.members


def shop(cid: str) -> bool:
    return cid in {"A", "B", "C", "D"}


# ---------------------------------------------------------------- direct co-occurrence (pure)


def test_a_three_unit_core_needs_both_teammates_on_the_same_board() -> None:
    games = [(1, frozenset({"A", "B"})), (2, frozenset({"A"})), (3, frozenset({"B"})), (8, frozenset({"A"}))]
    cores = {ids(c): c for c in recurring_cores(games, eligible=shop, sizes=(3,))}
    assert set(cores) == {("A", "B")} and cores[("A", "B")].evidence.games == 1


def test_a_four_unit_core_needs_all_four_units_on_the_same_board() -> None:
    games = [(1, frozenset({"A", "B", "C"})), (2, frozenset({"A", "B"})), (3, frozenset({"A", "C"})),
             (4, frozenset({"B", "C"}))]
    cores = {ids(c): c for c in recurring_cores(games, eligible=shop, sizes=(4,))}
    assert set(cores) == {("A", "B", "C")} and cores[("A", "B", "C")].evidence.games == 1


def test_frequent_pairs_never_add_up_to_a_core_that_was_not_observed() -> None:
    """A and B are each on 20 boards, never together: no A+B core exists,
    however strong the pairs look."""
    games = [(1, frozenset({"A"}))] * 20 + [(1, frozenset({"B"}))] * 20 + [(8, frozenset())] * 5
    assert recurring_cores(games, eligible=shop, sizes=(3, 4)) == []
    pairs = {ids(c): c.evidence.games for c in recurring_cores(games, eligible=shop, sizes=(2,))}
    assert pairs == {("A",): 20, ("B",): 20}


def test_a_larger_board_counts_once_toward_every_subset() -> None:
    cores = recurring_cores([(3, frozenset({"A", "B", "C"}))], eligible=shop)
    assert sorted((c.size, ids(c), c.evidence.games) for c in cores) == [
        (2, ("A",), 1), (2, ("B",), 1), (2, ("C",), 1),
        (3, ("A", "B"), 1), (3, ("A", "C"), 1), (3, ("B", "C"), 1),
        (4, ("A", "B", "C"), 1),
    ]


def test_only_eligible_units_become_members_and_ordering_is_by_boards_then_ids() -> None:
    games = [(1, frozenset({"B", "C", "X"}))] * 3 + [(1, frozenset({"A", "B", "X"}))] * 3 + [(8, frozenset({"A", "D"}))] * 5
    cores = recurring_cores(games, eligible=shop, sizes=(3,))
    assert all("X" not in c.members for c in cores)
    assert [ids(c) for c in cores] == [("A", "D"), ("A", "B"), ("B", "C")]  # 5 boards, then a 3-3 tie by ids


def test_shop_champion_eligibility_comes_from_the_roster() -> None:
    assert ROSTER.is_shop_champion(DRAVEN) and ROSTER.is_shop_champion(GROMP)
    assert not ROSTER.is_shop_champion(SUMMON)  # cost 1 in the feed, no trait
    assert not ROSTER.is_shop_champion("DA_NotAChampion")
    assert sum(ROSTER.is_shop_champion(c) for c in ROSTER.champions) == 74


# ---------------------------------------------------------------- Riot-shaped boards


def _board(match_id: str, partners: list[str], placement: int, *, version: str | None = None,
           carry: str = KOGMAW) -> dict:
    units = [make_unit(carry, tier=2, items=ITEMS)] + [make_unit(p, tier=2) for p in partners]
    kwargs = {"game_version": version} if version else {}
    return make_match(match_id, units=units, placement=placement, **kwargs)


def _seed(db: Database) -> None:
    # Core A: Draven + Karma on 20 boards, all bottom 4. Vi joins on 11 of them
    # (a 4-unit core). A summon and, on 5 boards, a second Draven copy are there too.
    for i in range(20):
        partners = [DRAVEN, KARMA, SUMMON] + ([VI] if i < 11 else []) + ([DRAVEN] if i < 5 else [])
        db.ingest_match(_board(f"A_{i}", partners, 5 + i % 4))
    # Core B: Gromp + Nidalee on 12 boards, all top 4.
    for i in range(12):
        db.ingest_match(_board(f"B_{i}", [GROMP, NIDALEE], 1 + i % 4))
    # Draven + Gromp on only 9 boards: below the floor.
    for i in range(9):
        db.ingest_match(_board(f"C_{i}", [DRAVEN, GROMP], 4))
    # Karma without Draven on 3 winning boards: Karma's own numbers now differ
    # from the Draven + Karma core's.
    for i in range(3):
        db.ingest_match(_board(f"K_{i}", [KARMA], 1))
    # Another balance window: Draven + Karma winning 15 times. Must not leak in.
    for i in range(15):
        db.ingest_match(_board(f"OLD_{i}", [DRAVEN, KARMA], 1, version=OTHER_VERSION))
    # A 3-board carry for the empty states.
    for i in range(3):
        db.ingest_match(_board(f"DRV_{i}", [KARMA, VI], 2, carry=DRAVEN))


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        database = Database(tmp_path / "cores.sqlite3")
    else:
        database = Database(POSTGRES_TEST_URL)
        for table in ("discovery_prepared_candidates", "discovery_prepared_runs", "traits", "units",
                      "participants", "matches"):
            database.execute(f"DELETE FROM {table}")
        database.commit()
    _seed(database)
    database.commit()
    yield database
    database.close()


def _cores(db: Database) -> dict:
    return champion_investigation(db, KOG, WINDOW)["cores"]


def _members(row) -> tuple[str, ...]:
    return tuple(row["member_ids"])


def test_payload_counts_whole_cores_within_one_window_and_hides_summons(db: Database) -> None:
    cores = _cores(db)
    assert cores["carry_boards"] == 44 and cores["min_boards"] == htp.MIN_BOARDS == 10
    three = {_members(r): r for r in cores["three_unit"]}
    a = (KOGMAW, *sorted([DRAVEN, KARMA]))
    assert three[a]["games"] == 20  # duplicate Draven copies don't double it; other window ignored
    assert all(SUMMON not in r["member_ids"] for r in [*cores["three_unit"], *cores["four_unit"]])
    assert [_members(r) for r in cores["four_unit"]] == [(KOGMAW, *sorted([DRAVEN, KARMA, VI]))]
    assert cores["subset_of_final_board"] is True and cores["evidence"] == "observed"
    assert "exact_board" not in str(cores) and "not an exact composition" in cores["definition"]


def test_the_ten_board_floor_applies(db: Database) -> None:
    cores = _cores(db)
    members = {_members(r) for r in cores["three_unit"]}
    assert (KOGMAW, *sorted([DRAVEN, GROMP])) not in members  # 9 boards
    assert all(r["games"] >= 10 for r in [*cores["three_unit"], *cores["four_unit"]])


def test_recurrence_orders_cores_even_when_a_rarer_core_placed_better(db: Database) -> None:
    three = _cores(db)["three_unit"]
    a, b = three[0], three[1]
    assert set(a["member_ids"]) == {KOGMAW, DRAVEN, KARMA} and set(b["member_ids"]) == {KOGMAW, GROMP, NIDALEE}
    assert a["games"] > b["games"] and a["top4_with"] < b["top4_with"]  # more boards, worse Top 4: still first
    assert [r["games"] for r in three] == sorted((r["games"] for r in three), reverse=True)


def test_core_results_and_without_side_use_boards_with_the_entire_core(db: Database) -> None:
    a = _cores(db)["three_unit"][0]
    # Its own 20 boards: placements 5, 6, 7, 8 repeating.
    assert (a["games"], a["top4_with"], a["win_with"], a["avg_placement_with"]) == (20, 0.0, 0.0, 6.5)
    assert round(a["share_of_carry_games"], 4) == round(20 / 44, 4)
    # Without = the other 24 boards, including the 9 with Draven but no Karma
    # and the 3 with Karma but no Draven.
    assert a["games_without"] == 24 and a["top4_without"] == 1.0
    assert a["avg_placement_without"] == pytest.approx((12 * 2.5 + 9 * 4 + 3 * 1) / 24)
    # Not a combination of the members' own pair numbers.
    pairs = {p.key: p for p in carry_partner_associations(db, KOGMAW, WINDOW)}
    assert pairs[DRAVEN].games == 29 and pairs[KARMA].games == 23
    assert a["adjusted_top4_difference"] not in {
        pairs[DRAVEN].top4_delta, pairs[KARMA].top4_delta,
        (pairs[DRAVEN].top4_delta or 0) + (pairs[KARMA].top4_delta or 0)}


def test_individual_teammate_evidence_is_unchanged(db: Database) -> None:
    body = champion_investigation(db, KOG, WINDOW)
    expected = [_partner_row(a) for a in carry_partner_associations(db, KOGMAW, WINDOW)][:6]
    assert body["partners"] == expected


def test_units_resolve_names_slugs_and_art_with_the_carry_first(db: Database) -> None:
    row = _cores(db)["four_unit"][0]
    carry, *mates = row["units"]
    assert carry["character_id"] == KOGMAW and carry["slug"] == "kogmaw"
    names = [m["name"] for m in mates]
    assert set(names) == {"Draven", "Karma", "Vi"}
    for m in mates:
        assert m["slug"] == champion_slug(m["character_id"], m["name"])
        assert m["art_url"] and m["art_url"].startswith("/static/game/champions/")
        assert m["cost"] == ROSTER.champions[m["character_id"]]["cost"]
    assert [(m["cost"], m["name"]) for m in mates] == sorted((m["cost"], m["name"]) for m in mates)


def test_how_to_play_shows_three_and_four_unit_cores_and_never_two_unit_ones(db: Database) -> None:
    h = champion_investigation(db, KOG, WINDOW)["how_to_play"]
    shown = h["recurring_cores"]
    assert [r["size"] for r in shown] == [4, 3, 3]  # up to 2 four-unit, then 3-unit, most common first
    assert set(shown[1]["member_ids"]) == {KOGMAW, DRAVEN, KARMA}
    assert all(r["size"] in (3, 4) for r in shown)


def test_low_sample_carry_gets_honest_empty_states(db: Database) -> None:
    draven = {"character_id": DRAVEN, "name": "Draven", "slug": "draven", "cost": 5, "art_url": None}
    body = champion_investigation(db, draven, WINDOW)
    assert body["cores"]["three_unit"] == body["cores"]["four_unit"] == []
    assert body["how_to_play"]["recurring_cores"] == []


def test_no_carry_boards_means_no_cores(db: Database) -> None:
    karma = {"character_id": KARMA, "name": "Karma", "slug": "karma", "cost": 1, "art_url": None}
    assert champion_investigation(db, karma, WINDOW)["cores"] is None


def test_one_partner_board_query_feeds_partners_and_cores(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    original = Database.query_all

    def spy(self, sql, *args, **kwargs):
        seen.append(sql)
        return original(self, sql, *args, **kwargs)

    monkeypatch.setattr(Database, "query_all", spy)
    champion_investigation(db, KOG, WINDOW)
    assert sum("LEFT JOIN units f" in sql for sql in seen) == 1


def test_concise_quota_is_a_fixed_presentation_rule() -> None:
    four = [{"size": 4, "games": g} for g in (30, 20, 15)]
    three = [{"size": 3, "games": g} for g in (60, 40)]
    assert [r["games"] for r in htp.concise_cores(four, three)] == [30, 20, 60]
    assert [r["games"] for r in htp.concise_cores([], three)] == [60, 40]
    assert [r["games"] for r in htp.concise_cores(four[:1], [])] == [30]


def test_page_labels_cores_as_recurring_subsets_never_strength() -> None:
    js = (Path(__file__).parents[1] / "src" / "tftlab" / "web" / "static" / "champion.js").read_text()
    for needle in ("Recurring cores", "Recurring 3–4 unit cores", "Individual teammates",
                   "Other units were also present; these are not exact compositions.",
                   "No recurring 3–4 unit core has enough boards yet."):
        assert needle in js, needle
    core_ui = js[js.index("const CORE_NOTE"):js.index("// Traits around the carry")]
    for word in ("best", "strong", "recommend", "optimal", "score"):
        assert word not in core_ui.lower(), word


def test_rare_member_pruning_never_changes_the_result() -> None:
    """Dropping teammates below the floor before enumerating is exact: same
    cores, same numbers as enumerating everything and filtering afterwards."""
    import random

    rng = random.Random(7)
    pool = [f"U{i:02d}" for i in range(14)]
    weights = [1 / (i + 1) for i in range(len(pool))]
    games = [(rng.randint(1, 8), frozenset(rng.choices(pool, weights=weights, k=6))) for _ in range(160)]

    def everyone(_: str) -> bool:
        return True

    pruned = recurring_cores(games, eligible=everyone, min_games=10)
    brute = [c for c in recurring_cores(games, eligible=everyone, min_games=1) if c.evidence.games >= 10]
    assert pruned and [(c.size, c.members, c.evidence) for c in pruned] == [(c.size, c.members, c.evidence) for c in brute]
