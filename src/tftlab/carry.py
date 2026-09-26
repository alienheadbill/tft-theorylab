"""Carry eligibility: was this unit actually itemized like a carry on this board?

A carry commitment is a champion that finished a board with >= 2 completed
items (meaningful itemization; 2-star misses included) **and** at least one
of those completed items carries Riot-backed carry evidence. Riot semantics
first, TheoryLabs inference last: an item's purpose comes from Riot's own
role item recommendations, never from one raw stat (Steadfast Heart has
`CritChance`, yet no Riot role recommends it).

The decision is per observed board and uses item evidence only: no champion
role (Riot's `TFTCharacterRecord.CharacterRole` links only 2 of 74 Set 18
shop champions), no allowlist, no per-champion list. The same champion can be
excluded on one board (Elise with Warmog's + Gargoyle) and included on another
(Elise with an emblem + Guinsoo's), which is what lets off-meta carries
surface.

Item intent is read from `data/item_intent.json`, a committed snapshot
(`tftlab.cdragon.item_intent_snapshot`) of Riot's `TFTCharacterRoleData`
recommended-item lists, keyed by the exact item ids Match-V1 boards store.
Each role is Tank or non-Tank by its Riot UI name ("Magic Tank" vs "Attack
Marksman"); each completed item is:

- DAMAGE: recommended by one or more non-Tank roles and no Tank role.
- TANK: recommended by one or more Tank roles and no non-Tank role.
- MIXED: recommended by both (e.g. an item a Hybrid Tank and a Fighter share).
- KNOWN_UNLISTED: a known completed item that no role recommends. This is
  not "tank"; it only means Riot gives no role evidence for it (Steadfast
  Heart, Crownguard, Thief's Gloves, Tactician's items, artifacts).
- UNKNOWN: not resolvable in the snapshot -- future ids, items with no
  Riot counterpart in the recommendation namespace (Set 18 trait emblems
  such as Ravager Emblem = `DA_18_EmblemSlayer`), or unresolved aliases.

A board counts when any completed item is DAMAGE, MIXED or UNKNOWN (unknown
stays conservative: hiding an unusual build is worse than a false
positive). TANK and KNOWN_UNLISTED items alone never prove carry intent, so
Spirit Visage + Steadfast Heart or Crownguard + Warmog's is not a carry
observation, while Ravager Emblem + Guinsoo's or Titan's + Sterak's is.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .items import ITEM_INTENT_PATH, is_component

DAMAGE = "damage"
TANK = "tank"
MIXED = "mixed"
KNOWN_UNLISTED = "known_unlisted"
UNKNOWN = "unknown"
INTENTS = (DAMAGE, TANK, MIXED, KNOWN_UNLISTED, UNKNOWN)

#: Intents that prove a completed item was bought for a carry (UNKNOWN is
#: included conservatively). TANK and KNOWN_UNLISTED do not.
CARRY_EVIDENCE = frozenset({DAMAGE, MIXED, UNKNOWN})


def intent_from_recommendations(*, resolved: bool, tank_roles: Iterable[str], non_tank_roles: Iterable[str]) -> str:
    """The intent Riot's recommendations imply for one item. `resolved` is
    False when the item has no counterpart in the recommendation namespace
    (absence of a recommendation then says nothing)."""
    if not resolved:
        return UNKNOWN
    tank, non_tank = bool(list(tank_roles)), bool(list(non_tank_roles))
    if tank and non_tank:
        return MIXED
    if tank:
        return TANK
    if non_tank:
        return DAMAGE
    return KNOWN_UNLISTED


@lru_cache(maxsize=1)
def load_item_intent(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Committed item-intent snapshot, loaded once per process. A missing
    file means every item is UNKNOWN, so no board is ever excluded."""
    target = Path(path) if path else ITEM_INTENT_PATH
    if not target.exists():
        return {}
    return dict(json.loads(target.read_text()).get("items") or {})


def item_intent(item_id: str, item_intents: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    intents = load_item_intent() if item_intents is None else item_intents
    meta = intents.get(item_id)
    return str(meta.get("intent")) if meta and meta.get("intent") in INTENTS else UNKNOWN


def no_carry_evidence_item_ids(item_intents: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, ...]:
    """Completed items that alone never prove carry intent (TANK and
    KNOWN_UNLISTED), sorted. Components are never included: they do not
    count as completed items."""
    intents = load_item_intent() if item_intents is None else item_intents
    return tuple(sorted(
        i for i, m in intents.items()
        if not is_component(i) and m.get("intent") in INTENTS and m["intent"] not in CARRY_EVIDENCE
    ))


def is_carry_observation(
    item_ids: Iterable[str],
    *,
    commitment_items: int = 2,
    item_intents: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    """True when a unit holding `item_ids` counts as a carry commitment."""
    completed = [i for i in item_ids if i and not is_component(i)]
    if len(completed) < commitment_items:
        return False
    return any(item_intent(i, item_intents) in CARRY_EVIDENCE for i in completed)


#: Stands in for one no-evidence item while counting (see
#: `carry_commitment_sql`). JSON escapes control characters, so a raw \x01
#: never occurs in `items_json` itself.
_MARKER = "\x01"
#: REPLACE calls nested per chain: SQLite's parser rejects deep nesting.
_CHAIN = 16


def carry_commitment_sql(
    alias: str,
    commitment_items: int = 2,
    item_intents: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, list[Any]]:
    """The same rule as `is_carry_observation`, as a SQL condition on a
    `units` row aliased `alias`, with its parameters.

    Eligible when `completed_item_count >= commitment_items` and fewer than
    all of those completed items lack carry evidence. No-evidence items are
    counted exactly (duplicates included) from `items_json`: a chain of
    REPLACE calls turns every quoted no-evidence id ('"DA_WarmogsArmor"')
    into one marker character, and the markers are counted with
    LENGTH(chain) - LENGTH(REPLACE(chain, marker, '')). Quotes on both
    sides stop one id matching inside another; LENGTH/REPLACE behave the
    same in SQLite and Postgres. The chains for one id namespace ("DA_",
    "TFT_", ...) only run when the json contains `"<prefix>` at all: a Set 18
    board holds no `TFT_*` id, so those chains are skipped in one check.
    (One small expression, rather than one count per id, also keeps
    Postgres' JIT from compiling hundreds of terms per query.)"""
    ids = no_carry_evidence_item_ids(item_intents)
    base = f"{alias}.completed_item_count >= ?"
    if not ids:
        return f"({base})", [commitment_items]
    json_col = f"{alias}.items_json"
    namespaces: dict[str, list[str]] = {}
    for item_id in ids:
        namespaces.setdefault(item_id.split("_", 1)[0] + "_", []).append(item_id)
    groups, params = [], [commitment_items]
    for prefix, members in namespaces.items():
        counts, count_params = [], []
        for start in range(0, len(members), _CHAIN):
            chain, chain_params = json_col, []
            for item_id in members[start:start + _CHAIN]:
                chain = f"REPLACE({chain}, ?, ?)"
                chain_params += [json.dumps(item_id), _MARKER]
            counts.append(f"(LENGTH({chain}) - LENGTH(REPLACE({chain}, ?, '')))")
            count_params += chain_params + chain_params + [_MARKER]
        groups.append(
            f"(CASE WHEN LENGTH(REPLACE({json_col}, ?, '')) < LENGTH({json_col}) THEN {' + '.join(counts)} ELSE 0 END)"
        )
        params += [f'"{prefix}', *count_params]
    return f"({base} AND {alias}.completed_item_count > ({' + '.join(groups)}))", params
