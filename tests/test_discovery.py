from pathlib import Path

import pytest

from tftlab.analytics import CarryStat, discover_candidates
from tftlab.analytics.discovery import OPPORTUNITY_WEIGHTS, compute_opportunity_score
from tftlab.storage import Database

from _helpers import make_match, make_unit


def _carry_stat(**overrides) -> CarryStat:
    defaults = dict(
        character_id="TFT14_Test",
        name="Test",
        cost=1,
        appearances=20,
        commitment_games=20,
        usage_rate=0.05,
        avg_placement=4.0,
        top4_rate=0.5,
        win_rate=0.1,
        hit_3star_rate=0.5,
        hit_top4_rate=0.5,
        miss_top4_rate=0.5,
        avg_placement_hit=4.0,
        avg_placement_miss=4.0,
        posterior_top4=0.5,
        confidence=0.25,
        opportunity_score=0.0,
    )
    defaults.update(overrides)
    return CarryStat(**defaults)


def test_opportunity_weights_sum_to_one() -> None:
    assert sum(OPPORTUNITY_WEIGHTS.values()) == pytest.approx(1.0)


def test_compute_opportunity_score_components_are_independently_inspectable() -> None:
    stat = _carry_stat(
        cost=1,
        usage_rate=0.01,
        posterior_top4=0.6,
        hit_top4_rate=0.7,
        miss_top4_rate=0.5,
        confidence=0.4,
    )
    score, components = compute_opportunity_score(
        stat, population_avg_top4=0.5, best_partner_score=0.05, item_flexibility=0.5
    )
    assert set(components) == set(OPPORTUNITY_WEIGHTS)
    assert all(0.0 <= v <= 1.0 for v in components.values())
    assert 0.0 <= score <= 100.0
    # A hand-checkable component: floor_ceiling is the plain average of hit
    # and miss Top4 rates.
    assert components["floor_ceiling"] == pytest.approx(0.5 * 0.7 + 0.5 * 0.5)
    # cost_bias favors 1-3 cost over 4-5 cost, all else equal.
    high_cost_score, high_cost_components = compute_opportunity_score(
        _carry_stat(cost=5, usage_rate=0.01, posterior_top4=0.6, hit_top4_rate=0.7, miss_top4_rate=0.5, confidence=0.4),
        population_avg_top4=0.5,
        best_partner_score=0.05,
        item_flexibility=0.5,
    )
    assert components["cost_bias"] > high_cost_components["cost_bias"]
    assert score > high_cost_score


def _seed_survivorship_scenario(db: Database) -> None:
    """Two cost-1 carries, each with 20 commitment games and identical usage:

    - Volatile: spectacular on its 2 hit games (placement 1), terrible on
      its 18 miss games (placement 8). Looks amazing "when it hits", but is
      genuinely bad overall (top4_rate 10%).
    - Solid: an even, unspectacular placement=3 (top4) every single game,
      hit or miss alike. Never flashy, but consistently fine (top4_rate 100%).
    """
    for i in range(2):
        db.ingest_match(
            make_match(
                f"VOLATILE_HIT_{i}",
                placement=1,
                units=[make_unit("TFT14_Volatile", rarity=0, tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )
    for i in range(18):
        db.ingest_match(
            make_match(
                f"VOLATILE_MISS_{i}",
                placement=8,
                units=[make_unit("TFT14_Volatile", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )
    for i in range(10):
        db.ingest_match(
            make_match(
                f"SOLID_HIT_{i}",
                placement=3,
                units=[make_unit("TFT14_Solid", rarity=0, tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )
    for i in range(10):
        db.ingest_match(
            make_match(
                f"SOLID_MISS_{i}",
                placement=3,
                units=[make_unit("TFT14_Solid", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )


def test_survivorship_bias_carry_ranks_below_consistent_carry(tmp_path: Path) -> None:
    with Database(tmp_path / "survivorship.sqlite3") as db:
        _seed_survivorship_scenario(db)
        candidates = discover_candidates(db, min_cost=1, max_cost=3, min_samples=1)

    by_id = {c.character_id: c for c in candidates}
    volatile = by_id["TFT14_Volatile"]
    solid = by_id["TFT14_Solid"]

    # The trap: looking only at hit games, Volatile appears flawless.
    assert volatile.hit_top4_rate == 1.0
    assert volatile.miss_top4_rate == 0.0
    # But its blended, all-commitment-games performance is genuinely bad --
    # this is the number a naive "only look at hit games" read would miss.
    assert volatile.top4_rate == pytest.approx(0.10)
    assert solid.top4_rate == pytest.approx(1.0)

    # The floor/ceiling component (every component is inspectable on the
    # candidate itself) must reflect the inconsistency distinctly: 100% on
    # hits and 0% on misses averages to a middling 0.5, not the flattering
    # 1.0 a "just look at hit games" read would suggest.
    assert volatile.opportunity_components["floor_ceiling"] == pytest.approx(0.5)
    assert solid.opportunity_components["floor_ceiling"] == pytest.approx(1.0)

    # Whatever the exact weighting, the overall score must not let the
    # "great when it hits" carry outrank the genuinely consistent one.
    assert solid.opportunity_score > volatile.opportunity_score
    ranked_ids = [c.character_id for c in candidates]
    assert ranked_ids.index("TFT14_Solid") < ranked_ids.index("TFT14_Volatile")


def test_low_usage_carry_with_evidence_surfaces_above_common_carry(tmp_path: Path) -> None:
    """A rare, well-evidenced carry should outrank a very common carry with
    similar raw performance -- rarity/discovery value, not just raw stats."""
    with Database(tmp_path / "rarity.sqlite3") as db:
        # 1000 filler participants nobody would call a "carry" (no completed
        # items), establishing a large population denominator.
        for i in range(1000):
            db.ingest_match(
                make_match(f"FILLER_{i}", placement=5, units=[make_unit("TFT14_Filler", tier=1, items=[])])
            )
        # Rare: only 15 commitment games (1.5% usage), 60% top4.
        for i in range(9):
            db.ingest_match(
                make_match(f"RARE_TOP4_{i}", placement=3, units=[make_unit("TFT14_Rare", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])])
            )
        for i in range(6):
            db.ingest_match(
                make_match(f"RARE_MISS_{i}", placement=6, units=[make_unit("TFT14_Rare", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])])
            )
        # Common: 100 commitment games (10% usage, above the rarity cap), same 60% top4.
        for i in range(60):
            db.ingest_match(
                make_match(f"COMMON_TOP4_{i}", placement=3, units=[make_unit("TFT14_Common", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])])
            )
        for i in range(40):
            db.ingest_match(
                make_match(f"COMMON_MISS_{i}", placement=6, units=[make_unit("TFT14_Common", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])])
            )

        candidates = discover_candidates(db, min_cost=1, max_cost=3, min_samples=1)

    by_id = {c.character_id: c for c in candidates}
    rare, common = by_id["TFT14_Rare"], by_id["TFT14_Common"]

    assert rare.top4_rate == pytest.approx(common.top4_rate, abs=0.01)
    assert rare.usage_rate < common.usage_rate
    assert rare.opportunity_score > common.opportunity_score
