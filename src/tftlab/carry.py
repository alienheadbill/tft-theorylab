"""Carry eligibility: was this unit actually itemized like a carry on this board?

A carry commitment is a champion that finished a board with >= 2 completed
items (meaningful itemization; 2-star misses included) **unless** positive
item-metadata evidence shows that every one of those completed items is
purely defensive -- a unit holding e.g. Warmog's Armor + Gargoyle Stoneplate
is itemized as a tank, not a carry, whatever its star level.

The decision is per observed board and uses item metadata only: no champion
role, allowlist or tank list. The same champion can be excluded on one board
(Elise with Warmog's + Gargoyle) and included on another (Elise with an
emblem + Guinsoo's), which is exactly what lets off-meta carries surface.

Item classes come from CommunityDragon stat metadata (`ItemMeta.stat_effects`
and readable `tags`, committed as `data/item_stats.json` so analytics never
calls the network), keyed by the exact item ids Match-V1 boards store. For
Set 18 those are `DA_*` ids (`DA_GargoyleStoneplate`, `DA_GuinsoosRageblade`,
...) whose own CommunityDragon entries have no named effects; each carries
the stats of its `TFT_Item_*` counterpart only when that alias is verified
unambiguous (see `tftlab.cdragon.item_stats_snapshot`):

- OFFENSIVE: any stat that raises the holder's own damage (see
  `OFFENSIVE_EFFECTS` / `OFFENSIVE_TAGS`). An item with offensive *and*
  defensive stats is offensive (e.g. Titan's Resolve, Sterak's Gage,
  Crownguard). Ally-buff stats (Zephyr's `AllyBonusAS`, Aegis's `ASBuff`,
  Banshee's `BuffAttackSpeed`, Chalice's `ChaliceAP`, Zeke's `AttackSpeed`,
  ...) are deliberately not offensive.
- DEFENSIVE: at least one defensive stat (`Health`, `Armor`, `MagicResist`
  effects, or the `Health` tag) and no offensive stat.
- UNKNOWN: anything else -- not in the snapshot, only unresolved `{hash}`
  names, or only non-stat passives. Unknown never proves anything.

A board is excluded only when every completed item is DEFENSIVE. One
offensive or unknown completed item keeps it eligible: uncertain => include,
because a false negative could hide exactly the unusual build Theory Lab is
looking for. Set 18 trait emblems (`DA_18_Emblem*`, e.g. Ravager Emblem =
`DA_18_EmblemSlayer`) carry no readable stats in CommunityDragon, so an
emblem is UNKNOWN and always keeps its board eligible.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .items import is_component

ITEM_STATS_PATH = Path(__file__).parent / "data" / "item_stats.json"

#: Readable CommunityDragon stat names, normalized explicitly (never from
#: an item's English display name).
OFFENSIVE_EFFECTS = frozenset({
    "AD", "AP", "AS", "CritChance",
    # Variants of the same stats on Set 18 items (audited 2026-09-26):
    "AD_NotStatBar", "AP_NotStatBar",  # Hand of Justice
    "ADIncrease", "APIncrease",  # Spite
    "StackingAD", "StackingSP",  # Titan's Resolve
    "ADOnAttack",  # Kraken's Fury
    "ADPerBonus", "APPerBonus", "ASPerStack",  # Flickerblades
    "AttackSpeedPerStack",  # Guinsoo's Rageblade
    "ADAPPerTakedown",  # Cappa Juice
    "CritDamageToGive", "CritDamageBonusPercent",  # Infinity Edge / JG, Prowler's Claw
    "DamageAmp",  # Giant Slayer, The Eternal Flame, Talisman of Ascension
})
OFFENSIVE_TAGS = frozenset({"AttackDamage", "AbilityPower", "AttackSpeed", "CritChance"})
DEFENSIVE_EFFECTS = frozenset({"Health", "Armor", "MagicResist"})
DEFENSIVE_TAGS = frozenset({"Health"})

OFFENSIVE = "offensive"
DEFENSIVE = "defensive"
UNKNOWN = "unknown"


def classify_item(meta: Mapping[str, Any] | None) -> str:
    """OFFENSIVE / DEFENSIVE / UNKNOWN for one item's snapshot entry."""
    if not meta:
        return UNKNOWN
    effects = set(meta.get("stat_effects") or ())
    tags = set(meta.get("tags") or ())
    if effects & OFFENSIVE_EFFECTS or tags & OFFENSIVE_TAGS:
        return OFFENSIVE
    if effects & DEFENSIVE_EFFECTS or tags & DEFENSIVE_TAGS:
        return DEFENSIVE
    return UNKNOWN


@lru_cache(maxsize=1)
def load_item_stats(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Committed item-stat snapshot, loaded once per process. A missing file
    means no item is known, so no board is ever excluded."""
    target = Path(path) if path else ITEM_STATS_PATH
    if not target.exists():
        return {}
    return dict(json.loads(target.read_text()).get("items") or {})


def defensive_item_ids(item_stats: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, ...]:
    """Completed items classified DEFENSIVE, sorted. Components (by
    `tftlab.items.is_component` or CommunityDragon's readable `component`
    tag) are never included: they do not count as completed items, so they
    must not count toward an all-defensive package either."""
    stats = load_item_stats() if item_stats is None else item_stats
    return tuple(sorted(
        i for i, m in stats.items()
        if not is_component(i) and "component" not in (m.get("tags") or ()) and classify_item(m) == DEFENSIVE
    ))


def is_carry_observation(
    item_ids: Iterable[str],
    *,
    commitment_items: int = 2,
    item_stats: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    """True when a unit holding `item_ids` counts as a carry commitment."""
    completed = [i for i in item_ids if i and not is_component(i)]
    if len(completed) < commitment_items:
        return False
    defensive = set(defensive_item_ids(item_stats))
    return not all(i in defensive for i in completed)


def carry_commitment_sql(
    alias: str,
    commitment_items: int = 2,
    item_stats: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, list[Any]]:
    """The same rule as `is_carry_observation`, as a SQL condition on a
    `units` row aliased `alias`, with its parameters.

    Eligible when `completed_item_count >= commitment_items` and fewer than
    all of those completed items are known-defensive. Defensive items are
    counted exactly (duplicates included) from `items_json` by quoted-name
    occurrence: (LENGTH(json) - LENGTH(REPLACE(json, '"ID"', ''))) /
    LENGTH('"ID"'). Quotes on both sides stop one id matching inside
    another; LENGTH/REPLACE behave the same in SQLite and Postgres."""
    ids = defensive_item_ids(item_stats)
    base = f"{alias}.completed_item_count >= ?"
    if not ids:
        return f"({base})", [commitment_items]
    terms = " + ".join(
        f"(LENGTH({alias}.items_json) - LENGTH(REPLACE({alias}.items_json, ?, ''))) / LENGTH(?)" for _ in ids
    )
    params: list[Any] = [commitment_items]
    for item_id in ids:
        quoted = json.dumps(item_id)
        params += [quoted, quoted]
    return f"({base} AND {alias}.completed_item_count > ({terms}))", params
