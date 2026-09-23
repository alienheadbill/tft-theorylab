from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedUnit:
    #: This unit's position within its participant's board, in the order
    #: Match-V1 lists them. A board can field more than one instance of the
    #: same `character_id` (e.g. via clone/duplication effects), so this --
    #: not `character_id` -- is part of a unit row's storage identity; see
    #: `storage.TABLES_SQL`'s `units` table.
    unit_index: int
    character_id: str
    name: str
    cost: int | None
    tier: int
    items: tuple[str, ...]
    completed_item_count: int


@dataclass(frozen=True)
class NormalizedParticipant:
    match_id: str
    participant_index: int
    placement: int
    level: int
    augments: tuple[str, ...]
    units: tuple[NormalizedUnit, ...]
    traits: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class NormalizedMatch:
    match_id: str
    game_version: str | None
    patch: str | None
    balance_window: str | None
    game_type: str | None
    queue_id: int | None
    set_number: int | None
    set_core_name: str | None
    game_datetime: int | None
    participants: tuple[NormalizedParticipant, ...]
