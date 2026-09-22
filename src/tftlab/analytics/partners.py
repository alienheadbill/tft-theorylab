from __future__ import annotations

from collections import defaultdict

from ..storage import Database
from .association import Association, compute_associations


def _carry_commitment_games_with_partners(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> tuple[list[tuple[int, frozenset[str]]], dict[str, str], dict[str, int]]:
    """The carry's commitment games, each paired with the set of other
    champions on that same board, plus name/cost lookups for those partners.
    """
    rows = db.query_all(
        """
        SELECT c.match_id, c.participant_index, p.placement,
               f.character_id, f.unit_name, f.cost
        FROM units c
        JOIN participants p
          ON p.match_id = c.match_id AND p.participant_index = c.participant_index
        JOIN matches m
          ON m.match_id = c.match_id
        LEFT JOIN units f
          ON f.match_id = c.match_id AND f.participant_index = c.participant_index
          AND f.character_id <> c.character_id
        WHERE c.character_id = ?
          AND c.completed_item_count >= ?
          AND m.balance_window = ?
        """,
        (character_id, commitment_items, balance_window),
    )

    placements: dict[tuple[str, int], int] = {}
    partner_sets: dict[tuple[str, int], set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    costs: dict[str, int] = {}

    for match_id, participant_index, placement, partner_id, partner_name, partner_cost in rows:
        game_key = (match_id, participant_index)
        placements[game_key] = int(placement)
        if partner_id:
            partner_id = str(partner_id)
            partner_sets[game_key].add(partner_id)
            if partner_name:
                names[partner_id] = str(partner_name)
            if partner_cost is not None:
                costs[partner_id] = int(partner_cost)

    games = [(placements[k], frozenset(partner_sets.get(k, ()))) for k in placements]
    return games, names, costs


def carry_partner_associations(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_games: int = 1,
) -> list[Association]:
    """Statistically-adjusted teammate associations for a committed carry.

    Ranked by `association_score`, not raw `top4_rate`: a partner seen in
    only a couple of games can look flawless by raw rate alone, but its
    shrinkage-adjusted delta and confidence will correctly keep it from
    outranking a partner with a large, consistently-good sample.
    """
    games, names, costs = _carry_commitment_games_with_partners(
        db, character_id, balance_window, commitment_items=commitment_items
    )
    return compute_associations(
        games,
        label_fn=lambda key: names.get(key, key),
        cost_fn=lambda key: costs.get(key),
        min_games=min_games,
    )
