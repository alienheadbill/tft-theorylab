"""What a unit's Match-V1 item list means: equipped items vs generated items.

Riot Match-V1 lists every item a unit held at the end of the game
(`itemNames`, stored unchanged in `units.items_json`). For most units every
listed item was equipped by the player. Thief's Gloves is the exception: it
takes all of the holder's item slots and, each round, equips two random
items on it. Riot lists the gloves plus that round's two rolls, e.g.

    ["DA_ThiefsGloves", "DA_Bloodthirster", "DA_DragonsClaw"]

The two rolls were not chosen. Derived analytics -- carry eligibility,
fixed items, item pairs and packages, item flexibility -- must therefore see
this unit's equipped itemization as Thief's Gloves alone, and the rolls only
as `generated`. `unit_itemization` is the one place that decides this; raw
storage keeps Riot's list exactly as sent.

Which ids are Thief's Gloves comes from the committed item metadata
(`data/item_stats.json`): Riot's own item `TFT_Item_ThiefsGloves` plus every
id the snapshot resolves to it (`DA_ThiefsGloves`, Academy copies), and each
of those ids' Radiant form (`<id>Radiant`, e.g. `DA_ThiefsGlovesRadiant`) --
the Riot naming the snapshot cannot list itself, because it keeps no
uncraftable `DA_*` entries (see `tftlab.game_art.item_kind`, which classifies
Radiant items by the same suffix).

Lucky Gloves / Lucky Gloves+ (augments that make Thief's Gloves roll
champion-appropriate items) are NOT handled yet: their CommunityDragon
apiNames are known, but which strings Match-V1 stores in a participant's
`augments` has not been verified against real match data. Until it is, a
Thief's Gloves unit is never carry evidence and its rolls never count as
chosen items.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from .items import ITEM_STATS_PATH, is_component

#: Riot's item for Thief's Gloves; every id resolving to it is Thief's Gloves.
THIEFS_GLOVES_RIOT_ITEM = "TFT_Item_ThiefsGloves"
#: Riot's naming for an item's Radiant form.
RADIANT_SUFFIX = "Radiant"

#: `UnitItemization.special` for a Thief's Gloves holder.
THIEFS_GLOVES = "thiefs_gloves"


@lru_cache(maxsize=1)
def thiefs_gloves_item_ids() -> frozenset[str]:
    """Every id that is Thief's Gloves (normal or Radiant), from the
    committed item snapshot. Empty without a snapshot, so nothing is
    reinterpreted then."""
    try:
        items = json.loads(ITEM_STATS_PATH.read_text()).get("items") or {}
    except FileNotFoundError:
        return frozenset()
    base: set[str] = set()
    for item_id, meta in items.items():
        aliases = set(meta.get("alias_of") or ())
        if item_id == THIEFS_GLOVES_RIOT_ITEM or THIEFS_GLOVES_RIOT_ITEM in aliases:
            base |= {item_id, *aliases}
    if not base:
        return frozenset()
    return frozenset(base | {f"{item_id}{RADIANT_SUFFIX}" for item_id in base})


@dataclass(frozen=True)
class UnitItemization:
    """One unit's items, split by meaning (Riot's order kept in each part).

    `equipped`: items the player put on the unit.
    `generated`: items the game equipped for the round (Thief's Gloves rolls).
    `special`: `THIEFS_GLOVES` when the unit holds Thief's Gloves, else None.
    """

    equipped: tuple[str, ...]
    generated: tuple[str, ...]
    special: str | None

    @property
    def completed_equipped(self) -> tuple[str, ...]:
        """Equipped completed (non-component) items: what fixed-item, pair and
        package evidence is built from."""
        return tuple(i for i in self.equipped if not is_component(i))


def unit_itemization(item_ids: Iterable[str]) -> UnitItemization:
    """Split a unit's Match-V1 item list (see the module docstring). A
    Thief's Gloves holder can equip nothing else, so every other listed item
    on it is one of that round's rolls."""
    items = tuple(str(i) for i in item_ids if i)
    gloves = thiefs_gloves_item_ids()
    held = tuple(i for i in items if i in gloves)
    if not held:
        return UnitItemization(items, (), None)
    return UnitItemization(held, tuple(i for i in items if i not in gloves), THIEFS_GLOVES)
