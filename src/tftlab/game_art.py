"""Cached current-set game art: champion portraits, item and trait icons.

CommunityDragon is where the art comes from, but the site never asks it for
anything: `tftlab.game_art_refresh` caches the images into the package and
writes a manifest keyed by canonical Riot / CommunityDragon id. This module
only reads that manifest. Resolution is by canonical id first; a display-name
fallback is used only when it matches exactly one entry, and anything
unresolved returns None so the page shows its text/initials fallback. No
network calls.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .game_art_refresh import ART_DIR, MANIFEST_PATH, empty_manifest
from .roster import id_key, load_roster, name_key

if TYPE_CHECKING:
    from .experiments import Experiment

URL_PREFIX = "/static/game/"


# ---------------------------------------------------------------- reading


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """The committed manifest, or an empty one if none has been generated."""
    if not MANIFEST_PATH.exists():
        return empty_manifest()
    return json.loads(MANIFEST_PATH.read_text())


@lru_cache(maxsize=4096)
def _file_exists(relative: str) -> bool:
    return (ART_DIR / relative).is_file()


def _url(kind: str, key: str | None) -> str | None:
    entry = load_manifest().get(kind, {}).get(key or "")
    if not entry or not _file_exists(entry["file"]):
        return None
    # The content hash in the query string keeps URLs stable between
    # refreshes and changes them only when the image itself changes.
    return f"{URL_PREFIX}{entry['file']}?v={entry['sha256'][:10]}"


def _unique_by_name(kind: str, text: str | None, extra_key: Any = None) -> str | None:
    """The one manifest id whose display name matches (same normalization as
    the roster), else the one whose id-derived key does; None if nothing or
    more than one matches at either step, never a guess."""
    key = name_key(text)
    if not key:
        return None
    entries = load_manifest().get(kind, {})
    for matches in (
        lambda k, e: name_key(e["name"]) == key,
        lambda k, e: bool(extra_key) and extra_key(k) == key,
    ):
        hits = [k for k, e in entries.items() if matches(k, e)]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def champion_art(character_id: str | None = None, name: str | None = None) -> str | None:
    """By canonical id; by display name only if the id is absent/unknown and
    exactly one cached champion has that name ("Lux" never matches a Lux form)."""
    champions = load_manifest().get("champions", {})
    if character_id and character_id in champions:
        return _url("champions", character_id)
    return _url("champions", _unique_by_name("champions", name)) if name else None


def item_art(item: str | None) -> str | None:
    """By canonical id ("TFT_Item_InfinityEdge"), else an exact, unique match
    on the display name or the id's normalized form ("Infinity Edge")."""
    if not item:
        return None
    if item in load_manifest().get("items", {}):
        return _url("items", item)
    return _url("items", _unique_by_name("items", item, extra_key=id_key))


def trait_art(trait: str | None) -> str | None:
    """By canonical id ("DA_18_Slayer"), else a unique display-name match ("Ravager")."""
    if not trait:
        return None
    if trait in load_manifest().get("traits", {}):
        return _url("traits", trait)
    return _url("traits", _unique_by_name("traits", trait))


def item_name(item_id: str) -> str | None:
    entry = load_manifest().get("items", {}).get(item_id)
    return entry["name"] if entry else None


def trait_name(trait_id: str) -> str | None:
    entry = load_manifest().get("traits", {}).get(trait_id)
    return entry["name"] if entry else load_roster().trait_name(trait_id)


# ---------------------------------------------------------------- API enrichment

_BREAKPOINT_LABEL = re.compile(r"^(.*) \((\d+)\)$")


def _item_refs(package_key: str) -> list[dict[str, Any]]:
    """One ref per item in a package key ("TFT_Item_A+TFT_Item_B") or label
    ("Infinity Edge + Last Whisper")."""
    parts = [p.strip() for p in (package_key or "").split("+")]
    return [{"id": p, "name": item_name(p), "art_url": item_art(p)} for p in parts if p]


def enrich_association(assoc: dict[str, Any], kind: str) -> dict[str, Any]:
    """Add local art to one partner/item/trait association (additive keys only)."""
    if kind == "partner":
        assoc["art_url"] = champion_art(assoc.get("key"), assoc.get("label"))
    elif kind == "item":
        assoc["items"] = _item_refs(assoc.get("key") or "")
    elif kind == "trait":
        trait_id = (assoc.get("key") or "").rsplit(":", 1)[0]
        assoc["art_url"] = trait_art(trait_id)
        assoc["trait_name"] = trait_name(trait_id)
    return assoc


def enrich_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate["art_url"] = champion_art(candidate.get("character_id"), candidate.get("name"))
    for key, kind in (("best_partners", "partner"), ("best_item_packages", "item"), ("best_trait_breakpoints", "trait")):
        for assoc in candidate.get(key) or []:
            enrich_association(assoc, kind)
    return candidate


def experiment_art(experiment: Experiment) -> dict[str, Any]:
    """Local art for an experiment, index-aligned with its comp lists."""
    comp = experiment.comp
    secondary = comp.get("secondary_carry") or {}
    return {
        "carry": champion_art(experiment.carry_character_id, experiment.carry_name),
        "core_units": [champion_art(u.get("character_id"), u["name"]) for u in comp["core_units"]],
        "optional_units": [champion_art(u.get("character_id"), u["name"]) for u in comp["optional_units"]],
        "target_traits": [trait_art(t["name"]) for t in comp["target_traits"]],
        "carry_items": [item_art(i) for i in comp["carry_items"]],
        "tank_items": [item_art(i) for i in comp["tank_items"]],
        "secondary_carry": champion_art(name=secondary.get("unit")) if secondary.get("unit") else None,
        "secondary_items": [item_art(i) for i in secondary.get("items") or []],
    }


def _trait_from_label(label: str) -> str:
    m = _BREAKPOINT_LABEL.match(label or "")
    return m.group(1) if m else label


def field_note_art(note: dict[str, Any]) -> dict[str, Any] | None:
    """Local art for the evidence inside a saved "our data" note."""
    data = note.get("data") or {}
    if note.get("kind") != "riot_evidence" or data.get("status") != "ok":
        return None
    return {
        "carry": champion_art(data.get("character_id"), data.get("carry")),
        "best_partners": [champion_art(name=p.get("label")) for p in data.get("best_partners") or []],
        "best_item_packages": [_item_refs(p.get("label") or "") for p in data.get("best_item_packages") or []],
        "best_trait_breakpoints": [
            {"art_url": trait_art(tid), "trait_name": trait_name(tid)}
            for tid in (_trait_from_label(t.get("label") or "") for t in data.get("best_trait_breakpoints") or [])
        ],
        "core_units": [champion_art(name=u.get("name")) for u in data.get("core_units") or []],
        "trait_targets": [trait_art(t.get("name")) for t in data.get("trait_targets") or []],
    }
