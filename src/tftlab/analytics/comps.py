"""Observed comps: the exact final lineups players built across a whole
balance window, carry-agnostic, grouped into observed board patterns.

The window-wide counterpart of `tftlab.analytics.boards` (one champion's
committed boards). It reuses that module's rules unchanged; only the board
universe differs:

- **Universe:** every player board in the window (eight per match), each
  counted exactly once. A board is not selected through a carry, so a board
  with two committed carries is still one board in one lineup.
- **Lineup identity, special units, level grouping, promotion floor and
  ordering** are exactly those of `tftlab.analytics.boards` (`shop_champion_id`,
  `group_boards`, `lineup_order_key`): duplicates kept, summons as context,
  levels never pooled, a lineup promoted at `min_boards` boards at one level.
- **Committed carries** (`carry_commitment_sql`, the same rule as everywhere
  else) are context on each board (`ObservedBoard.carries`): every committed
  carry on the board is listed. A board with two committed carries counts
  once in its lineup and once under EACH of its carries in "carries
  observed", so those per-carry counts can add up to more than the boards; a
  board with no committed carry is counted separately. The card's lead carry
  is the one on the most of the pattern's boards (ties by id).
- **Patterns** use `board_patterns(..., carry_id=None)`: the same N-1 shell
  rule with no unit protected, so each promoted lineup sits in exactly one
  pattern. A pattern's `boards` is the number of boards in its lineups (each
  board is in one lineup and each lineup in one pattern, so this is a count,
  not a pooled result); no placement number is ever combined across lineups.
- **Order:** by boards, most first, then the anchor lineup's
  `lineup_order_key`, then level. Never by placement or any score.

Two scoped queries per window (see `window_final_boards`), then grouping in
memory.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from ..carry import carry_commitment_sql
from ..roster import Roster
from ..storage import Database
from .boards import (
    BoardEvidence,
    BoardPattern,
    LineupGroup,
    ObservedBoard,
    board_patterns,
    group_boards,
    lineup_order_key,
    shop_champion_id,
    valid_level,
)


def window_final_boards(
    db: Database,
    balance_window: str,
    *,
    roster: Roster,
    commitment_items: int = 2,
) -> tuple[list[ObservedBoard], dict[str, str], dict[str, int]]:
    """Every player board in the window, once, with its whole final lineup
    (shop champions under their verified shop identity, everything else as
    special units) and its committed carries. Returns `(boards, names, costs)`
    for the units seen (keyed by raw unit id).

    Two queries. The canonical commitment rule (`carry_commitment_sql`)
    reads only a unit's `items_json` and `completed_item_count`, so it is
    applied once per DISTINCT pair in the window (materialized first, so the
    rule is not pushed back down onto every unit row) rather than once per
    unit: same rule, same result, a fraction of the work on a patch-sized
    window. Then the window's boards left-joined to their units (so a board
    with no units still counts toward the window), ordered so the result
    never depends on database row order; a unit is committed when its pair
    passed the rule."""
    eligible_sql, eligible_params = carry_commitment_sql("d", commitment_items)
    committed_items = {
        (str(items_json), int(count))
        for items_json, count in db.query_all(
            f"""
            WITH d AS MATERIALIZED (
                SELECT DISTINCT f.items_json, f.completed_item_count
                FROM units f
                JOIN matches m ON m.match_id = f.match_id
                WHERE m.balance_window = ? AND f.completed_item_count >= ?
            )
            SELECT d.items_json, d.completed_item_count FROM d WHERE {eligible_sql}
            """,
            (balance_window, commitment_items, *eligible_params),
        )
    }
    rows = db.query_all(
        """
        SELECT p.match_id, p.participant_index, p.placement, p.level,
               f.unit_index, f.character_id, f.unit_name, f.cost, f.items_json, f.completed_item_count
        FROM participants p
        JOIN matches m ON m.match_id = p.match_id
        LEFT JOIN units f ON f.match_id = p.match_id AND f.participant_index = p.participant_index
        WHERE m.balance_window = ?
        ORDER BY p.match_id, p.participant_index, f.unit_index
        """,
        (balance_window,),
    )
    head: dict[tuple[str, int], tuple[int, int | None]] = {}
    shop: dict[tuple[str, int], list[str]] = defaultdict(list)
    special: dict[tuple[str, int], list[str]] = defaultdict(list)
    carries: dict[tuple[str, int], set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    costs: dict[str, int] = {}
    resolved: dict[str, str | None] = {}  # raw unit id -> shop identity, resolved once per id
    for match_id, participant_index, placement, level, _ui, unit_id, unit_name, cost, items_json, count in rows:
        key = (str(match_id), int(participant_index))
        head[key] = (int(placement), None if level is None else int(level))
        if unit_id is None:
            continue
        unit_id = str(unit_id)
        if unit_id not in resolved:
            resolved[unit_id] = shop_champion_id(roster, unit_id)
        identity = resolved[unit_id]
        if identity is not None:
            shop[key].append(identity)
        else:
            special[key].append(unit_id)
        if count is not None and (str(items_json), int(count)) in committed_items:
            carries[key].add(identity or unit_id)
        if unit_name:
            names.setdefault(unit_id, str(unit_name))
        if cost is not None:
            costs.setdefault(unit_id, int(cost))
    boards = [
        ObservedBoard(key[0], key[1], placement, level, 0, tuple(sorted(shop.get(key, ()))),
                      tuple(sorted(special.get(key, ()))), tuple(sorted(carries.get(key, ()))))
        for key, (placement, level) in head.items()
    ]
    return boards, names, costs


@dataclass(frozen=True)
class CarryContext:
    """Committed carries on one set of boards: `carries` maps a carry to the
    boards it was a committed carry on (a board may count under several);
    `without_carry` counts the boards with none."""

    carries: dict[str, int]
    without_carry: int

    def ordered(self) -> list[tuple[str, int]]:
        """Most boards first, ties by id."""
        return sorted(self.carries.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def lead(self) -> str | None:
        ordered = self.ordered()
        return ordered[0][0] if ordered else None


def _context(boards: Iterable[ObservedBoard]) -> CarryContext:
    counts: Counter[str] = Counter()
    without = 0
    for board in boards:
        if board.carries:
            counts.update(board.carries)
        else:
            without += 1
    return CarryContext(dict(counts), without)


@dataclass(frozen=True)
class WindowComps:
    """Grouped window boards: the boards module's per-level evidence plus
    the committed-carry context of every promoted lineup."""

    evidence: BoardEvidence
    #: (level, lineup) -> committed carries on that lineup's boards
    lineup_carries: dict[tuple[int, tuple[str, ...]], CarryContext]

    @property
    def promoted(self) -> list[LineupGroup]:
        return [lg for g in self.evidence.levels for lg in g.lineups]

    def level_boards(self, level: int) -> int:
        return next((g.carry_boards for g in self.evidence.levels if g.level == level), 0)


def group_window_boards(boards: Iterable[ObservedBoard], *, min_boards: int) -> WindowComps:
    """`group_boards` over the window's boards (each counted once), plus the
    committed carries of each promoted lineup."""
    boards = list(boards)
    evidence = group_boards(boards, min_boards=min_boards)
    promoted = {(lg.level, lg.lineup) for g in evidence.levels for lg in g.lineups}
    by_key: dict[tuple[int, tuple[str, ...]], list[ObservedBoard]] = defaultdict(list)
    for board in boards:
        if valid_level(board.level) and (int(board.level), board.lineup) in promoted:
            by_key[(int(board.level), board.lineup)].append(board)
    return WindowComps(evidence, {key: _context(group) for key, group in by_key.items()})


@dataclass(frozen=True)
class CompPattern:
    """One observed board pattern window-wide, with the committed carries
    across its boards (context only, never an ordering input)."""

    pattern: BoardPattern
    boards: int
    level_boards: int
    carries: CarryContext

    @property
    def level(self) -> int:
        return self.pattern.level

    @property
    def share_of_level(self) -> float:
        return self.boards / self.level_boards if self.level_boards else 0.0


def comp_order_key(p: CompPattern) -> tuple:
    """Boards first (most first), then the anchor's recurrence key, then the
    level: never results."""
    return (-p.boards, lineup_order_key(p.pattern.anchor), p.level)


def comp_patterns(comps: WindowComps, *, min_boards: int) -> list[CompPattern]:
    """Every promoted lineup (`min_boards`+ boards at one level) in exactly
    one carry-agnostic pattern, ordered by `comp_order_key`. Built from ALL
    promoted lineups, so a later display filter (level, cost, limit) can
    never change a pattern."""
    found: list[CompPattern] = []
    for level, patterns in board_patterns(comps.promoted, carry_id=None, min_boards=min_boards).items():
        for pattern in patterns:
            members = [g for _, g in pattern.options]
            counts: Counter[str] = Counter()
            without = 0
            for g in members:
                context = comps.lineup_carries.get((g.level, g.lineup), CarryContext({}, g.boards))
                counts.update(context.carries)
                without += context.without_carry
            found.append(CompPattern(pattern, sum(g.boards for g in members), comps.level_boards(level),
                                     CarryContext(dict(counts), without)))
    found.sort(key=comp_order_key)
    return found
