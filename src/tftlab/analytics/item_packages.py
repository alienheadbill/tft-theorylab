from __future__ import annotations

import itertools
import json
from typing import Sequence

from ..carry import carry_commitment_sql
from ..items import is_component
from ..storage import Database
from .association import Association, compute_associations

#: Deterministic tie-break for picking one "canonical" carry instance when a
#: board fields more than one copy of the same champion in the same game
#: (see `units.unit_index`): highest completed-item count first, then
#: highest star tier, then lowest `unit_index` as a final, stable tiebreaker.
#: Used for item-package analysis, where a game must contribute exactly one
#: item observation, not one per instance -- see `_carry_commitment_item_games`.
CANONICAL_UNIT_TIEBREAK_SQL = "completed_item_count DESC, tier DESC, unit_index ASC"


def _completed_items(items_json: str) -> tuple[str, ...]:
    raw = json.loads(items_json)
    return tuple(sorted(item for item in raw if item and not is_component(item)))


def _carry_commitment_item_games(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> list[tuple[int, tuple[str, ...]]]:
    """The carry's commitment games, each paired with its own sorted,
    completed (non-component) item tuple.

    A board can field more than one committed instance of `character_id` in
    the same game; when it does, exactly one instance is chosen per
    `CANONICAL_UNIT_TIEBREAK_SQL` so that game contributes one item
    observation, not one per instance.
    """
    return _item_games_for(db, [character_id], balance_window, commitment_items=commitment_items).get(character_id, [])


def _item_games_for(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
) -> dict[str, list[tuple[int, tuple[str, ...]]]]:
    """`_carry_commitment_item_games` for several carries in ONE query (each
    carry's rows are exactly what its own query returns; ordered, so the
    result doesn't depend on the database's row order)."""
    if not character_ids:
        return {}
    eligible_sql, eligible_params = carry_commitment_sql("c", commitment_items)
    rows = db.query_all(
        f"""
        WITH ranked AS (
            SELECT
                c.character_id, c.match_id, c.participant_index, c.items_json, p.placement,
                ROW_NUMBER() OVER (
                    PARTITION BY c.character_id, c.match_id, c.participant_index
                    ORDER BY {CANONICAL_UNIT_TIEBREAK_SQL}
                ) AS rn
            FROM units c
            JOIN participants p
              ON p.match_id = c.match_id AND p.participant_index = c.participant_index
            JOIN matches m
              ON m.match_id = c.match_id
            WHERE c.character_id IN ({", ".join("?" for _ in character_ids)})
              AND {eligible_sql}
              AND m.balance_window = ?
        )
        SELECT character_id, placement, items_json FROM ranked WHERE rn = 1
        ORDER BY character_id, match_id, participant_index
        """,
        (*character_ids, *eligible_params, balance_window),
    )
    games: dict[str, list[tuple[int, tuple[str, ...]]]] = {}
    for character_id, placement, items_json in rows:
        games.setdefault(str(character_id), []).append((int(placement), _completed_items(items_json)))
    return games


def _label_for(item_names: dict[str, str] | None, key: str) -> str:
    if not item_names:
        return key
    return " + ".join(item_names.get(part, part) for part in key.split("+"))


def item_package_stats(
    db: Database,
    character_id: str,
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_pair_games: int = 2,
    min_package_games: int = 2,
    item_names: dict[str, str] | None = None,
) -> dict[str, list[Association]]:
    """Individual/pair/exact-3-item association stats for a committed carry.

    Returns `{"items": [...], "pairs": [...], "packages": [...]}`, each
    ranked by `association_score` (not raw performance) so a tiny high-roll
    sample can't masquerade as a proven best-in-slot package -- see
    `min_pair_games`/`min_package_games` for the sample floor below which a
    combination isn't surfaced at all, on top of the score's own shrinkage.
    """
    return item_package_stats_for_many(
        db, [character_id], balance_window, commitment_items=commitment_items,
        min_pair_games=min_pair_games, min_package_games=min_package_games, item_names=item_names,
    )[character_id]


def item_package_stats_for_many(
    db: Database,
    character_ids: Sequence[str],
    balance_window: str,
    *,
    commitment_items: int = 2,
    min_pair_games: int = 2,
    min_package_games: int = 2,
    item_names: dict[str, str] | None = None,
) -> dict[str, dict[str, list[Association]]]:
    """`item_package_stats` for several carries from one row query: each
    carry's result is exactly what its own call returns."""
    games = _item_games_for(db, character_ids, balance_window, commitment_items=commitment_items)
    return {
        character_id: _package_stats(
            games.get(character_id, []), min_pair_games=min_pair_games,
            min_package_games=min_package_games, item_names=item_names,
        )
        for character_id in character_ids
    }


def _package_stats(
    rows: list[tuple[int, tuple[str, ...]]],
    *,
    min_pair_games: int,
    min_package_games: int,
    item_names: dict[str, str] | None,
) -> dict[str, list[Association]]:
    singles_games = [(placement, frozenset(items)) for placement, items in rows]
    pairs_games = [
        (placement, frozenset(f"{a}+{b}" for a, b in itertools.combinations(items, 2)))
        for placement, items in rows
    ]
    packages_games = [
        (placement, frozenset(f"{a}+{b}+{c}" for a, b, c in itertools.combinations(items, 3)))
        for placement, items in rows
    ]

    label_fn = lambda key: _label_for(item_names, key)  # noqa: E731

    return {
        "items": compute_associations(singles_games, label_fn=label_fn, min_games=1),
        "pairs": compute_associations(pairs_games, label_fn=label_fn, min_games=min_pair_games),
        "packages": compute_associations(packages_games, label_fn=label_fn, min_games=min_package_games),
    }
