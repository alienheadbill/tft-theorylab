from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

CDRAGON_BASE = "https://raw.communitydragon.org"


@dataclass(frozen=True)
class ChampionMeta:
    character_id: str
    name: str
    cost: int
    icon_url: str | None
    #: Traits as CommunityDragon lists them (display names). Shop champions have at least
    #: one; some special units have none, others (with a cost) have traits too.
    traits: tuple[str, ...] = ()
    #: CommunityDragon's champion `role` (e.g. "APTank", "ADTank", "APCaster")
    #: exactly as served, or None when it is null. For Set 18 (checked
    #: 2026-09-26) only 2 of 74 shop champions have a value, so nothing may
    #: treat a missing role as meaning anything.
    role: str | None = None


@dataclass(frozen=True)
class ItemMeta:
    item_id: str
    name: str
    icon_url: str | None
    #: Component apiNames this item is built from (empty for components
    #: and uncraftable items).
    composition: tuple[str, ...] = ()
    #: Named stat keys from CommunityDragon's `effects` (e.g. "AD", "AP",
    #: "AS", "CritChance", "Health", "Armor", "MagicResist"); hashed
    #: `{xxxxxxxx}` keys are dropped because their meaning is unknown.
    stat_effects: tuple[str, ...] = ()
    #: Readable `tags` (e.g. "AbilityPower", "AttackDamage", "Health");
    #: hashed tags are dropped for the same reason.
    tags: tuple[str, ...] = ()
    #: Trait apiNames the item is tied to (`associatedTraits`, e.g. emblems).
    associated_traits: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraitMeta:
    trait_id: str
    name: str
    icon_url: str | None


@dataclass(frozen=True)
class SetMetadata:
    """Patch-aware TFT static metadata: champion costs, item/trait names and art."""

    patch: str
    set_number: int | None
    champions: dict[str, ChampionMeta]
    items: dict[str, ItemMeta]
    traits: dict[str, TraitMeta]

    def cost_for_champion(self, character_id: str) -> int | None:
        champion = self.champions.get(character_id)
        return champion.cost if champion else None


def _icon_url(patch: str, relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    path = relative_path.lower()
    # Game textures are listed by their packed names (.tex/.dds); the raw
    # CommunityDragon mirror serves them converted to .png.
    for ext in (".tex", ".dds"):
        if path.endswith(ext):
            path = path[: -len(ext)] + ".png"
    return f"{CDRAGON_BASE}/{patch}/game/{path}"


def _readable(values: Any) -> tuple[str, ...]:
    """Drop CommunityDragon's unresolved `{hash}` names, keep order."""
    return tuple(str(v) for v in (values or []) if v and not str(v).startswith("{"))


def _set_number_of(entry: dict[str, Any]) -> int:
    return int(entry.get("number") or 0)


def parse_set_metadata(
    payload: dict[str, Any],
    *,
    patch: str,
    set_number: int | None = None,
) -> SetMetadata:
    """Parse a CommunityDragon TFT bundle (`cdragon/tft/<locale>.json` shape).

    When `set_number` is given, only that set's champions/traits are kept;
    otherwise the highest set number present is treated as the current set.
    Items are shared across the whole bundle rather than scoped per set.
    """
    all_sets = payload.get("setData") or payload.get("sets") or []
    if not all_sets:
        raise ValueError("CommunityDragon payload has no set data")

    if set_number is not None:
        chosen = next((s for s in all_sets if _set_number_of(s) == set_number), None)
        if chosen is None:
            raise ValueError(f"Set {set_number} not found in CommunityDragon payload")
    else:
        chosen = max(all_sets, key=_set_number_of)

    champions = {
        str(c["apiName"]): ChampionMeta(
            character_id=str(c["apiName"]),
            name=str(c.get("name") or c["apiName"]),
            cost=int(c.get("cost") or 0),
            icon_url=_icon_url(patch, c.get("squareIcon") or c.get("icon")),
            traits=tuple(str(t) for t in (c.get("traits") or []) if t),
            role=str(c["role"]) if c.get("role") else None,
        )
        for c in chosen.get("champions", [])
        if c.get("apiName")
    }
    traits = {
        str(t["apiName"]): TraitMeta(
            trait_id=str(t["apiName"]),
            name=str(t.get("name") or t["apiName"]),
            icon_url=_icon_url(patch, t.get("icon")),
        )
        for t in chosen.get("traits", [])
        if t.get("apiName")
    }
    items = {
        str(i["apiName"]): ItemMeta(
            item_id=str(i["apiName"]),
            name=str(i.get("name") or i["apiName"]),
            icon_url=_icon_url(patch, i.get("icon")),
            composition=tuple(str(x) for x in (i.get("composition") or []) if x),
            stat_effects=_readable(sorted((i.get("effects") or {}).keys())),
            tags=_readable(i.get("tags")),
            associated_traits=tuple(str(t) for t in (i.get("associatedTraits") or []) if t),
        )
        for i in payload.get("items", [])
        if i.get("apiName")
    }
    return SetMetadata(
        patch=patch,
        set_number=_set_number_of(chosen),
        champions=champions,
        items=items,
        traits=traits,
    )


class CommunityDragonClient:
    """Fetches and disk-caches TFT static metadata from CommunityDragon.

    Injecting `client` lets tests/CI point this at a mock transport instead of
    the real network; injecting `cache_dir` keeps repeated CLI/ingest runs from
    re-fetching the same patch bundle every time.
    """

    def __init__(
        self,
        *,
        cache_dir: Path | str = "data/cdragon-cache",
        client: httpx.Client | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "CommunityDragonClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _cache_path(self, patch: str) -> Path:
        safe = patch.replace("/", "_")
        return self.cache_dir / f"{safe}.json"

    def fetch_raw(self, patch: str = "latest", *, use_cache: bool = True) -> dict[str, Any]:
        cache_path = self._cache_path(patch)
        if use_cache and cache_path.exists():
            return json.loads(cache_path.read_text())

        response = self._client.get(f"{CDRAGON_BASE}/{patch}/cdragon/tft/en_us.json")
        response.raise_for_status()
        data = response.json()

        if use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data))
        return data

    def fetch_game_json(self, path: str, patch: str = "latest") -> Any:
        """One raw game-data file as CommunityDragon exports it, e.g.
        `MAP22_BIN_PATH`. Never cached and never called by analytics or the
        web app: only the explicit snapshot refresh (the opt-in live tests)
        reads these."""
        response = self._client.get(f"{CDRAGON_BASE}/{patch}/game/{path}")
        response.raise_for_status()
        return response.json()

    def get_set_metadata(
        self,
        patch: str = "latest",
        *,
        set_number: int | None = None,
        use_cache: bool = True,
    ) -> SetMetadata:
        raw = self.fetch_raw(patch, use_cache=use_cache)
        return parse_set_metadata(raw, patch=patch, set_number=set_number)


