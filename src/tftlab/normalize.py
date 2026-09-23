from __future__ import annotations

from typing import Any, Callable

from .balance_window import resolve_balance_window
from .items import completed_item_count
from .models import NormalizedMatch, NormalizedParticipant, NormalizedUnit
from .patch import patch_from_game_version
from .unreal_patch import UNRESOLVED_UNREAL_PATCH

CostLookup = Callable[[str], "int | None"]


def cost_from_unit(unit: dict[str, Any], *, cost_lookup: CostLookup | None = None) -> int | None:
    """Resolve a unit's shop cost.

    Prefers authoritative static metadata (a CommunityDragon champion cost
    lookup) when `cost_lookup` is supplied and knows the unit. Falls back to
    Match-V1 `rarity + 1`, which is zero-based for normal shop units in
    standard TFT matches but does not hold for special/event units, so it
    remains a fallback rather than the source of truth.
    """
    character_id = str(unit.get("character_id") or "")
    if cost_lookup is not None and character_id:
        looked_up = cost_lookup(character_id)
        if looked_up is not None:
            return looked_up

    rarity = unit.get("rarity")
    if isinstance(rarity, int) and 0 <= rarity <= 4:
        return rarity + 1
    return None


def normalize_match(match: dict[str, Any], *, cost_lookup: CostLookup | None = None) -> NormalizedMatch:
    metadata = match.get("metadata", {})
    info = match.get("info", {})
    match_id = str(metadata.get("match_id") or info.get("match_id") or "unknown")
    game_version = info.get("game_version")
    game_datetime = info.get("game_datetime")
    client_patch = patch_from_game_version(game_version, game_datetime)
    # An unresolved masked-Unreal match must never get a balance window: it
    # would either be `None` already (resolve_balance_window's normal
    # behavior for an unregistered "patch") or -- worse -- collapse every
    # such match into one shared fake window if `UNRESOLVED_UNREAL_PATCH`
    # ever coincidentally matched a registered balance-window patch. Neither
    # is correct, so this is short-circuited explicitly rather than relying
    # on resolve_balance_window to happen to do the right thing.
    balance_window = (
        None if client_patch == UNRESOLVED_UNREAL_PATCH else resolve_balance_window(client_patch, game_datetime)
    )

    participants: list[NormalizedParticipant] = []
    for idx, p in enumerate(info.get("participants", [])):
        units: list[NormalizedUnit] = []
        for unit_index, unit in enumerate(p.get("units", [])):
            item_ids = tuple(unit.get("itemNames") or unit.get("item_names") or [])
            units.append(
                NormalizedUnit(
                    unit_index=unit_index,
                    character_id=str(unit.get("character_id") or ""),
                    name=str(unit.get("name") or unit.get("character_id") or ""),
                    cost=cost_from_unit(unit, cost_lookup=cost_lookup),
                    tier=int(unit.get("tier") or 1),
                    items=item_ids,
                    completed_item_count=completed_item_count(item_ids),
                )
            )

        participants.append(
            NormalizedParticipant(
                match_id=match_id,
                participant_index=idx,
                placement=int(p.get("placement") or 0),
                level=int(p.get("level") or 0),
                augments=tuple(p.get("augments") or []),
                units=tuple(units),
                traits=tuple(p.get("traits") or []),
            )
        )

    return NormalizedMatch(
        match_id=match_id,
        game_version=game_version,
        patch=client_patch,
        balance_window=balance_window,
        game_type=info.get("tft_game_type"),
        queue_id=info.get("queue_id"),
        set_number=info.get("tft_set_number"),
        set_core_name=info.get("tft_set_core_name"),
        game_datetime=game_datetime,
        participants=tuple(participants),
    )
