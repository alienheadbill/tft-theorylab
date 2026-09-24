"""Cached game art: resolution, fallbacks and API wiring (offline).

These run against a small stand-in manifest so they don't depend on which
images the committed refresh happened to contain; tests/test_game_art_manifest.py
checks the committed manifest itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tftlab import game_art
from tftlab.game_art import champion_art, enrich_candidate, field_note_art, item_art, trait_art

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # the resolver never decodes; content is irrelevant

CHAMPIONS = {
    "DA_18_KhaZix": "Kha'Zix",
    "DA_18_Cassiopeia": "Cassiopeia",
    "DA_Fiddlesticks18": "Fiddlesticks",
    "DA_Lux18_Base": "Lux",
    "DA_18_Lux_Fae": "Lux (Fae)",
    "DA_18_Lux_Inferno": "Lux (Inferno)",
}
TRAITS = {"DA_18_Slayer": "Ravager", "DA_18_Spellweaver": "Spellweaver"}
ITEMS = {
    "TFT_Item_InfinityEdge": "Infinity Edge",
    "TFT_Item_BlueBuff": "Blue Buff",
    "TFT_Item_JeweledGauntlet": "Jeweled Gauntlet",
    "TFT_Item_RabadonsDeathcap": "Rabadon's Deathcap",
    "TFT_Item_RapidFireCannon": "Red Buff",
    "TFT_Item_RedBuff": "Sunfire Cape",
}
MISSING_FILE = "TFT_Item_LastWhisper"  # in the manifest, but its file is gone


@pytest.fixture(autouse=True)
def art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    art_dir = tmp_path / "game"
    manifest: dict = {"manifest_version": 1, "set_number": 18, "missing": {}}
    for kind, entries in (("champions", CHAMPIONS), ("traits", TRAITS), ("items", {**ITEMS, MISSING_FILE: "Last Whisper"})):
        manifest[kind] = {}
        for asset_id, name in entries.items():
            file = f"{kind}/{asset_id}.png"
            if asset_id != MISSING_FILE:
                (art_dir / kind).mkdir(parents=True, exist_ok=True)
                (art_dir / file).write_bytes(PNG)
            manifest[kind][asset_id] = {"name": name, "file": file, "sha256": hashlib.sha256(PNG + file.encode()).hexdigest()}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(game_art, "ART_DIR", art_dir)
    monkeypatch.setattr(game_art, "MANIFEST_PATH", manifest_path)
    game_art.load_manifest.cache_clear()
    game_art._file_exists.cache_clear()
    yield art_dir
    game_art.load_manifest.cache_clear()
    game_art._file_exists.cache_clear()


def _is_local(url: str | None, kind: str, asset_id: str) -> bool:
    return bool(url) and url.startswith(f"/static/game/{kind}/{asset_id}.png?v=")


def test_champions_resolve_by_canonical_id() -> None:
    assert _is_local(champion_art("DA_18_KhaZix"), "champions", "DA_18_KhaZix")
    assert _is_local(champion_art("DA_18_Cassiopeia", "Cassiopeia"), "champions", "DA_18_Cassiopeia")


def test_champion_name_fallback_uses_roster_normalization() -> None:
    assert champion_art(name="kha'zix") == champion_art("DA_18_KhaZix")
    assert champion_art(name="KhaZix") == champion_art("DA_18_KhaZix")


def test_lux_forms_never_collapse() -> None:
    fae, inferno, base = champion_art("DA_18_Lux_Fae"), champion_art("DA_18_Lux_Inferno"), champion_art("DA_Lux18_Base")
    assert len({fae, inferno, base}) == 3
    assert champion_art(name="Lux (Fae)") == fae
    assert champion_art(name="Lux") == base  # only the base unit is called just "Lux"


def test_ambiguous_name_resolves_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = json.loads(game_art.MANIFEST_PATH.read_text())
    manifest["champions"]["DA_18_Lux_Fae"]["name"] = "Lux"  # two champions now share a name
    game_art.MANIFEST_PATH.write_text(json.dumps(manifest))
    game_art.load_manifest.cache_clear()
    assert champion_art(name="Lux") is None
    assert champion_art("DA_18_Lux_Fae") is not None  # the id still resolves


def test_trait_resolves_by_id_and_display_name() -> None:
    assert _is_local(trait_art("DA_18_Slayer"), "traits", "DA_18_Slayer")
    assert trait_art("Ravager") == trait_art("DA_18_Slayer")
    assert trait_art("Slayer") is None  # the internal name isn't what players call it


def test_item_resolves_consistently_by_id_name_and_normalized_text() -> None:
    by_id = item_art("TFT_Item_InfinityEdge")
    assert _is_local(by_id, "items", "TFT_Item_InfinityEdge")
    assert item_art("Infinity Edge") == item_art("infinity edge") == item_art("InfinityEdge") == by_id


def test_display_name_beats_id_derived_key() -> None:
    # "Red Buff" is the display name of TFT_Item_RapidFireCannon, even though
    # TFT_Item_RedBuff (Sunfire Cape) has "redbuff" in its id.
    assert item_art("Red Buff") == item_art("TFT_Item_RapidFireCannon")
    assert item_art("Sunfire Cape") == item_art("TFT_Item_RedBuff")


def test_unresolved_lookups_fall_back_to_none() -> None:
    assert champion_art("DA_18_NotAChampion") is None
    assert champion_art(None, "Nobody") is None
    assert champion_art() is None
    assert item_art("TFT_Item_Made_Up") is None
    assert item_art("") is None
    assert trait_art("DA_18_Unknown") is None
    assert trait_art(None) is None


def test_missing_file_resolves_to_none() -> None:
    assert item_art(MISSING_FILE) is None


def test_no_manifest_at_all_means_no_art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_art, "MANIFEST_PATH", tmp_path / "absent.json")
    game_art.load_manifest.cache_clear()
    assert champion_art("DA_18_KhaZix") is None
    assert item_art("TFT_Item_InfinityEdge") is None


def test_enrich_candidate_adds_local_urls_for_every_evidence_kind() -> None:
    candidate = {
        "character_id": "DA_18_KhaZix",
        "name": "Kha'Zix",
        "best_partners": [{"key": "DA_18_Cassiopeia", "label": "Cassiopeia"}],
        "best_item_packages": [{"key": "TFT_Item_InfinityEdge+TFT_Item_Unknown", "label": "TFT_Item_InfinityEdge+TFT_Item_Unknown"}],
        "best_trait_breakpoints": [{"key": "DA_18_Slayer:2", "label": "DA_18_Slayer (2)"}],
    }
    enriched = enrich_candidate(candidate)
    assert enriched["art_url"] == champion_art("DA_18_KhaZix")
    assert enriched["best_partners"][0]["art_url"] == champion_art("DA_18_Cassiopeia")
    items = enriched["best_item_packages"][0]["items"]
    assert [i["id"] for i in items] == ["TFT_Item_InfinityEdge", "TFT_Item_Unknown"]
    assert items[0]["name"] == "Infinity Edge" and items[0]["art_url"] == item_art("TFT_Item_InfinityEdge")
    assert items[1] == {"id": "TFT_Item_Unknown", "name": None, "art_url": None}
    trait = enriched["best_trait_breakpoints"][0]
    assert trait["art_url"] == trait_art("DA_18_Slayer") and trait["trait_name"] == "Ravager"


def test_field_note_art_only_for_ok_riot_evidence() -> None:
    assert field_note_art({"kind": "my_note", "data": {}}) is None
    assert field_note_art({"kind": "riot_evidence", "data": {"status": "no_data"}}) is None
    art = field_note_art({
        "kind": "riot_evidence",
        "data": {
            "status": "ok",
            "character_id": "DA_18_KhaZix",
            "carry": "Kha'Zix",
            "best_partners": [{"label": "Cassiopeia"}],
            "best_item_packages": [{"label": "TFT_Item_BlueBuff+TFT_Item_JeweledGauntlet"}],
            "best_trait_breakpoints": [{"label": "DA_18_Slayer (2)"}],
            "core_units": [{"name": "Fiddlesticks"}],
            "trait_targets": [{"name": "Ravager"}],
        },
    })
    assert art["carry"] == champion_art("DA_18_KhaZix")
    assert art["best_partners"] == [champion_art("DA_18_Cassiopeia")]
    assert [r["name"] for r in art["best_item_packages"][0]] == ["Blue Buff", "Jeweled Gauntlet"]
    assert art["best_trait_breakpoints"] == [{"art_url": trait_art("DA_18_Slayer"), "trait_name": "Ravager"}]
    assert art["core_units"] == [champion_art("DA_Fiddlesticks18")]
    assert art["trait_targets"] == [trait_art("DA_18_Slayer")]


# ---------------------------------------------------------------- API


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


def _all_art_urls(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, v in value.items():
            if key == "art_url" and v is not None:
                found.append(v)
            elif key == "art" and isinstance(v, dict):
                found.extend(_flat_urls(v))
            else:
                found.extend(_all_art_urls(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_all_art_urls(v))
    return found


def _flat_urls(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [u for v in value.values() for u in _flat_urls(v)]
    if isinstance(value, list):
        return [u for v in value for u in _flat_urls(v)]
    return []


def test_discovery_api_returns_local_art_urls(client: TestClient) -> None:
    body = client.get("/api/discovery", params={"min_samples": 1}).json()
    by_id = {c["character_id"]: c for c in body["candidates"]}
    assert by_id["DA_18_KhaZix"]["art_url"] == champion_art("DA_18_KhaZix")
    packages = [p for c in body["candidates"] for p in c["best_item_packages"]]
    assert any(i["art_url"] for p in packages for i in p["items"])
    urls = _all_art_urls(body)
    assert urls and all(u.startswith("/static/game/") for u in urls)
    assert "communitydragon.org" not in json.dumps(body).lower()


def test_discovery_detail_api_has_art(client: TestClient) -> None:
    body = client.get("/api/discovery/DA_18_Cassiopeia").json()
    assert body["candidate"]["art_url"] == champion_art("DA_18_Cassiopeia")
    assert all(u.startswith("/static/game/") for u in _all_art_urls(body))


def test_experiments_api_adds_index_aligned_art(client: TestClient) -> None:
    entries = {e["slug"]: e for e in client.get("/api/experiments").json()["experiments"]}
    cass = next(e for e in entries.values() if e["carry"] and e["carry"]["character_id"] == "DA_18_Cassiopeia")
    assert cass["carry"]["art_url"] == champion_art("DA_18_Cassiopeia")
    assert cass["art"]["core_units"] == [champion_art("DA_18_Cassiopeia"), champion_art("DA_Fiddlesticks18")]
    assert cass["art"]["carry_items"] == [item_art("Blue Buff"), item_art("Jeweled Gauntlet"), item_art("TFT_Item_RabadonsDeathcap")]
    assert None not in cass["art"]["carry_items"]
    detail = client.get(f"/api/experiments/{cass['slug']}").json()["experiment"]
    assert detail["art"] == cass["art"]
    khazix = next(e for e in entries.values() if e["carry"] and e["carry"]["character_id"] == "DA_18_KhaZix")
    assert khazix["art"]["target_traits"] == [trait_art("DA_18_Slayer")]
    assert khazix["art"]["carry_items"][1] is None  # Last Whisper's file is missing: text only


def test_art_lookups_never_touch_the_network(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("a web request tried to reach the network")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    import tftlab.cdragon

    monkeypatch.setattr(tftlab.cdragon.CommunityDragonClient, "fetch_raw", refuse)
    game_art.load_manifest.cache_clear()
    # TestClient is itself httpx-based, so exercise the app in-process instead.
    from tftlab.webapp import create_app

    app = create_app()
    routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
    assert routes["/api/discovery"](max_cost=3, min_samples=1, balance_window=None, top_n=5, limit=20)["candidates"]
    assert routes["/api/experiments"](status=None, lifecycle=None, carry=None, tag=None)["experiments"]


def test_pages_survive_a_missing_art_file(client: TestClient, art: Path) -> None:
    (art / "champions" / "DA_18_KhaZix.png").unlink()
    game_art._file_exists.cache_clear()
    body = client.get("/api/discovery", params={"min_samples": 1}).json()
    khazix = next(c for c in body["candidates"] if c["character_id"] == "DA_18_KhaZix")
    assert khazix["art_url"] is None  # the page shows initials
    exp = client.get("/api/experiments").json()["experiments"]
    assert all(e["carry"] is None or e["carry"]["character_id"] != "DA_18_KhaZix" or e["carry"]["art_url"] is None for e in exp)
    assert client.get("/").status_code == 200
    assert client.get("/experiments").status_code == 200


def test_frontend_never_references_communitydragon() -> None:
    web = Path(game_art.__file__).parent / "web"
    for path in [*web.glob("*.html"), *(web / "static").glob("*.js"), *(web / "static").glob("*.css")]:
        assert "communitydragon.org" not in path.read_text().lower(), path.name


def test_frontend_art_is_lazy_sized_and_has_a_fallback() -> None:
    js = (Path(game_art.__file__).parent / "web" / "static" / "art.js").read_text()
    assert 'loading="lazy"' in js and 'width="${size}" height="${size}"' in js and 'alt=""' in js
    assert "startsWith('/static/game/')" in js  # only local URLs are ever rendered
    assert "addEventListener(\n  'error'" in js  # broken files fall back instead of showing a broken image
