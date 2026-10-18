"""Observed final boards: the exact lineups players finished with around one
committed carry, grouped by final player level.

A board here is one player's final Match-V1 board on which the requested
champion was a committed carry -- the same `carry_commitment_sql` rule, window
and board universe as the carry's partners and recurring cores
(`carry_commitment_games_with_partners`). Nothing is inferred: a lineup is
the exact multiset of shop champions Riot listed on that board.

Identity and grouping rules (one place, so the API and tests agree):

- **Lineup identity** is the sorted tuple of the board's shop-champion ids,
  duplicates kept: two copies of a champion are two entries, so a
  double-Sivir board never pools with a single-Sivir one. Star levels, items,
  traits, placement, positioning and players are NOT part of it. Each raw
  unit id is resolved to its verified shop identity (`shop_champion_id`): an
  exact shop champion is itself; an id absent from the roster (e.g. an
  alternate form such as an `_AD` variant of a champion the roster lists as
  `_AP`) counts as a shop champion only when its normalized id matches
  exactly one shop champion. Anything else stays a special unit.
- **Special units** (summons and other non-shop units Riot lists on the
  board; see the README for the ones verified in real data) never enter the
  lineup; they are kept per lineup as context ("on N of these boards").
- **Player level** groups everything: a lineup at level 8 and the same
  lineup at level 9 are separate rows with separate denominators, because
  final level is entangled with survival and strength. Only boards with a
  valid observed level (1-10) take part; level 0 / missing never becomes a
  "level 0" group.
- **Promotion and ordering** are by recurrence only: a lineup is shown once
  it has `min_boards` boards at one level; within a level rows are ordered by
  board count (most first), then by the canonical lineup. Placement, Top 4
  and wins are reported per row as description, never used to rank.

One scoped query per champion and window, then grouping in memory.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from ..carry import carry_commitment_sql
from ..roster import Roster, id_key
from ..storage import Database

#: Riot player levels that are real observations. Older public snapshots
#: stored a sanitized 0 for every board; 0 (or anything outside this range)
#: is "level unknown", never a level.
MIN_PLAYER_LEVEL, MAX_PLAYER_LEVEL = 1, 10


def valid_level(level: int | None) -> bool:
    return level is not None and MIN_PLAYER_LEVEL <= int(level) <= MAX_PLAYER_LEVEL


def shop_champion_id(roster: Roster, unit_id: str) -> str | None:
    """The verified shop-champion identity of a raw Match-V1 unit id, or None.

    - A roster shop champion (`Roster.is_shop_champion`) is itself, so
      genuinely distinct shop forms (each Lux form has its own roster id)
      stay distinct.
    - An id the roster lists but not as a shop champion (summons, camps,
      dummies) is never resolved: the roster already says what it is.
    - An id the roster does not list resolves only when its normalized id
      (`roster.id_key`: set prefix and `_AD`/`_AP`/`_Base` suffix removed)
      equals that of exactly ONE shop champion. No match, or more than one,
      stays unresolved (fail closed): no fuzzy or name-based matching."""
    if roster.is_shop_champion(unit_id):
        return unit_id
    if unit_id in roster.champions:
        return None
    key = id_key(unit_id)
    if not key:
        return None
    matches = [cid for cid in roster.champions if roster.is_shop_champion(cid) and id_key(cid) == key]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ObservedBoard:
    """One committed-carry final board, as stored."""

    match_id: str
    participant_index: int
    placement: int
    level: int | None
    #: Highest star level among the board's committed copies of the carry.
    carry_tier: int
    #: Canonical lineup: sorted shop-champion ids, duplicates kept.
    lineup: tuple[str, ...]
    #: Non-shop units on the board (sorted ids, duplicates kept); context only.
    special_units: tuple[str, ...] = ()


def committed_carry_boards(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    roster: Roster,
    commitment_items: int = 2,
) -> tuple[list[ObservedBoard], dict[str, str], dict[str, int]]:
    """Every board in the window where `character_id` is a committed carry
    (counted once however many committed copies it had), with its whole
    final lineup: shop champions under their verified shop identity
    (`shop_champion_id`), everything else as special units. Returns
    `(boards, names, costs)` for the units seen (keyed by raw unit id).

    One query: the committed boards (DISTINCT board, MAX committed tier) are
    joined to all of their units, ordered so the result never depends on
    database row order."""
    eligible_sql, eligible_params = carry_commitment_sql("c", commitment_items)
    rows = db.query_all(
        f"""
        SELECT b.match_id, b.participant_index, p.placement, p.level, b.carry_tier,
               f.unit_index, f.character_id, f.unit_name, f.cost
        FROM (
            SELECT c.match_id, c.participant_index, MAX(c.tier) AS carry_tier
            FROM units c
            JOIN matches m ON m.match_id = c.match_id
            WHERE c.character_id = ? AND m.balance_window = ? AND {eligible_sql}
            GROUP BY c.match_id, c.participant_index
        ) AS b
        JOIN participants p ON p.match_id = b.match_id AND p.participant_index = b.participant_index
        JOIN units f ON f.match_id = b.match_id AND f.participant_index = b.participant_index
        ORDER BY b.match_id, b.participant_index, f.unit_index
        """,
        (character_id, balance_window, *eligible_params),
    )
    head: dict[tuple[str, int], tuple[int, int | None, int]] = {}
    shop: dict[tuple[str, int], list[str]] = defaultdict(list)
    special: dict[tuple[str, int], list[str]] = defaultdict(list)
    names: dict[str, str] = {}
    costs: dict[str, int] = {}
    resolved: dict[str, str | None] = {}  # raw unit id -> shop identity, resolved once per id
    for match_id, participant_index, placement, level, carry_tier, _ui, unit_id, unit_name, cost in rows:
        key = (str(match_id), int(participant_index))
        head[key] = (int(placement), None if level is None else int(level), int(carry_tier or 0))
        unit_id = str(unit_id)
        if unit_id not in resolved:
            resolved[unit_id] = shop_champion_id(roster, unit_id)
        if resolved[unit_id] is not None:
            shop[key].append(resolved[unit_id])
        else:
            special[key].append(unit_id)
        if unit_name:
            names.setdefault(unit_id, str(unit_name))
        if cost is not None:
            costs.setdefault(unit_id, int(cost))
    boards = [
        ObservedBoard(key[0], key[1], placement, level, tier, tuple(sorted(shop.get(key, ()))),
                      tuple(sorted(special.get(key, ()))))
        for key, (placement, level, tier) in head.items()
    ]
    return boards, names, costs


@dataclass(frozen=True)
class LineupGroup:
    """One exact lineup at one player level (aggregates only)."""

    level: int
    lineup: tuple[str, ...]
    boards: int
    level_boards: int  # committed carry boards at this level with a valid level: the denominator
    placement_sum: int
    top4: int
    wins: int
    carry_three_star: int
    #: special unit id -> number of this lineup's boards that had it
    special_units: dict[str, int] = field(default_factory=dict)

    @property
    def share_of_level(self) -> float:
        return self.boards / self.level_boards if self.level_boards else 0.0

    @property
    def avg_placement(self) -> float:
        return self.placement_sum / self.boards

    @property
    def top4_rate(self) -> float:
        return self.top4 / self.boards

    @property
    def win_rate(self) -> float:
        return self.wins / self.boards

    @property
    def carry_three_star_rate(self) -> float:
        return self.carry_three_star / self.boards


@dataclass(frozen=True)
class LevelGroup:
    level: int
    carry_boards: int  # denominator for every lineup at this level
    distinct_lineups: int
    lineups: list[LineupGroup]  # promoted only, ordered by recurrence


@dataclass(frozen=True)
class BoardEvidence:
    carry_boards: int
    boards_with_level: int
    min_boards: int
    levels: list[LevelGroup]  # every level with at least one valid board, ascending

    @property
    def level_coverage(self) -> float:
        return self.boards_with_level / self.carry_boards if self.carry_boards else 0.0

    @property
    def promoted(self) -> int:
        return sum(len(g.lineups) for g in self.levels)

    @property
    def boards_in_promoted(self) -> int:
        return sum(lg.boards for g in self.levels for lg in g.lineups)


def lineup_order_key(group: LineupGroup) -> tuple:
    """Recurrence first, then the canonical lineup: never results."""
    return (-group.boards, group.lineup)


def group_boards(boards: Iterable[ObservedBoard], *, min_boards: int) -> BoardEvidence:
    """Exact lineups per player level; a lineup is promoted at `min_boards`
    boards. Boards without a valid level only count toward `carry_boards`."""
    boards = list(boards)
    by_level: dict[int, list[ObservedBoard]] = defaultdict(list)
    for board in boards:
        if valid_level(board.level):
            by_level[int(board.level)].append(board)
    levels = []
    for level in sorted(by_level):
        at_level = by_level[level]
        by_lineup: dict[tuple[str, ...], list[ObservedBoard]] = defaultdict(list)
        for board in at_level:
            by_lineup[board.lineup].append(board)
        promoted = []
        for lineup, group in by_lineup.items():
            if len(group) < min_boards:
                continue
            special: Counter[str] = Counter()
            for board in group:
                special.update(set(board.special_units))
            promoted.append(LineupGroup(
                level=level, lineup=lineup, boards=len(group), level_boards=len(at_level),
                placement_sum=sum(b.placement for b in group),
                top4=sum(1 for b in group if b.placement <= 4),
                wins=sum(1 for b in group if b.placement == 1),
                carry_three_star=sum(1 for b in group if b.carry_tier >= 3),
                special_units=dict(sorted(special.items())),
            ))
        promoted.sort(key=lineup_order_key)
        levels.append(LevelGroup(level, len(at_level), len(by_lineup), promoted))
    return BoardEvidence(
        carry_boards=len(boards),
        boards_with_level=sum(len(v) for v in by_level.values()),
        min_boards=min_boards,
        levels=levels,
    )

