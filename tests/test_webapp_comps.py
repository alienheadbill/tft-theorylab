"""Observed comps: GET /api/comps and the /comps page (tftlab.observed_comps
over tftlab.analytics.comps). Window-wide exact lineups, grouped into
observed board patterns and ordered by boards only."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _helpers import build_live_ingest_like_source, make_match, make_unit
from tftlab import observed_comps as oc
from tftlab import public_snapshot as ps
from tftlab.roster import load_roster
from tftlab.storage import Database

WEB = Path(__file__).parents[1] / "src" / "tftlab" / "web"
JS_TESTS = Path(__file__).parent / "js" / "comps_load.test.js"
WINDOW, OTHER_VERSION = "14.6", "Version 14.7.581.1234 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
ROSTER = load_roster()
KOGMAW, = ROSTER.champion_ids("Kog'Maw")   # 3-cost
DRAVEN, = ROSTER.champion_ids("Draven")
KARMA, = ROSTER.champion_ids("Karma")
GROMP, = ROSTER.champion_ids("Gromp")
NIDALEE, = ROSTER.champion_ids("Nidalee")  # 4-cost
VI, = ROSTER.champion_ids("Vi")
X1, X2, X3 = [cid for cid in sorted(ROSTER.champions)
              if ROSTER.is_shop_champion(cid) and cid not in {KOGMAW, DRAVEN, KARMA, GROMP, NIDALEE, VI}][:3]
ITEMS = ["DA_GuinsoosRageblade", "DA_InfinityEdge"]

A = [KOGMAW, DRAVEN, KARMA, VI]          # the most played lineup; every board 8th
A2 = [KOGMAW, DRAVEN, KARMA, GROMP]      # one unit from A; every board 1st
B = [NIDALEE, X1, X2, X3]                # every board 1st
D = [KOGMAW, NIDALEE, X1, X2]            # two committed carries on every board; one unit from B
CARRIES = {KOGMAW, NIDALEE}


def _board(match_id: str, units: list[str], placement: int, level: int, *, version: str | None = None) -> dict:
    """A one-board match; Kog'Maw and Nidalee hold carry items (committed)."""
    payload = [make_unit(u, items=ITEMS if u in CARRIES else None) for u in units]
    kwargs = {"game_version": version} if version else {}
    return make_match(match_id, units=payload, placement=placement, level=level, **kwargs)


