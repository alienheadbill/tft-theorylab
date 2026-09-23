"""Tests for the small `/api/balance-windows` addition (PR #11): it exposes
already-computed `available_balance_windows` data plus a count of
intentionally-unresolved Unreal-era matches, purely so the frontend's
balance-window selector can offer only real, resolvable windows and show an
honest note about the rollout-gap matches it excludes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tftlab.unreal_patch import UNRESOLVED_UNREAL_PATCH

from _helpers import make_match, make_unit


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))

    from tftlab.webapp import create_app

    return TestClient(create_app())


def test_demo_dataset_reports_windows_and_no_unresolved_matches(client: TestClient) -> None:
    response = client.get("/api/balance-windows")
    assert response.status_code == 200
    body = response.json()

    assert body["default_balance_window"]
    assert len(body["windows"]) >= 1
    window = body["windows"][0]
    assert set(window) == {"balance_window", "matches", "latest_game_datetime"}
    assert window["balance_window"] == body["default_balance_window"]
    # The demo dataset never contains masked Unreal-era matches.
    assert body["unresolved_unreal_matches"] == 0


def test_unresolved_unreal_matches_never_appear_as_a_selectable_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A production-shaped store with real resolved matches plus matches
    stuck in the Unreal registry's rollout gap: the gap matches must be
    counted (so the UI can show its note) but never listed as a window,
    since their balance_window is NULL, not a fake patch string."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "prod_shaped.sqlite3"
    monkeypatch.setenv("TFT_DB_PATH", str(db_path))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))

    from tftlab.storage import Database

    with Database(db_path) as db:
        for i in range(3):
            db.ingest_match(
                make_match(f"RESOLVED_{i}", units=[make_unit("TFT14_Foo", tier=2, items=["TFT_Item_BlueBuff"])])
            )
        for i in range(5):
            db.ingest_match(
                make_match(
                    f"GAP_{i}",
                    game_version="TFT Unreal Version ?.?.?.?",
                    game_datetime=1_790_121_600_000 + i,
                    units=[make_unit("TFT18_Foo", tier=2, items=["TFT_Item_BlueBuff"])],
                )
            )

    from tftlab.webapp import create_app

    client = TestClient(create_app())
    response = client.get("/api/balance-windows")
    assert response.status_code == 200
    body = response.json()

    window_names = [w["balance_window"] for w in body["windows"]]
    assert "unreal-unresolved" not in window_names
    assert None not in window_names
    assert body["unresolved_unreal_matches"] == 5


def test_empty_store_reports_no_default_window(tmp_path: Path) -> None:
    """A fresh, empty production database (before first ingest) is a normal,
    honest state -- `/api/balance-windows` builds its response directly from
    `available_balance_windows`/an `UNRESOLVED_UNREAL_PATCH` count, both of
    which must degrade to "nothing yet" rather than erroring. Exercised at
    this level (not through the FastAPI env-based DB resolution, which has
    its own demo-fallback tests) to isolate exactly what the route computes.
    """
    from tftlab.analytics import available_balance_windows
    from tftlab.storage import Database

    with Database(tmp_path / "empty.sqlite3") as db:
        windows = available_balance_windows(db)
        unresolved = db.query_one("SELECT COUNT(*) FROM matches WHERE patch = ?", (UNRESOLVED_UNREAL_PATCH,))[0]

    assert windows == []
    assert unresolved == 0
