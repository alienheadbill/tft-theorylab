from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from ..carry import carry_commitment_sql
from ..storage import Database
from .association import Association, compute_associations


def _breakpoint_key(trait_name: str, tier_current: int) -> str:
    return f"{trait_name}:{tier_current}"


def _breakpoint_label(key: str) -> str:
    trait_name, _, tier = key.rpartition(":")
    return f"{trait_name} ({tier})"


def _carry_commitment_trait_games(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> list[tuple[int, frozenset[str]]]:
    """The carry's commitment games, each paired with the set of active
    trait breakpoints (`"TraitName:tier"`) on that board."""
    return _trait_games_for(db, [character_id], balance_window, commitment_items=commitment_items).get(
        character_id, []
    )


def _trait_games_for(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> dict[str, list[tuple[int, frozenset[str]]]]:
    """`_carry_commitment_trait_games` for several carries in ONE query."""
    if not character_ids:
        return {}
    eligible_sql, eligible_params = carry_commitment_sql("c", commitment_items)
    rows = db.query_all(
        f"""
        SELECT c.character_id, c.match_id, c.participant_index, p.placement,
               t.trait_name, t.tier_current
        FROM units c
        JOIN participants p
          ON p.match_id = c.match_id AND p.participant_index = c.participant_index
        JOIN matches m
          ON m.match_id = c.match_id
        LEFT JOIN traits t
          ON t.match_id = c.match_id AND t.participant_index = c.participant_index
          AND t.tier_current >= 1
        WHERE c.character_id IN ({", ".join("?" for _ in character_ids)})
          AND {eligible_sql}
          AND m.balance_window = ?
        ORDER BY c.character_id, c.match_id, c.participant_index
        """,
        (*character_ids, *eligible_params, balance_window),
    )
    placements: dict[str, dict[tuple[str, int], int]] = defaultdict(dict)
    breakpoints: dict[str, dict[tuple[str, int], set[str]]] = defaultdict(lambda: defaultdict(set))
    for carry_id, match_id, participant_index, placement, trait_name, tier_current in rows:
        carry_id = str(carry_id)
        game_key = (match_id, participant_index)
        placements[carry_id][game_key] = int(placement)
        if trait_name and tier_current is not None:
            breakpoints[carry_id][game_key].add(_breakpoint_key(str(trait_name), int(tier_current)))
    return {
        carry_id: [(p, frozenset(breakpoints[carry_id].get(k, ()))) for k, p in games.items()]
        for carry_id, games in placements.items()
    }


def trait_breakpoint_associations(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_games: int = 2,
) -> list[Association]:
    """Statistically-adjusted associations between active trait breakpoints
    and a committed carry's results, ranked by `association_score`."""
    return trait_breakpoint_associations_for_many(
        db, [character_id], balance_window, commitment_items=commitment_items, min_games=min_games
    )[character_id]


def trait_breakpoint_associations_for_many(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_games: int = 2,
) -> dict[str, list[Association]]:
    """`trait_breakpoint_associations` for several carries from one row query."""
    per_carry = _trait_games_for(db, character_ids, balance_window, commitment_items=commitment_items)
    return {
        character_id: compute_associations(
            per_carry.get(character_id, []), label_fn=_breakpoint_label, min_games=min_games
        )
        for character_id in character_ids
    }
