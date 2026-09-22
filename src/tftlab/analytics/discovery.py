from __future__ import annotations

from dataclasses import dataclass, field

from ..storage import Database
from .association import Association
from .commitment import CarryStat, carry_commitment_stats, default_balance_window
from .item_packages import item_package_stats
from .partners import carry_partner_associations
from .traits import trait_breakpoint_associations

# Opportunity Score v2: a confidence-adjusted blend of several *mostly
# orthogonal* signals, deliberately not just "raw Top 4 rate":
#
#   relative_performance -- posterior Top4 vs this balance window's own
#       carry population, replacing v1's hardcoded 50% midpoint with an
#       actual, data-driven baseline (a window where everyone runs hot at
#       55% Top4 shouldn't call a 55%-Top4 carry "exceptional").
#   rarity                -- unchanged from v1: low usage/pickup rate.
#   confidence            -- unchanged from v1: sample-size trust.
#   floor_ceiling         -- NEW: rewards carries that are *also* fine on a
#       miss, not just strong when they hit 3-star. A carry that's only
#       good in its hit games (survivorship bait) scores low here even if
#       its blended top4_rate looks fine.
#   partner_shell         -- NEW: is there at least one partner with real,
#       shrinkage-adjusted positive evidence (from `carry_partner_associations`)?
#       A carry with zero synergy evidence is a harder sell than one with a
#       proven shell forming around it.
#   item_flexibility      -- NEW: fraction of this carry's observed items
#       that show positive shrinkage-adjusted evidence. A carry that only
#       works with one exact BIS item is a narrower recommendation than one
#       that's flexible across several.
#   cost_bias             -- favors 1/2/3-cost carries for *discovery*
#       purposes (rerolling a 4/5-cost is a different, less "hidden gem"
#       proposition), without excluding 4/5-cost carries entirely.
#
# `relative_performance` and `floor_ceiling` both derive from placement
# outcomes, but they measure different things (population-relative overall
# strength vs. hit/miss consistency), and each has a moderate rather than
# dominant weight specifically to avoid one placement-derived signal
# swamping the score just because it's counted twice under different names.
OPPORTUNITY_WEIGHTS: dict[str, float] = {
    "relative_performance": 0.25,
    "rarity": 0.20,
    "confidence": 0.10,
    "floor_ceiling": 0.20,
    "partner_shell": 0.10,
    "item_flexibility": 0.05,
    "cost_bias": 0.10,
}
assert abs(sum(OPPORTUNITY_WEIGHTS.values()) - 1.0) < 1e-9

# Caps used to rescale a raw signal into 0..1 before weighting. Documented
# here rather than buried as magic numbers so the whole formula is
# inspectable end to end.
_RARITY_USAGE_CAP = 0.08  # usage_rate at/above this maps to zero rarity credit
_PERFORMANCE_HEADROOM = 2.0  # posterior_top4 at 2x the population average maps to full credit
_PARTNER_SCORE_CAP = 0.15  # an association_score at/above this maps to full partner-shell credit
_LOW_COST_BIAS = 1.0
_HIGH_COST_BIAS = 0.6  # 4/5-cost carries still count, just not favored


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_opportunity_score(
    stat: CarryStat,
    *,
    population_avg_top4: float,
    best_partner_score: float,
    item_flexibility: float,
) -> tuple[float, dict[str, float]]:
    """Returns `(score_0_to_100, components)`; every component is 0..1 and
    independently inspectable before weighting."""
    relative_performance = (
        _clamp01(stat.posterior_top4 / (population_avg_top4 * _PERFORMANCE_HEADROOM))
        if population_avg_top4 > 0
        else 0.0
    )
    rarity = _clamp01(1.0 - stat.usage_rate / _RARITY_USAGE_CAP)
    confidence = _clamp01(stat.confidence)

    ceiling = stat.hit_top4_rate if stat.hit_top4_rate is not None else stat.top4_rate
    floor = stat.miss_top4_rate if stat.miss_top4_rate is not None else stat.top4_rate
    floor_ceiling = _clamp01(0.5 * ceiling + 0.5 * floor)

    partner_shell = _clamp01(best_partner_score / _PARTNER_SCORE_CAP)
    flexibility = _clamp01(item_flexibility)
    cost_bias = _LOW_COST_BIAS if stat.cost <= 3 else _HIGH_COST_BIAS

    components = {
        "relative_performance": relative_performance,
        "rarity": rarity,
        "confidence": confidence,
        "floor_ceiling": floor_ceiling,
        "partner_shell": partner_shell,
        "item_flexibility": flexibility,
        "cost_bias": cost_bias,
    }
    score = 100.0 * sum(OPPORTUNITY_WEIGHTS[name] * value for name, value in components.items())
    return score, components


