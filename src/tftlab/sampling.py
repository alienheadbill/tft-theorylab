"""Deterministic, rotating seed-player selection for Riot ingestion.

DATA POPULATION. Theory Lab's statistical population is *recent NA standard
Ranked TFT matches discovered through selected ladder players*. It is not
"all TFT games" and is not globally representative: it is whatever the seed
players played recently (Match-V1 history), restricted to the ranked queue.

SEED COHORTS. Seeds come from five separate ladder cohorts: Challenger,
Grandmaster, Master, Diamond and Platinum. There is no combined "elite"
cohort -- Challenger, Grandmaster and Master are always selected, counted
and reported on their own. Diamond I-IV collapse into one ``diamond``
cohort and Platinum I-IV into one ``platinum`` cohort: the division is used
only to spread seeds across the tier, never exposed analytically. (Emerald,
between Platinum and Diamond, is not a cohort.)

A cohort is *sampling provenance*: it says which ladder a seed player was
on when we read their history, i.e. how a lobby was discovered. It is not
the rank of every player in that lobby, and matches/participants are never
labelled with it.

ANALYTICAL SEARCH SPACE. Inside the population the analytics look at
everything that happened -- every champion, carry, partner, item package
and trait breakpoint on every board in those games, across all cohorts
combined. Nothing here, and nothing in Discovery, is limited to champions
or comps anyone has named or saved as an experiment.

This module therefore knows nothing about champions, items, traits,
placements or performance. It only decides *which players' histories* to
read, from ladder standing and the sampling ledger alone:

- Each cohort with a non-zero request is fetched once (never one that
  wasn't requested). A PUUID listed in more than one fetched cohort (e.g.
  promoted between two requests) counts once, in the highest cohort, so no
  player is ever seeded twice in a run.
- Within a cohort, players are ranked by division (I before IV), then
  League Points, then PUUID -- so Riot's response order never matters.
- Rotation (`rotate`): players never sampled before come first, then the
  least-recently sampled. Players are grouped by when they were last
  sampled, oldest group first (never-sampled is the oldest); whole groups
  are taken while they fit, and the group that doesn't fit is thinned to
  evenly spaced ranks across the whole tier (not its top LP). With an empty
  ledger this is exactly "evenly spaced across the tier".

Explicit per-cohort counts (`select_cohort_seeds`) are honoured as given:
a cohort with fewer players than requested gives what it has, and its
shortfall is reported rather than moved to another cohort. The older
weighted modes (`SAMPLING_MODES`, `select_seeds`) are kept for backward
compatibility and still report each tier separately.

Everything is deterministic: the same ladder payloads and the same ledger
always give the same seeds, in the same order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

#: Every seed cohort, highest first. Never merged into a combined cohort.
COHORTS: tuple[str, ...] = ("challenger", "grandmaster", "master", "diamond", "platinum")

#: Apex tiers: one league list each (`/tft/league/v1/<tier>`).
LADDER_TIERS: tuple[str, ...] = ("challenger", "grandmaster", "master")

#: Divisional tiers: `/tft/league/v1/entries/{TIER}/{DIVISION}`, all four
#: divisions collapsed into one cohort.
DIVISION_TIERS: dict[str, str] = {"diamond": "DIAMOND", "platinum": "PLATINUM"}
DIVISIONS: tuple[str, ...] = ("I", "II", "III", "IV")

#: Named legacy sampling modes -> (tiers to fetch, integer weight per tier).
SAMPLING_MODES: dict[str, tuple[tuple[str, ...], dict[str, int]]] = {
    "challenger": (("challenger",), {"challenger": 1}),
    # 4:3:3 -> 20 / 15 / 15 of 50 seeds.
    "high_elo": (LADDER_TIERS, {"challenger": 4, "grandmaster": 3, "master": 3}),
}


@dataclass(frozen=True)
class CohortReport:
    cohort: str
    requested: int
    #: Seeds actually selected from this cohort.
    selected: int
    #: Distinct players available in this cohort (after cross-cohort dedupe).
    available: int
    never_sampled_selected: int = 0
    previously_sampled_selected: int = 0


@dataclass(frozen=True)
class SeedSelection:
    puuids: tuple[str, ...]
    requested: int
    #: Seeds actually taken from each tier, in tier order.
    by_tier: dict[str, int]
    #: Distinct players available in each fetched tier (after cross-tier dedupe).
    ladder_sizes: dict[str, int] = field(default_factory=dict)
    #: The cohort each seed was selected from, parallel to `puuids`.
    cohorts: tuple[str, ...] = ()
    #: Per-cohort selection report, in cohort order.
    reports: dict[str, CohortReport] = field(default_factory=dict)


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
    if k <= 0:
        return []
    if k >= n:
        return list(ranked)
    return [ranked[(2 * i + 1) * n // (2 * k)] for i in range(k)]


def _division_index(entry: Mapping[str, Any]) -> int:
    rank = entry.get("rank")
    return DIVISIONS.index(rank) if rank in DIVISIONS else 0


def rank_entries(entries: Iterable[Mapping[str, Any]], exclude: set[str] = frozenset()) -> list[str]:
    """Distinct PUUIDs ranked by (division, -League Points, PUUID).

    Only `puuid`, `rank` (division) and `leaguePoints` are read. A PUUID
    listed twice keeps its best standing; one in `exclude` is dropped."""
    best: dict[str, tuple[int, int]] = {}
    for entry in entries:
        puuid = entry.get("puuid")
        if not puuid or puuid in exclude:
            continue
        key = (_division_index(entry), -int(entry.get("leaguePoints") or 0))
        best[puuid] = min(key, best.get(puuid, key))
    return sorted(best, key=lambda p: (*best[p], p))


def rotate(ranked: Sequence[str], k: int, last_sampled: Mapping[str, int]) -> list[str]:
    """Pick `k` of `ranked`: never-sampled first, then least-recently
    sampled; the group that doesn't fully fit is spread evenly across the
    tier. Returned in ranked order.

    `last_sampled` maps PUUID -> when it was last sampled (any comparable
    number, e.g. epoch ms); a PUUID absent from it has never been sampled."""
    if k <= 0:
        return []
    groups: dict[int, list[str]] = {}
    for puuid in ranked:  # stays in ranked order within each group
        groups.setdefault(last_sampled.get(puuid, -1), []).append(puuid)
    chosen: set[str] = set()
    remaining = k
    for key in sorted(groups):
        if remaining <= 0:
            break
        members = groups[key]
        take = members if len(members) <= remaining else evenly_spaced(members, remaining)
        chosen.update(take)
        remaining -= len(take)
    return [p for p in ranked if p in chosen]


def _selection(
    ranked: Mapping[str, list[str]],
    seats: Mapping[str, int],
    requested: Mapping[str, int],
    last_sampled: Mapping[str, int],
    total_requested: int,
) -> SeedSelection:
    puuids: list[str] = []
    cohorts: list[str] = []
    reports: dict[str, CohortReport] = {}
    for cohort, candidates in ranked.items():
        picked = rotate(candidates, seats.get(cohort, 0), last_sampled)
        never = sum(1 for p in picked if p not in last_sampled)
        puuids.extend(picked)
        cohorts.extend([cohort] * len(picked))
        reports[cohort] = CohortReport(
            cohort=cohort,
            requested=requested.get(cohort, 0),
            selected=len(picked),
            available=len(candidates),
            never_sampled_selected=never,
            previously_sampled_selected=len(picked) - never,
        )
    return SeedSelection(
        puuids=tuple(puuids),
        requested=total_requested,
        by_tier={c: r.selected for c, r in reports.items()},
        ladder_sizes={c: r.available for c, r in reports.items()},
        cohorts=tuple(cohorts),
        reports=reports,
    )


def select_cohort_seeds(
    fetch_entries: Callable[[str], Iterable[Mapping[str, Any]]],
    allocation: Mapping[str, int],
    *,
    last_sampled: Mapping[str, int] | None = None,
) -> SeedSelection:
    """Seeds for explicit per-cohort counts, e.g. ``{"challenger": 20,
    "diamond": 10}``.

    `fetch_entries(cohort)` returns that cohort's league entries (for
    Diamond/Platinum, all fetched divisions and pages concatenated); it is
    called once per cohort with a non-zero count, highest cohort first, and
    for no other cohort. A cohort short of players yields what it has.
    `last_sampled` is the sampling ledger (PUUID -> last sampled time)."""
    unknown = sorted(set(allocation) - set(COHORTS))
    if unknown:
        raise ValueError(f"Unknown seed cohort(s) {', '.join(unknown)}; expected {', '.join(COHORTS)}")
    if any(int(n) < 0 for n in allocation.values()):
        raise ValueError("seed counts must be non-negative")
    requested = {c: int(allocation.get(c, 0)) for c in COHORTS if allocation.get(c, 0)}
    ranked: dict[str, list[str]] = {}
    claimed: set[str] = set()
    for cohort in requested:
        ranked[cohort] = rank_entries(fetch_entries(cohort), claimed)
        claimed.update(ranked[cohort])
    seats = {c: min(n, len(ranked[c])) for c, n in requested.items()}
    return _selection(ranked, seats, requested, last_sampled or {}, sum(requested.values()))


def select_seeds(
    fetch_tier: Callable[[str], Mapping[str, Any]],
    *,
    total: int,
    mode: str = "challenger",
    last_sampled: Mapping[str, int] | None = None,
) -> SeedSelection:
    """Choose `total` seed PUUIDs for a legacy weighted `mode` (see
    `SAMPLING_MODES`), with the same rotation as `select_cohort_seeds`.

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
        ranked[tier] = rank_entries(fetch_tier(tier).get("entries") or [], claimed)
        claimed.update(ranked[tier])

    capacities = {t: len(ranked[t]) for t in tiers}
    seats = allocate_seats(total, capacities, weights, tiers)
    return _selection(ranked, seats, seats, last_sampled or {}, total)
