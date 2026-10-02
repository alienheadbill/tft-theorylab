from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from ..carry import carry_commitment_sql
from ..storage import Database
from .association import Association, compute_associations


def carry_commitment_games_with_partners(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> tuple[list[tuple[int, frozenset[str]]], dict[str, str], dict[str, int]]:
    """The carry's commitment games, each paired with the set of other
    champions on that same board, plus name/cost lookups for those partners.

    Returns `(games, names, costs)`: `games` is one `(placement,
    frozenset(partner character_ids))` per committed board in the window,
    the same universe `carry_partner_associations` scores. Public so other
    features (e.g. Comp Scout's co-occurrence counts) can reuse the exact
    commitment-game definition instead of re-querying it.
    """
    return _partner_games_for(db, [character_id], balance_window, commitment_items=commitment_items).get(
        character_id, ([], {}, {})
    )


def _partner_games_for(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> dict[str, tuple[list[tuple[int, frozenset[str]]], dict[str, str], dict[str, int]]]:
    """`carry_commitment_games_with_partners` for several carries in ONE
    query; each carry's `(games, names, costs)` is exactly what its own call
    returns (rows are ordered, so ties never depend on database row order)."""
    if not character_ids:
        return {}
    eligible_sql, eligible_params = carry_commitment_sql("c", commitment_items)
    rows = db.query_all(
        f"""
        SELECT c.character_id, c.match_id, c.participant_index, p.placement,
               f.character_id, f.unit_name, f.cost
        FROM units c
        JOIN participants p
          ON p.match_id = c.match_id AND p.participant_index = c.participant_index
        JOIN matches m
          ON m.match_id = c.match_id
        LEFT JOIN units f
          ON f.match_id = c.match_id AND f.participant_index = c.participant_index
          AND f.character_id <> c.character_id
        WHERE c.character_id IN ({", ".join("?" for _ in character_ids)})
          AND {eligible_sql}
          AND m.balance_window = ?
        ORDER BY c.character_id, c.match_id, c.participant_index
        """,
        (*character_ids, *eligible_params, balance_window),
    )
    placements: dict[str, dict[tuple[str, int], int]] = defaultdict(dict)
    partner_sets: dict[str, dict[tuple[str, int], set[str]]] = defaultdict(lambda: defaultdict(set))
    names: dict[str, dict[str, str]] = defaultdict(dict)
    costs: dict[str, dict[str, int]] = defaultdict(dict)
    for carry_id, match_id, participant_index, placement, partner_id, partner_name, partner_cost in rows:
        carry_id = str(carry_id)
        game_key = (match_id, participant_index)
        placements[carry_id][game_key] = int(placement)
        if partner_id:
            partner_id = str(partner_id)
            partner_sets[carry_id][game_key].add(partner_id)
            if partner_name:
                names[carry_id][partner_id] = str(partner_name)
            if partner_cost is not None:
                costs[carry_id][partner_id] = int(partner_cost)
    return {
        carry_id: (
            [(p, frozenset(partner_sets[carry_id].get(k, ()))) for k, p in games.items()],
            dict(names[carry_id]),
            dict(costs[carry_id]),
        )
        for carry_id, games in placements.items()
    }


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
    return carry_partner_associations_for_many(
        db, [character_id], balance_window, commitment_items=commitment_items, min_games=min_games
    )[character_id]


def carry_partner_associations_for_many(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_games: int = 1,
) -> dict[str, list[Association]]:
    """`carry_partner_associations` for several carries from one row query."""
    per_carry = _partner_games_for(db, character_ids, balance_window, commitment_items=commitment_items)
    return {
        character_id: partner_associations_from_games(*per_carry.get(character_id, ([], {}, {})), min_games=min_games)
        for character_id in character_ids
    }


def partner_associations_from_games(
    games: list[tuple[int, frozenset[str]]],
    names: dict[str, str],
    costs: dict[str, int],
    *,
    min_games: int = 1,
) -> list[Association]:
    """`carry_partner_associations` from an already-fetched
    `carry_commitment_games_with_partners` result, so one board query can
    feed both the individual partners and other board-level evidence (e.g.
    `tftlab.analytics.cores`) without a second query."""
    return compute_associations(
        games,
        label_fn=lambda key: names.get(key, key),
        cost_fn=lambda key: costs.get(key),
        min_games=min_games,
    )
