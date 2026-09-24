"""Read-only experiments API and pages."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tftlab.experiments import DEMO_EXPERIMENTS, create_experiment
from tftlab.storage import Database

from _helpers import make_match, make_unit


def _demo_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


def _live_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    """A non-demo local database with real match data, like a dev copy of prod."""
    path = tmp_path / "live.sqlite3"
    with Database(path) as db:
        db.ingest_match(make_match("M1", units=[make_unit("TFT14_Foo", tier=2, items=[])]))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(path))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app()), path


def test_demo_notebook_lists_example_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _demo_client(tmp_path, monkeypatch)
    body = client.get("/api/experiments").json()
    assert body["demo"] is True
    assert body["count"] == len(DEMO_EXPERIMENTS) == len(body["experiments"])
    for entry in body["experiments"]:
        assert entry["is_example"] is True
        assert entry["evidence_status"] == "THEORYCRAFTED"
        assert set(entry) == {
            "id", "slug", "title", "carry", "evidence_status", "lifecycle", "summary",
            "author_notes", "comp", "tags", "is_example", "created_at", "updated_at", "art",
        }
        assert "field_notes" not in entry  # detail only


def test_real_database_is_never_seeded_with_examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _live_client(tmp_path, monkeypatch)
    body = client.get("/api/experiments").json()
    assert body["demo"] is False
    assert body == {"demo": False, "count": 0, "experiments": []}


def test_detail_and_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, path = _live_client(tmp_path, monkeypatch)
    with Database(path) as db:
        kha = create_experiment(db, {
            "title": "6 Ravager Kha'Zix", "carry_name": "Kha'Zix", "carry_character_id": "DA_18_KhaZix",
            "comp": {"target_traits": ["6 Ravager"]}, "tags": ["ravager"],
        })
        create_experiment(db, {"title": "Caitlyn sketch", "evidence_status": "VARIANT", "lifecycle": "watching"})

    detail = client.get(f"/api/experiments/{kha.slug}")
    assert detail.status_code == 200
    e = detail.json()["experiment"]
    assert {k: e["carry"][k] for k in ("character_id", "name")} == {"character_id": "DA_18_KhaZix", "name": "Kha'Zix"}
    assert e["comp"]["target_traits"] == [{"name": "Ravager", "breakpoint": 6, "note": None}]
    assert e["field_notes"] == []
    assert e["is_example"] is False
    assert client.get(f"/api/experiments/{kha.experiment_id}").json()["experiment"]["slug"] == kha.slug

    def slugs(**params):
        return [x["slug"] for x in client.get("/api/experiments", params=params).json()["experiments"]]

    assert slugs(status="VARIANT") == ["caitlyn-sketch"]
    assert slugs(lifecycle="watching") == ["caitlyn-sketch"]
    assert slugs(carry="Kha'Zix") == [kha.slug]
    assert slugs(tag="ravager") == [kha.slug]
    assert slugs(tag="nope") == []


def test_unknown_experiment_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _demo_client(tmp_path, monkeypatch)
    response = client.get("/api/experiments/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Experiment not found"}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/api/experiments", "/api/experiments/6-ravager-khazix"])
def test_no_public_write_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str) -> None:
    client = _demo_client(tmp_path, monkeypatch)
    before = client.get("/api/experiments").json()["count"]
    response = getattr(client, method)(path)
    assert response.status_code == 405
    assert client.get("/api/experiments").json()["count"] == before


def test_every_api_route_is_read_only() -> None:
    from tftlab.webapp import create_app

    for route in create_app().routes:
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, f"{route.path} allows {sorted(methods)}"


def test_experiment_pages_are_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _demo_client(tmp_path, monkeypatch)
    for path in ("/experiments", "/experiments/6-ravager-khazix", "/experiments/anything-at-all"):
        response = client.get(path)
        assert response.status_code == 200
        assert "My Experiments" in response.text
        assert "/static/experiments.js" in response.text
    assert client.get("/static/experiments.js").status_code == 200
    assert 'href="/experiments"' in client.get("/").text


def test_detail_serves_field_notes_fingerprint_and_checklist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tftlab.experiments import add_field_note

    client, path = _live_client(tmp_path, monkeypatch)
    with Database(path) as db:
        e = create_experiment(db, {"title": "6 Ravager Kha'Zix", "carry_name": "Kha'Zix",
                                   "comp": {"target_traits": ["6 Ravager"]}})
        add_field_note(db, e.slug, kind="scout_report", source="TFT Academy", source_url="https://tftacademy.com/x",
                       body="No sufficiently similar listed comp.", research_label="NO_PUBLIC_MATCH_FOUND",
                       noted_at="2026-09-21")
        add_field_note(db, e.slug, kind="my_note", body="hunch", noted_at="2026-09-20")

    body = client.get(f"/api/experiments/{e.slug}").json()["experiment"]
    assert body["fingerprint"]["signature"] == "carry=khazix;traits=ravager@6"
    assert [n["kind"] for n in body["field_notes"]] == ["my_note", "scout_report"]  # oldest first
    note = body["field_notes"][1]
    assert {k: note[k] for k in ("kind_label", "source_key", "source_name", "source_url", "research_label_text")} == {
        "kind_label": "Scout report", "source_key": "tft_academy", "source_name": "TFT Academy",
        "source_url": "https://tftacademy.com/x", "research_label_text": "No public match found",
    }
    checked = {c["key"]: c["checked"] for c in body["scout_checklist"]}
    assert checked == {"riot": False, "tft_academy": True, "metatft": False, "tactics_tools": False,
                       "mechanics": False, "community": False, "tournament": False}
    # The list view stays light: no notes or checklist there.
    listed = client.get("/api/experiments").json()["experiments"][0]
    assert "field_notes" not in listed and "scout_checklist" not in listed
