"""Cached current-set game art: champion portraits, item and trait icons.

CommunityDragon is where the art comes from, but the site never asks it for
anything at page load. `refresh_game_art` (the `tftlab refresh-game-art`
command, normally run by the manual "Game art refresh" GitHub workflow)
downloads the current set's images once, normalizes them, and writes:

- the images under `web/static/game/{champions,items,traits}/<id>.png`,
  served by the app itself at `/static/game/...`;
- `data/game_art_manifest.json`, mapping each canonical Riot /
  CommunityDragon id to its local file. It lives outside the static folder,
  so provenance paths are never served to browsers.

Everything else in this module only reads that manifest: resolution is by
canonical id first; a display-name fallback is used only when it matches
exactly one entry, and anything unresolved returns None so the page shows
its text/initials fallback. No network calls.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .items import is_component
from .roster import id_key, load_roster, name_key

if TYPE_CHECKING:
    from .cdragon import CommunityDragonClient
    from .experiments import Experiment

PACKAGE_DIR = Path(__file__).resolve().parent
ART_DIR = PACKAGE_DIR / "web" / "static" / "game"
MANIFEST_PATH = PACKAGE_DIR / "data" / "game_art_manifest.json"
URL_PREFIX = "/static/game/"

MANIFEST_VERSION = 1
KINDS = ("champions", "items", "traits")
# Longest side of the cached copy, in pixels (roughly 2x the largest size
# each kind is drawn at, for sharp high-DPI screens).
MAX_SIZE = {"champions": 128, "items": 64, "traits": 64}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpeg"}


# ---------------------------------------------------------------- reading


def _empty_manifest() -> dict[str, Any]:
    return {"manifest_version": MANIFEST_VERSION, "set_number": None, **{k: {} for k in KINDS}, "missing": {}}


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """The committed manifest, or an empty one if none has been generated."""
    if not MANIFEST_PATH.exists():
        return _empty_manifest()
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
    """The one manifest id whose display name (or id-derived key) matches;
    None if nothing or more than one matches, never a guess."""
    key = name_key(text)
    if not key:
        return None
    hits = [
        k for k, e in load_manifest().get(kind, {}).items()
        if name_key(e["name"]) == key or (extra_key and extra_key(k) == key)
    ]
    return hits[0] if len(hits) == 1 else None


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
    return [{"id": k, "name": item_name(k), "art_url": item_art(k)} for k in package_key.split("+") if k]


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
        "best_trait_breakpoints": [trait_art(_trait_from_label(t.get("label") or "")) for t in data.get("best_trait_breakpoints") or []],
        "core_units": [champion_art(name=u.get("name")) for u in data.get("core_units") or []],
        "trait_targets": [trait_art(t.get("name")) for t in data.get("trait_targets") or []],
    }


# ---------------------------------------------------------------- refreshing


class GameArtError(RuntimeError):
    """The refresh couldn't produce a trustworthy asset set."""


@dataclass
class Selection:
    kind: str
    asset_id: str
    name: str
    url: str | None
    extra: dict[str, Any] = field(default_factory=dict)


def _relative_source(url: str) -> str:
    # Provenance only: the path under the CommunityDragon patch root.
    return url.split("/game/", 1)[1] if "/game/" in url else url


