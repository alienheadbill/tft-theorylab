from __future__ import annotations

from collections import defaultdict

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
    rows = db.query_all(
        """
        SELECT c.match_id, c.participant_index, p.placement,
               t.trait_name, t.tier_current
        FROM units c
        JOIN participants p
          ON p.match_id = c.match_id AND p.participant_index = c.participant_index
        JOIN matches m
          ON m.match_id = c.match_id
        LEFT JOIN traits t
          ON t.match_id = c.match_id AND t.participant_index = c.participant_index
          AND t.tier_current >= 1
        WHERE c.character_id = ?
          AND c.completed_item_count >= ?
          AND m.balance_window = ?
        """,
        (character_id, commitment_items, balance_window),
    )

    placements: dict[tuple[str, int], int] = {}
    breakpoints: dict[tuple[str, int], set[str]] = defaultdict(set)
    for match_id, participant_index, placement, trait_name, tier_current in rows:
        game_key = (match_id, participant_index)
        placements[game_key] = int(placement)
        if trait_name and tier_current is not None:
            breakpoints[game_key].add(_breakpoint_key(str(trait_name), int(tier_current)))

    return [(placements[k], frozenset(breakpoints.get(k, ()))) for k in placements]


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
    games = _carry_commitment_trait_games(
        db, character_id, balance_window, commitment_items=commitment_items
    )
    return compute_associations(games, label_fn=_breakpoint_label, min_games=min_games)