@dataclass(frozen=True)
class DiscoveryCandidate:
    character_id: str
    name: str
    cost: int
    balance_window: str
    commitment_games: int
    usage_rate: float
    avg_placement: float
    top4_rate: float
    win_rate: float
    hit_3star_rate: float
    hit_top4_rate: float | None
    miss_top4_rate: float | None
    best_partners: list[Association]
    best_item_packages: list[Association]
    best_trait_breakpoints: list[Association]
    confidence: float
    opportunity_score: float
    opportunity_components: dict[str, float] = field(default_factory=dict)


def _population_avg_top4(db: Database, balance_window: str) -> float:
    population = carry_commitment_stats(
        db, balance_window=balance_window, min_cost=1, max_cost=5, min_samples=1
    )
    if not population:
        return 0.0
    return sum(s.posterior_top4 for s in population) / len(population)


def _build_candidate(
    db: Database,
    stat: CarryStat,
    balance_window: str,
    *,
    population_avg_top4: float,
    top_n: int,
) -> DiscoveryCandidate:
    partners = carry_partner_associations(db, stat.character_id, balance_window)
    item_stats = item_package_stats(db, stat.character_id, balance_window)
    traits = trait_breakpoint_associations(db, stat.character_id, balance_window)

    best_partner_score = max((a.association_score for a in partners), default=0.0)
    item_singles = item_stats["items"]
    item_flexibility = (
        sum(1 for a in item_singles if a.association_score > 0) / len(item_singles)
        if item_singles
        else 0.0
    )

    best_item_packages = sorted(
        item_stats["pairs"] + item_stats["packages"],
        key=lambda a: a.association_score,
        reverse=True,
    )[:top_n]

    score, components = compute_opportunity_score(
        stat,
        population_avg_top4=population_avg_top4,
        best_partner_score=best_partner_score,
        item_flexibility=item_flexibility,
    )

    return DiscoveryCandidate(
        character_id=stat.character_id,
        name=stat.name,
        cost=stat.cost,
        balance_window=balance_window,
        commitment_games=stat.commitment_games,
        usage_rate=stat.usage_rate,
        avg_placement=stat.avg_placement,
        top4_rate=stat.top4_rate,
        win_rate=stat.win_rate,
        hit_3star_rate=stat.hit_3star_rate,
        hit_top4_rate=stat.hit_top4_rate,
        miss_top4_rate=stat.miss_top4_rate,
        best_partners=partners[:top_n],
        best_item_packages=best_item_packages,
        best_trait_breakpoints=traits[:top_n],
        confidence=stat.confidence,
        opportunity_score=score,
        opportunity_components=components,
    )


def discover_candidates(
    db: Database,
    *,
    balance_window: str | None = None,
    min_cost: int = 1,
    max_cost: int = 3,
    min_samples: int = 10,
    top_n: int = 5,
) -> list[DiscoveryCandidate]:
    """The first comp-discovery pass: carries enriched with statistically-
    adjusted partner/item/trait evidence and a confidence-adjusted
    Opportunity Score, all scoped to a single balance window.

    `min_cost`/`max_cost` default to 1..3 to favor reroll-style discovery,
    matching the rest of the API's defaults; pass `max_cost=5` for full
    coverage. Ranking itself also biases toward 1-3 cost carries via the
    Opportunity Score's `cost_bias` component (see `compute_opportunity_score`),
    but a 4/5-cost carry can still rank if its evidence is strong.
    """
    resolved_window = balance_window or default_balance_window(db)
    if resolved_window is None:
        return []

    population_avg_top4 = _population_avg_top4(db, resolved_window)

    stats = carry_commitment_stats(
        db,
        balance_window=resolved_window,
        min_cost=min_cost,
        max_cost=max_cost,
        min_samples=min_samples,
    )

    candidates = [
        _build_candidate(
            db, stat, resolved_window, population_avg_top4=population_avg_top4, top_n=top_n
        )
        for stat in stats
    ]
    return sorted(candidates, key=lambda c: c.opportunity_score, reverse=True)


def discovery_candidate_for(
    db: Database,
    character_id: str,
    *,
    balance_window: str | None = None,
    min_cost: int = 1,
    max_cost: int = 5,
    min_samples: int = 1,
    top_n: int = 8,
) -> DiscoveryCandidate | None:
    """A single carry's `DiscoveryCandidate`, or `None` if it has no
    qualifying commitment games in the resolved balance window."""
    resolved_window = balance_window or default_balance_window(db)
    if resolved_window is None:
        return None

    population_avg_top4 = _population_avg_top4(db, resolved_window)
    stats = carry_commitment_stats(
        db,
        balance_window=resolved_window,
        min_cost=min_cost,
        max_cost=max_cost,
        min_samples=min_samples,
    )
    stat = next((s for s in stats if s.character_id == character_id), None)
    if stat is None:
        return None

    return _build_candidate(
        db, stat, resolved_window, population_avg_top4=population_avg_top4, top_n=top_n
    )
