from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

#: Committed CommunityDragon item metadata for the current set (see
#: `tftlab.cdragon.item_stats_snapshot`); read offline, never fetched.
ITEM_STATS_PATH = Path(__file__).parent / "data" / "item_stats.json"

# Match-V1 returns item API names. Components should not count toward our
# carry-commitment threshold. This set intentionally includes common aliases
# seen across TFT sets; unknown item IDs are treated as completed/special items.
# The current set's own component ids (e.g. Set 18's `DA_Component_*`) come
# from the snapshot's "component" tag -- see `component_ids`.
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


@lru_cache(maxsize=1)
def component_ids() -> frozenset[str]:
    """Every item id counted as a component: the legacy `TFT_Item_*` list
    plus each id the committed item snapshot tags "component" (Set 18:
    `DA_Component_BFSword`, `DA_Component_ChainVest`, ...). Metadata decides,
    never an id prefix; a missing snapshot leaves the legacy list."""
    try:
        items = json.loads(ITEM_STATS_PATH.read_text()).get("items") or {}
    except FileNotFoundError:
        items = {}
    tagged = {i for i, meta in items.items() if "component" in (meta.get("tags") or ())}
    return frozenset(_COMPONENT_IDS | tagged)


def component_ids_version() -> str:
    """Short, stable fingerprint of `component_ids()`; changes whenever the
    recognized component set changes (e.g. a new set's namespace)."""
    return hashlib.sha256("\n".join(sorted(component_ids())).encode()).hexdigest()[:16]


def is_component(item_id: str) -> bool:
    return item_id in component_ids()


def completed_item_count(item_ids: list[str] | tuple[str, ...]) -> int:
    return sum(1 for item_id in item_ids if item_id and not is_component(item_id))
