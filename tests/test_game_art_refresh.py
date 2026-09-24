"""Offline tests for `tftlab refresh-game-art` (CommunityDragon is mocked)."""

import io
import json

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from tftlab import cli, game_art_refresh
from tftlab.cdragon import CDRAGON_BASE, CommunityDragonClient
from tftlab.game_art_refresh import GameArtError, normalize_image, refresh_game_art, select_assets


def _png(size: int = 256, color=(200, 40, 40, 128)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(out, format="PNG")
    return out.getvalue()


def _payload(set_number: int = 18) -> dict:
    return {
        "items": [
            {"apiName": "TFT_Item_BFSword", "name": "B.F. Sword", "icon": "ASSETS/Maps/TFT/Icons/Items/BFSword.tex"},
            {"apiName": "TFT_Item_SparringGloves", "name": "Sparring Gloves", "icon": "ASSETS/Items/Gloves.png"},
            {
                "apiName": "TFT_Item_InfinityEdge", "name": "Infinity Edge", "icon": "ASSETS/Items/IE.png",
                "composition": ["TFT_Item_BFSword", "TFT_Item_SparringGloves"],
            },
            # Older-set/radiant/artifact/event variants are not "standard" items.
            {
                "apiName": "TFT5_Item_InfinityEdgeRadiant", "name": "Radiant Infinity Edge",
                "icon": "ASSETS/Items/IER.png", "composition": [],
            },
            {"apiName": "TFT_Item_Artifact_Fishbones", "name": "Fishbones", "icon": "ASSETS/Items/Fish.png"},
            {"apiName": "TFT_Item_NoIcon", "name": "Nameless", "composition": ["TFT_Item_BFSword", "TFT_Item_BFSword"]},
            # A same-named variant and an unnamed placeholder are left out.
            {
                "apiName": "TFT_Item_CorruptedInfinityEdge", "name": "Infinity Edge", "icon": "ASSETS/Items/CIE.png",
                "composition": ["TFT_Item_BFSword", "TFT_Item_SparringGloves"],
            },
            {"apiName": "TFT_Item_EmptyBag", "name": "TFT_Item_EmptyBag", "icon": "ASSETS/Items/Bag.png"},
        ],
        "setData": [
            {
                "number": set_number,
                "champions": [
                    {"apiName": "DA_18_KhaZix", "name": "Kha'Zix", "cost": 1, "traits": ["Ravager"],
                     "squareIcon": "ASSETS/Characters/KhaZix/Square.tex"},
                    {"apiName": "DA_18_Cassiopeia", "name": "Cassiopeia", "cost": 2, "traits": ["X"],
                     "squareIcon": "ASSETS/Characters/Cass/Square.png"},
                    # A summon: not in the roster, no traits.
                    {"apiName": "DA_18_Summon_Wolf", "name": "Wolf", "cost": 1, "traits": [],
                     "squareIcon": "ASSETS/Characters/Wolf.png"},
                ],
                "traits": [
                    {"apiName": "DA_18_Slayer", "name": "Ravager", "icon": "ASSETS/Traits/Slayer.tex"},
                ],
            }
        ],
    }


class FakeCDragon:
    """A mock transport for the bundle plus every image it points at."""

    def __init__(self, payload: dict, image: bytes | None = None) -> None:
        self.payload = payload
        self.image = image or _png()
        self.overrides: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.etag = '"v1"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.endswith("/cdragon/tft/en_us.json"):
            return httpx.Response(200, json=self.payload)
        for fragment, response in self.overrides.items():
            if fragment in url:
                return response
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304)
        return httpx.Response(200, content=self.image, headers={"content-type": "image/png", "etag": self.etag})

    def client(self) -> CommunityDragonClient:
        return CommunityDragonClient(client=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def image_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if not str(r.url).endswith(".json")]


def _refresh(fake: FakeCDragon, tmp_path, **kwargs):
    with fake.client() as client:
        return refresh_game_art(
            client, art_dir=tmp_path / "game", manifest_path=tmp_path / "manifest.json", **kwargs
        )