def item_stats_snapshot(meta: SetMetadata) -> dict[str, Any]:
    """The committed-fixture shape of the current set's item stat metadata
    (`src/tftlab/data/item_stats.json`, used offline by carry eligibility).

    Keyed by the exact apiNames Match-V1 boards can store:

    - the shared `TFT_Item_*` and this set's `TFT<n>_Item_*` items;
    - this set's equipment in the `DA_*` namespace (Set 18 boards store e.g.
      `DA_GargoyleStoneplate`): craftable `DA_*` items (those with a
      composition), `DA_Component_*` components, `DA_<n>_Emblem*` emblems and
      `DA_Item_*`. The rest of `DA_*` -- augments and augment-like entries,
      some with stats such as `Health` -- is deliberately left out.

    `DA_*` entries carry little readable metadata of their own (no named
    `effects` at all; only some readable tags), so each also gets the
    readable stats of its `TFT_Item_*` counterpart **only when that alias is
    unambiguous**: same exact display name, component names not
    contradicting, and every remaining candidate sharing one identical stat
    signature (e.g. a Corrupted copy with the same stats). The display name,
    not the id, is the bridge -- `DA_RedBuff` ("Red Buff") is
    `TFT_Item_RapidFireCannon`, while `TFT_Item_RedBuff` is "Sunfire Cape".
    Otherwise the item keeps only its own metadata (often none: unknown)."""
    n = meta.set_number
    tft = {i: m for i, m in meta.items.items() if i.startswith(("TFT_Item_", f"TFT{n}_Item_"))}
    by_name: dict[str, list[str]] = {}
    for item_id, item in tft.items():
        by_name.setdefault(item.name, []).append(item_id)

    def component_names(item: ItemMeta) -> list[str]:
        return sorted((meta.items[c].name if c in meta.items else c).casefold() for c in item.composition)

    def da_relevant(item_id: str, item: ItemMeta) -> bool:
        if not item_id.startswith("DA_"):
            return False
        return bool(item.composition) or item_id.startswith(("DA_Component_", f"DA_{n}_Emblem", "DA_Item_"))

    items: dict[str, dict[str, Any]] = {
        item_id: {"name": item.name, "stat_effects": list(item.stat_effects), "tags": list(item.tags)}
        for item_id, item in tft.items()
    }
    for item_id, item in meta.items.items():
        if not da_relevant(item_id, item):
            continue
        candidates = [
            c for c in by_name.get(item.name, [])
            if not (item.composition and tft[c].composition and component_names(tft[c]) != component_names(item))
        ]
        signatures = {(tft[c].stat_effects, tft[c].tags) for c in candidates}
        alias = sorted(candidates) if len(signatures) == 1 else []
        effects, tags = (next(iter(signatures)) if alias else ((), ()))
        items[item_id] = {
            "name": item.name,
            "stat_effects": sorted(set(item.stat_effects) | set(effects)),
            "tags": sorted(set(item.tags) | set(tags)),
            "own_stat_effects": list(item.stat_effects),
            "own_tags": list(item.tags),
            "alias_of": alias,
        }
    return {"set_number": n, "items": dict(sorted(items.items()))}