def _seed(db: Database) -> None:
    for i in range(30):
        db.ingest_match(_board(f"A8_{i}", A, 8, 8))
    for i in range(12):
        db.ingest_match(_board(f"A28_{i}", A2, 1, 8))
    for i in range(15):
        db.ingest_match(_board(f"B8_{i}", B, 1, 8))
    for i in range(10):
        db.ingest_match(_board(f"D8_{i}", D, 4, 8))
    for i in range(9):  # below the floor
        db.ingest_match(_board(f"C8_{i}", [GROMP, NIDALEE, X1], 2, 8))
    for i in range(11):  # the same lineup as A, at level 9: never pooled with level 8
        db.ingest_match(_board(f"A9_{i}", A, 2, 9))
    for i in range(5):  # no observed level: never a "level 0" group
        db.ingest_match(_board(f"A0_{i}", A, 3, 0))
    for i in range(20):  # another window: never mixed in
        db.ingest_match(_board(f"AW_{i}", A, 1, 8, version=OTHER_VERSION))


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_path: Path | None = None) -> TestClient:
    for key in ("DATABASE_URL", "TFT_DATA_SOURCE", "TFT_STALE_AFTER_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(db_path or tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


@pytest.fixture()
def seeded(tmp_path: Path) -> Path:
    path = tmp_path / "comps.sqlite3"
    with Database(path) as db:
        _seed(db)
    return path


@pytest.fixture()
def client(seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(monkeypatch, tmp_path, seeded)


def _get(client: TestClient, **params) -> dict:
    response = client.get("/api/comps", params={"balance_window": WINDOW, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _keys(lineup: list[str]) -> str:
    return "+".join(sorted(lineup))


# ---------------------------------------------------------------- shape


def test_response_shape_window_freshness_and_rules(client: TestClient) -> None:
    body = _get(client)
    assert body["balance_window"] == WINDOW and body["demo"] is False
    assert body["matches"] == 92 and body["boards"] == 92  # one board per test match; every window board counted
    assert body["window"] == {"balance_window": WINDOW, "matches": 92, "boards": 92,
                              "latest_game_datetime": body["latest_game"]}
    assert body["latest_game"]
    # The same data-source / freshness block as /api/source.
    source = client.get("/api/source").json()
    for key in ("mode", "label", "observed", "synthetic", "demo", "matches", "boards", "latest_game_datetime",
                "stale", "stale_after_days", "age_days"):
        assert body["source"][key] == source[key], key
    assert body["evidence"] == "observed" and body["available"] is True and body["reason"] is None
    assert body["promotion_rule"]["min_boards_floor"] == 10 and body["promotion_rule"]["min_boards"] == 10
    assert "never by placement" in body["ordering"]
    assert body["placement_label"] == "how these boards placed"
    assert body["filters"] == {"level": None, "cost": None, "min_boards": 10, "limit": 50}
    assert body["boards_with_level"] == 87 and body["window_boards"] == 92


def test_pattern_rows_carry_units_boards_level_and_descriptive_placement(client: TestClient) -> None:
    first = _get(client)["patterns"][0]
    assert first["type"] == "flex_shell" and first["level"] == 8 and first["unit_count"] == 4
    assert first["boards"] == 42 and first["window_boards_at_level"] == 76
    assert first["share_of_level"] == pytest.approx(42 / 76)
    assert [u["character_id"] for u in first["shared_units"]] == [KOGMAW, KARMA, DRAVEN]  # carry, then by cost
    assert first["shared_units"][0]["is_carry"] is True
    assert first["lead_carry"]["character_id"] == KOGMAW and first["lead_carry"]["slug"] == "kogmaw"
    assert [o["lineup_key"] for o in first["lineups"]] == [_keys(A), _keys(A2)]
    assert [o["flex_unit"]["character_id"] for o in first["lineups"]] == [VI, GROMP]
    assert [o["is_anchor"] for o in first["lineups"]] == [True, False]
    option = first["lineups"][0]
    assert option["boards"] == 30 and option["share_of_level"] == pytest.approx(30 / 76)
    assert option["how_these_boards_placed"] == {"label": "how these boards placed", "avg_placement": 8.0,
                                                 "top4_rate": 0.0, "win_rate": 0.0}
    assert option["carries"][0]["character_id"] == KOGMAW and option["carries"][0]["boards"] == 30
    assert option["sample"]["low_sample"] is False and option["evidence"] == "observed"
    # No result is combined across the pattern's lineups.
    for key in ("avg_placement", "top4_rate", "win_rate", "how_these_boards_placed"):
        assert key not in first, key


def test_card_names_are_made_from_units() -> None:
    w = oc.ComputedWindow(WINDOW, 0, None, {}, {})
    assert oc.comp_name([KOGMAW, DRAVEN, KARMA], KOGMAW, 4, w) == "Kog'Maw · Draven · Karma + 1"
    assert oc.comp_name([KOGMAW, DRAVEN, DRAVEN, KARMA, VI], KOGMAW, 5, w) == "Kog'Maw · Draven ×2 · Vi + 1"
    assert oc.comp_name([KARMA, VI], None, 2, w) == "Vi · Karma"  # no carry: most expensive first


# ---------------------------------------------------------------- ordering


def test_most_played_lineup_comes_first_even_though_it_places_worst(client: TestClient) -> None:
    patterns = _get(client)["patterns"]
    # Pattern A (+A2): 42 boards, its anchor finished 8th every time. B (+D): 25 boards, 1st and 4th.
    assert [p["boards"] for p in patterns] == [42, 25, 11]
    assert patterns[0]["anchor_lineup_key"] == _keys(A)
    assert patterns[0]["lineups"][0]["how_these_boards_placed"]["avg_placement"] == 8.0
    assert patterns[1]["lineups"][0]["how_these_boards_placed"]["avg_placement"] == 1.0
    # A standalone lineup that places worst still leads a level-filtered list.
    level9 = _get(client, level=9)["patterns"]
    assert [p["anchor_lineup_key"] for p in level9] == [_keys(A)]


def test_order_is_by_boards_never_by_results() -> None:
    from tftlab.analytics.boards import ObservedBoard
    from tftlab.analytics.comps import comp_patterns, group_window_boards

    def boards(lineup, n, placement, tag):
        key = tuple(sorted(lineup))
        return [ObservedBoard(f"{tag}{i}", 0, placement, 8, 0, key, (), (lineup[0],)) for i in range(n)]

    # Three unrelated lineups: worst results on the most boards, best on the fewest.
    seq = boards([KOGMAW, DRAVEN], 40, 8, "w") + boards([NIDALEE, X1], 20, 4, "m") + boards([VI, X2], 10, 1, "b")
    patterns = comp_patterns(group_window_boards(seq, min_boards=10), min_boards=10)
    assert [p.boards for p in patterns] == [40, 20, 10]


# ---------------------------------------------------------------- universe and counting


def test_a_board_with_two_committed_carries_counts_once_and_under_both_carries(client: TestClient) -> None:
    second = _get(client)["patterns"][1]
    assert second["type"] == "flex_shell" and second["boards"] == 25
    assert [o["lineup_key"] for o in second["lineups"]] == [_keys(B), _keys(D)]
    d = second["lineups"][1]
    assert d["boards"] == 10  # ten boards, not twenty
    assert {c["character_id"]: c["boards"] for c in d["carries"]} == {KOGMAW: 10, NIDALEE: 10}
    # Carry-agnostic: Kog'Maw (a committed carry on D) is D's flex unit.
    assert d["flex_unit"]["character_id"] == KOGMAW
    # Pattern carries add up to more than its boards; the lead is the carry on the most boards.
    assert {c["character_id"]: c["boards"] for c in second["carries"]} == {NIDALEE: 25, KOGMAW: 10}
    assert second["lead_carry"]["character_id"] == NIDALEE
    assert second["boards_without_committed_carry"] == 0


def test_levels_are_never_pooled_and_unreported_levels_are_no_group(client: TestClient) -> None:
    body = _get(client)
    assert [g["level"] for g in body["levels"]] == [8, 9]
    assert {g["level"]: g["window_boards_at_level"] for g in body["levels"]} == {8: 76, 9: 11}
    level9 = body["patterns"][2]
    assert level9["level"] == 9 and level9["boards"] == 11 and level9["window_boards_at_level"] == 11
    assert level9["type"] == "exact_board" and level9["lineups"][0]["how_these_boards_placed"]["avg_placement"] == 2.0


def test_the_promotion_floor_and_min_boards(client: TestClient) -> None:
    body = _get(client)
    keys = {o["lineup_key"] for p in body["patterns"] for o in p["lineups"]}
    assert _keys([GROMP, NIDALEE, X1]) not in keys  # 9 boards
    assert body["qualifying_lineups"] == 5
    higher = _get(client, min_boards=12)
    # D (10) and level-9 A (11) drop out; B is now a standalone lineup.
    assert [[o["lineup_key"] for o in p["lineups"]] for p in higher["patterns"]] == [[_keys(A), _keys(A2)], [_keys(B)]]
    assert [p["type"] for p in higher["patterns"]] == ["flex_shell", "exact_board"]
    assert higher["promotion_rule"]["min_boards"] == 12
    response = client.get("/api/comps", params={"balance_window": WINDOW, "min_boards": 9})
    assert response.status_code == 422  # never below the product floor


def test_filters_select_patterns_without_changing_any(client: TestClient) -> None:
    everything = {p["anchor_lineup_key"] + f"@{p['level']}": p for p in _get(client, limit=500)["patterns"]}
    for params in ({"level": 8}, {"level": 9}, {"cost": "3"}, {"cost": "4"}, {"cost": "1,4"}, {"limit": 1}):
        body = _get(client, **params)
        for p in body["patterns"]:
            assert p == everything[p["anchor_lineup_key"] + f"@{p['level']}"], params
        assert body["qualifying_patterns"] == 3
    assert [p["level"] for p in _get(client, level=8)["patterns"]] == [8, 8]
    assert [p["lead_carry"]["cost"] for p in _get(client, cost="3")["patterns"]] == [3, 3]
    assert [p["boards"] for p in _get(client, cost="4")["patterns"]] == [25]
    limited = _get(client, limit=1)
    assert len(limited["patterns"]) == 1 and limited["matching_patterns"] == 3


# ---------------------------------------------------------------- states


def test_filters_that_match_nothing_say_so(client: TestClient) -> None:
    body = _get(client, cost="1")
    assert body["patterns"] == [] and body["matching_patterns"] == 0
    assert body["note"] == oc.NOTHING_MATCHES and body["available"] is True


def test_a_window_with_nothing_promoted_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "thin.sqlite3"
    with Database(path) as db:
        for i in range(9):
            db.ingest_match(_board(f"T{i}", A, 4, 8))
    body = _get(_client(monkeypatch, tmp_path, path))
    assert body["available"] is True and body["patterns"] == [] and body["qualifying_lineups"] == 0
    assert body["note"] == oc.NOTHING_PROMOTED.format(n=10)


def test_unknown_window_and_empty_store_are_404(client: TestClient, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    response = client.get("/api/comps", params={"balance_window": "9.99"})
    assert response.status_code == 404 and response.json()["detail"] == "Balance window not found"
    empty = tmp_path / "empty.sqlite3"
    Database(empty).close()
    monkeypatch.setenv("TFT_DATA_SOURCE", "database")
    monkeypatch.setenv("DATABASE_URL", str(empty))
    from tftlab.webapp import create_app

    response = TestClient(create_app()).get("/api/comps")
    assert response.status_code == 404 and response.json()["detail"] == "No data available"


def test_default_window_and_bad_parameters(client: TestClient) -> None:
    default = client.get("/api/balance-windows").json()["default_balance_window"]
    assert client.get("/api/comps").json()["balance_window"] == default
    for params in ({"level": 0}, {"level": 11}, {"cost": "6"}, {"cost": "1,,2"}, {"limit": 0}):
        assert client.get("/api/comps", params=params).status_code == 422, params


def test_demo_mode_is_labelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFT_DATA_SOURCE", "demo")
    body = _client(monkeypatch, tmp_path).get("/api/comps")
    assert body.status_code == 200
    body = body.json()
    assert body["demo"] is True and body["source"]["synthetic"] is True and body["source"]["observed"] is False
    assert body["source"]["label"] == "demo data (synthetic)"


def _snapshot(tmp_path: Path) -> Path:
    src = tmp_path / "source.sqlite3"
    build_live_ingest_like_source(src, 24)
    with Database.open_existing(src) as db:
        return ps.export_public_snapshot(db, tmp_path / "pub" / "public.sqlite3", prepare=False).path


def _snapshot_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: Path) -> TestClient:
    for key in ("DATABASE_URL", "RIOT_API_KEY", "TFT_STALE_AFTER_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TFT_DATA_SOURCE", "snapshot")
    monkeypatch.setenv("TFT_SNAPSHOT_PATH", str(path))
    monkeypatch.setenv("TFT_SNAPSHOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


def test_a_snapshot_with_levels_is_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _snapshot_client(monkeypatch, tmp_path, _snapshot(tmp_path)).get("/api/comps").json()
    assert body["available"] is True and body["source"]["mode"] == "snapshot"
    assert body["boards_with_level"] == body["boards"] > 0


def test_a_snapshot_without_levels_says_levels_were_not_preserved(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    path = _snapshot(tmp_path)
    conn = sqlite3.connect(path)  # what a snapshot exported before levels were kept looks like
    prov = json.loads(conn.execute(f"SELECT value FROM {ps.METADATA_TABLE}").fetchone()[0])
    prov.pop(ps.PARTICIPANT_LEVEL_KEY)
    prov["sanitized_columns"]["participants"]["level"] = 0
    conn.execute(f"UPDATE {ps.METADATA_TABLE} SET value = ?", (json.dumps(prov, sort_keys=True),))
    conn.execute("UPDATE participants SET level = 0")
    conn.commit()
    conn.close()
    response = _snapshot_client(monkeypatch, tmp_path, path).get("/api/comps")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False and body["reason"] == oc.LEVELS_NOT_PRESERVED
    assert body["reason"].startswith("Player levels not preserved in this data")
    assert body["patterns"] == [] and body["levels"] == [] and body["boards"] > 0


# ---------------------------------------------------------------- cache


def test_the_window_grouping_is_cached_and_recomputed_on_new_matches(seeded: Path, tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.webapp as webapp

    calls = []
    real = webapp.compute_window
    monkeypatch.setattr(webapp, "compute_window", lambda db, w: calls.append(w) or real(db, w))
    client = _client(monkeypatch, tmp_path, seeded)
    first = _get(client)
    assert _get(client, level=8) and _get(client, cost="3") and _get(client) == first
    assert calls == [WINDOW]
    with Database(seeded) as db:  # a new match in the window
        db.ingest_match(_board("NEW_0", B, 1, 8))
    assert _get(client)["patterns"][1]["boards"] == 26
    assert calls == [WINDOW, WINDOW]


def test_the_window_grouping_uses_two_queries(seeded: Path) -> None:
    db = Database(seeded)
    seen = []
    real = db.query_all
    db.query_all = lambda sql, params=(): seen.append(sql) or real(sql, params)  # type: ignore[method-assign]
    try:
        oc.compute_window(db, WINDOW)
    finally:
        db.close()
    assert len(seen) == 2  # distinct item sets through the carry rule, then every board's units


# ---------------------------------------------------------------- page and wording

#: Brief A's banned words (results ranking or advice).
BANNED = ("best", "optimal", "recommended", "recommend", "tier", "tiers", "s-tier", "meta", "strongest",
          "top comp", "should play")


def _visible(html: str) -> str:
    """Page text and attributes, without the <head> metadata tags."""
    return re.sub(r"<meta [^>]*>", "", html)


def test_page_and_script_never_use_ranking_words() -> None:
    texts = {"comps.html": _visible((WEB / "comps.html").read_text()),
             "comps.js": (WEB / "static" / "comps.js").read_text()}
    for name, text in texts.items():
        lower = text.lower()
        for word in BANNED:
            assert not re.search(rf"(?<![a-z-]){re.escape(word)}(?![a-z])", lower), (name, word)
    js = texts["comps.js"]
    assert "How these boards placed" in js and "LOW SAMPLE" in js
    assert ".sort(" not in js  # the API's order is the page's order


def test_api_wording_never_ranks() -> None:
    text = " ".join([oc.COMPS_DEFINITION, oc.COMPS_ORDERING, oc.COMPS_PATTERN_DEFINITION, oc.COMPS_CARRY_RULE,
                     oc.PLACEMENT_NOTE, oc.LEVELS_NOT_PRESERVED, oc.NOTHING_PROMOTED, oc.NOTHING_MATCHES]).lower()
    for word in BANNED:
        assert not re.search(rf"(?<![a-z-]){re.escape(word)}(?![a-z])", text), word


def test_comps_page_is_served_and_cards_link_to_the_champion_boards_tab(client: TestClient) -> None:
    response = client.get("/comps")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert '<script src="/static/comps.js"></script>' in response.text
    assert '<a href="/comps" aria-current="page">comps</a>' in response.text
    js = (WEB / "static" / "comps.js").read_text()
    assert "championHref(p.lead_carry.slug, 'boards')" in js


def test_comps_page_load_behaviour(seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    client = _client(monkeypatch, tmp_path, seeded)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    bodies = {
        "balance_windows": client.get("/api/balance-windows").json(),
        "comps": _get(client),
        "comps_level9": _get(client, level=9),
        "comps_empty": _get(client, cost="1"),
    }
    for name, body in bodies.items():
        (fixtures / f"{name}.json").write_text(json.dumps(body))
    proc = subprocess.run([node, "--test", "--test-reporter=tap", str(JS_TESTS)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "COMPS_FIXTURES": str(fixtures)})
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
    assert "# fail 0" in proc.stdout
