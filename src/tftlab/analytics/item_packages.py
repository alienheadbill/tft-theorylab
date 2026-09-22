from __future__ import annotations

import itertools
import json

from ..items import is_component
from ..storage import Database
from .association import Association, compute_associations


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
    completed (non-component) item tuple."""
    rows = db.query_all(
        """
        SELECT p.placement, c.items_json
        FROM units c
        JOIN participants p
          ON p.match_id = c.match_id AND p.participant_index = c.participant_index
        JOIN matches m
          ON m.match_id = c.match_id
        WHERE c.character_id = ?
          AND c.completed_item_count >= ?
          AND m.balance_window = ?
        """,
        (character_id, commitment_items, balance_window),
    )
    return [(int(placement), _completed_items(items_json)) for placement, items_json in rows]


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
    rows = _carry_commitment_item_games(db, character_id, balance_window, commitment_items=commitment_items)

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