# ---------------------------------------------------------------- Riot item intent

#: Riot's TFT map data (CommunityDragon's export of `data/maps/shipping/
#: map22/map22.bin`): holds every `TFTCharacterRoleData` object -- a role's
#: internal `name`, its UI string keys and its recommended `items`.
MAP22_BIN_PATH = "data/maps/shipping/map22/map22.bin.json"
#: Riot's English TFT string table: resolves role UI string keys to the
#: names the client shows ("Magic Tank", "Attack Marksman", ...).
TFT_STRINGTABLE_PATH = "en_us/data/menu/en_us/tft.stringtable.json"
#: A role's recommended items are links to TftItemData entries by path.
ROLE_ITEM_PREFIX = "Maps/Shipping/Map22/Items/"
#: The UI string key of a role in Riot's current role vocabulary
#: ("TFT_CharacterRole_RolesRevamped_APTank_Name" -> "Magic Tank"). It sits
#: under a hashed field name, so it is found by value, not by field.
_ROLE_NAME_KEY = re.compile(r"^TFT_CharacterRole_RolesRevamped_[A-Za-z0-9]+_Name$")
TANK_FAMILY = "tank"
NON_TANK_FAMILY = "non_tank"
#: Riot's current role UI names are "<Attack|Magic|Hybrid> <family>"; the
#: family word decides Tank vs non-Tank. Every family Riot used on
#: 2026-09-26 is listed; a new word fails the snapshot build loudly
#: instead of being guessed.
ROLE_FAMILIES: dict[str, str] = {
    "Tank": TANK_FAMILY,
    "Assassin": NON_TANK_FAMILY,
    "Caster": NON_TANK_FAMILY,
    "Fighter": NON_TANK_FAMILY,
    "Marksman": NON_TANK_FAMILY,
    "Specialist": NON_TANK_FAMILY,
}


