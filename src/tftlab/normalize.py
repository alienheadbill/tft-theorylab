from __future__ import annotations

from typing import Any

from .items import completed_item_count
from .models import NormalizedParticipant, NormalizedUnit


def cost_from_unit(unit: dict[str, Any]) -> int | None:
    """Infer standard shop cost from Match-V1 rarity.

    Riot Match-V1 unit rarity is zero-based for normal shop units in standard
    TFT matches. Special/event units can fall outside this convention, so the
    caller should treat this as a fallback until static metadata is joined.
    """
    rarity = unit.get("rarity")
    if isinstance(rarity, int) and 0 <= rarity <= 4:
        return rarity + 1
    return None


def normalize_match(match: dict[str, Any]) -> list[NormalizedParticipant]:
    metadata = match.get("metadata", {})
    info = match.get("info", {})
    match_id = metadata.get("match_id") or info.get("match_id") or "unknown"
    result: list[NormalizedParticipant] = []

    for idx, p in enumerate(info.get("participants", [])):
        units: list[NormalizedUnit] = []
        for unit in p.get("units", []):
            item_ids = tuple(unit.get("itemNames") or unit.get("item_names") or [])
            units.append(
                NormalizedUnit(
                    character_id=str(unit.get("character_id") or ""),
                    name=str(unit.get("name") or unit.get("character_id") or ""),
                    cost=cost_from_unit(unit),
                    tier=int(unit.get("tier") or 1),
                    items=item_ids,
                    completed_item_count=completed_item_count(item_ids),
                )
            )

        result.append(
            NormalizedParticipant(
                match_id=str(match_id),
                participant_index=idx,
                placement=int(p.get("placement") or 0),
                level=int(p.get("level") or 0),
                augments=tuple(p.get("augments") or []),
                units=tuple(units),
                traits=tuple(p.get("traits") or []),
            )
        )
    return result
