from pathlib import Path

import pytest

from tftlab.analytics import Association, CarryStat, compute_item_flexibility, discover_candidates
from tftlab.analytics.discovery import (
    OPPORTUNITY_WEIGHTS,
    _population_baseline_top4,
    _shrunk_floor_ceiling,
    compute_opportunity_score,
)
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
        appearance_rate=0.05,
        commitment_rate=0.05,
        carry_conversion_rate=1.0,
        avg_placement=4.0,
        top4_rate=0.5,
        win_rate=0.1,
        hit_3star_rate=0.5,
        hit_top4_rate=0.5,
        miss_top4_rate=0.5,
        hit_games=10,
        miss_games=10,
        avg_placement_hit=4.0,
        avg_placement_miss=4.0,
        posterior_top4=0.5,
        confidence=0.25,
        opportunity_score=0.0,
    )
    defaults.update(overrides)
    return CarryStat(**defaults)


def _association(key: str, *, games: int, confidence: float, top4_delta: float | None) -> Association:
    return Association(
        key=key,
        label=key,
        cost=None,
        games=games,
        inclusion_rate=0.5,
        avg_placement=4.0,
        top4_rate=0.5,
        win_rate=0.1,
        games_without=games,
        avg_placement_without=4.0,
        top4_rate_without=0.5,
        win_rate_without=0.1,
        top4_delta=top4_delta,
        avg_placement_delta=0.0,
        confidence=confidence,
        association_score=(top4_delta or 0.0) * confidence,
    )


def test_opportunity_weights_sum_to_one() -> None:
    assert sum(OPPORTUNITY_WEIGHTS.values()) == pytest.approx(1.0)


def test_compute_opportunity_score_components_are_independently_inspectable() -> None:
    stat = _carry_stat(
        cost=1,
        appearance_rate=0.02,
        commitment_rate=0.01,
        posterior_top4=0.6,
        hit_top4_rate=0.7,
        miss_top4_rate=0.5,
        hit_games=50,
        miss_games=50,
        confidence=0.4,
    )
    score, components = compute_opportunity_score(
        stat, population_baseline_top4=0.5, best_partner_score=0.05, item_flexibility=0.5
    )
    assert set(components) == set(OPPORTUNITY_WEIGHTS)
    assert all(0.0 <= v <= 1.0 for v in components.values())
    assert 0.0 <= score <= 100.0

    # With 50 games backing each of hit/miss, shrinkage should barely move
    # floor_ceiling away from the raw 0.5*0.7+0.5*0.5=0.6 average.
    assert components["floor_ceiling"] == pytest.approx(0.6, abs=0.02)

    # carry_rarity and champion_rarity are separate, independently inspectable
    # numbers -- not collapsed into one "rarity" value.
    assert "carry_rarity" in components and "champion_rarity" in components
    assert components["carry_rarity"] != components["champion_rarity"]

    # cost_bias favors 1-3 cost over 4-5 cost, all else equal.
    high_cost_score, high_cost_components = compute_opportunity_score(
        _carry_stat(
            cost=5,
            appearance_rate=0.02,
            commitment_rate=0.01,
            posterior_top4=0.6,
            hit_top4_rate=0.7,
            miss_top4_rate=0.5,
            hit_games=50,
            miss_games=50,
            confidence=0.4,
        ),
        population_baseline_top4=0.5,
        best_partner_score=0.05,
        item_flexibility=0.5,
    )
    assert components["cost_bias"] > high_cost_components["cost_bias"]
    assert score > high_cost_score


def test_champion_and_carry_rarity_distinguish_pick_vs_build_rarity() -> None:
    """Same distinguishing power the task calls for: rare champion + rare
    carry vs. common champion + off-meta carry must not collapse to one
    number."""
    rare_champion_rare_carry = _carry_stat(appearance_rate=0.01, commitment_rate=0.01)
    common_champion_offmeta_carry = _carry_stat(appearance_rate=0.30, commitment_rate=0.01)

    _, a = compute_opportunity_score(
        rare_champion_rare_carry, population_baseline_top4=0.5, best_partner_score=0.0, item_flexibility=0.0
    )
    _, b = compute_opportunity_score(
        common_champion_offmeta_carry, population_baseline_top4=0.5, best_partner_score=0.0, item_flexibility=0.0
    )

    # Same carry_rarity (same commitment_rate)...
    assert a["carry_rarity"] == pytest.approx(b["carry_rarity"])
    # ...but very different champion_rarity (different appearance_rate).
    assert a["champion_rarity"] > b["champion_rarity"]


