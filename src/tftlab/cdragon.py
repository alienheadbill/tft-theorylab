from __future__ import annotations

import json
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

    Keeps the shared `TFT_Item_*` items and this set's `TFT<n>_Item_*` items
    (emblems included), with their readable stat effects and tags only."""
    prefixes = ("TFT_Item_", f"TFT{meta.set_number}_Item_")
    return {
        "set_number": meta.set_number,
        "items": {
            item_id: {"name": item.name, "stat_effects": list(item.stat_effects), "tags": list(item.tags)}
            for item_id, item in sorted(meta.items.items())
            if item_id.startswith(prefixes)
        },
    }
