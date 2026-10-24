"""Item and trait directories: window-wide, observed, ordered by recurrence.

- **Items:** every completed item seen on committed carries in one balance
  window, with the carry boards that held it and the carries that held it
  most. A carry board is one committed carry on one board (the site's
  standard carry rule), counted once however many copies of the carry the
  board had (the same canonical copy as the champion page's item evidence,
  `CANONICAL_UNIT_TIEBREAK_SQL`); an item counts once per carry board even
  when the carry held two of it.
- **Traits:** every trait active on the window's player boards, with the
  unit counts Riot reported (`num_units`) and the committed carries most
  often on those boards. Breakpoints are CommunityDragon game data beside
  the observed counts (`Roster.breakpoints`).

Everything is ordered by how often it was observed, never by placement,
and no placement number appears. Rows need `DIRECTORY_MIN_BOARDS` boards;
so does every carry listed under them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .analytics.item_packages import CANONICAL_UNIT_TIEBREAK_SQL, _completed_items
from .carry import carry_commitment_sql
from .champion_investigation import BREAKPOINTS_REJECTED_NOTE, OBSERVED, OBSERVED_BOARD_MIN_BOARDS, _champion_ref
from .game_art import item_ref, trait_art, trait_name
from .roster import load_roster
from .storage import Database

#: A row (and a carry under it) is shown once it has this many boards.
DIRECTORY_MIN_BOARDS = OBSERVED_BOARD_MIN_BOARDS
#: Carries listed per row; the rest are counted.
CARRIES_SHOWN = 6
#: `item_ref` kinds an item can be filtered by.
ITEM_KINDS = ("standard", "emblem", "artifact", "radiant", "unknown")

ITEMS_DEFINITION = (
    "Completed items directly observed on committed carries in this balance window: a carry board is one committed "
    "carry (at least two completed items, the site's standard carry rule) on one player board, counted once however "
    "many copies of it the board had; an item counts once per carry board. Under each item, the carries that held it "
    "on the most boards, each with the share of its own carry boards."
)
TRAITS_DEFINITION = (
    "Traits active on this balance window's player boards, each board counted once, with the unit counts Riot "
    "reported for the trait on those boards and the committed carries most often on them. Breakpoints, where shown, "
    "are game data from CommunityDragon for reference, never inferred from matches."
)
DIRECTORY_ORDERING = "by boards observed, most first; ties by name, then id; never by placement or any score"


@dataclass(frozen=True)
class ComputedDirectories:
    """One window's item and trait counts: what the web app caches."""

    balance_window: str
    window_boards: int
    carry_boards: int
    #: carry -> its carry boards in the window
    per_carry: Mapping[str, int]
    #: item -> Counter(carry -> carry boards with the item)
    items: Mapping[str, Counter]
    #: trait -> Counter(num_units -> boards with the trait active at that count)
    trait_counts: Mapping[str, Counter]
    #: trait -> Counter(carry -> boards where the trait was active and the carry committed)
    trait_carries: Mapping[str, Counter]
    names: Mapping[str, str]
    costs: Mapping[str, int]


