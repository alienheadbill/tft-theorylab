"""Observed comps: the window-wide page payload over
`tftlab.analytics.comps`.

Exact final lineups from every board in one balance window, grouped into
observed board patterns (a shared shell plus one flex slot), ordered by how
many boards they appeared on. Nothing here is a tier, a grade or a
piece of advice, and it never uses the experimental composition-family
research. Placement numbers appear per exact lineup only, as "how these
boards placed": description, never an order or a filter.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .analytics.boards import LineupGroup
from .analytics.comps import (
    CarryContext,
    CompPattern,
    WindowComps,
    comp_patterns,
    group_window_boards,
    window_final_boards,
)
from .champion_investigation import OBSERVED, OBSERVED_BOARD_MIN_BOARDS, _champion_ref, _sample
from .game_art import champion_name
from .public_snapshot import PARTICIPANT_LEVEL_PRESERVED, snapshot_participant_level
from .roster import load_roster
from .storage import Database

#: The product floor: a lineup is shown once it has this many boards at one
#: player level (the same 10-board floor as the Champion page's Boards tab).
COMPS_MIN_BOARDS = OBSERVED_BOARD_MIN_BOARDS
#: Units named in a card title before "+ N".
NAMED_UNITS = 3
#: Carries listed per card (the rest are counted).
CARRIES_SHOWN = 5
#: Pattern sets kept per window grouping (one per distinct min_boards).
PATTERN_MEMO_SIZE = 8

COMPS_DEFINITION = (
    "Exact final lineups directly observed in Riot match data across every player board in this balance window: "
    "every shop champion on the board, duplicates counted, star levels and items ignored. Each board is counted "
    "once. Boards are grouped by the player's final level, and a lineup is shown only when it recurs. Lineups that "
    "share every unit but one are shown together as one board pattern; each option keeps its own numbers. They "
    "are observations, not composition families, grades or advice."
)
COMPS_ORDERING = (
    "by boards observed, most first; ties by the anchor lineup's boards, then its canonical lineup (sorted unit "
    "ids), then player level; never by placement, Top 4, win rate or any score"
)
COMPS_PATTERN_DEFINITION = (
    "A board pattern is one exact set of units (the shell) that several qualifying lineups at the same player level "
    "share, each with exactly one more unit -- its flex option. Any unit can be the flex slot. A lineup that shares "
    "no shell is a standalone exact lineup. Each qualifying lineup is in exactly one pattern. A pattern's boards are "
    "the boards of its lineups added up (each board belongs to one lineup); no result is combined across lineups."
)
COMPS_CARRY_RULE = (
    "Committed carries are listed as context: a unit with at least two completed items that carry the usual carry "
    "evidence (the site's standard carry rule). A board with more than one committed carry is still one board; it "
    "is listed under each of its carries, so the carry counts can add up to more than the boards. Boards without a "
    "committed carry are counted separately. The card links to the carry on the most of its boards."
)
PLACEMENT_LABEL = "how these boards placed"
PLACEMENT_NOTE = (
    "Average placement, Top 4 and Win describe the boards of one exact lineup, as observed. They are not why those "
    "boards placed that way, and they never decide the order."
)
LEVELS_NOT_PRESERVED = (
    "Player levels not preserved in this data. Observed comps group boards by final player level, so they can't be "
    "shown for this data source."
)
LEVELS_NOT_OBSERVED = "None of this window's boards has an observed final player level."
NOTHING_PROMOTED = "No exact lineup appeared on {n} or more boards at the same player level in this window yet."
NOTHING_MATCHES = "No observed pattern matches these filters."


@dataclass(frozen=True)
class ComputedWindow:
    """One window's grouped boards, ready to filter: what the web app caches.
    `comps` is None when the data source holds no trustworthy level."""

    balance_window: str
    window_boards: int
    comps: WindowComps | None
    names: Mapping[str, str]
    costs: Mapping[str, int]
    #: min_boards -> patterns (deterministic for this grouping), kept with it
    #: so filter changes don't rebuild them; a few entries at most.
    _patterns: dict[int, list[CompPattern]] = field(default_factory=dict, compare=False, repr=False)

    def patterns(self, min_boards: int) -> list[CompPattern]:
        found = self._patterns.get(min_boards)
        if found is None:
            found = comp_patterns(self.comps, min_boards=min_boards) if self.comps is not None else []
            if len(self._patterns) >= PATTERN_MEMO_SIZE:
                self._patterns.pop(next(iter(self._patterns)))
            self._patterns[min_boards] = found
        return found


def compute_window(db: Database, balance_window: str) -> ComputedWindow:
    """The one window query plus grouping at the product floor. Higher
    `min_boards` filters are taken from this at request time (promotion is
    per lineup, so grouping once at the floor is exact)."""
    if snapshot_participant_level(db) not in (None, PARTICIPANT_LEVEL_PRESERVED):
        row = db.query_one(
            "SELECT COUNT(*) FROM participants p JOIN matches m ON m.match_id = p.match_id "
            "WHERE m.balance_window = ?", (balance_window,))
        return ComputedWindow(balance_window, int(row[0] or 0) if row else 0, None, {}, {})
    boards, names, costs = window_final_boards(db, balance_window, roster=load_roster())
    return ComputedWindow(balance_window, len(boards), group_window_boards(boards, min_boards=COMPS_MIN_BOARDS),
                          names, costs)


# ---------------------------------------------------------------- rows


def _ref(cid: str, w: ComputedWindow) -> dict[str, Any]:
    return _champion_ref(cid, w.names, w.costs)


def _units(lineup: Sequence[str], lead: str | None, w: ComputedWindow) -> list[dict[str, Any]]:
    """Every copy, the lead carry first, then by cost, name and id (the
    Champion page's Boards order)."""
    refs = [{**_ref(cid, w), "is_carry": cid == lead} for cid in lineup]
    return sorted(refs, key=lambda u: (not u["is_carry"], u["cost"] or 0, u["name"].casefold(), u["character_id"]))


def comp_name(lineup: Sequence[str], lead: str | None, unit_count: int, w: ComputedWindow) -> str:
    """A card title made from units only: the lead carry, then the most
    expensive units, e.g. "Kha'Zix · Diana · Hecarim + 4". "+ N" is the other
    units on the board (a flex shell's flex slot included); "×2" marks two
    copies. Never an invented comp name or a trait nickname."""
    copies = Counter(lineup)
    refs = {cid: _ref(cid, w) for cid in copies}
    order = sorted(copies, key=lambda c: (c != lead, -(refs[c]["cost"] or 0), refs[c]["name"].casefold(), c))
    named = order[:NAMED_UNITS]
    parts = [f"{refs[c]['name']} ×{copies[c]}" if copies[c] > 1 else refs[c]["name"] for c in named]
    rest = unit_count - sum(copies[c] for c in named)
    return " · ".join(parts) + (f" + {rest}" if rest > 0 else "")


def _carry_rows(context: CarryContext, w: ComputedWindow) -> list[dict[str, Any]]:
    return [{**_ref(cid, w), "boards": n} for cid, n in context.ordered()]


def _lineup_row(g: LineupGroup, flex: str | None, is_anchor: bool, w: ComputedWindow) -> dict[str, Any]:
    context = w.comps.lineup_carries.get((g.level, g.lineup), CarryContext({}, g.boards))
    return {
        "flex_unit": _ref(flex, w) if flex is not None else None,
        "is_anchor": is_anchor,
        # Canonical identity: the sorted shop-champion ids, duplicates kept.
        "lineup_key": "+".join(g.lineup),
        # The units themselves are the pattern's `shared_units` plus `flex_unit`.
        "unit_ids": list(g.lineup),
        "unit_count": len(g.lineup),
        "boards": g.boards,
        "window_boards_at_level": g.level_boards,
        "share_of_level": g.share_of_level,
        # Descriptive only: never an order or a filter (PLACEMENT_NOTE).
        "how_these_boards_placed": {
            "label": PLACEMENT_LABEL,
            "avg_placement": g.avg_placement,
            "top4_rate": g.top4_rate,
            "win_rate": g.win_rate,
        },
        "carries": _carry_rows(context, w),
        "boards_without_committed_carry": context.without_carry,
        "special_units": [
            {"character_id": cid, "name": champion_name(cid, w.names.get(cid, cid)), "boards": n}
            for cid, n in g.special_units.items()
        ],
        "sample": _sample(g.boards),
        "evidence": OBSERVED,
    }


def comp_row(p: CompPattern, w: ComputedWindow) -> dict[str, Any]:
    pattern = p.pattern
    lead = p.carries.lead
    flex = pattern.shell is not None
    shared = pattern.shell if flex else pattern.anchor.lineup
    unit_count = len(pattern.anchor.lineup)
    carries = _carry_rows(p.carries, w)
    return {
        "type": "flex_shell" if flex else "exact_board",
        "name": comp_name(shared, lead, unit_count, w),
        "level": p.level,
        "unit_count": unit_count,
        "boards": p.boards,
        "window_boards_at_level": p.level_boards,
        "share_of_level": p.share_of_level,
        "lead_carry": next((c for c in carries if c["character_id"] == lead), None),
        "carries": carries[:CARRIES_SHOWN],
        "carries_observed": len(carries),
        "boards_without_committed_carry": p.carries.without_carry,
        # The shell for a pattern; the whole lineup for a standalone board.
        "shared_units": _units(shared, lead, w),
        "anchor_lineup_key": "+".join(pattern.anchor.lineup),
        "lineup_count": len(pattern.options),
        # Each its own exact lineup with its own numbers, anchor first, then by boards.
        "lineups": [_lineup_row(g, f, i == 0, w) for i, (f, g) in enumerate(pattern.options)],
        "evidence": OBSERVED,
    }


# ---------------------------------------------------------------- payload


def comps_payload(w: ComputedWindow, *, level: int | None = None, costs: Sequence[int] | None = None,
                  min_boards: int = COMPS_MIN_BOARDS, limit: int = 50) -> dict[str, Any]:
    """The `/api/comps` body (without the source/window block the web app
    adds). Patterns are built from every lineup on `min_boards`+ boards, then
    filtered by `level` and lead-carry `costs`, then capped at `limit`; no
    filter can change a pattern."""
    min_boards = max(min_boards, COMPS_MIN_BOARDS)
    body: dict[str, Any] = {
        "evidence": OBSERVED,
        "available": False,
        "reason": None,
        "note": None,
        "definition": COMPS_DEFINITION,
        "ordering": COMPS_ORDERING,
        "pattern_definition": COMPS_PATTERN_DEFINITION,
        "carry_rule": COMPS_CARRY_RULE,
        "placement_label": PLACEMENT_LABEL,
        "placement_note": PLACEMENT_NOTE,
        "promotion_rule": {
            "min_boards_floor": COMPS_MIN_BOARDS,
            "min_boards": min_boards,
            "rule": f"An exact lineup is shown once it appears on {min_boards} or more boards at one player level "
                    f"(never fewer than {COMPS_MIN_BOARDS}); levels are never combined.",
        },
        "filters": {"level": level, "cost": list(costs) if costs else None, "min_boards": min_boards,
                    "limit": limit},
        "window_boards": w.window_boards,
        "boards_with_level": 0,
        "level_coverage": 0.0,
        "levels": [],
        "qualifying_lineups": 0,
        "qualifying_patterns": 0,
        "matching_patterns": 0,
        "patterns": [],
    }
    if w.comps is None:
        body["reason"] = LEVELS_NOT_PRESERVED
        return body
    evidence = w.comps.evidence
    body["boards_with_level"] = evidence.boards_with_level
    body["level_coverage"] = evidence.boards_with_level / w.window_boards if w.window_boards else 0.0
    if not evidence.boards_with_level:
        body["reason"] = LEVELS_NOT_OBSERVED
        return body
    body["available"] = True
    patterns = w.patterns(min_boards)
    per_level = Counter(p.level for p in patterns)
    body["levels"] = [
        {"level": g.level, "window_boards_at_level": g.carry_boards, "distinct_lineups": g.distinct_lineups,
         "qualifying_lineups": sum(1 for lg in g.lineups if lg.boards >= min_boards),
         "patterns": per_level.get(g.level, 0)}
        for g in evidence.levels
    ]
    body["qualifying_lineups"] = sum(g["qualifying_lineups"] for g in body["levels"])
    body["qualifying_patterns"] = len(patterns)
    selected = [
        p for p in patterns
        if (level is None or p.level == level)
        and (not costs or (p.carries.lead is not None and _ref(p.carries.lead, w)["cost"] in costs))
    ]
    body["matching_patterns"] = len(selected)
    body["patterns"] = [comp_row(p, w) for p in selected[:limit]]
    if not patterns:
        body["note"] = NOTHING_PROMOTED.format(n=min_boards)
    elif not selected:
        body["note"] = NOTHING_MATCHES
    return body
