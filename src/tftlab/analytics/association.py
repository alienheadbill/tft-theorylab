from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

# Shrinkage strength for with/without comparisons. Deliberately smaller than
# the carry-level prior (60, in commitment.py): partner/item/trait samples
# are naturally much smaller than a carry's total commitment-game count, and
# an overly strong prior would flatten every real signal to ~0. This value
# still meaningfully protects against small-sample noise (see
# tests/test_association.py for the concrete "2-game 100% partner must not
# outrank a proven larger-sample partner" case this exists to prevent).
DEFAULT_PRIOR_STRENGTH = 20.0


@dataclass(frozen=True)
class Association:
    """One factor's (partner/item/item-pair/item-package/trait-breakpoint)
    statistically-adjusted relationship to a carry's commitment-game results.

    `games` is this factor's sample size -- the number of the carry's
    commitment games where it was present. Every rate/placement field is
    computed only over that subset ("with"); the matching `..._without`
    field covers the carry's remaining commitment games ("without"), and the
    `..._delta` fields are the (shrinkage-adjusted, for top4) difference. Use
    `association_score` -- not `top4_rate` -- to rank factors: it is the
    shrinkage-adjusted delta scaled by confidence, so a tiny high-roll sample
    cannot outrank a well-evidenced, moderately-good one.
    """

    key: str
    label: str
    cost: int | None
    games: int
    inclusion_rate: float
    avg_placement: float
    top4_rate: float
    win_rate: float
    games_without: int
    avg_placement_without: float | None
    top4_rate_without: float | None
    win_rate_without: float | None
    top4_delta: float | None
    avg_placement_delta: float | None
    confidence: float
    association_score: float


def compute_associations(
    games: Iterable[tuple[int, frozenset[str]]],
    *,
    label_fn: Callable[[str], str] | None = None,
    cost_fn: Callable[[str], "int | None"] | None = None,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    min_games: int = 1,
) -> list[Association]:
    """Compute with/without/shrinkage-adjusted association stats for every
    key seen across `games`.

    `games` is the full universe of a carry's commitment games in one
    balance window: `(placement, keys_present)` pairs, where `keys_present`
    is whichever factor set applies at the call site (partner character_ids,
    completed item ids, item-pair/-package keys, or trait breakpoint keys).
    Every key's "without" baseline is computed as this same universe minus
    the games where that key was present, so it never leaks games from a
    different carry or balance window -- the caller controls isolation
    entirely through what it includes in `games`.
    """
    games = list(games)
    total_games = len(games)
    if total_games == 0:
        return []

    total_sum_placement = float(sum(placement for placement, _ in games))
    total_top4 = sum(1 for placement, _ in games if placement <= 4)
    total_win = sum(1 for placement, _ in games if placement == 1)
    baseline_top4 = total_top4 / total_games

    # key -> [games, sum_placement, top4s, wins]
    counts: dict[str, list[float]] = {}
    for placement, keys in games:
        for key in keys:
            bucket = counts.setdefault(key, [0, 0.0, 0, 0])
            bucket[0] += 1
            bucket[1] += placement
            bucket[2] += 1 if placement <= 4 else 0
            bucket[3] += 1 if placement == 1 else 0

    results: list[Association] = []
    for key, (games_with_f, sum_place_with, top4_with_f, win_with_f) in counts.items():
        games_with = int(games_with_f)
        top4_with = int(top4_with_f)
        win_with = int(win_with_f)
        if games_with < min_games:
            continue

        avg_place_with = sum_place_with / games_with
        top4_rate_with = top4_with / games_with
        win_rate_with = win_with / games_with

        games_without = total_games - games_with
        if games_without > 0:
            sum_place_without = total_sum_placement - sum_place_with
            top4_without = total_top4 - top4_with
            win_without = total_win - win_with
            avg_place_without = sum_place_without / games_without
            top4_rate_without = top4_without / games_without
            win_rate_without = win_without / games_without

            posterior_with = (top4_with + baseline_top4 * prior_strength) / (games_with + prior_strength)
            posterior_without = (top4_without + baseline_top4 * prior_strength) / (
                games_without + prior_strength
            )
            top4_delta = posterior_with - posterior_without
            # Positive means "better with this factor" (lower placement is better).
            avg_placement_delta = avg_place_without - avg_place_with
            confidence = min(games_with, games_without) / (min(games_with, games_without) + prior_strength)
        else:
            avg_place_without = top4_rate_without = win_rate_without = None
            top4_delta = avg_placement_delta = None
            confidence = games_with / (games_with + prior_strength)

        association_score = (top4_delta or 0.0) * confidence

        results.append(
            Association(
                key=key,
                label=label_fn(key) if label_fn else key,
                cost=cost_fn(key) if cost_fn else None,
                games=games_with,
                inclusion_rate=games_with / total_games,
                avg_placement=avg_place_with,
                top4_rate=top4_rate_with,
                win_rate=win_rate_with,
                games_without=games_without,
                avg_placement_without=avg_place_without,
                top4_rate_without=top4_rate_without,
                win_rate_without=win_rate_without,
                top4_delta=top4_delta,
                avg_placement_delta=avg_placement_delta,
                confidence=confidence,
                association_score=association_score,
            )
        )

    return sorted(results, key=lambda a: a.association_score, reverse=True)
