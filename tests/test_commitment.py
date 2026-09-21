from pathlib import Path

from tftlab.analytics import carry_commitment_stats
from tftlab.storage import Database

from _helpers import make_match, make_unit


def _build_scenario(db: Database) -> None:
    # 6 "hit" games (>=2 items AND reached 3-star): placements 1,2,3,4,5,6.
    hit_placements = [1, 2, 3, 4, 5, 6]
    for i, placement in enumerate(hit_placements):
        db.ingest_match(
            make_match(
                f"HIT_{i}",
                placement=placement,
                units=[
                    make_unit(
                        "TFT14_Foo",
                        rarity=0,
                        tier=3,
                        items=["TFT_Item_BlueBuff", "TFT_Item_JeweledGauntlet"],
                    )
                ],
            )
        )

    # 4 "miss" games (>=2 items but stayed 2-star): placements 2,5,6,7.
    miss_placements = [2, 5, 6, 7]
    for i, placement in enumerate(miss_placements):
        db.ingest_match(
            make_match(
                f"MISS_{i}",
                placement=placement,
                units=[
                    make_unit(
                        "TFT14_Foo",
                        rarity=0,
                        tier=2,
                        items=["TFT_Item_BlueBuff", "TFT_Item_JeweledGauntlet"],
                    )
                ],
            )
        )

    # 2 non-commitment appearances (<2 completed items): must count toward
    # `appearances`/usage but NOT toward commitment_games, hit/miss rates, or
    # avg_placement, regardless of star level.
    db.ingest_match(
        make_match(
            "NONCOMMIT_0",
            placement=8,
            units=[make_unit("TFT14_Foo", rarity=0, tier=2, items=["TFT_Item_BFSword"])],
        )
    )
    db.ingest_match(
        make_match(
            "NONCOMMIT_1",
            placement=4,
            units=[make_unit("TFT14_Foo", rarity=0, tier=1, items=[])],
        )
    )

    # 8 filler-only participants (no Foo at all) so usage_rate has a
    # meaningful denominator distinct from Foo's own appearance count.
    for i in range(8):
        db.ingest_match(
            make_match(
                f"FILLER_{i}",
                placement=5,
                units=[make_unit("TFT14_Bar", rarity=2, tier=2, items=[])],
            )
        )


def test_carry_commitment_hit_miss_logic(tmp_path: Path) -> None:
    with Database(tmp_path / "commitment.sqlite3") as db:
        _build_scenario(db)
        stats = carry_commitment_stats(db, min_cost=1, max_cost=3, min_samples=1)

    foo = next(s for s in stats if s.character_id == "TFT14_Foo")

    # Commitment is items-only: 2 completed items counts even on a 2-star miss.
    assert foo.commitment_games == 10
    assert foo.appearances == 12  # 10 commitment + 2 non-commitment appearances
    # usage_rate is commitment usage (n / total participants), not raw pickup rate.
    assert foo.usage_rate == 10 / 20  # 20 total participants across all matches

    assert foo.hit_3star_rate == 6 / 10
    assert foo.hit_top4_rate == 4 / 6  # placements 1,2,3,4 of the 6 hits
    assert foo.miss_top4_rate == 1 / 4  # placement 2 of the 4 misses

    assert foo.avg_placement_hit == sum([1, 2, 3, 4, 5, 6]) / 6
    assert foo.avg_placement_miss == sum([2, 5, 6, 7]) / 4
    # Overall avg_placement covers only commitment games, hit or miss alike.
    assert foo.avg_placement == sum([1, 2, 3, 4, 5, 6, 2, 5, 6, 7]) / 10

    assert foo.top4_rate == 5 / 10  # 4 hit top4s + 1 miss top4
    assert foo.win_rate == 1 / 10  # only placement-1 hit game
    assert 0.0 <= foo.opportunity_score <= 100.0


def test_non_commitment_games_excluded_from_hit_miss(tmp_path: Path) -> None:
    """A 3-star with <2 completed items must not count as a commitment hit."""
    with Database(tmp_path / "noncommit.sqlite3") as db:
        db.ingest_match(
            make_match(
                "THREE_STAR_NO_ITEMS",
                placement=1,
                units=[make_unit("TFT14_Baz", rarity=0, tier=3, items=[])],
            )
        )
        stats = carry_commitment_stats(db, min_cost=1, max_cost=3, min_samples=1)

    # Zero commitment games means it's filtered out entirely rather than
    # reported with a bogus (division-by-zero-prone) 3-star hit rate.
    assert all(s.character_id != "TFT14_Baz" for s in stats)
