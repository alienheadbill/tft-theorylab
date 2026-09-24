"""Refreshing the cached current-set game art from CommunityDragon.

`tftlab refresh-game-art` (normally run by the manual "Game art refresh"
GitHub workflow, since it needs the network) downloads the current set's
champion portraits, trait icons and standard item icons once, normalizes
them, and writes:

- the images under `web/static/game/{champions,items,traits}/<id>.png`,
  served by the app itself at `/static/game/...`;
- `data/game_art_manifest.json`, mapping each canonical Riot /
  CommunityDragon id to its local file. It lives outside the static folder,
  so provenance paths are never served to browsers.

Only URLs derived from CommunityDragon's own metadata are ever fetched; no
URL can be passed in. The site never calls this at page load.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .items import is_component
from .roster import load_roster

if TYPE_CHECKING:
    from .cdragon import CommunityDragonClient

PACKAGE_DIR = Path(__file__).resolve().parent
ART_DIR = PACKAGE_DIR / "web" / "static" / "game"
MANIFEST_PATH = PACKAGE_DIR / "data" / "game_art_manifest.json"

MANIFEST_VERSION = 1
KINDS = ("champions", "items", "traits")
# Longest side of the cached copy, in pixels (roughly 2x the largest size
# each kind is drawn at, for sharp high-DPI screens).
MAX_SIZE = {"champions": 128, "items": 64, "traits": 64}

# Craftable variants that share a standard item's display name.
_VARIANT_ITEM_PREFIXES = ("TFT_Item_Corrupted",)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpeg"}


def empty_manifest() -> dict[str, Any]:
    return {"manifest_version": MANIFEST_VERSION, "set_number": None, **{k: {} for k in KINDS}, "missing": {}}


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
      sets' variants, radiant/artifact items and event items are left out,
      as are the craftable "Corrupted" variants, which reuse the base
      items' names, and unnamed placeholders.
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
            iid.startswith("TFT_Item_") and not iid.startswith(_VARIANT_ITEM_PREFIXES)
            and len(item.composition) == 2 and set(item.composition) <= components
        )
        if (iid in components or standard_craft) and item.name != iid:
            picks.append(Selection("items", iid, item.name, item.icon_url, {"component": iid in components}))

    names = Counter(p.name for p in picks if p.kind == "items")
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
        "duplicate_item_names": sorted(n for n, count in names.items() if count > 1),
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

    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else empty_manifest()
    manifest = empty_manifest()
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
    return report