def riot_roles(map_bin: dict[str, Any], strings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every `TFTCharacterRoleData` in Riot's map data, by internal role name.

    A role in the current vocabulary has one `RolesRevamped` UI name key; its
    English name gives the family. Legacy role objects without one (e.g.
    `ADCarryCrit`, `TutorialADCarry`) are kept for provenance with
    `family: None` and contribute no item evidence. Raises ValueError when a
    current role cannot be classified (missing UI string, unknown family
    word, conflicting name keys, duplicate role names)."""
    entries = strings.get("entries", strings) if isinstance(strings, dict) else {}
    ui_text = {str(k).lower(): str(v) for k, v in entries.items()}
    roles: dict[str, dict[str, Any]] = {}
    for key, obj in sorted(map_bin.items()):
        if not isinstance(obj, dict) or obj.get("__type") != "TFTCharacterRoleData":
            continue
        name = str(obj.get("name") or key)
        if name in roles:
            raise ValueError(f"two TFTCharacterRoleData objects are named {name!r}")
        items, unresolved = [], []
        for ref in obj.get("items") or []:
            ref = str(ref)
            rest = ref[len(ROLE_ITEM_PREFIX):] if ref.startswith(ROLE_ITEM_PREFIX) else ""
            (items if rest and "/" not in rest else unresolved).append(rest or ref)
        name_keys = sorted({v for v in obj.values() if isinstance(v, str) and _ROLE_NAME_KEY.match(v)})
        role: dict[str, Any] = {"object": key, "ui_name_key": None, "ui_name": None, "family": None,
                                "recommended_items": items, "unresolved_items": unresolved}
        if len(name_keys) > 1:
            raise ValueError(f"role {name!r} has conflicting UI name keys {name_keys}")
        if name_keys:
            ui_name = ui_text.get(name_keys[0].lower())
            if not ui_name:
                raise ValueError(f"role {name!r}: UI string {name_keys[0]} is missing from the TFT string table")
            family = ROLE_FAMILIES.get(ui_name.split()[-1])
            if family is None:
                raise ValueError(f"role {name!r} ({ui_name!r}) has no known Tank/non-Tank family")
            role.update(ui_name_key=name_keys[0], ui_name=ui_name, family=family)
        roles[name] = role
    return roles


def _normalized_name(text: str) -> str:
    """Display names compared without case or punctuation ("Warmogs Armor"
    is "Warmog's Armor")."""
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def item_intent_snapshot(meta: SetMetadata, map_bin: dict[str, Any], strings: dict[str, Any]) -> dict[str, Any]:
    """The committed-fixture shape of Riot item-intent evidence for the
    current set (`src/tftlab/data/item_intent.json`, used offline by
    `tftlab.carry`).

    Items are the completed items of `item_stats_snapshot(meta)` (the exact
    ids Match-V1 boards store; components are left out). Riot's role lists
    name `TFT_Item_*` ids, so:

    - a `TFT_Item_*`/`TFT<n>_Item_*` id is its own Riot item;
    - a `DA_*` id is bridged to the `TFT_Item_*` items with the same display
      name (case and punctuation ignored), dropping candidates whose
      component names contradict -- every Corrupted/Academy copy of a name
      shares its evidence. No candidate (e.g. trait emblems) means
      unresolved, i.e. UNKNOWN.

    Each item keeps its evidence: the Riot items it resolved to and exactly
    which Tank and non-Tank roles recommend them, plus the derived intent
    (`tftlab.carry.intent_from_recommendations`)."""
    from .carry import intent_from_recommendations
    from .items import LEGACY_COMPONENT_IDS

    roles = riot_roles(map_bin, strings)
    recommended: dict[str, dict[str, set[str]]] = {}
    for role_name, role in roles.items():
        if role["family"] is None:
            continue
        for item_id in role["recommended_items"]:
            recommended.setdefault(item_id, {TANK_FAMILY: set(), NON_TANK_FAMILY: set()})[role["family"]].add(role_name)

    stats = item_stats_snapshot(meta)["items"]
    riot_ids = {i for i in stats if not i.startswith("DA_")}
    by_name: dict[str, list[str]] = {}
    for item_id in riot_ids:
        by_name.setdefault(_normalized_name(stats[item_id]["name"]), []).append(item_id)

    def component_names(item_id: str) -> list[str]:
        item = meta.items.get(item_id)
        return sorted(_normalized_name(meta.items[c].name if c in meta.items else c) for c in (item.composition if item else ()))

    items: dict[str, dict[str, Any]] = {}
    for item_id, stat in stats.items():
        if item_id in LEGACY_COMPONENT_IDS or "component" in (stat.get("tags") or ()):
            continue
        if item_id in riot_ids:
            resolved = [item_id]
        else:
            own = component_names(item_id)
            resolved = sorted(
                c for c in by_name.get(_normalized_name(stat["name"]), [])
                if not (own and component_names(c) and component_names(c) != own)
            )
        tank = sorted({r for c in resolved for r in recommended.get(c, {}).get(TANK_FAMILY, ())})
        non_tank = sorted({r for c in resolved for r in recommended.get(c, {}).get(NON_TANK_FAMILY, ())})
        items[item_id] = {
            "name": stat["name"],
            "riot_items": resolved,
            "recommended_by_tank_roles": tank,
            "recommended_by_non_tank_roles": non_tank,
            "intent": intent_from_recommendations(resolved=bool(resolved), tank_roles=tank, non_tank_roles=non_tank),
        }
    return {"set_number": meta.set_number, "roles": roles, "items": dict(sorted(items.items()))}


def champion_role_coverage(
    meta: SetMetadata, map_bin: dict[str, Any], records: dict[str, dict[str, Any] | None]
) -> dict[str, Any]:
    """How many current-set shop champions (cost 1-5 with traits) Riot links
    to a role via `TFTCharacterRecord.CharacterRole`. `records` maps each
    shop champion id to its record (None when its character file is
    missing). Report-only: carry eligibility never reads champion roles."""
    role_names = {k: str(o.get("name")) for k, o in map_bin.items()
                  if isinstance(o, dict) and o.get("__type") == "TFTCharacterRoleData"}
    shop = sorted(c for c, m in meta.champions.items() if 1 <= m.cost <= 5 and m.traits)
    linked: dict[str, str] = {}
    for champion in shop:
        link = (records.get(champion) or {}).get("CharacterRole")
        if link:
            linked[champion] = role_names.get(str(link), str(link))
    return {
        "shop_champions": len(shop),
        "with_role": dict(sorted(linked.items())),
        "missing_record": sorted(c for c in shop if records.get(c) is None),
    }
