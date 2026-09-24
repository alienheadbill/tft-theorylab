"""The committed game-art manifest and files (offline).

Written by the "Game art refresh" workflow (`tftlab refresh-game-art`);
these check the committed result resolves the current set correctly.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest

from tftlab import game_art
from tftlab.demo import CARRIES, FILLERS, generate_demo_matches
from tftlab.experiments import DEMO_EXPERIMENTS
from tftlab.game_art import champion_art, item_art, load_manifest, trait_art
from tftlab.game_art_refresh import ART_DIR, MANIFEST_PATH, MANIFEST_VERSION, MAX_SIZE
from tftlab.roster import load_roster

REPO = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def _fresh_cache():
    game_art.load_manifest.cache_clear()
    game_art._file_exists.cache_clear()
    yield


def _file_of(url: str) -> Path:
    assert url.startswith("/static/game/"), url
    return ART_DIR / url.removeprefix("/static/game/").split("?", 1)[0]


def test_manifest_loads_with_set_and_version_metadata() -> None:
    manifest = load_manifest()
    assert manifest["manifest_version"] == MANIFEST_VERSION
    assert manifest["set_number"] == load_roster().set_number == 18
    assert manifest["missing"] == {}
    assert len(manifest["champions"]) >= 60 and len(manifest["traits"]) >= 30 and len(manifest["items"]) >= 40


def test_package_data_ships_the_manifest_and_art() -> None:
    patterns = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["tftlab"]
    assert "data/*.json" in patterns and "web/static/game/*/*.png" in patterns
    assert MANIFEST_PATH == Path(game_art.__file__).parent / "data" / "game_art_manifest.json"


def test_every_manifest_entry_has_its_file_and_hash() -> None:
    manifest = load_manifest()
    safe = re.compile(r"^(champions|items|traits)/[A-Za-z0-9_]+\.png$")
    for kind in ("champions", "items", "traits"):
        for asset_id, entry in manifest[kind].items():
            assert safe.match(entry["file"]) and entry["file"] == f"{kind}/{asset_id}.png"
            data = (ART_DIR / entry["file"]).read_bytes()
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
            assert max(entry["width"], entry["height"]) <= MAX_SIZE[kind]
            assert "://" not in json.dumps(entry)  # provenance paths only, never a remote URL
    on_disk = {p.relative_to(ART_DIR).as_posix() for p in ART_DIR.rglob("*.png")}
    listed = {e["file"] for kind in ("champions", "items", "traits") for e in manifest[kind].values()}
    assert on_disk == listed  # no orphaned files


def test_khazix_and_cassiopeia_resolve_to_their_own_art() -> None:
    khazix, cass = champion_art("DA_18_KhaZix"), champion_art("DA_18_Cassiopeia")
    assert _file_of(khazix).name == "DA_18_KhaZix.png"
    assert _file_of(cass).name == "DA_18_Cassiopeia.png"
    assert champion_art(name="Kha'Zix") == khazix
    assert load_manifest()["champions"]["DA_18_KhaZix"]["name"] == "Kha'Zix"


def test_lux_forms_do_not_collapse() -> None:
    lux_ids = [cid for cid, e in load_manifest()["champions"].items() if e["name"].startswith("Lux")]
    assert len(lux_ids) >= 2
    urls = {champion_art(cid) for cid in lux_ids}
    assert len(urls) == len(lux_ids) and None not in urls
    for cid in lux_ids:
        assert _file_of(champion_art(cid)).name == f"{cid}.png"
    # Plain "Lux" is only ever the unit whose display name is exactly "Lux".
    plain = [cid for cid in lux_ids if load_manifest()["champions"][cid]["name"] == "Lux"]
    assert champion_art(name="Lux") == (champion_art(plain[0]) if len(plain) == 1 else None)


def test_ravager_resolves_by_id_and_name() -> None:
    url = trait_art("DA_18_Slayer")
    assert _file_of(url).name == "DA_18_Slayer.png"
    assert trait_art("Ravager") == url


def test_infinity_edge_resolves_by_id_and_by_written_name() -> None:
    url = item_art("TFT_Item_InfinityEdge")
    assert _file_of(url).name == "TFT_Item_InfinityEdge.png"
    assert item_art("Infinity Edge") == item_art("infinity edge") == url


def test_every_resolved_url_is_a_real_local_file() -> None:
    manifest = load_manifest()
    for asset_id in manifest["champions"]:
        assert _file_of(champion_art(asset_id)).is_file()
    for asset_id in manifest["items"]:
        assert _file_of(item_art(asset_id)).is_file()
    for asset_id in manifest["traits"]:
        assert _file_of(trait_art(asset_id)).is_file()


def test_unresolved_names_fall_back_safely() -> None:
    assert champion_art("DA_18_NotAUnit") is None
    assert item_art("TFT_Item_NotAnItem") is None
    assert trait_art("DA_18_NotATrait") is None


def test_demo_data_ids_all_have_art() -> None:
    """The demo notebook shows real art, not fallbacks, for everything it uses."""
    for cid, *_ in (*CARRIES, *FILLERS):
        assert champion_art(cid), cid
    item_ids = {
        item for match in generate_demo_matches(count=5)
        for p in match["info"]["participants"] for u in p["units"] for item in u["itemNames"]
    }
    for item_id in item_ids:
        assert item_art(item_id), item_id
    for entry in DEMO_EXPERIMENTS:
        comp = entry["comp"]
        for name in (*comp.get("carry_items", []), *comp.get("tank_items", [])):
            assert item_art(name), name
        for trait in comp.get("target_traits", []):
            assert trait_art(trait.split(" ", 1)[-1]), trait
