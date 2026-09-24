"""Deterministic, stratified seed-player selection for Riot ingestion.

DATA POPULATION. Theory Lab's statistical population is *recent high-Elo NA
standard Ranked TFT matches reached through selected Challenger /
Grandmaster / Master ladder players*. It is not "all TFT games" and is not
globally representative: it is whatever those seed players played recently
(Match-V1 history), restricted to the ranked queue.

ANALYTICAL SEARCH SPACE. Inside that population the analytics look at
everything that happened -- every champion, carry, partner, item package
and trait breakpoint on every board in those games. Nothing here, and
nothing in Discovery, is limited to champions or comps anyone has named or
saved as an experiment.

This module therefore knows nothing about champions, items or comps. It only
decides *which players' histories* to read:

- Each requested ladder tier is fetched once (never a tier that wasn't
  requested). A PUUID listed in more than one tier's payload (e.g. promoted
  between the two requests) counts once, in the highest tier.
- Seats are split between tiers by fixed integer weights with the
  largest-remainder method (ties broken by tier order), so 50 seeds over
  Challenger / Grandmaster / Master at 4:3:3 is exactly 20 / 15 / 15. If a
  tier has fewer players than its share, its unused seats are re-split among
  the tiers that still have room, by the same weights, until the request is
  filled or every tier is exhausted.
- Within a tier, players are ranked by League Points (then PUUID, so Riot's
  response order never matters) and seeds are taken at evenly spaced ranks
  across the whole tier rather than only from its top. Players close in LP
  queue into the same lobbies, so spreading seeds across the tier covers
  more distinct games per request, and it represents the whole tier instead
  of only its upper edge.

Everything is deterministic: the same ladder payloads always give the same
seeds, in the same order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

LADDER_TIERS: tuple[str, ...] = ("challenger", "grandmaster", "master")

#: Named sampling modes -> (tiers to fetch, integer weight per tier).
SAMPLING_MODES: dict[str, tuple[tuple[str, ...], dict[str, int]]] = {
    "challenger": (("challenger",), {"challenger": 1}),
    # 4:3:3 -> 20 / 15 / 15 of 50 seeds.
    "high_elo": (LADDER_TIERS, {"challenger": 4, "grandmaster": 3, "master": 3}),
}


@dataclass(frozen=True)
class SeedSelection:
    puuids: tuple[str, ...]
    requested: int
    #: Seeds actually taken from each tier, in tier order.
    by_tier: dict[str, int]
    #: Distinct players available in each fetched tier (after cross-tier dedupe).
    ladder_sizes: dict[str, int] = field(default_factory=dict)


def _largest_remainder(total: int, weights: Mapping[str, int], order: Sequence[str]) -> dict[str, int]:
    """Split `total` seats by integer weights; leftover seats go to the
    largest fractional remainders, ties to the earlier tier."""
    weight_sum = sum(weights[t] for t in order)
    base = {t: total * weights[t] // weight_sum for t in order}
    remainders = {t: total * weights[t] % weight_sum for t in order}
    leftover = total - sum(base.values())
    for t in sorted(order, key=lambda t: (-remainders[t], order.index(t)))[:leftover]:
        base[t] += 1
    return base


def allocate_seats(
    total: int, capacities: Mapping[str, int], weights: Mapping[str, int], order: Sequence[str]
) -> dict[str, int]:
    """Seats per tier: weighted, capped at each tier's capacity, with any
    shortfall re-split across the tiers that still have room."""
    if total < 0:
        raise ValueError("total must be non-negative")
    alloc = {t: 0 for t in order}
    remaining = min(total, sum(capacities.get(t, 0) for t in order))
    active = [t for t in order if capacities.get(t, 0) > 0 and weights.get(t, 0) > 0]
    while remaining > 0 and active:
        shares = _largest_remainder(remaining, weights, active)
        for t in active:
            give = min(shares[t], capacities[t] - alloc[t])
            alloc[t] += give
            remaining -= give
        active = [t for t in active if alloc[t] < capacities[t]]
    return alloc


def evenly_spaced(ranked: Sequence[str], k: int) -> list[str]:
    """`k` items at evenly spaced positions across `ranked` (the midpoint of
    each of k equal slices), keeping their order."""
    n = len(ranked)
    if k >= n:
        return list(ranked)
    return [ranked[(2 * i + 1) * n // (2 * k)] for i in range(k)]


def _ranked_puuids(payload: Mapping[str, Any], exclude: set[str]) -> list[str]:
    best: dict[str, int] = {}
    for entry in payload.get("entries") or []:
        puuid = entry.get("puuid")
        if not puuid or puuid in exclude:
            continue
        lp = int(entry.get("leaguePoints") or 0)
        best[puuid] = max(lp, best.get(puuid, lp))
    return sorted(best, key=lambda p: (-best[p], p))


def select_seeds(
    fetch_tier: Callable[[str], Mapping[str, Any]],
    *,
    total: int,
    mode: str = "challenger",
) -> SeedSelection:
    """Choose `total` seed PUUIDs for `mode` (see `SAMPLING_MODES`).

    `fetch_tier(tier)` returns that tier's league payload (Riot's
    `/tft/league/v1/<tier>` shape); it is called once per tier in the mode
    and for no other tier.
    """
    if mode not in SAMPLING_MODES:
        raise ValueError(f"Unknown sampling mode {mode!r}; expected one of {', '.join(SAMPLING_MODES)}")
    tiers, weights = SAMPLING_MODES[mode]

    ranked: dict[str, list[str]] = {}
    claimed: set[str] = set()
    for tier in tiers:
        ranked[tier] = _ranked_puuids(fetch_tier(tier), claimed)
        claimed.update(ranked[tier])

    capacities = {t: len(ranked[t]) for t in tiers}
    seats = allocate_seats(total, capacities, weights, tiers)
    puuids = [p for t in tiers for p in evenly_spaced(ranked[t], seats[t])]
    return SeedSelection(puuids=tuple(puuids), requested=total, by_tier=seats, ladder_sizes=capacities)
