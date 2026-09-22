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

    # Candidates must be sorted by opportunity_score, descending.
    scores = [c["opportunity_score"] for c in body["candidates"]]
    assert scores == sorted(scores, reverse=True)


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
