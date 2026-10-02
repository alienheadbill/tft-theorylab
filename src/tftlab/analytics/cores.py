"""Recurring cores: small groups of units directly observed together around a
committed carry.

A core of size N is the carry plus N-1 teammates. It is counted on a board
only when EVERY member was on that same final board, so its numbers come
from complete co-occurrence, never from combining pair statistics.

Universe and denominator. The input is exactly
`carry_commitment_games_with_partners`: one `(placement, frozenset(partner
ids))` per committed carry board of one carry in one balance window -- the
same boards the individual partner evidence uses. For each core:

- `games` = boards whose partner set contains every teammate of the core;
- `inclusion_rate` (share) = games / the carry's committed boards;
- placement / Top 4 / first-place rates describe those same boards;
- the "without" side is the carry's other committed boards, and the
  shrinkage-adjusted Top 4 difference is `compute_associations`' own,
  evaluated for the whole core as one key.

What a core is not: an exact board (other units were there too), a complete
composition, a cause of the result, or a statement about when units were
bought or where they stood. Larger cores contain smaller ones, so their
samples overlap and are never independent: no evidence is ever added up
across cores or members.

Members are player-selectable shop champions only (`Roster.is_shop_champion`);
summons and other generated units in the raw rows are not core members, and
the rows themselves are untouched. Duplicate copies of a champion on one
board count once (partner sets are sets), and a board counts at most once for
any core.

Selection is by recurrence: cores are kept when they reach `min_games` and
ordered by boards together (most first), then by their canonical member ids.
Performance never selects or orders them -- searching hundreds of
combinations for the best-looking Top 4 would mostly find noise.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Iterable

from .association import Association, compute_associations

#: Total core sizes (carry included) this module can enumerate.
CORE_SIZES = (2, 3, 4)
_KEY_SEPARATOR = "+"


@dataclass(frozen=True)
class RecurringCore:
    """One observed core. `members` are the teammates (the carry is implied),
    in canonical character-id order; `evidence` is the whole core's own
    `compute_associations` row over the carry's committed boards."""

    size: int
    members: tuple[str, ...]
    evidence: Association


def recurring_cores(
    games: Iterable[tuple[int, frozenset[str]]],
    *,
    eligible: Callable[[str], bool],
    sizes: Iterable[int] = CORE_SIZES,
    min_games: int = 1,
) -> list[RecurringCore]:
    """Every core of the given total `sizes` seen on at least `min_games` of
    the carry's boards, ordered by size, then boards together (most first),
    then canonical member ids. Enumerated from each board's own partner set
    (no top-N prefilter), so a package of individually less common units is
    found as readily as one of the most common teammates.

    Exact pruning: a core is on no more boards than any of its members, so a
    teammate on fewer than `min_games` boards cannot be in any qualifying
    core and is left out before enumerating (results are identical)."""
    games = list(games)
    teammate_counts = sorted({size - 1 for size in sizes if size in CORE_SIZES})
    boards = [(placement, {p for p in partners if eligible(p)}) for placement, partners in games]
    frequent = {m for m, n in Counter(m for _, mates in boards for m in mates).items() if n >= min_games}
    keyed = []
    for placement, eligible_mates in boards:
        mates = sorted(eligible_mates & frequent)
        keyed.append((placement, frozenset(
            _KEY_SEPARATOR.join(combo) for k in teammate_counts for combo in combinations(mates, k)
        )))
    cores = [
        RecurringCore(size=len(members) + 1, members=members, evidence=a)
        for a in compute_associations(keyed, min_games=min_games)
        for members in [tuple(a.key.split(_KEY_SEPARATOR))]
    ]
    return sorted(cores, key=lambda c: (c.size, -c.evidence.games, c.members))
