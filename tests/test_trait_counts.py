"""Traits around a carry use Riot's own observed unit count (`num_units`).

Grouping is `trait_name` + `num_units`; `tier_current` only decides whether a
trait is active and is never shown or translated into a threshold. These
tests pin the counts, shares, placements and ordering on a hand-built
fixture, on SQLite and (when `TFTLAB_TEST_DATABASE_URL` is set) Postgres.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from _helpers import make_match, make_unit
from tftlab.analytics import trait_count_associations, trait_profile
from tftlab.champion_investigation import carry_summary, champion_investigation
from tftlab.storage import Database

CARRY = "TFT14_Carry"
WINDOW = "14.6"
V_OTHER = "Version 14.7.580.4321 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
ITEMS = ["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]
POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


def _trait(name: str, num_units: int, tier_current: int) -> dict:
    return {"name": name, "num_units": num_units, "style": 1 if tier_current else 0,
            "tier_current": tier_current, "tier_total": 3}


def _seed(db: Database) -> None:
    """Five carry boards in 14.6 and one in 14.7.

    B1 1st  Solar 4 (tier 1), Blackthorn 2
    B2 3rd  Solar 4 (tier 1), Arcanist 2
    B3 5th  Solar 5 (tier 1: same tier as 4 units), Arcanist 2
    B4 8th  Solar 2 inactive (tier 0), Blackthorn 2
    B5 2nd  Solar 6 (tier 2), two rows of the carry on the board
    X  8th  Solar 4, other balance window: never counted
    """
    boards = [
        ("B1", 1, [_trait("Solar", 4, 1), _trait("Blackthorn", 2, 1)], 1),
        ("B2", 3, [_trait("Solar", 4, 1), _trait("Arcanist", 2, 1)], 1),
        ("B3", 5, [_trait("Solar", 5, 1), _trait("Arcanist", 2, 1)], 1),
        ("B4", 8, [_trait("Solar", 2, 0), _trait("Blackthorn", 2, 1)], 1),
        ("B5", 2, [_trait("Solar", 6, 2)], 2),
    ]
    for match_id, placement, traits, copies in boards:
        db.ingest_match(make_match(
            match_id, placement=placement, traits=traits,
            units=[make_unit(CARRY, tier=2, items=ITEMS) for _ in range(copies)],
        ))
    db.ingest_match(make_match(
        "X", placement=8, traits=[_trait("Solar", 4, 1)], game_version=V_OTHER,
        units=[make_unit(CARRY, tier=2, items=ITEMS)],
    ))


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest, tmp_path: Path):
    database = Database(tmp_path / "traits.sqlite3") if request.param == "sqlite" else Database(POSTGRES_TEST_URL)
    if request.param == "postgres":
        for table in ("traits", "units", "participants", "matches"):
            database.execute(f"DELETE FROM {table}")
        database.commit()
    _seed(database)
    yield database
    database.close()


def _by_key(rows):
    return {a.key: a for a in rows}


def test_counts_come_from_num_units_and_stay_distinct(db: Database) -> None:
    profile = trait_profile(db, CARRY, WINDOW)
    solar = [a.key for a in profile.counts["Solar"]]
    # 4 and 5 units share tier_current 1 and would merge under a tier key;
    # the inactive 2-unit Solar (tier 0) is not an observed active count.
    assert solar == ["Solar:4", "Solar:5", "Solar:6"]
    assert "Solar:1" not in solar and "Solar:2" not in solar
    assert set(_by_key(trait_count_associations(db, CARRY, WINDOW, min_games=1))) == {
        "Solar:4", "Solar:5", "Solar:6", "Blackthorn:2", "Arcanist:2"
    }


def test_board_counts_shares_placement_and_top4_are_exact(db: Database) -> None:
    profile = trait_profile(db, CARRY, WINDOW)
    # One balance window, and B5's two carry rows are one board.
    assert profile.carry_boards == 5

    four = _by_key(profile.counts["Solar"])["Solar:4"]
    assert (four.games, four.inclusion_rate) == (2, 0.4)
    assert (four.avg_placement, four.top4_rate, four.win_rate) == (2.0, 1.0, 0.5)
    # Without: B3 (5th), B4 (8th), B5 (2nd).
    assert four.games_without == 3
    assert four.avg_placement_without == pytest.approx(5.0)
    assert four.top4_rate_without == pytest.approx(1 / 3)

    six = _by_key(profile.counts["Solar"])["Solar:6"]
    assert (six.games, six.avg_placement, six.top4_rate, six.win_rate) == (1, 2.0, 1.0, 0.0)


def test_active_aggregate_counts_are_exact_and_reconcile_with_counts(db: Database) -> None:
    profile = trait_profile(db, CARRY, WINDOW)
    active = _by_key(profile.active)
    solar = active["Solar"]
    assert (solar.games, solar.inclusion_rate) == (4, 0.8)  # B1, B2, B3, B5; B4's Solar was inactive
    assert solar.avg_placement == pytest.approx(2.75)
    assert (solar.top4_rate, solar.win_rate) == (0.75, 0.25)
    assert (solar.games_without, solar.top4_rate_without) == (1, 0.0)
    for name, a in active.items():
        # every active board has exactly one count of that trait
        assert sum(c.games for c in profile.counts[name]) == a.games
    # the same board counts under every trait it had active
    assert sum(a.games for a in profile.active) > profile.carry_boards


def test_traits_are_ordered_by_frequency_then_name_and_counts_by_units(db: Database) -> None:
    profile = trait_profile(db, CARRY, WINDOW)
    # Solar (4) first; Arcanist and Blackthorn tie at 2 and fall back to name.
    assert [a.key for a in profile.active] == ["Solar", "Arcanist", "Blackthorn"]


def test_duplicate_unit_rows_do_not_inflate_board_counts(db: Database) -> None:
    six = _by_key(trait_profile(db, CARRY, WINDOW).counts["Solar"])["Solar:6"]
    assert six.games == 1


def test_investigation_payload_and_summary_use_unit_counts(db: Database) -> None:
    champion = {"character_id": CARRY, "name": "Carry", "slug": "carry", "cost": 1, "art_url": None}
    body = champion_investigation(db, champion, WINDOW)
    assert body["carry"]["games"] == 5
    traits = body["traits"]
    assert [t["trait_id"] for t in traits] == ["Solar", "Arcanist", "Blackthorn"]
    solar = traits[0]
    assert (solar["games"], solar["share_of_carry_games"], solar["top4_with"]) == (4, 0.8, 0.75)
    assert [(c["num_units"], c["games"]) for c in solar["counts"]] == [(4, 2), (5, 1), (6, 1)]
    assert all("tier" not in row for t in traits for row in [t, *t["counts"]])

    observed = body["summary"]["observed"]
    assert (
        "Most common active trait: Solar, active on 80.0% of carry boards (4 of 5); "
        "most often at 4 units (2 carry boards), then 5 units (1 carry board)."
    ) in observed
    assert "Next most common active traits: Arcanist (40.0%), Blackthorn (40.0%)." in observed
    text = " ".join(observed + body["summary"]["interpretation"]).lower()
    assert "breakpoint" not in text and "tier" not in text


def test_one_trait_query_per_investigation(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.analytics.traits as traits_module

    calls = []
    original = traits_module._trait_boards_for
    monkeypatch.setattr(traits_module, "_trait_boards_for", lambda *a, **k: calls.append(a[1]) or original(*a, **k))
    champion = {"character_id": CARRY, "name": "Carry", "slug": "carry", "cost": 1, "art_url": None}
    champion_investigation(db, champion, WINDOW)
    assert calls == [[CARRY]]


def test_discovery_trait_evidence_uses_unit_counts_and_never_moves_the_score(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tftlab.analytics.discovery as discovery

    candidates = discovery.discover_candidates(db, balance_window=WINDOW, min_cost=1, max_cost=5, min_samples=1)
    carry = next(c for c in candidates if c.character_id == CARRY)
    assert carry.best_trait_breakpoints == trait_count_associations(db, CARRY, WINDOW)[: len(carry.best_trait_breakpoints)]
    assert {a.key for a in carry.best_trait_breakpoints} <= {"Solar:4", "Arcanist:2", "Blackthorn:2"}

    # Trait evidence is shown next to the Opportunity Score but is not an input to it.
    monkeypatch.setattr(discovery, "trait_count_associations_for_many", lambda _db, ids, _w, **_k: {i: [] for i in ids})
    without_traits = discovery.discover_candidates(db, balance_window=WINDOW, min_cost=1, max_cost=5, min_samples=1)
    assert [(c.character_id, c.opportunity_score) for c in without_traits] == [
        (c.character_id, c.opportunity_score) for c in candidates
    ]


# ---------------------------------------------------------------- summary rules (no database)


def _count(name, n, games, share, with_, without, score, games_without=50, limited=False):
    return {"trait_id": name, "name": name, "num_units": n, "games": games, "share_of_carry_games": share,
            "top4_with": with_, "top4_without": without, "adjusted_top4_difference": score,
            "games_without": games_without, "limited_sample": limited}


def _active(name, games, share, counts):
    return {"trait_id": name, "name": name, "games": games, "share_of_carry_games": share, "counts": counts,
            "top4_with": 0.5, "top4_without": 0.5, "adjusted_top4_difference": 0.0, "games_without": 10,
            "limited_sample": False}


def _carry(n=200):
    return {"games": n, "three_star": {"hit_games": 0, "miss_games": n, "hit_rate": 0.0,
                                       "hit_top4_rate": None, "miss_top4_rate": 0.5}}


CAUSAL = ("causes", "caused by", "because of", "leads to", "makes you", "improves", "guarantee", "breakpoint")


def test_trait_summary_is_source_led_and_names_no_winning_count() -> None:
    traits = [
        _active("Solar", 150, 0.75, [
            _count("Solar", 4, 100, 0.5, 0.62, 0.48, 0.05),
            _count("Solar", 5, 40, 0.2, 0.70, 0.50, 0.08),
            _count("Solar", 6, 10, 0.05, 0.9, 0.5, 0.01, limited=True),
        ]),
        _active("Hunter", 90, 0.45, [_count("Hunter", 2, 90, 0.45, 0.9, 0.3, 0.3)]),
        _active("Vanguard", 40, 0.2, [_count("Vanguard", 2, 40, 0.2, 0.5, 0.5, 0.0)]),
    ]
    s = carry_summary("X", _carry(), [], [], traits)
    trait_lines = [l for l in s["observed"] if "trait" in l.lower()]
    # Frequency and Riot's observed unit counts only: no line picks a winning
    # trait/count out of the many with-vs-without comparisons, however strong.
    assert trait_lines == [
        "Most common active trait: Solar, active on 75.0% of carry boards (150 of 200); "
        "most often at 4 units (100 carry boards), then 5 units (40 carry boards).",
        "Next most common active traits: Hunter (45.0%), Vanguard (20.0%).",
    ]
    assert not any("strongest" in l.lower() or "best" in l.lower() for l in trait_lines)
    assert any("associations, not a proven core" in l for l in s["interpretation"])
    text = " ".join(s["observed"] + s["interpretation"]).lower()
    assert not any(w in text for w in CAUSAL)


def test_trait_rows_keep_their_with_vs_without_numbers(db: Database) -> None:
    champion = {"character_id": CARRY, "name": "Carry", "slug": "carry", "cost": 1, "art_url": None}
    body = champion_investigation(db, champion, WINDOW)
    four = next(c for c in body["traits"][0]["counts"] if c["num_units"] == 4)
    assert (four["top4_with"], four["games_without"], four["avg_placement_without"]) == (1.0, 3, 5.0)
    assert four["top4_without"] == pytest.approx(1 / 3)
    assert not any("trait count" in l for l in body["summary"]["observed"])


# ---------------------------------------------------------------- page text

WEB = Path(__file__).parent.parent / "src" / "tftlab" / "web"


def test_no_breakpoint_ordinal_labels_remain_in_the_pages() -> None:
    champion_js = (WEB / "static" / "champion.js").read_text()
    app_js = (WEB / "static" / "app.js").read_text().replace("best_trait_breakpoints", "")
    for text in (champion_js, app_js, (WEB / "index.html").read_text(), (WEB / "champion.html").read_text()):
        assert "breakpoint" not in text.lower()
        assert not re.search(r"row\.tier|\.tier\b|traitTier", text)
    assert "Traits around" in champion_js and "'unit'" in champion_js