def select_assets(payload: dict[str, Any], *, patch: str = "latest") -> tuple[int, list[Selection], dict[str, Any]]:
    """Choose the current set's art from a CommunityDragon bundle.

    - Champions: the current set's units that are in our shipped roster
      (`data/set_roster.json`), cost 1-5, and have at least one trait.
      Summons, camps, anvils and other special units don't qualify.
    - Traits: every trait of the current set.
    - Items: the standard components (`tftlab.items`) plus every
      bundle-wide `TFT_Item_*` item crafted from exactly two of them. Older
      sets' variants, radiant/artifact items and event items are left out.
    """
    from .cdragon import parse_set_metadata

    meta = parse_set_metadata(payload, patch=patch)
    roster = load_roster()
    if meta.set_number != roster.set_number:
        raise GameArtError(
            f"CommunityDragon's current set is {meta.set_number}, but the shipped roster is set "
            f"{roster.set_number}. Refresh src/tftlab/data/set_roster.json first."
        )
    picks: list[Selection] = []
    for cid, c in sorted(meta.champions.items()):
        if cid in roster.champions and 1 <= c.cost <= 5 and c.traits:
            picks.append(Selection("champions", cid, c.name, c.icon_url, {"cost": c.cost}))
    for tid, t in sorted(meta.traits.items()):
        picks.append(Selection("traits", tid, t.name, t.icon_url))
    components = {iid for iid in meta.items if is_component(iid)}
    for iid, item in sorted(meta.items.items()):
        standard_craft = (
            iid.startswith("TFT_Item_") and len(item.composition) == 2 and set(item.composition) <= components
        )
        if iid in components or standard_craft:
            picks.append(Selection("items", iid, item.name, item.icon_url, {"component": iid in components}))

    # A short shape summary so a dry run shows what the bundle looks like.
    chosen = max(payload.get("setData") or payload.get("sets") or [], key=lambda s: int(s.get("number") or 0))
    shape = {
        "set_number": meta.set_number,
        "set_keys": sorted(chosen.keys()),
        "bundle_items": len(meta.items),
        "item_prefixes": Counter(i.split("_Item_")[0] + "_Item_" if "_Item_" in i else i.split("_")[0] for i in meta.items).most_common(12),
        "set_champions": len(meta.champions),
        "set_traits": len(meta.traits),
        "components_found": sorted(components),
        "roster_champions_not_selected": sorted(
            set(roster.champions) - {p.asset_id for p in picks if p.kind == "champions"}
        ),
    }
    return meta.set_number, picks, shape


def _sniff(data: bytes) -> str | None:
    for magic, fmt in _MAGIC.items():
        if data.startswith(magic):
            return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def normalize_image(data: bytes, max_size: int) -> tuple[bytes, int, int]:
    """Validate an image and re-encode it as a deterministic PNG no larger
    than `max_size` on its longest side (never upscaled; alpha kept)."""
    from PIL import Image  # optional dependency: pip install ".[art]"

    if _sniff(data) is None:
        raise GameArtError("not a PNG/JPEG/WebP image")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if not (4 <= img.width <= 4096 and 4 <= img.height <= 4096):
                raise GameArtError(f"implausible size {img.width}x{img.height}")
            img = img.convert("RGBA")
            if max(img.size) > max_size:
                scale = max_size / max(img.size)
                img = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS
                )
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue(), img.width, img.height
    except GameArtError:
        raise
    except Exception as exc:  # Pillow raises a variety of decode errors
        raise GameArtError(f"undecodable image ({type(exc).__name__})") from exc


@dataclass
class RefreshReport:
    set_number: int | None = None
    fetched: Counter = field(default_factory=Counter)
    unchanged: Counter = field(default_factory=Counter)
    cached: Counter = field(default_factory=Counter)
    missing: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in KINDS})
    failures: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    shape: dict[str, Any] = field(default_factory=dict)
    planned: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in KINDS})
    bytes_written: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures and self.cached["champions"] > 0 and self.cached["traits"] > 0 and self.cached["items"] > 0


