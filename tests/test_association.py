from pathlib import Path

import pytest

from tftlab.analytics import carry_partner_associations, item_package_stats, trait_breakpoint_associations
from tftlab.analytics.association import compute_associations
from tftlab.storage import Database

from _helpers import make_match, make_unit


def test_compute_associations_empty_games_returns_empty() -> None:
    assert compute_associations([]) == []


def test_compute_associations_key_present_every_game_has_no_without_baseline() -> None:
    # If a key is in 100% of games, there is no "without" group to compare
    # against -- delta/top4_rate_without must be None, not a divide-by-zero.
    games = [(1, frozenset({"X"})), (8, frozenset({"X"}))]
    [assoc] = compute_associations(games)
    assert assoc.games_without == 0
    assert assoc.top4_rate_without is None
    assert assoc.avg_placement_without is None
    assert assoc.top4_delta is None
    assert assoc.avg_placement_delta is None
    assert assoc.association_score == 0.0


def _seed_partner_ranking_scenario(db: Database) -> None:
    """20 commitment games for one carry: a 2-game "perfect" partner and a
    15-game partner with strong-but-imperfect real evidence."""
    # PartnerSmall: 2 games, both wins (looks flawless by raw rate).
    for i in range(2):
        db.ingest_match(
            make_match(
                f"SMALL_{i}",
                placement=1,
                units=[
                    make_unit("TFT14_MainCarry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit("TFT14_PartnerSmall", tier=2, items=[]),
                ],
            )
        )
    # PartnerBig: 15 games, 12 top4 (placement 2) + 3 not (placement 6).
    for i in range(12):
        db.ingest_match(
            make_match(
                f"BIG_HIT_{i}",
                placement=2,
                units=[
                    make_unit("TFT14_MainCarry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit("TFT14_PartnerBig", tier=2, items=[]),
                ],
            )
        )
    for i in range(3):
        db.ingest_match(
            make_match(
                f"BIG_MISS_{i}",
                placement=6,
                units=[
                    make_unit("TFT14_MainCarry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit("TFT14_PartnerBig", tier=2, items=[]),
                ],
            )
        )
    # Neither partner: 3 filler commitment games.
    for i in range(3):
        db.ingest_match(
            make_match(
                f"NEITHER_{i}",
                placement=5,
                units=[make_unit("TFT14_MainCarry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )


def test_small_sample_perfect_partner_does_not_outrank_large_sample_partner(tmp_path: Path) -> None:
    with Database(tmp_path / "partners.sqlite3") as db:
        _seed_partner_ranking_scenario(db)
        associations = carry_partner_associations(db, "TFT14_MainCarry", "14.6")

    by_id = {a.key: a for a in associations}
    small = by_id["TFT14_PartnerSmall"]
    big = by_id["TFT14_PartnerBig"]

    # Raw rate alone would say PartnerSmall (100%) beats PartnerBig (80%).
    assert small.top4_rate == 1.0
    assert big.top4_rate == pytest.approx(12 / 15)

    # But the shrinkage-adjusted, confidence-weighted score must rank the
    # well-evidenced partner above the barely-sampled "perfect" one.
    assert big.association_score > small.association_score
    ranked_ids = [a.key for a in associations]
    assert ranked_ids.index("TFT14_PartnerBig") < ranked_ids.index("TFT14_PartnerSmall")

    # Hand-derived expected values (baseline_top4 = 14/20 = 0.7, prior_strength=20).
    assert small.association_score == pytest.approx(0.00392, abs=1e-4)
    assert big.association_score == pytest.approx(0.02057, abs=1e-4)


def test_tiny_high_roll_item_package_does_not_outrank_larger_sample_package(tmp_path: Path) -> None:
    """Same statistical shape as the partner test, applied to a 3-item package."""
    with Database(tmp_path / "items.sqlite3") as db:
        tiny_package = ["TFT_Item_BlueBuff", "TFT_Item_Deathcap", "TFT_Item_JeweledGauntlet"]
        big_package = ["TFT_Item_BlueBuff", "TFT_Item_Deathcap", "TFT_Item_RabadonsDeathcap"]

        for i in range(2):
            db.ingest_match(
                make_match(f"TINY_{i}", placement=1, units=[make_unit("TFT14_Foo", tier=2, items=tiny_package)])
            )
        for i in range(12):
            db.ingest_match(
                make_match(f"BIGHIT_{i}", placement=2, units=[make_unit("TFT14_Foo", tier=2, items=big_package)])
            )
        for i in range(3):
            db.ingest_match(
                make_match(f"BIGMISS_{i}", placement=6, units=[make_unit("TFT14_Foo", tier=2, items=big_package)])
            )
        for i in range(3):
            db.ingest_match(
                make_match(
                    f"NEITHER_{i}",
                    placement=5,
                    units=[make_unit("TFT14_Foo", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_TearOfTheGoddess2"])],
                )
            )

        stats = item_package_stats(db, "TFT14_Foo", "14.6", min_package_games=1)

    tiny_key = "+".join(sorted(tiny_package))
    big_key = "+".join(sorted(big_package))
    by_id = {a.key: a for a in stats["packages"]}

    assert by_id[tiny_key].top4_rate == 1.0
    assert by_id[big_key].top4_rate == pytest.approx(12 / 15)
    assert by_id[big_key].association_score > by_id[tiny_key].association_score


def test_associations_isolate_by_balance_window(tmp_path: Path) -> None:
    with Database(tmp_path / "windows.sqlite3") as db:
        # Window A: partner is amazing.
        for i in range(6):
            db.ingest_match(
                make_match(
                    f"WINA_{i}",
                    game_version="Version 14.5.1 (Aug 1 2024) [PUBLIC] <Releases/14.5>",
                    placement=1,
                    units=[
                        make_unit("TFT14_Carry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                        make_unit("TFT14_Partner", tier=2, items=[]),
                    ],
                )
            )
        # Window B: same carry+partner combo, but the partner is terrible.
        for i in range(6):
            db.ingest_match(
                make_match(
                    f"WINB_{i}",
                    game_version="Version 14.6.1 (Sep 1 2024) [PUBLIC] <Releases/14.6>",
                    placement=8,
                    units=[
                        make_unit("TFT14_Carry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                        make_unit("TFT14_Partner", tier=2, items=[]),
                    ],
                )
            )

        window_a = carry_partner_associations(db, "TFT14_Carry", "14.5")
        window_b = carry_partner_associations(db, "TFT14_Carry", "14.6")

    assert window_a[0].top4_rate == 1.0
    assert window_b[0].top4_rate == 0.0


def test_trait_breakpoint_associations_scoped_to_balance_window(tmp_path: Path) -> None:
    with Database(tmp_path / "traits.sqlite3") as db:
        for i in range(5):
            db.ingest_match(
                make_match(
                    f"TRAIT_{i}",
                    placement=1,
                    units=[make_unit("TFT14_Carry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                    traits=[{"name": "Juggernaut", "num_units": 4, "style": 3, "tier_current": 2, "tier_total": 3}],
                )
            )
        for i in range(5):
            db.ingest_match(
                make_match(
                    f"NOTRAIT_{i}",
                    placement=8,
                    units=[make_unit("TFT14_Carry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )

        associations = trait_breakpoint_associations(db, "TFT14_Carry", "14.6", min_games=1)

    assert len(associations) == 1
    juggernaut = associations[0]
    assert juggernaut.key == "Juggernaut:2"
    assert juggernaut.games == 5
    assert juggernaut.top4_rate == 1.0
    assert juggernaut.top4_rate_without == 0.0
    assert juggernaut.top4_delta > 0