def test_selection_keeps_playable_units_all_traits_and_standard_items() -> None:
    set_number, picks, shape = select_assets(_payload())
    chosen = {(p.kind, p.asset_id) for p in picks}
    assert set_number == 18
    assert ("champions", "DA_18_KhaZix") in chosen
    assert ("champions", "DA_18_Summon_Wolf") not in chosen
    assert ("traits", "DA_18_Slayer") in chosen
    assert {a for k, a in chosen if k == "items"} == {
        "TFT_Item_BFSword", "TFT_Item_SparringGloves", "TFT_Item_InfinityEdge", "TFT_Item_NoIcon",
    }
    assert "TFT_Item_BFSword" in shape["components_found"]
    assert shape["duplicate_item_names"] == []


def test_selection_refuses_a_set_that_is_not_the_shipped_roster() -> None:
    with pytest.raises(GameArtError, match="roster"):
        select_assets(_payload(set_number=19))


def test_refresh_writes_resized_pngs_and_a_manifest(tmp_path) -> None:
    fake = FakeCDragon(_payload())
    report = _refresh(fake, tmp_path)

    assert report.ok, report.failures
    assert report.cached == {"champions": 2, "traits": 1, "items": 3}
    assert report.missing["items"] == ["TFT_Item_NoIcon"]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    khazix = manifest["champions"]["DA_18_KhaZix"]
    assert khazix["file"] == "champions/DA_18_KhaZix.png"
    assert khazix["source_path"] == "assets/characters/khazix/square.png"  # .tex served as .png
    assert (khazix["width"], khazix["height"]) == (128, 128)
    assert manifest["items"]["TFT_Item_InfinityEdge"]["width"] == 64
    assert manifest["missing"] == {"items": ["TFT_Item_NoIcon"]}
    with Image.open(tmp_path / "game" / "traits" / "DA_18_Slayer.png") as img:
        assert img.mode == "RGBA" and img.size == (64, 64)
        assert img.getpixel((10, 10))[3] == 128  # transparency survives
    # Only CommunityDragon was contacted.
    assert all(str(r.url).startswith(CDRAGON_BASE) for r in fake.requests)


def test_refresh_is_deterministic(tmp_path) -> None:
    _refresh(FakeCDragon(_payload()), tmp_path / "a")
    _refresh(FakeCDragon(_payload()), tmp_path / "b")
    assert (tmp_path / "a" / "manifest.json").read_bytes() == (tmp_path / "b" / "manifest.json").read_bytes()
    for path in (tmp_path / "a" / "game").rglob("*.png"):
        twin = tmp_path / "b" / "game" / path.relative_to(tmp_path / "a" / "game")
        assert path.read_bytes() == twin.read_bytes()


def test_second_refresh_does_not_redownload_unchanged_images(tmp_path) -> None:
    fake = FakeCDragon(_payload())
    _refresh(fake, tmp_path)
    before = (tmp_path / "manifest.json").read_bytes()
    fake.requests.clear()

    report = _refresh(fake, tmp_path)
    assert report.ok
    assert sum(report.fetched.values()) == 0
    assert sum(report.unchanged.values()) == 6
    # Every image request was conditional and answered 304 (no body).
    assert all(r.headers.get("if-none-match") == '"v1"' for r in fake.image_requests())
    assert report.bytes_written == 0
    assert (tmp_path / "manifest.json").read_bytes() == before


def test_changed_upstream_image_is_replaced(tmp_path) -> None:
    fake = FakeCDragon(_payload())
    _refresh(fake, tmp_path)
    fake.image, fake.etag = _png(color=(0, 0, 255, 255)), '"v2"'
    report = _refresh(fake, tmp_path)
    assert report.fetched["champions"] == 2


