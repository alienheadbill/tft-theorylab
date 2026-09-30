"""Champion Investigation: the read-only /api/champions endpoints and pages."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _helpers import make_match, make_unit

V_OLD = "Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>"
V_NEW = "Version 14.7.580.4321 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
KHA = "DA_18_KhaZix"
SLAYER = {"name": "DA_18_Slayer", "num_units": 4, "style": 2, "tier_current": 2, "tier_total": 3}


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_path: Path | None = None) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(db_path or tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


@pytest.fixture()
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(monkeypatch, tmp_path)


def _kha_board(match_id: str, *, version: str, when: int, placement: int, items: list[str], partner: str) -> dict:
    """A live-shaped board: Match-V1 units carry no display names (the id is
    stored as the name), items are Set 18 `DA_*` ids."""
    units = [
        {"character_id": KHA, "rarity": 2, "tier": 2, "itemNames": items},
        {"character_id": partner, "rarity": 2, "tier": 2, "itemNames": []},
    ]
    return make_match(match_id, game_version=version, game_datetime=when, set_number=18, units=units,
                      placement=placement, traits=[SLAYER])


@pytest.fixture()
def two_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Kha'Zix carried in two balance windows with different builds and partners."""
    from tftlab.storage import Database

    db_path = tmp_path / "live_shaped.sqlite3"
    with Database(db_path) as db:
        for i in range(3):
            db.ingest_match(_kha_board(f"OLD_{i}", version=V_OLD, when=1_700_000_000_000 + i, placement=1 + i,
                                       items=["DA_Bloodthirster", "DA_InfinityEdge", "DA_EdgeOfNight"],
                                       partner="DA_18_Diana"))
        for i in range(5):
            db.ingest_match(_kha_board(f"NEW_{i}", version=V_NEW, when=1_800_000_000_000 + i, placement=2 + i,
                                       items=["DA_GiantSlayer", "DA_InfinityEdge", "DA_LastWhisper"],
                                       partner="DA_18_Hecarim"))
        # Ahri is on a board in the new window, never as a carry.
        db.ingest_match(make_match("NEW_AHRI", game_version=V_NEW, game_datetime=1_800_000_000_100, set_number=18,
                                   units=[make_unit("DA_18_Ahri", tier=2, items=[])], placement=5))
    return _client(monkeypatch, tmp_path, db_path)


# ---------------------------------------------------------------- picker


def test_directory_lists_champions_by_name_with_art_and_carry_counts(demo: TestClient) -> None:
    body = demo.get("/api/champions").json()
    assert body["evidence_type"] == "observed" and body["balance_window"]
    by_slug = {c["slug"]: c for c in body["champions"]}
    kha = by_slug["khazix"]
    assert kha["name"] == "Kha'Zix" and kha["character_id"] == KHA and kha["cost"] == 3
    assert kha["art_url"].startswith("/static/game/champions/") and kha["carry_games"] > 0
    assert by_slug["ahri"]["carry_games"] == 0  # listed even with no carry games
    assert len(by_slug) == len(body["champions"])  # slugs are unique
    assert all(1 <= c["cost"] <= 5 for c in body["champions"])


@pytest.mark.parametrize("key", ["khazix", "Kha'Zix", "kha zix", "KHAZIX", KHA])
def test_champion_lookup_needs_no_riot_id(demo: TestClient, key: str) -> None:
    response = demo.get(f"/api/champions/{key}")
    assert response.status_code == 200
    assert response.json()["champion"]["character_id"] == KHA


@pytest.mark.parametrize("key", ["not-a-champion", "TFT99_Nobody", "%20", "zz"])
def test_unknown_champion_is_a_clean_404(demo: TestClient, key: str) -> None:
    response = demo.get(f"/api/champions/{key}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Champion not found"}


# ---------------------------------------------------------------- investigation


def test_investigation_carries_the_evidence_sections(demo: TestClient) -> None:
    body = demo.get("/api/champions/khazix").json()
    carry = body["carry"]
    assert carry["games"] > 0 and 1 <= carry["avg_placement"] <= 8
    for key in ("top4_rate", "win_rate", "carry_conversion_rate", "appearance_rate"):
        assert 0 <= carry[key] <= 1
    ts = carry["three_star"]
    assert ts["hit_games"] + ts["miss_games"] == carry["games"]
    assert carry["sample"]["label"] and carry["sample"]["low_sample"] is (carry["games"] < 30)
    assert body["items"]["most_common_build"]["items"][0]["name"]
    assert body["partners"] and all(p["name"] and p["slug"] for p in body["partners"])
    assert body["window"]["balance_window"] == body["balance_window"]
    assert body["window_average"]["carry_games"] >= carry["games"]


def test_everything_shown_is_observed_and_internal_scores_stay_out(demo: TestClient) -> None:
    body = demo.get("/api/champions/khazix").json()
    rows = [body["items"]["most_common_build"], *body["items"]["builds"], *body["items"]["pairs"],
            *body["partners"], *body["traits"]]
    assert body["evidence_type"] == "observed" and body["carry"]["evidence"] == "observed"
    assert rows and all(r["evidence"] == "observed" for r in rows)
    text = json.dumps(body).lower()
    for absent in ("variant", "theorycraft", "inferred", "association_score", "posterior", "opportunity",
                   "archetype", "cluster"):
        assert absent not in text, absent


def test_known_champion_without_carry_games_gets_an_empty_state_not_an_error(demo: TestClient) -> None:
    body = demo.get("/api/champions/ahri").json()  # never in the demo data at all
    assert body["carry"] is None and body["appearances"] == 0
    assert body["partners"] == body["traits"] == [] and body["items"]["builds"] == []
    filler = demo.get("/api/champions/leona").json()  # on demo boards, never carried
    assert filler["carry"] is None and filler["appearances"] > 0


