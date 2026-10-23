import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Force a fresh demo database per test, isolated from any other test's
    # data/DATABASE_URL, and reload the app so module-level state (none
    # currently, but future-proofing) doesn't leak between tests.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))

    from tftlab.webapp import create_app

    return TestClient(create_app())


def test_discovery_endpoint_returns_candidates(client: TestClient) -> None:
    response = client.get("/api/discovery", params={"max_cost": 3, "min_samples": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["balance_window"]
    assert len(body["candidates"]) > 0

    candidate = body["candidates"][0]
    for field in (
        "character_id",
        "cost",
        "balance_window",
        "commitment_games",
        "usage_rate",
        "avg_placement",
        "top4_rate",
        "win_rate",
        "hit_3star_rate",
        "hit_top4_rate",
        "miss_top4_rate",
        "best_partners",
        "best_item_packages",
        "best_trait_breakpoints",
        "confidence",
        "opportunity_score",
        "opportunity_components",
    ):
        assert field in candidate, f"missing field {field}"

    # Recurrence order by default: carry boards, most first; ties by name, then id.
    assert body["order"] == "boards_desc"
    keys = [(-c["commitment_games"], c["name"], c["character_id"]) for c in body["candidates"]]
    assert keys == sorted(keys)


def test_discovery_fewest_first_reverses_under_the_same_floor(client: TestClient) -> None:
    params = {"max_cost": 5, "min_samples": 5, "limit": 200}
    most = client.get("/api/discovery", params=params).json()["candidates"]
    fewest = client.get("/api/discovery", params={**params, "order": "boards_asc"}).json()
    assert fewest["order"] == "boards_asc"
    keys = [(c["commitment_games"], c["name"], c["character_id"]) for c in fewest["candidates"]]
    assert keys == sorted(keys)
    assert {c["character_id"] for c in fewest["candidates"]} == {c["character_id"] for c in most}
    assert all(c["commitment_games"] >= 5 for c in fewest["candidates"])
    # `limit` applies after the order: the fewest-first page starts at the other end.
    one = client.get("/api/discovery", params={**params, "order": "boards_asc", "limit": 1}).json()
    assert one["candidates"] == fewest["candidates"][:1]


@pytest.mark.parametrize("order", ["top4_desc", "opportunity_score", "", "BOARDS_DESC"])
def test_discovery_rejects_unknown_order(client: TestClient, order: str) -> None:
    assert client.get("/api/discovery", params={"order": order}).status_code == 422


def test_most_carried_line_comes_first_even_when_it_places_worst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from _helpers import make_match, make_unit
    from tftlab.storage import Database
    import tftlab.webapp as webapp

    path = tmp_path / "order.sqlite3"
    items = ["TFT_Item_BlueBuff", "TFT_Item_JeweledGauntlet"]
    with Database(path) as db:
        for cid, boards, placement in (("TFT14_Popular", 30, 8), ("TFT14_Winner", 12, 1), ("TFT14_Tiny", 4, 1)):
            for i in range(boards):
                db.ingest_match(make_match(f"{cid}_{i}", placement=placement,
                                           units=[make_unit(cid, rarity=0, tier=2, items=items)]))
    for key in ("TFT_DATA_SOURCE", "TFT_SNAPSHOT_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", str(path))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "unused-demo.sqlite3"))
    webapp._POPULATION_CACHE.clear()
    client = TestClient(webapp.create_app())

    most = client.get("/api/discovery", params={"min_samples": 10}).json()["candidates"]
    assert [c["character_id"] for c in most] == ["TFT14_Popular", "TFT14_Winner"]
    assert most[0]["top4_rate"] < most[1]["top4_rate"]
    assert "opportunity_score" in most[0]  # still in the JSON (additive change)
    fewest = client.get("/api/discovery", params={"min_samples": 10, "order": "boards_asc"}).json()["candidates"]
    assert [c["character_id"] for c in fewest] == ["TFT14_Winner", "TFT14_Popular"]


def test_discovery_detail_endpoint(client: TestClient) -> None:
    listing = client.get("/api/discovery", params={"max_cost": 3, "min_samples": 5}).json()
    character_id = listing["candidates"][0]["character_id"]

    response = client.get(f"/api/discovery/{character_id}")
    assert response.status_code == 200
    assert response.json()["candidate"]["character_id"] == character_id


def test_discovery_detail_404_for_unknown_carry(client: TestClient) -> None:
    response = client.get("/api/discovery/TFT99_DoesNotExist")
    assert response.status_code == 404


def test_carry_partners_endpoint(client: TestClient) -> None:
    listing = client.get("/api/discovery", params={"max_cost": 3, "min_samples": 5}).json()
    character_id = listing["candidates"][0]["character_id"]

    response = client.get(f"/api/carries/{character_id}/partners")
    assert response.status_code == 200
    body = response.json()
    assert body["character_id"] == character_id
    assert isinstance(body["partners"], list)
    if body["partners"]:
        assert "association_score" in body["partners"][0]


def test_carry_items_endpoint(client: TestClient) -> None:
    listing = client.get("/api/discovery", params={"max_cost": 3, "min_samples": 5}).json()
    character_id = listing["candidates"][0]["character_id"]

    response = client.get(f"/api/carries/{character_id}/items")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"items", "pairs", "packages"}


def test_carry_traits_endpoint(client: TestClient) -> None:
    listing = client.get("/api/discovery", params={"max_cost": 3, "min_samples": 5}).json()
    character_id = listing["candidates"][0]["character_id"]

    response = client.get(f"/api/carries/{character_id}/traits")
    assert response.status_code == 200
    assert "traits" in response.json()


def test_carry_partners_404_for_unknown_carry(client: TestClient) -> None:
    response = client.get("/api/carries/TFT99_DoesNotExist/partners")
    assert response.status_code == 404


def test_existing_carry_detail_endpoint_unchanged(client: TestClient) -> None:
    """The pre-existing /api/carries/{id} contract (used by the current
    frontend) must still work exactly as before -- no frontend redesign in
    this milestone, so its response shape must not change."""
    listing = client.get("/api/carries", params={"max_cost": 3, "min_samples": 5}).json()
    character_id = listing["carries"][0]["character_id"]

    response = client.get(f"/api/carries/{character_id}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"demo", "carry", "partners", "item_sets"}