def compute_directories(db: Database, balance_window: str) -> ComputedDirectories:
    """Two window queries (committed carries with their items; active traits)
    and the counting in memory."""
    eligible_sql, eligible_params = carry_commitment_sql("c")
    carry_rows = db.query_all(
        f"""
        WITH ranked AS (
            SELECT c.character_id, c.match_id, c.participant_index, c.items_json, c.unit_name, c.cost,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.character_id, c.match_id, c.participant_index
                       ORDER BY {CANONICAL_UNIT_TIEBREAK_SQL}
                   ) AS rn
            FROM units c
            JOIN matches m ON m.match_id = c.match_id
            WHERE m.balance_window = ? AND {eligible_sql}
        )
        SELECT character_id, match_id, participant_index, items_json, unit_name, cost
        FROM ranked WHERE rn = 1
        ORDER BY match_id, participant_index, character_id
        """,
        (balance_window, *eligible_params),
    )
    per_carry: Counter = Counter()
    items: dict[str, Counter] = defaultdict(Counter)
    board_carries: dict[tuple[str, int], set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    costs: dict[str, int] = {}
    for character_id, match_id, participant_index, items_json, unit_name, cost in carry_rows:
        carry = str(character_id)
        per_carry[carry] += 1
        board_carries[(str(match_id), int(participant_index))].add(carry)
        for item in set(_completed_items(items_json)):
            items[item][carry] += 1
        if unit_name:
            names.setdefault(carry, str(unit_name))
        if cost is not None:
            costs.setdefault(carry, int(cost))

    trait_rows = db.query_all(
        """
        SELECT t.match_id, t.participant_index, t.trait_name, t.num_units
        FROM traits t JOIN matches m ON m.match_id = t.match_id
        WHERE m.balance_window = ? AND t.tier_current >= 1
        ORDER BY t.match_id, t.participant_index, t.trait_name
        """,
        (balance_window,),
    )
    trait_counts: dict[str, Counter] = defaultdict(Counter)
    trait_carries: dict[str, Counter] = defaultdict(Counter)
    for match_id, participant_index, trait, num_units in trait_rows:
        trait_counts[str(trait)][int(num_units)] += 1
        for carry in board_carries.get((str(match_id), int(participant_index)), ()):
            trait_carries[str(trait)][carry] += 1

    row = db.query_one(
        "SELECT COUNT(*) FROM participants p JOIN matches m ON m.match_id = p.match_id WHERE m.balance_window = ?",
        (balance_window,))
    return ComputedDirectories(
        balance_window=balance_window, window_boards=int(row[0] or 0) if row else 0,
        carry_boards=sum(per_carry.values()), per_carry=dict(per_carry), items=dict(items),
        trait_counts=dict(trait_counts), trait_carries=dict(trait_carries), names=names, costs=costs,
    )


def _carries(counter: Counter, d: ComputedDirectories, *, min_boards: int,
             trait_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
    """Carries with `min_boards`+ boards, most first (ties by name, then id),
    the first `CARRIES_SHOWN`; and how many more qualified."""
    roster = load_roster()
    qualified = []
    for cid, n in counter.items():
        if n < min_boards:
            continue
        ref = _champion_ref(cid, d.names, d.costs)
        qualified.append({
            **ref, "boards": n, "share_of_carry_boards": n / d.per_carry[cid] if d.per_carry.get(cid) else 0.0,
            **({"has_trait": trait_id in roster.champion_traits(cid)} if trait_id else {}),
        })
    qualified.sort(key=lambda r: (-r["boards"], r["name"].casefold(), r["character_id"]))
    return qualified[:CARRIES_SHOWN], max(0, len(qualified) - CARRIES_SHOWN)


def items_payload(d: ComputedDirectories, *, kind: str | None = None, min_boards: int = DIRECTORY_MIN_BOARDS,
                  limit: int = 200) -> dict[str, Any]:
    rows = []
    for item_id, carries in d.items.items():
        boards = sum(carries.values())
        if boards < min_boards:
            continue
        ref = item_ref(item_id)
        if kind and ref["kind"] != kind:
            continue
        shown, more = _carries(carries, d, min_boards=min_boards)
        rows.append({
            "item": ref, "carry_boards": boards,
            "share_of_carry_boards": boards / d.carry_boards if d.carry_boards else 0.0,
            "carries": shown, "more_carries": more,
        })
    rows.sort(key=lambda r: (-r["carry_boards"], r["item"]["name"].casefold(), r["item"]["id"]))
    return {
        "evidence_type": OBSERVED, "definition": ITEMS_DEFINITION, "ordering": DIRECTORY_ORDERING,
        "min_boards": min_boards, "kind": kind, "kinds": list(ITEM_KINDS),
        "carry_boards": d.carry_boards, "matching_items": len(rows), "items": rows[:limit],
    }


def traits_payload(d: ComputedDirectories, *, min_boards: int = DIRECTORY_MIN_BOARDS,
                   limit: int = 200) -> dict[str, Any]:
    roster = load_roster()
    rows = []
    for trait_id, counts in d.trait_counts.items():
        boards = sum(counts.values())
        if boards < min_boards:
            continue
        shown, more = _carries(d.trait_carries.get(trait_id, Counter()), d, min_boards=min_boards, trait_id=trait_id)
        breakpoints = roster.breakpoints(trait_id)
        rows.append({
            "trait_id": trait_id, "name": trait_name(trait_id) or trait_id, "art_url": trait_art(trait_id),
            "boards": boards, "share_of_window_boards": boards / d.window_boards if d.window_boards else 0.0,
            # An ordered scale: lowest unit count first, never by results.
            "counts": [{"num_units": n, "boards": c, "share": c / boards} for n, c in sorted(counts.items())],
            "breakpoints": list(breakpoints) if breakpoints else None,
            "breakpoints_note": BREAKPOINTS_REJECTED_NOTE
            if trait_id in (roster.trait_breakpoints_rejected or {}) else None,
            "carries": shown, "more_carries": more,
        })
    rows.sort(key=lambda r: (-r["boards"], r["name"].casefold(), r["trait_id"]))
    return {
        "evidence_type": OBSERVED, "definition": TRAITS_DEFINITION, "ordering": DIRECTORY_ORDERING,
        "min_boards": min_boards, "window_boards": d.window_boards,
        "matching_traits": len(rows), "traits": rows[:limit],
    }


def item_kinds(item_ids: Sequence[str]) -> dict[str, str]:
    """`item_ref` kind per id (for tests and filters)."""
    return {i: item_ref(i)["kind"] for i in item_ids}
