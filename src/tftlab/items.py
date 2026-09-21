from __future__ import annotations

# Match-V1 returns item API names. Components should not count toward our
# carry-commitment threshold. This set intentionally includes common aliases
# seen across TFT sets; unknown item IDs are treated as completed/special items.
_COMPONENT_IDS = {
    "TFT_Item_BFSword",
    "TFT_Item_ChainVest",
    "TFT_Item_GiantsBelt",
    "TFT_Item_NeedlesslyLargeRod",
    "TFT_Item_NegatronCloak",
    "TFT_Item_RecurveBow",
    "TFT_Item_SparringGloves",
    "TFT_Item_Spatula",
    "TFT_Item_TearOfTheGoddess",
    "TFT_Item_FryingPan",
    "TFT_Item_EmptyBag",
}


def is_component(item_id: str) -> bool:
    return item_id in _COMPONENT_IDS


def completed_item_count(item_ids: list[str] | tuple[str, ...]) -> int:
    return sum(1 for item_id in item_ids if item_id and not is_component(item_id))