def test_investigation_is_scoped_to_one_balance_window(two_windows: TestClient) -> None:
    windows = [w["balance_window"] for w in two_windows.get("/api/balance-windows").json()["windows"]]
    assert len(windows) == 2
    newest, oldest = windows  # latest first

    default = two_windows.get("/api/champions/khazix").json()
    new = two_windows.get("/api/champions/khazix", params={"balance_window": newest}).json()
    old = two_windows.get("/api/champions/khazix", params={"balance_window": oldest}).json()
    assert default["balance_window"] == newest and default["carry"]["games"] == 5
    assert new["carry"]["games"] == 5 and old["carry"]["games"] == 3
    assert [p["name"] for p in new["partners"]] == ["Hecarim"]
    assert [p["name"] for p in old["partners"]] == ["Diana"]
    new_build = {i["name"] for i in new["items"]["most_common_build"]["items"]}
    old_build = {i["name"] for i in old["items"]["most_common_build"]["items"]}
    assert new_build == {"Giant Slayer", "Infinity Edge", "Last Whisper"}
    assert old_build == {"Bloodthirster", "Infinity Edge", "Edge of Night"}
    assert new["window"]["matches"] == 6 and old["window"]["matches"] == 3

    counts = {c["slug"]: c["carry_games"] for c in two_windows.get(
        "/api/champions", params={"balance_window": oldest}).json()["champions"]}
    assert counts["khazix"] == 3


def test_live_shaped_ids_resolve_to_display_names_and_art(two_windows: TestClient) -> None:
    body = two_windows.get("/api/champions/khazix").json()
    assert body["champion"]["name"] == "Kha'Zix"
    items = body["items"]["most_common_build"]["items"]
    assert all(i["art_url"] and i["art_url"].startswith("/static/game/items/") for i in items)
    partner = body["partners"][0]
    assert partner["name"] == "Hecarim" and partner["slug"] == "hecarim" and partner["art_url"]
    trait = body["traits"][0]
    assert trait["name"] == "Ravager" and trait["art_url"] and "tier" not in trait
    # Riot's num_units (4), never the tier_current ordinal (2).
    assert [c["num_units"] for c in trait["counts"]] == [4]
    text = json.dumps({k: body[k] for k in ("champion", "items", "partners", "traits")})
    assert '"name": "DA_' not in text


def test_small_samples_are_flagged(two_windows: TestClient) -> None:
    body = two_windows.get("/api/champions/khazix").json()
    sample = body["carry"]["sample"]
    assert sample["low_sample"] is True
    assert sample["label"] == "Low sample"
    assert "stable estimate" in sample["meaning"]
    assert "band" not in sample  # research-report bands are not production confidence classes
    assert all(r["limited_sample"] for r in body["partners"])


def test_missing_or_unknown_window_is_an_empty_view(two_windows: TestClient) -> None:
    body = two_windows.get("/api/champions/khazix", params={"balance_window": "99.9"}).json()
    assert body["window"] is None and body["carry"] is None
    ahri = two_windows.get("/api/champions/ahri").json()
    assert ahri["carry"] is None and ahri["appearances"] == 1


# ---------------------------------------------------------------- pages and Discovery


@pytest.mark.parametrize("path", ["/champions", "/champions/khazix"])
def test_champion_pages_are_served(demo: TestClient, path: str) -> None:
    response = demo.get(path)
    assert response.status_code == 200
    assert "/static/champion.js" in response.text and "Champion Investigation" in response.text


def test_discovery_candidates_gain_display_names_and_an_investigation_slug(two_windows: TestClient) -> None:
    candidate = two_windows.get("/api/discovery", params={"min_samples": 1}).json()["candidates"][0]
    assert candidate["name"] == KHA  # unchanged: the stored value
    assert candidate["display_name"] == "Kha'Zix" and candidate["slug"] == "khazix"
    assert candidate["best_partners"][0]["display_name"] == "Hecarim"


def test_static_page_never_hardcodes_a_champion() -> None:
    """Data-driven: no ids anywhere, and no champion name in the script (the
    HTML's search placeholder shows one only as example text)."""
    web = Path(__file__).parent.parent / "src" / "tftlab" / "web"
    for name in ("champion.html", "static/champion.js"):
        text = (web / name).read_text()
        assert "DA_18_" not in text and "TFT18_" not in text and "/static/game/" not in text
    assert "Kha" not in (web / "static" / "champion.js").read_text()


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_investigation_queries_run_on_postgres() -> None:
    from tftlab.champion_investigation import champion_directory, champion_investigation, find_champion
    from tftlab.storage import Database

    db = Database(POSTGRES_TEST_URL)
    try:
        for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        for i in range(4):
            db.ingest_match(_kha_board(f"PG_KHA_{i}", version=V_NEW, when=1_800_000_000_000 + i, placement=1 + i,
                                       items=["DA_GiantSlayer", "DA_InfinityEdge", "DA_LastWhisper"],
                                       partner="DA_18_Hecarim"))
        window = db.query_one("SELECT balance_window FROM matches LIMIT 1")[0]
        directory = champion_directory(db, window)
        kha = champion_investigation(db, find_champion(directory, "khazix"), window)
        assert kha["carry"]["games"] == 4 and kha["partners"][0]["name"] == "Hecarim"
        hecarim = champion_investigation(db, find_champion(directory, "hecarim"), window)
        assert hecarim["carry"] is None and hecarim["appearances"] == 4  # the subquery count
    finally:
        db.close()
