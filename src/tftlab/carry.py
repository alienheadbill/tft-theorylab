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
- KNOWN_UNLISTED: an ordinary completed item inside Riot's recommendation
  domain that no role recommends (Steadfast Heart, Crownguard). This is not
  "tank"; it only means Riot considered this kind of item and gives no role
  evidence for it. The domain is Riot's own: items whose map22 `ItemTags`
  hold every tag all role-recommended items share and no tag none of them
  carry (see `tftlab.cdragon.item_intent_snapshot`).
- UNKNOWN: no usable evidence -- future ids, unresolved aliases, items with
  no Riot counterpart in the recommendation namespace (Set 18 trait emblems
  such as Ravager Emblem = `DA_18_EmblemSlayer`), and special items outside
  the recommendation domain (Tactician's items, artifacts such as Talisman
  of Ascension, Thief's Gloves), whose absence from role lists says nothing.

A board counts when any completed item is DAMAGE, MIXED or UNKNOWN (unknown
stays conservative: hiding an unusual build is worse than a false
positive). TANK and KNOWN_UNLISTED items alone never prove carry intent, so
Spirit Visage + Steadfast Heart or Crownguard + Warmog's is not a carry
observation, while Ravager Emblem + Guinsoo's, Titan's + Sterak's or
Talisman of Ascension + Warmog's is.

Contextual exception (a TheoryLabs interpretation, not a Riot semantic):
Adaptive Helm keeps its Riot-derived intent (DAMAGE -- recommended by
non-Tank caster roles; the snapshot is untouched), but for carry
eligibility it is weak evidence on its own: frontliners often hold it next
to defensive items. So when a unit holds Adaptive Helm, the carry evidence
must come from another, non-Adaptive completed item whose intent is DAMAGE
or MIXED. UNKNOWN, TANK and KNOWN_UNLISTED items do not corroborate it (nor
does another Adaptive Helm): Adaptive Helm + Warmog's, or Adaptive Helm + an
unknown item + Gargoyle, is not a carry observation, while Adaptive Helm +
Guinsoo's or Adaptive Helm + Titan's is. Boards without Adaptive Helm follow
the ordinary rule above, UNKNOWN included. Adaptive Helm is recognized by
Riot's item id (`TFT_Item_AdaptiveHelm`) through the snapshot's alias
bridge, so every Match-V1 id resolving to it (`DA_AdaptiveHelm`) is covered.
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

#: Riot's item for Adaptive Helm. Its source intent is left as Riot's
#: recommendations make it; only carry eligibility treats it contextually
#: (see the module docstring).
ADAPTIVE_HELM_RIOT_ITEM = "TFT_Item_AdaptiveHelm"
#: Intents that corroborate Adaptive Helm when it is on the unit.
ADAPTIVE_HELM_CORROBORATION = frozenset({DAMAGE, MIXED})


def intent_from_recommendations(
    *, resolved: bool, tank_roles: Iterable[str], non_tank_roles: Iterable[str], in_recommendation_domain: bool
) -> str:
    """The intent Riot's recommendations imply for one item. A
    recommendation is positive evidence wherever it appears; the absence of
    one is only evidence (KNOWN_UNLISTED) for a resolved item inside the
    recommendation domain -- otherwise it says nothing (UNKNOWN)."""
    if not resolved:
        return UNKNOWN
    tank, non_tank = bool(list(tank_roles)), bool(list(non_tank_roles))
    if tank and non_tank:
        return MIXED
    if tank:
        return TANK
    if non_tank:
        return DAMAGE
    return KNOWN_UNLISTED if in_recommendation_domain else UNKNOWN


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


def adaptive_helm_item_ids(item_intents: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, ...]:
    """Snapshot ids that are Adaptive Helm: Riot's own item and every id the
    snapshot resolves to it (`DA_AdaptiveHelm`), sorted. Empty without a
    snapshot, so a missing snapshot still excludes nothing."""
    intents = load_item_intent() if item_intents is None else item_intents
    return tuple(sorted(
        i for i, m in intents.items()
        if i == ADAPTIVE_HELM_RIOT_ITEM or ADAPTIVE_HELM_RIOT_ITEM in (m.get("riot_items") or ())
    ))


def adaptive_helm_corroborating_item_ids(
    item_intents: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, ...]:
    """Completed, non-Adaptive items whose intent corroborates Adaptive Helm
    (DAMAGE or MIXED), sorted."""
    intents = load_item_intent() if item_intents is None else item_intents
    adaptive = set(adaptive_helm_item_ids(intents))
    return tuple(sorted(
        i for i, m in intents.items()
        if i not in adaptive and not is_component(i) and m.get("intent") in ADAPTIVE_HELM_CORROBORATION
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
    adaptive = set(adaptive_helm_item_ids(item_intents))
    if adaptive.intersection(completed):
        # Adaptive Helm alone is not enough: another completed item must be DAMAGE or MIXED.
        return any(
            item_intent(i, item_intents) in ADAPTIVE_HELM_CORROBORATION for i in completed if i not in adaptive
        )
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
    Postgres' JIT from compiling hundreds of terms per query.)

    A unit holding Adaptive Helm (any id in `adaptive_helm_item_ids`) is
    instead eligible only when its json also holds a corroborating id
    (`adaptive_helm_corroborating_item_ids`: DAMAGE or MIXED, never Adaptive
    Helm itself) -- found the same way, as a REPLACE chain that shortens the
    json."""
    ids = no_carry_evidence_item_ids(item_intents)
    adaptive = adaptive_helm_item_ids(item_intents)
    base = f"{alias}.completed_item_count >= ?"
    if not ids and not adaptive:
        return f"({base})", [commitment_items]
    json_col = f"{alias}.items_json"
    params: list[Any] = [commitment_items]
    ordinary, ordinary_params = f"{alias}.completed_item_count > 0", []
    if ids:
        groups = []
        for prefix, members in _by_namespace(ids).items():
            counts, count_params = [], []
            for start in range(0, len(members), _CHAIN):
                chain, chain_params = _replace_chain(json_col, members[start:start + _CHAIN])
                counts.append(f"(LENGTH({chain}) - LENGTH(REPLACE({chain}, ?, '')))")
                count_params += chain_params + chain_params + [_MARKER]
            groups.append(f"(CASE WHEN {_contains(json_col)} THEN {' + '.join(counts)} ELSE 0 END)")
            ordinary_params += [f'"{prefix}', *count_params]
        ordinary = f"{alias}.completed_item_count > ({' + '.join(groups)})"
    if not adaptive:
        return f"({base} AND {ordinary})", params + ordinary_params
    holds_adaptive = " OR ".join(_contains(json_col) for _ in adaptive)
    holds_adaptive_params = [json.dumps(i) for i in adaptive]
    corroborated, corroborated_params = [], []
    for prefix, members in _by_namespace(adaptive_helm_corroborating_item_ids(item_intents)).items():
        chains, chains_params = [], []
        for start in range(0, len(members), _CHAIN):
            chain, chain_params = _replace_chain(json_col, members[start:start + _CHAIN])
            chains.append(f"LENGTH({chain}) < LENGTH({json_col})")
            chains_params += chain_params
        corroborated.append(f"({_contains(json_col)} AND ({' OR '.join(chains)}))")
        corroborated_params += [f'"{prefix}', *chains_params]
    condition = (
        f"CASE WHEN ({holds_adaptive}) THEN ({' OR '.join(corroborated) or '1 = 0'}) ELSE ({ordinary}) END"
    )
    return (
        f"({base} AND {condition})",
        params + holds_adaptive_params + corroborated_params + ordinary_params,
    )


def _by_namespace(ids: Iterable[str]) -> dict[str, list[str]]:
    namespaces: dict[str, list[str]] = {}
    for item_id in ids:
        namespaces.setdefault(item_id.split("_", 1)[0] + "_", []).append(item_id)
    return namespaces


def _replace_chain(json_col: str, members: Iterable[str]) -> tuple[str, list[Any]]:
    """REPLACE calls turning every quoted id in `members` into the marker."""
    chain, chain_params = json_col, []
    for item_id in members:
        chain = f"REPLACE({chain}, ?, ?)"
        chain_params += [json.dumps(item_id), _MARKER]
    return chain, chain_params


def _contains(json_col: str) -> str:
    """True when `json_col` holds the next parameter as a substring."""
    return f"LENGTH(REPLACE({json_col}, ?, '')) < LENGTH({json_col})"
