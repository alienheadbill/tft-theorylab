"""Trait evidence around a committed carry, straight from Riot's own counts.

Riot Match-V1 reports, per participant trait: `num_units` ("Number of units
with this trait"), `tier_current` ("Current active tier for the trait"),
`tier_total` and `style` (0 = no style ... 4 = chromatic). They are stored
unchanged in the `traits` table, one row per trait per board.

Grouping uses `trait_name` + `num_units`, the observed unit count Riot
reports. `tier_current` is only used for the active condition below; it is an
ordinal ("which tier"), not a unit count, and it is never translated into a
threshold: the canonical unit thresholds per tier are not in any verified
static metadata this repository holds.

Intrinsic traits. A trait that only the carry itself provides (a one-champion
trait, from the roster's static trait membership: `Roster.intrinsic_traits`)
is present because that champion was picked, not because of the units built
around it. It is left out of every carry's trait evidence here -- trait/count
associations (Discovery, the traits API) and the trait profile (Champion
Investigation) -- and reported separately as champion context. Only the
carry's OWN intrinsic traits are dropped: another champion's one-champion
trait on the board means that champion was added, which is shell evidence.
Stored trait rows are never changed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from ..carry import carry_commitment_sql
from ..roster import load_roster
from ..storage import Database
from .association import Association, compute_associations

#: A trait is active on a board when Riot reports a current tier: Match-V1
#: `tier_current` is the "current active tier", so 0 means no tier reached.
ACTIVE_TRAIT_SQL = "t.tier_current >= 1"

#: One board's active traits: trait name -> Riot's `num_units`.
BoardTraits = dict[str, int]


def _trait_count_key(trait_name: str, num_units: int) -> str:
    return f"{trait_name}:{num_units}"


def _trait_count_label(key: str) -> str:
    trait_name, _, num_units = key.rpartition(":")
    return f"{trait_name} ({num_units})"


def split_trait_count_key(key: str) -> tuple[str, int | None]:
    """`"Juggernaut:4"` -> `("Juggernaut", 4)`."""
    trait_name, _, num_units = key.rpartition(":")
    return trait_name, int(num_units) if num_units.lstrip("-").isdigit() else None


def _trait_boards_for(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> dict[str, list[tuple[int, BoardTraits]]]:
    """Each carry's commitment boards in ONE query, as `(placement, {trait:
    num_units})` over the board's active traits. A board is keyed by
    (match, participant), so duplicate unit rows for the carry never count
    it twice, and the traits primary key allows one row per trait per board.
    Each carry's own intrinsic traits are left out (see the module docstring)."""
    if not character_ids:
        return {}
    eligible_sql, eligible_params = carry_commitment_sql("c", commitment_items)
    rows = db.query_all(
        f"""
        SELECT c.character_id, c.match_id, c.participant_index, p.placement,
               t.trait_name, t.num_units
        FROM units c
        JOIN participants p
          ON p.match_id = c.match_id AND p.participant_index = c.participant_index
        JOIN matches m
          ON m.match_id = c.match_id
        LEFT JOIN traits t
          ON t.match_id = c.match_id AND t.participant_index = c.participant_index
          AND {ACTIVE_TRAIT_SQL}
        WHERE c.character_id IN ({", ".join("?" for _ in character_ids)})
          AND {eligible_sql}
          AND m.balance_window = ?
        ORDER BY c.character_id, c.match_id, c.participant_index
        """,
        (*character_ids, *eligible_params, balance_window),
    )
    roster = load_roster()
    intrinsic = {str(cid): frozenset(roster.intrinsic_traits(str(cid))) for cid in character_ids}
    placements: dict[str, dict[tuple[str, int], int]] = defaultdict(dict)
    traits: dict[str, dict[tuple[str, int], BoardTraits]] = defaultdict(lambda: defaultdict(dict))
    for carry_id, match_id, participant_index, placement, trait_name, num_units in rows:
        carry_id = str(carry_id)
        board = (match_id, participant_index)
        placements[carry_id][board] = int(placement)
        if trait_name and num_units is not None and str(trait_name) not in intrinsic.get(carry_id, ()):
            traits[carry_id][board][str(trait_name)] = int(num_units)
    return {
        carry_id: [(p, dict(traits[carry_id].get(board, {}))) for board, p in boards.items()]
        for carry_id, boards in placements.items()
    }


def _trait_games_for(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> dict[str, list[tuple[int, frozenset[str]]]]:
    """Each carry's commitment boards with their `"TraitName:num_units"` keys."""
    boards = _trait_boards_for(db, character_ids, balance_window, commitment_items=commitment_items)
    return {
        carry_id: [(p, frozenset(_trait_count_key(n, u) for n, u in bt.items())) for p, bt in games]
        for carry_id, games in boards.items()
    }


def _carry_commitment_trait_games(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> list[tuple[int, frozenset[str]]]:
    """The carry's commitment games, each paired with the set of active
    trait counts (`"TraitName:num_units"`) on that board."""
    return _trait_games_for(db, [character_id], balance_window, commitment_items=commitment_items).get(
        character_id, []
    )


def trait_count_associations(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_games: int = 2,
) -> list[Association]:
    """With/without associations between an active trait at an observed unit
    count (key `"TraitName:num_units"`) and a committed carry's results,
    ranked by `association_score`."""
    return trait_count_associations_for_many(
        db, [character_id], balance_window, commitment_items=commitment_items, min_games=min_games
    )[character_id]


def trait_count_associations_for_many(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_games: int = 2,
) -> dict[str, list[Association]]:
    """`trait_count_associations` for several carries from one row query."""
    per_carry = _trait_games_for(db, character_ids, balance_window, commitment_items=commitment_items)
    return {
        character_id: compute_associations(
            per_carry.get(character_id, []), label_fn=_trait_count_label, min_games=min_games
        )
        for character_id in character_ids
    }


# The names Discovery, the API and the reports have always imported. The keys
# are now observed unit counts (`num_units`), not Riot's tier ordinal.
trait_breakpoint_associations = trait_count_associations
trait_breakpoint_associations_for_many = trait_count_associations_for_many


@dataclass(frozen=True)
class TraitProfile:
    """The traits around one committed carry in one balance window.

    `carry_boards` is the denominator of every share. `active` has one
    Association per trait name (key = trait name): boards where the trait was
    active at any count. `counts` has, per trait name, one Association per
    observed `num_units` (key `"TraitName:num_units"`), sorted by count. A
    board contributes to exactly one count of each of its active traits, so a
    trait's count boards add up to its active boards; the same board appears
    under every trait it had active.

    `active` is ordered by boards (most common first), then trait name. Every
    row carries its with/without comparison as secondary evidence; nothing is
    ordered by it here.

    `intrinsic` lists the champion's intrinsic trait ids (static data), which
    `active`/`counts` never contain."""

    carry_boards: int
    active: list[Association]
    counts: dict[str, list[Association]]
    intrinsic: tuple[str, ...] = ()


def trait_profile(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> TraitProfile:
    boards = _trait_boards_for(db, [character_id], balance_window, commitment_items=commitment_items).get(
        character_id, []
    )
    active = compute_associations(((p, frozenset(bt)) for p, bt in boards), min_games=1)
    per_count = compute_associations(
        ((p, frozenset(_trait_count_key(n, u) for n, u in bt.items())) for p, bt in boards),
        label_fn=_trait_count_label,
        min_games=1,
    )
    counts: dict[str, list[Association]] = defaultdict(list)
    for a in per_count:
        counts[split_trait_count_key(a.key)[0]].append(a)
    return TraitProfile(
        intrinsic=load_roster().intrinsic_traits(character_id),
        carry_boards=len(boards),
        active=sorted(active, key=lambda a: (-a.games, a.key)),
        counts={
            name: sorted(rows, key=lambda a: (split_trait_count_key(a.key)[1] or 0, a.key))
            for name, rows in counts.items()
        },
    )
