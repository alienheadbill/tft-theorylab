from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedUnit:
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