def refresh_game_art(
    client: CommunityDragonClient,
    *,
    art_dir: Path | None = None,
    manifest_path: Path | None = None,
    dry_run: bool = False,
) -> RefreshReport:
    """Download the current set's art from CommunityDragon into the package.

    Only URLs derived from CommunityDragon's own metadata are fetched (never
    user input); every download is status-, size- and format-checked and
    re-encoded deterministically. An image already cached from the same
    source is re-requested conditionally (its stored ETag), so unchanged
    files aren't downloaded again. `dry_run` fetches only the metadata and
    reports what would be cached. Nothing on disk changes unless the whole
    run passes (`report.ok`).
    """
    from .cdragon import CDRAGON_BASE

    art_dir = art_dir or ART_DIR
    manifest_path = manifest_path or MANIFEST_PATH
    report = RefreshReport()
    payload = client.fetch_raw("latest", use_cache=False)
    report.set_number, picks, report.shape = select_assets(payload)

    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else _empty_manifest()
    manifest = _empty_manifest()
    manifest.update({
        "set_number": report.set_number,
        "source": "CommunityDragon latest cdragon/tft/en_us.json (via tftlab refresh-game-art)",
        "max_size": MAX_SIZE,
    })
    pending: dict[str, bytes] = {}

    for pick in picks:
        report.planned[pick.kind].append(f"{pick.asset_id} ({pick.name})")
        if not _SAFE_ID.match(pick.asset_id):
            report.failures.append(f"{pick.kind}/{pick.asset_id}: unsafe id, skipped")
            continue
        if not pick.url:
            report.missing[pick.kind].append(pick.asset_id)
            continue
        if not pick.url.startswith(f"{CDRAGON_BASE}/") or ".." in pick.url:
            report.failures.append(f"{pick.kind}/{pick.asset_id}: refusing non-CommunityDragon URL")
            continue
        relative_file = f"{pick.kind}/{pick.asset_id}.png"
        entry = {"name": pick.name, "file": relative_file, "source_path": _relative_source(pick.url), **pick.extra}
        if dry_run:
            report.cached[pick.kind] += 1
            continue

        target = art_dir / relative_file
        old = previous.get(pick.kind, {}).get(pick.asset_id) or {}
        current = target.read_bytes() if target.is_file() else None
        reusable = (
            current is not None and old.get("source_path") == entry["source_path"]
            and hashlib.sha256(current).hexdigest() == old.get("sha256")
        )
        headers = {"If-None-Match": old["source_etag"]} if reusable and old.get("source_etag") else {}
        try:
            response = client._client.get(pick.url, headers=headers, timeout=20.0)
            if response.status_code == 304 and headers:
                manifest[pick.kind][pick.asset_id] = {**old, **entry}
                report.unchanged[pick.kind] += 1
                report.cached[pick.kind] += 1
                continue
            if response.status_code != 200:
                raise GameArtError(f"HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise GameArtError(f"content-type {content_type or 'missing'}")
            if len(response.content) > _MAX_DOWNLOAD_BYTES:
                raise GameArtError("larger than 5 MB")
            png, width, height = normalize_image(response.content, MAX_SIZE[pick.kind])
        except Exception as exc:  # report every failure, never skip silently
            report.failures.append(f"{pick.kind}/{pick.asset_id}: {exc}")
            continue
        etag = response.headers.get("etag")
        manifest[pick.kind][pick.asset_id] = {
            **entry, "sha256": hashlib.sha256(png).hexdigest(), "width": width, "height": height,
            **({"source_etag": etag} if etag else {}),
        }
        report.cached[pick.kind] += 1
        if png == current:
            report.unchanged[pick.kind] += 1
        else:
            pending[relative_file] = png
            report.fetched[pick.kind] += 1

    manifest["missing"] = {k: sorted(v) for k, v in report.missing.items() if v}
    if dry_run or not report.ok:
        # A bad run leaves the previous assets and manifest untouched.
        return report

    for relative_file, png in sorted(pending.items()):
        target = art_dir / relative_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        report.bytes_written += len(png)
    for kind in KINDS:
        keep = {entry["file"] for entry in manifest[kind].values()}
        for path in sorted((art_dir / kind).glob("*.png")) if (art_dir / kind).exists() else []:
            if f"{kind}/{path.name}" not in keep:
                path.unlink()
                report.pruned.append(f"{kind}/{path.name}")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(manifest_path)
    load_manifest.cache_clear()
    _file_exists.cache_clear()
    return report
