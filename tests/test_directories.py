"""Item and trait directories (`tftlab.directories`, `/api/items`,
`/api/traits`, `/items`, `/traits`): what is counted, how, and in what order
-- recurrence only, never placement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _helpers import make_match, make_unit
from tftlab.directories import compute_directories, items_payload, traits_payload
from tftlab.roster import load_roster
from tftlab.storage import Database

WINDOW, OTHER_VERSION = "14.6", "Version 14.7.581.1234 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
ROSTER = load_roster()
KOGMAW, = ROSTER.champion_ids("Kog'Maw")
DRAVEN, = ROSTER.champion_ids("Draven")
KARMA, = ROSTER.champion_ids("Karma")
ADAPTOR, = ROSTER.trait_ids("Adaptor")
CAUSTIC, = ROSTER.trait_ids("Caustic")
GUINSOO, IE, EMBLEM = "DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_18_EmblemBlackthorn"
WEB = Path(__file__).parent.parent / "src" / "tftlab" / "web"


def _trait(trait_id: str, num_units: int, active: bool = True) -> dict:
    return {"name": trait_id, "num_units": num_units, "style": 1 if active else 0,
            "tier_current": 1 if active else 0, "tier_total": 3}


def _seed(db: Database) -> None:
    # Kog'Maw carries Guinsoo + IE on 12 boards (Adaptor at 3, Caustic at 1); placements are all 8th.
    for i in range(12):
        db.ingest_match(make_match(f"K{i}", placement=8, units=[make_unit(KOGMAW, items=[GUINSOO, IE]), make_unit(KARMA)],
                                   traits=[_trait(ADAPTOR, 3), _trait(CAUSTIC, 1)]))
    # Draven carries Guinsoo + IE on 11 boards, all wins; Adaptor active at 2 (from teammates), Caustic inactive.
    for i in range(11):
        db.ingest_match(make_match(f"D{i}", placement=1, units=[make_unit(DRAVEN, items=[GUINSOO, IE]), make_unit(KARMA)],
                                   traits=[_trait(ADAPTOR, 2), _trait(CAUSTIC, 1, active=False)]))
    # Two committed Kog'Maw copies on one board: one carry board; the canonical copy (most items) holds the emblem.
    db.ingest_match(make_match("KK", units=[make_unit(KOGMAW, items=[GUINSOO, IE, EMBLEM]), make_unit(KOGMAW, items=[GUINSOO, IE])],
                               traits=[_trait(ADAPTOR, 3)]))
    # A Kog'Maw holding two Guinsoo's: the item counts once for that carry board.
    db.ingest_match(make_match("KG", units=[make_unit(KOGMAW, items=[GUINSOO, GUINSOO, IE])], traits=[_trait(ADAPTOR, 2)]))
    # Another balance window: never counted.
    for i in range(20):
        db.ingest_match(make_match(f"OLD{i}", game_version=OTHER_VERSION, units=[make_unit(DRAVEN, items=[GUINSOO, IE])],
                                   traits=[_trait(ADAPTOR, 4)]))
    db.commit()


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "dir.sqlite3")
    _seed(database)
    yield database
    database.close()


def test_carry_boards_items_and_traits_are_counted_once_per_board(db: Database) -> None:
    d = compute_directories(db, WINDOW)
    assert d.window_boards == 25 and d.per_carry == {KOGMAW: 14, DRAVEN: 11} and d.carry_boards == 25
    assert d.items[GUINSOO] == {KOGMAW: 14, DRAVEN: 11}  # KG's two Guinsoo's count once; KK's two copies once
    assert d.items[EMBLEM] == {KOGMAW: 1}
    assert d.trait_counts[ADAPTOR] == {2: 12, 3: 13} and d.trait_counts[CAUSTIC] == {1: 12}  # inactive never counted
    assert d.trait_carries[ADAPTOR] == {KOGMAW: 14, DRAVEN: 11}


def test_items_are_ordered_by_carry_boards_never_by_placement(db: Database) -> None:
    body = items_payload(compute_directories(db, WINDOW))
    ids = [r["item"]["id"] for r in body["items"]]
    assert ids == sorted([GUINSOO, IE], key=lambda i: {GUINSOO: "Guinsoo's Rageblade", IE: "Infinity Edge"}[i])
    guinsoo = body["items"][0]
    assert guinsoo["carry_boards"] == 25 and guinsoo["share_of_carry_boards"] == 1.0
    # Kog'Maw (14 boards, all 8th) is listed above Draven (11 boards, all wins): recurrence only.
    assert [(c["character_id"], c["boards"]) for c in guinsoo["carries"]] == [(KOGMAW, 14), (DRAVEN, 11)]
    assert guinsoo["carries"][0]["share_of_carry_boards"] == 1.0
    text = json.dumps(body["items"]).lower()  # the rows: no result numbers at all
    for absent in ("placement", "top4", "top_4", "win_rate", "score"):
        assert absent not in text, absent


def test_the_board_floor_applies_to_rows_and_to_the_carries_under_them(db: Database) -> None:
    d = compute_directories(db, WINDOW)
    assert EMBLEM not in [r["item"]["id"] for r in items_payload(d)["items"]]  # one board: under the floor
    raised = items_payload(d, min_boards=12)
    assert [(c["character_id"]) for c in raised["items"][0]["carries"]] == [KOGMAW]  # Draven's 11 < 12


def test_items_can_be_filtered_by_class(db: Database) -> None:
    d = compute_directories(db, WINDOW)
    assert items_payload(d, kind="emblem", min_boards=10)["items"] == []
    assert {r["item"]["kind"] for r in items_payload(d, kind="standard")["items"]} == {"standard"}


def test_traits_show_unit_counts_in_order_breakpoints_and_who_brings_the_trait(db: Database) -> None:
    body = traits_payload(compute_directories(db, WINDOW))
    rows = {r["trait_id"]: r for r in body["traits"]}
    adaptor = rows[ADAPTOR]
    assert [r["trait_id"] for r in body["traits"]][0] == ADAPTOR  # 25 boards, most first
    assert adaptor["boards"] == 25 and adaptor["share_of_window_boards"] == 1.0
    assert [(c["num_units"], c["boards"]) for c in adaptor["counts"]] == [(2, 12), (3, 13)]  # value order
    assert adaptor["breakpoints"] == list(ROSTER.breakpoints(ADAPTOR))
    carries = {c["character_id"]: c for c in adaptor["carries"]}
    assert carries[KOGMAW]["has_trait"] is True and carries[DRAVEN]["has_trait"] is False
    assert rows[CAUSTIC]["boards"] == 12


# ---------------------------------------------------------------- API and pages


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "site.sqlite3"
    database = Database(path)
    _seed(database)
    database.close()
    monkeypatch.setenv("TFT_DB_PATH", str(path))
    import tftlab.webapp as webapp

    webapp._DIRECTORY_CACHE.clear()
    return TestClient(webapp.create_app())


def test_the_items_and_traits_apis(client: TestClient) -> None:
    items = client.get("/api/items", params={"balance_window": WINDOW}).json()
    assert items["balance_window"] == WINDOW and items["boards"] == 25 and items["evidence_type"] == "observed"
    assert items["min_boards"] == 10 and "never by placement" in items["ordering"] and items["source"]
    assert items["matching_items"] == 2 and items["items"][0]["carries"]
    assert client.get("/api/items", params={"balance_window": WINDOW, "kind": "emblem"}).json()["items"] == []
    assert client.get("/api/items", params={"kind": "bogus"}).status_code == 422
    assert client.get("/api/items", params={"min_boards": 5}).status_code == 422  # never under the floor
    assert client.get("/api/items", params={"balance_window": "9.9"}).status_code == 404
    traits = client.get("/api/traits", params={"balance_window": WINDOW}).json()
    assert traits["window_boards"] == 25 and traits["traits"][0]["trait_id"] == ADAPTOR


def test_the_directory_is_computed_once_per_window_until_new_matches(client: TestClient, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.webapp as webapp

    calls = []
    real = webapp.compute_directories
    monkeypatch.setattr(webapp, "compute_directories", lambda db, w: calls.append(w) or real(db, w))
    client.get("/api/items", params={"balance_window": WINDOW})
    client.get("/api/traits", params={"balance_window": WINDOW})
    client.get("/api/items", params={"balance_window": WINDOW, "kind": "standard"})
    assert calls == [WINDOW]
    with Database(tmp_path / "site.sqlite3") as db:
        db.ingest_match(make_match("NEW", game_datetime=1_790_000_000_001, units=[make_unit(KOGMAW, items=[GUINSOO, IE])]))
        db.commit()
    assert client.get("/api/items", params={"balance_window": WINDOW}).json()["items"][0]["carry_boards"] == 26
    assert calls == [WINDOW, WINDOW]


@pytest.mark.parametrize("page", ["items", "traits"])
def test_the_pages_load_their_script_and_say_how_they_are_ordered(client: TestClient, page: str) -> None:
    html = client.get(f"/{page}").text
    assert f'data-directory="{page}"' in html and '/static/directory.js' in html
    assert f'<a href="/{page}" aria-current="page">{page}</a>' in html
    assert "never by how they placed" in html


def test_every_page_links_to_the_directories() -> None:
    for page in sorted(WEB.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        assert text.count('href="/items"') == 2 and text.count('href="/traits"') == 2, page.name


def test_the_directory_script_never_ranks_or_advises() -> None:
    js = (WEB / "static" / "directory.js").read_text(encoding="utf-8").lower()
    for banned in ("best", "optimal", "recommended", "tier list", "strongest", "should play", "meta", "avg place",
                   "top 4", "win rate", ".sort("):
        assert banned not in js, banned