def test_shrunk_floor_ceiling_low_sample_hit_does_not_dominate() -> None:
    """A single hit game at 100% Top4 must not swing floor_ceiling as hard
    as a well-sampled miss subgroup pulls the other way."""
    stat = _carry_stat(
        commitment_games=21,
        hit_games=1,
        miss_games=20,
        hit_top4_rate=1.0,  # the 1 hit game was a top-4 finish
        miss_top4_rate=0.5,  # 20 miss games, evenly split
    )
    shrunk = _shrunk_floor_ceiling(stat, population_baseline_top4=0.5)
    naive = 0.5 * 1.0 + 0.5 * 0.5  # what an unshrunk average would give: 0.75

    assert shrunk < naive
    # The well-sampled miss side (20 games) should barely move; the 1-game
    # hit side should be pulled hard toward the 0.5 baseline.
    assert shrunk == pytest.approx(0.5 * ((1 + 0.5 * 10) / (1 + 10)) + 0.5 * ((10 + 0.5 * 10) / (20 + 10)))


def test_compute_item_flexibility_requires_evidence_not_just_one_positive_item() -> None:
    # Carry A: exactly one well-evidenced, neutral-or-positive item. The old
    # `positive / all observed` formula would have scored this a "perfect"
    # 1.0; it must not here.
    carry_a_items = [_association("ItemX", games=10, confidence=0.5, top4_delta=0.05)]
    flexibility_a = compute_item_flexibility(carry_a_items)
    assert flexibility_a < 1.0

    # Carry B: several independently well-evidenced, neutral-or-positive items.
    carry_b_items = [
        _association("ItemA", games=10, confidence=0.5, top4_delta=0.05),
        _association("ItemB", games=8, confidence=0.4, top4_delta=0.0),
        _association("ItemC", games=12, confidence=0.6, top4_delta=None),  # always present -> neutral
        _association("ItemD", games=6, confidence=0.3, top4_delta=0.02),
    ]
    flexibility_b = compute_item_flexibility(carry_b_items)

    assert flexibility_b > flexibility_a
    assert flexibility_b == 1.0  # capped once enough viable alternatives exist


def test_compute_item_flexibility_excludes_low_evidence_and_negative_items() -> None:
    items = [
        _association("TooFewGames", games=1, confidence=0.5, top4_delta=0.5),  # below min games
        _association("LowConfidence", games=10, confidence=0.05, top4_delta=0.5),  # below min confidence
        _association("ActivelyBad", games=10, confidence=0.5, top4_delta=-0.1),  # negative delta
        _association("Viable", games=10, confidence=0.5, top4_delta=0.01),
    ]
    assert compute_item_flexibility(items) == pytest.approx(1 / 3)


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

    # floor_ceiling (shrunk) must still reflect the inconsistency distinctly:
    # Volatile's is well below Solid's, even after shrinkage pulls both
    # slightly toward the population baseline.
    assert volatile.opportunity_components["floor_ceiling"] < solid.opportunity_components["floor_ceiling"]

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


def test_population_baseline_is_commitment_weighted(tmp_path: Path) -> None:
    """A tiny, extreme-performing carry must not meaningfully distort the
    balance-window baseline used for relative_performance."""
    with Database(tmp_path / "baseline.sqlite3") as db:
        # Many carries with a large, realistic sample around 50% Top4.
        for c in range(5):
            for i in range(80):
                placement = 3 if i % 2 == 0 else 6  # 50% top4
                db.ingest_match(
                    make_match(
                        f"NORMAL_{c}_{i}",
                        placement=placement,
                        units=[make_unit(f"TFT14_Normal{c}", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                    )
                )
        baseline_with_normals_only = _population_baseline_top4(db, "14.6")

        # One tiny, extreme carry: 2 games, both wins.
        for i in range(2):
            db.ingest_match(
                make_match(
                    f"EXTREME_{i}",
                    placement=1,
                    units=[make_unit("TFT14_Extreme", rarity=0, tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )
        baseline_with_extreme = _population_baseline_top4(db, "14.6")

    # The extreme carry's 2 games out of (5*80 + 2) = 402 total commitment
    # games can only shift a weighted baseline by a tiny amount.
    assert baseline_with_extreme == pytest.approx(baseline_with_normals_only, abs=0.01)
