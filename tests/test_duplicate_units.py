"""Regression coverage for the second live-ingest failure: a real TFT board
fielded two instances of the same champion in one game, and the old `units`
primary key -- `(match_id, participant_index, character_id)` -- rejected the
second instance outright with a `UniqueViolation`. `unit_index` fixes
storage identity (see `storage.TABLES_SQL`); this file proves every
analytics query that reads `units` still counts each participant/game
exactly once even when a board has duplicate champion instances.
"""

from pathlib import Path

import pytest

from tftlab.analytics import carry_commitment_stats, carry_partner_associations
from tftlab.analytics.item_packages import _carry_commitment_item_games
from tftlab.analytics.partners import _carry_commitment_games_with_partners
from tftlab.analytics.traits import _carry_commitment_trait_games
from tftlab.storage import Database

from _helpers import make_match, make_unit

CARRY = "TFT14_DupCarry"
PARTNER = "TFT14_Buddy"
BALANCE_WINDOW = "14.6"  # matches make_match's default game_version's patch


def _seed_duplicate_champion_scenario(db: Database) -> None:
    """Mirrors the exact structural situation that broke the second live
    ingest run: one participant fields two instances of the same champion.
    Both instances here individually satisfy the >=2-item commitment
    threshold -- the worst case for any per-champion aggregation that
    doesn't first collapse rows by (match_id, participant_index,
    character_id) before counting appearances/commitment/hits."""
    db.ingest_match(
        make_match(
            "DUP_GAME",
            placement=2,
            units=[
                # Richer instance: 3 completed items, tier 3 -- the
                # canonical pick per CANONICAL_UNIT_TIEBREAK_SQL.
                make_unit(
                    CARRY,
                    tier=3,
                    items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap", "TFT_Item_JeweledGauntlet"],
                ),
                # Second, independently-committed instance of the SAME
                # champion on the SAME board -- this is what UniqueViolation'd
                # under the old (match_id, participant_index, character_id) key.
                make_unit(CARRY, tier=2, items=["TFT_Item_GuinsoosRageblade", "TFT_Item_RunaansHurricane"]),
                make_unit(PARTNER, tier=2, items=[]),
            ],
            traits=[{"name": "Arcanist", "num_units": 4, "style": 2, "tier_current": 1, "tier_total": 3}],
        )
    )
    # Baseline single-instance commitment games: gives commitment_games/
    # appearances a real denominator beyond the one duplicate game, and
    # gives the partner association a "without" baseline to contrast against.
    for i, placement in enumerate([5, 6, 7]):
        db.ingest_match(
            make_match(
                f"SOLO_{i}",
                placement=placement,
                units=[make_unit(CARRY, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )


def test_ingest_succeeds_and_stores_both_unit_instances(tmp_path: Path) -> None:
    with Database(tmp_path / "dup_ingest.sqlite3") as db:
        inserted = db.ingest_match(
            make_match(
                "DUP_ONLY",
                units=[
                    make_unit(CARRY, tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit(CARRY, tier=1, items=[]),
                ],
            )
        )
        rows = db.query_all(
            "SELECT unit_index, character_id, tier FROM units WHERE match_id = ? ORDER BY unit_index",
            ("DUP_ONLY",),
        )

    assert inserted is True
    assert [r[1] for r in rows] == [CARRY, CARRY]
    assert [r[0] for r in rows] == [0, 1]  # both copies stored, distinct unit_index


def test_champion_appearance_and_commitment_count_once_per_game(tmp_path: Path) -> None:
    with Database(tmp_path / "dup_commitment.sqlite3") as db:
        _seed_duplicate_champion_scenario(db)
        stats = carry_commitment_stats(db, min_cost=1, max_cost=5, min_samples=1)

    carry_stat = next(s for s in stats if s.character_id == CARRY)
    # 1 duplicate game + 3 solo games = 4 -- never 5, which a naive
    # per-unit-row count (one row per champion instance) would give.
    assert carry_stat.appearances == 4
    assert carry_stat.commitment_games == 4


def test_placement_and_top4_counted_once_for_the_duplicate_game(tmp_path: Path) -> None:
    with Database(tmp_path / "dup_placement.sqlite3") as db:
        _seed_duplicate_champion_scenario(db)
        stats = carry_commitment_stats(db, min_cost=1, max_cost=5, min_samples=1)

    carry_stat = next(s for s in stats if s.character_id == CARRY)
    # DUP_GAME (placement 2, top4) + SOLO_0/1/2 (placements 5,6,7, not top4).
    # A double-counted DUP_GAME would corrupt both the top4 fraction's
    # denominator and the placement average.
    assert carry_stat.top4_rate == pytest.approx(1 / 4)
    assert carry_stat.avg_placement == pytest.approx((2 + 5 + 6 + 7) / 4)


def test_3star_hit_uses_a_relevant_committed_instance(tmp_path: Path) -> None:
    """The richer DUP_GAME instance is tier 3; the other committed instance
    of the same champion in that game is only tier 2. The champion must
    still be counted as a 3-star hit for that game (a relevant -- i.e.
    committed -- instance reached tier 3), not excluded or double-counted."""
    with Database(tmp_path / "dup_hit.sqlite3") as db:
        _seed_duplicate_champion_scenario(db)
        stats = carry_commitment_stats(db, min_cost=1, max_cost=5, min_samples=1)

    carry_stat = next(s for s in stats if s.character_id == CARRY)
    assert carry_stat.hit_games == 1  # only DUP_GAME reaches tier 3
    assert carry_stat.miss_games == 3  # the 3 solo (tier 2) games
    assert carry_stat.avg_placement_hit == pytest.approx(2.0)


def test_partner_association_does_not_duplicate_the_game(tmp_path: Path) -> None:
    with Database(tmp_path / "dup_partners.sqlite3") as db:
        _seed_duplicate_champion_scenario(db)
        games, _, _ = _carry_commitment_games_with_partners(db, CARRY, BALANCE_WINDOW)
        associations = carry_partner_associations(db, CARRY, BALANCE_WINDOW, min_games=1)

    # Exactly 4 commitment games total, not 5 (DUP_GAME's two independently
    # committed carry instances must not each contribute a game).
    assert len(games) == 4
    buddy = next(a for a in associations if a.key == PARTNER)
    assert buddy.games == 1  # the partner is on the board in exactly 1 game


def test_item_package_produces_one_canonical_carry_game_observation(tmp_path: Path) -> None:
    with Database(tmp_path / "dup_items.sqlite3") as db:
        _seed_duplicate_champion_scenario(db)
        item_games = _carry_commitment_item_games(db, CARRY, BALANCE_WINDOW)

    # 1 duplicate game + 3 solo games = 4 item-game observations, not 5.
    assert len(item_games) == 4
    dup_game_items = next(items for placement, items in item_games if placement == 2)
    # Canonical instance = highest completed_item_count (3 beats 2), per
    # CANONICAL_UNIT_TIEBREAK_SQL -- not the tier-3 vs tier-2 comparison,
    # and not both instances' items merged together.
    assert dup_game_items == ("TFT_Item_BlueBuff", "TFT_Item_Deathcap", "TFT_Item_JeweledGauntlet")


def test_trait_breakpoint_games_do_not_duplicate_the_game(tmp_path: Path) -> None:
    with Database(tmp_path / "dup_traits.sqlite3") as db:
        _seed_duplicate_champion_scenario(db)
        games = _carry_commitment_trait_games(db, CARRY, BALANCE_WINDOW)

    assert len(games) == 4