@pytest.mark.parametrize(
    "response, reason",
    [
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(200, content=b"<html>nope</html>", headers={"content-type": "text/html"}), "content-type"),
        (httpx.Response(200, content=b"not really a png", headers={"content-type": "image/png"}), "not a PNG"),
    ],
)
def test_a_bad_download_fails_the_run_and_leaves_old_files(tmp_path, response, reason) -> None:
    fake = FakeCDragon(_payload())
    _refresh(fake, tmp_path)
    before = (tmp_path / "manifest.json").read_bytes()

    fake.image, fake.etag = _png(color=(0, 255, 0, 255)), '"v2"'
    fake.overrides["/cass/"] = response
    report = _refresh(fake, tmp_path)

    assert not report.ok
    assert any("DA_18_Cassiopeia" in f and reason in f for f in report.failures)
    assert (tmp_path / "manifest.json").read_bytes() == before
    with Image.open(tmp_path / "game" / "champions" / "DA_18_KhaZix.png") as img:
        assert img.getpixel((5, 5))[1] < 100  # still the old red, not the failed run's green


def test_path_traversal_in_metadata_is_refused(tmp_path) -> None:
    payload = _payload()
    payload["setData"][0]["traits"][0]["icon"] = "../../../evil.png"
    fake = FakeCDragon(payload)
    report = _refresh(fake, tmp_path)
    assert not report.ok
    assert any("refusing non-CommunityDragon URL" in f for f in report.failures)
    assert not any("evil" in str(r.url) for r in fake.requests)


def test_stale_files_are_pruned(tmp_path) -> None:
    fake = FakeCDragon(_payload())
    (tmp_path / "game" / "champions").mkdir(parents=True)
    (tmp_path / "game" / "champions" / "DA_17_Old.png").write_bytes(_png())
    report = _refresh(fake, tmp_path)
    assert report.pruned == ["champions/DA_17_Old.png"]
    assert not (tmp_path / "game" / "champions" / "DA_17_Old.png").exists()


def test_dry_run_downloads_no_images_and_writes_nothing(tmp_path) -> None:
    fake = FakeCDragon(_payload())
    report = _refresh(fake, tmp_path, dry_run=True)
    assert report.cached["champions"] == 2
    assert fake.image_requests() == []
    assert not (tmp_path / "manifest.json").exists()
    assert "DA_18_KhaZix (Kha'Zix)" in report.planned["champions"]


def test_normalize_image_rejects_non_images_and_never_upscales() -> None:
    with pytest.raises(GameArtError):
        normalize_image(b"GIF89a....", 64)
    png, width, height = normalize_image(_png(32), 64)
    assert (width, height) == (32, 32)
    assert png.startswith(b"\x89PNG")


def _cli_with(monkeypatch, tmp_path, fake: FakeCDragon):
    monkeypatch.setattr(cli, "CommunityDragonClient", fake.client)
    monkeypatch.setattr(game_art_refresh, "ART_DIR", tmp_path / "game")
    monkeypatch.setattr(game_art_refresh, "MANIFEST_PATH", tmp_path / "manifest.json")
    return CliRunner().invoke(cli.app, ["refresh-game-art"])


def test_cli_reports_counts_and_missing_icons(monkeypatch, tmp_path) -> None:
    result = _cli_with(monkeypatch, tmp_path, FakeCDragon(_payload()))
    assert result.exit_code == 0, result.output
    assert "champions: 2 cached" in result.output
    assert "missing icon: items/TFT_Item_NoIcon" in result.output
    assert (tmp_path / "manifest.json").exists()


def test_cli_exits_nonzero_on_integrity_failure(monkeypatch, tmp_path) -> None:
    fake = FakeCDragon(_payload())
    fake.overrides["slayer"] = httpx.Response(500)
    result = _cli_with(monkeypatch, tmp_path, fake)
    assert result.exit_code == 1
    assert "traits/DA_18_Slayer: HTTP 500" in result.output
    assert not (tmp_path / "manifest.json").exists()


def test_cli_takes_no_url_argument() -> None:
    result = CliRunner().invoke(cli.app, ["refresh-game-art", "https://example.com/evil.png"])
    assert result.exit_code != 0
