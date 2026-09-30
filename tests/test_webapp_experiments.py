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


# ---------------------------------------------------------------- saved trait evidence

LEGACY_TRAIT = {"label": "DA_18_Slayer (2)", "games": 5, "top4_rate": 0.6, "top4_delta": 0.05}  # "(2)" = old tier
NEW_TRAIT = {**LEGACY_TRAIT, "label": "DA_18_Slayer (6)", "trait_id": "DA_18_Slayer", "num_units": 6}


def _riot_note_data(trait: dict) -> dict:
    return {"status": "ok", "balance_window": "14.6", "commitment_games": 10, "low_sample": True,
            "avg_placement": 4.2, "top4_rate": 0.5, "win_rate": 0.1, "hit_3star_rate": 0.2,
            "best_partners": [], "best_item_packages": [], "best_trait_breakpoints": [trait]}


def test_saved_trait_evidence_is_served_as_saved_with_art_for_old_and_new_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tftlab.experiments import add_field_note

    client, path = _live_client(tmp_path, monkeypatch)
    with Database(path) as db:
        e = create_experiment(db, {"title": "k", "carry_name": "Kha'Zix"})
        for day, trait in (("2026-09-01", LEGACY_TRAIT), ("2026-09-02", NEW_TRAIT)):
            add_field_note(db, e.slug, kind="riot_evidence", body="x", source="riot",
                           data=_riot_note_data(trait), system=True, noted_at=day)

    legacy, new = client.get(f"/api/experiments/{e.slug}").json()["experiment"]["field_notes"]
    # Not migrated or reinterpreted: the legacy note gains no unit count.
    assert legacy["data"]["best_trait_breakpoints"] == [LEGACY_TRAIT]
    assert new["data"]["best_trait_breakpoints"] == [NEW_TRAIT]
    for note in (legacy, new):
        art = note["art"]["best_trait_breakpoints"][0]
        assert art["trait_name"] == "Ravager" and art["art_url"]


def test_field_note_art_prefers_the_saved_trait_id() -> None:
    from tftlab.game_art import field_note_art, trait_art

    note = {"kind": "riot_evidence", "data": _riot_note_data({**NEW_TRAIT, "label": "unparseable"})}
    assert field_note_art(note)["best_trait_breakpoints"] == [
        {"art_url": trait_art("DA_18_Slayer"), "trait_name": "Ravager"}
    ]


def _render_slip(data: dict, art: dict) -> str:
    """Run the notebook's own `riotSlip` in node (DOM stubbed; the page's
    router is not started) and return the slip's text."""
    import json
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    static = Path(__file__).parent.parent / "src" / "tftlab" / "web" / "static"
    page = (static / "experiments.js").read_text()
    assert page.rstrip().endswith("route();")
    source = (static / "art.js").read_text() + "\n" + page.rstrip()[: -len("route();")] + "\nriotSlip"
    script = f"""
      const vm = require('vm');
      const el = {{ addEventListener() {{}}, querySelector() {{ return null; }}, set innerHTML(v) {{}} }};
      const ctx = {{ document: {{ addEventListener() {{}}, querySelector: () => el, querySelectorAll: () => [] }}, window: {{ location: {{}} }},
                    location: {{}}, history: {{}}, fetch: () => new Promise(() => {{}}) }};
      vm.createContext(ctx);
      const riotSlip = vm.runInContext({json.dumps(source)}, ctx);
      process.stdout.write(riotSlip({json.dumps(data)}, {json.dumps(art)}));
    """
    html = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


@pytest.mark.parametrize(
    "trait, art_name, shown",
    [
        (LEGACY_TRAIT, "Ravager", "best trait: Ravager (2) (5 g)"),   # exactly as legacy notes render today
        (LEGACY_TRAIT, None, "best trait: Slayer (2) (5 g)"),          # legacy fallback, unchanged
        (NEW_TRAIT, "Ravager", "best trait: Ravager · 6 units (5 g)"),
        (NEW_TRAIT, None, "best trait: Slayer · 6 units (5 g)"),
    ],
)
def test_notebook_shows_units_only_for_notes_that_saved_them(trait: dict, art_name: str | None, shown: str) -> None:
    art = {"best_trait_breakpoints": [{"art_url": None, "trait_name": art_name}]}
    text = _render_slip(_riot_note_data(trait), art)
    assert shown in text
    if "num_units" not in trait:
        assert "unit" not in text
