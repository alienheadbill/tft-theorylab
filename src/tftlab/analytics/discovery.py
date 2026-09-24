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
#       actual, data-driven, commitment-observation-weighted baseline (see
#       `_population_baseline_top4`).
#   carry_rarity          -- low carry-COMMITMENT rate (how rare it is for
#       this unit to be built as a >=2-item carry at all). This is what v1
#       called "rarity"; the name is now explicit because it's one of two
#       distinct rarity signals (see champion_rarity).
#   champion_rarity        -- NEW: low champion PRESENCE rate (how rare the
#       unit is on a board in the first place, regardless of build).
#       Keeping this separate from carry_rarity is what lets a reader tell
#       apart "rare champion + rare carry" (both high), "common champion +
#       unusual/off-meta carry" (champion_rarity low, carry_rarity high),
#       and "rare champion that's commonly itemized whenever played"
#       (both track the same underlying pick rarity here, but
#       `carry_conversion_rate` on the CarryStat -- exposed, not folded
#       into the score -- tells you *why*: conversion near 1.0 means "any
#       appearance becomes a carry", not a distinct build-choice signal).
#   confidence            -- unchanged from v1: sample-size trust.
#   floor_ceiling         -- rewards carries that are *also* fine on a
#       miss, not just strong when they hit 3-star. A carry that's only
#       good in its (possibly tiny) sample of hit games scores low here
#       even if its blended top4_rate looks fine. hit/miss subgroup rates
#       are shrunk toward the population baseline before combining (see
#       `_shrunk_floor_ceiling`) specifically so a 1-game "100% on hit"
#       fluke can't inflate this component.
#   partner_shell         -- is there at least one partner with real,
#       shrinkage-adjusted positive evidence (from `carry_partner_associations`)?
#       A carry with zero synergy evidence is a harder sell than one with a
#       proven shell forming around it.
#   item_flexibility      -- how many independently-supported, at-least-
#       neutral completed items this carry has (see
#       `compute_item_flexibility`), not just whether its single best-seen
#       item happens to be positive.
#   cost_bias             -- favors 1/2/3-cost carries for *discovery*
#       purposes (rerolling a 4/5-cost is a different, less "hidden gem"
#       proposition), without excluding 4/5-cost carries entirely.
#
# `relative_performance` and `floor_ceiling` both derive from placement
# outcomes, but they measure different things (population-relative overall
# strength vs. hit/miss consistency), and each has a moderate rather than
# dominant weight specifically to avoid one placement-derived signal
# swamping the score just because it's counted twice under different names.
# Likewise `carry_rarity`/`champion_rarity` split what v1 counted once, at
# the same combined weight, rather than adding a second full-weight rarity
# signal.
OPPORTUNITY_WEIGHTS: dict[str, float] = {
    "relative_performance": 0.25,
    "carry_rarity": 0.12,
    "champion_rarity": 0.08,
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
_CARRY_RARITY_USAGE_CAP = 0.08  # commitment_rate at/above this maps to zero carry_rarity credit
_CHAMPION_RARITY_APPEARANCE_CAP = 0.20  # appearance_rate at/above this maps to zero champion_rarity credit
_PERFORMANCE_HEADROOM = 2.0  # posterior_top4 at 2x the population baseline maps to full credit
_PARTNER_SCORE_CAP = 0.15  # an association_score at/above this maps to full partner-shell credit
_LOW_COST_BIAS = 1.0
_HIGH_COST_BIAS = 0.6  # 4/5-cost carries still count, just not favored

# Item-flexibility thresholds (see `compute_item_flexibility`).
_ITEM_FLEXIBILITY_MIN_GAMES = 3
_ITEM_FLEXIBILITY_MIN_CONFIDENCE = 0.15
_ITEM_FLEXIBILITY_CAP = 3  # this many independently-viable items maps to full credit

# Shrinkage strength for the hit/miss floor-ceiling subgroups. Smaller than
# the carry-level prior (60, in commitment.py) or even the association-level
# one (20, in association.py) because hit/miss are the *smallest* subgroups
# in this whole pipeline -- a carry can have plenty of commitment games and
# still have only a handful of hits (or misses).
_FLOOR_CEILING_PRIOR_STRENGTH = 10.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _shrink(successes: float, attempts: float, *, prior: float, strength: float) -> float:
    return (successes + prior * strength) / (attempts + strength)


def compute_item_flexibility(item_associations: list[Association]) -> float:
    """0..1 flexibility score for a carry's individual-item associations.

    Counts completed items that are both well-evidenced (>= `_ITEM_FLEXIBILITY_MIN_GAMES`
    games and >= `_ITEM_FLEXIBILITY_MIN_CONFIDENCE` confidence) and at least
    neutral (a `top4_delta` >= 0, or no "without" baseline at all -- an item
    present in every commitment game has no evidence against it, so it
    counts as neutral rather than being excluded). A negative-delta item
    never counts, however well-sampled.

    This deliberately does NOT require strict positivity or use
    `association_score` directly: the old `positive / all observed` formula
    could give a carry with exactly one lightly-sampled positive item a
    "perfect" 1.0, which said nothing about actual build flexibility. The
    viable count is instead capped at `_ITEM_FLEXIBILITY_CAP` (beyond that
    many real alternatives, more options don't add more signal) and mapped
    linearly to 0..1.
    """
    viable = 0
    for assoc in item_associations:
        if assoc.games < _ITEM_FLEXIBILITY_MIN_GAMES:
            continue
        if assoc.confidence < _ITEM_FLEXIBILITY_MIN_CONFIDENCE:
            continue
        if assoc.top4_delta is not None and assoc.top4_delta < 0:
            continue
        viable += 1
    return _clamp01(viable / _ITEM_FLEXIBILITY_CAP)


def _shrunk_floor_ceiling(stat: CarryStat, *, population_baseline_top4: float) -> float:
    """Hit/miss Top4 rates, each pulled toward the population baseline in
    proportion to how few hit/miss games back them, then averaged evenly.

    Raw `hit_top4_rate`/`miss_top4_rate` stay exposed on `CarryStat`/the API
    unchanged; this shrinkage only affects the Opportunity Score's
    `floor_ceiling` component. Without it, a carry with e.g. a single hit
    game at 100% Top4 would count that fluke exactly as heavily as a carry
    whose hit rate is backed by fifty games.
    """
    prior = population_baseline_top4 if population_baseline_top4 > 0 else 0.5

    if stat.hit_games > 0 and stat.hit_top4_rate is not None:
        hit_successes = round(stat.hit_top4_rate * stat.hit_games)
        ceiling = _shrink(hit_successes, stat.hit_games, prior=prior, strength=_FLOOR_CEILING_PRIOR_STRENGTH)
    else:
        ceiling = stat.top4_rate

    if stat.miss_games > 0 and stat.miss_top4_rate is not None:
        miss_successes = round(stat.miss_top4_rate * stat.miss_games)
        floor = _shrink(miss_successes, stat.miss_games, prior=prior, strength=_FLOOR_CEILING_PRIOR_STRENGTH)
    else:
        floor = stat.top4_rate

    return _clamp01(0.5 * ceiling + 0.5 * floor)


def compute_opportunity_score(
    stat: CarryStat,
    *,
    population_baseline_top4: float,
    best_partner_score: float,
    item_flexibility: float,
) -> tuple[float, dict[str, float]]:
    """Returns `(score_0_to_100, components)`; every component is 0..1 and
    independently inspectable before weighting."""
    relative_performance = (
        _clamp01(stat.posterior_top4 / (population_baseline_top4 * _PERFORMANCE_HEADROOM))
        if population_baseline_top4 > 0
        else 0.0
    )
    carry_rarity = _clamp01(1.0 - stat.commitment_rate / _CARRY_RARITY_USAGE_CAP)
    champion_rarity = _clamp01(1.0 - stat.appearance_rate / _CHAMPION_RARITY_APPEARANCE_CAP)
    confidence = _clamp01(stat.confidence)

    floor_ceiling = _shrunk_floor_ceiling(stat, population_baseline_top4=population_baseline_top4)

    partner_shell = _clamp01(best_partner_score / _PARTNER_SCORE_CAP)
    flexibility = _clamp01(item_flexibility)
    cost_bias = _LOW_COST_BIAS if stat.cost <= 3 else _HIGH_COST_BIAS

    components = {
        "relative_performance": relative_performance,
        "carry_rarity": carry_rarity,
        "champion_rarity": champion_rarity,
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
    appearance_rate: float
    commitment_rate: float
    carry_conversion_rate: float
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


def _population_baseline_top4(db: Database, balance_window: str) -> float:
    """Commitment-observation-weighted average posterior Top4 rate across
    every carry (any cost, `min_samples=1`) in this balance window.

    Weighted by each carry's own `commitment_games`, not a plain per-carry
    average: a carry with 2 games and a fluky 100% Top4 contributes almost
    nothing to the baseline, while one with 2,000 games at a real 55% Top4
    dominates it appropriately. An unweighted per-carry average would let a
    handful of games from an extreme outlier carry swing the "typical"
    performance for the whole window just as much as a well-evidenced one.
    """
    population = carry_commitment_stats(
        db, balance_window=balance_window, min_cost=1, max_cost=5, min_samples=1
    )
    total_games = sum(s.commitment_games for s in population)
    if not population or total_games == 0:
        return 0.0
    return sum(s.posterior_top4 * s.commitment_games for s in population) / total_games


def _build_candidate(
    db: Database,
    stat: CarryStat,
    balance_window: str,
    *,
    population_baseline_top4: float,
    top_n: int,
) -> DiscoveryCandidate:
    partners = carry_partner_associations(db, stat.character_id, balance_window)
    item_stats = item_package_stats(db, stat.character_id, balance_window)
    traits = trait_breakpoint_associations(db, stat.character_id, balance_window)

    best_partner_score = max((a.association_score for a in partners), default=0.0)
    item_flexibility = compute_item_flexibility(item_stats["items"])

    best_item_packages = sorted(
        item_stats["pairs"] + item_stats["packages"],
        key=lambda a: a.association_score,
        reverse=True,
    )[:top_n]

    score, components = compute_opportunity_score(
        stat,
        population_baseline_top4=population_baseline_top4,
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
        appearance_rate=stat.appearance_rate,
        commitment_rate=stat.commitment_rate,
        carry_conversion_rate=stat.carry_conversion_rate,
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

    The search space is every carry observed in the window's sampled
    matches (see `tftlab.sampling` for the population): no champion
    allowlist, and saved experiments play no part in selection. v1 is
    carry-centric -- it surfaces unusual carries and their partner shells,
    item packages and trait breakpoints, but not arbitrary complete boards
    as entities of their own (a niche comp around a common carry isn't
    separated out); board signatures / clustering are a later milestone.

    `min_cost`/`max_cost` default to 1..3 to favor reroll-style discovery,
    matching the rest of the API's defaults; pass `max_cost=5` for full
    coverage. Ranking itself also biases toward 1-3 cost carries via the
    Opportunity Score's `cost_bias` component (see `compute_opportunity_score`),
    but a 4/5-cost carry can still rank if its evidence is strong.
    """
    resolved_window = balance_window or default_balance_window(db)
    if resolved_window is None:
        return []

    population_baseline = _population_baseline_top4(db, resolved_window)

    stats = carry_commitment_stats(
        db,
        balance_window=resolved_window,
        min_cost=min_cost,
        max_cost=max_cost,
        min_samples=min_samples,
    )

    candidates = [
        _build_candidate(
            db, stat, resolved_window, population_baseline_top4=population_baseline, top_n=top_n
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

    population_baseline = _population_baseline_top4(db, resolved_window)
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
        db, stat, resolved_window, population_baseline_top4=population_baseline, top_n=top_n
    )
