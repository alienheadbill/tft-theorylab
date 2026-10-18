"""Observed final boards (tftlab.analytics.boards + `observed_boards` on the
Champion Investigation): exact lineups around a committed carry, grouped by
final player level and ordered by recurrence only."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _helpers import build_live_ingest_like_source, make_match, make_unit
from tftlab import champion_investigation as ci
from tftlab import public_snapshot as ps
from tftlab.analytics import carry_commitment_stats
from tftlab.analytics.boards import (
    MAX_PLAYER_LEVEL,
    ObservedBoard,
    committed_carry_boards,
    group_boards,
    shop_champion_id,
    valid_level,
)
from tftlab.champion_investigation import champion_investigation
from tftlab.how_to_play import MIN_BOARDS
from tftlab.roster import Roster, load_roster
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
WINDOW, OTHER_VERSION = "14.6", "Version 14.7.581.1234 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
ROSTER = load_roster()
KOGMAW, = ROSTER.champion_ids("Kog'Maw")
DRAVEN, = ROSTER.champion_ids("Draven")
KARMA, = ROSTER.champion_ids("Karma")
GROMP, = ROSTER.champion_ids("Gromp")
NIDALEE, = ROSTER.champion_ids("Nidalee")
VI, = ROSTER.champion_ids("Vi")
SUMMON = "TFT_Voidspawn"  # in the roster with a cost but no trait: not a shop champion
KOG = {"character_id": KOGMAW, "name": "Kog'Maw", "slug": "kogmaw", "cost": 3,
       "art_url": "/static/game/champions/kogmaw.png"}
ITEMS = ["DA_GuinsoosRageblade", "DA_InfinityEdge"]
TANK_ITEMS = ["DA_WarmogsArmor", "DA_GargoyleStoneplate"]


def _board(match_id: str, mates: list[str], placement: int, level: int, *, carry_tier: int = 2,
           mate_tier: int = 2, carry_items: list[str] = ITEMS, extra_carry: dict | None = None,
           version: str | None = None) -> dict:
    units = [make_unit(KOGMAW, tier=carry_tier, items=carry_items)]
    if extra_carry is not None:
        units.append(extra_carry)
    units += [make_unit(m, tier=mate_tier) for m in mates]
    kwargs = {"game_version": version} if version else {}
    return make_match(match_id, units=units, placement=placement, level=level, **kwargs)


A = [DRAVEN, KARMA, VI]            # lineup A (with the carry): Kog'Maw + Draven + Karma + Vi
B = [GROMP, NIDALEE, VI]           # lineup B
A_DOUBLE = [DRAVEN, DRAVEN, KARMA, VI]  # A with a second Draven copy: a different lineup


def _seed(db: Database) -> None:
    # Level 8: A on 14 boards, all bottom 4 (placements 5-8).
    for i in range(14):
        db.ingest_match(_board(f"A8_{i}", A, 5 + i % 4, 8, carry_tier=3 if i < 4 else 2))
    # Level 8: B on 11 boards, all firsts. Rarer than A, so it must stay below A.
    for i in range(11):
        db.ingest_match(_board(f"B8_{i}", B, 1, 8))
    # Level 8: A with a duplicate Draven on 10 boards, half of them with 1-star teammates.
    for i in range(10):
        db.ingest_match(_board(f"AD8_{i}", A_DOUBLE, 3, 8, mate_tier=1 if i % 2 else 2))
    # Level 8: a one-off lineup on 9 boards: below the floor.
    for i in range(9):
        db.ingest_match(_board(f"C8_{i}", [GROMP, KARMA], 2, 8))
    # Level 9: the SAME lineup A on 12 boards, all wins. Must never pool with level 8's A.
    for i in range(12):
        db.ingest_match(_board(f"A9_{i}", A, 1, 9))
    # Level 9: B on 10 boards with a summon listed too (context, not lineup).
    for i in range(10):
        db.ingest_match(_board(f"B9_{i}", [*B, SUMMON, SUMMON], 2, 9))
    # Level 0 (not reported): never a level group, only in carry_boards.
    for i in range(10):
        db.ingest_match(_board(f"L0_{i}", A, 4, 0))
    # Two committed Kog'Maw copies on one board: still one board.
    for i in range(10):
        db.ingest_match(_board(f"KK7_{i}", A, 4, 7, extra_carry=make_unit(KOGMAW, tier=3, items=ITEMS)))
    # Kog'Maw present but not a carry (tank items only): not a carry board at all.
    for i in range(10):
        db.ingest_match(_board(f"T8_{i}", A, 4, 8, carry_items=TANK_ITEMS))
    # Another balance window: lineup A at level 8 winning 15 times. Must not leak in.
    for i in range(15):
        db.ingest_match(_board(f"OLD_{i}", A, 1, 8, version=OTHER_VERSION))


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        database = Database(tmp_path / "boards.sqlite3")
    else:
        database = Database(POSTGRES_TEST_URL)
        for table in ("discovery_prepared_candidates", "discovery_prepared_runs", "traits", "units",
                      "participants", "matches"):
            database.execute(f"DELETE FROM {table}")
        database.commit()
    _seed(database)
    database.commit()
    yield database
    database.close()


def _section(db: Database, **kw) -> dict:
    return champion_investigation(db, KOG, WINDOW, **kw)["observed_boards"]


def _lineup(*mates: str) -> list[str]:
    return sorted([KOGMAW, *mates])


def _rows(section: dict, level: int) -> list[dict]:
    return next(g["boards"] for g in section["levels"] if g["level"] == level)


def _by_key(section: dict, level: int) -> dict[str, dict]:
    return {r["lineup_key"]: r for r in _rows(section, level)}


# ---------------------------------------------------------------- universe and denominators


def test_boards_use_the_canonical_carry_definition_and_window(db: Database) -> None:
    section = _section(db)
    stat, = carry_commitment_stats(db, balance_window=WINDOW, min_cost=1, max_cost=5, min_samples=1,
                                   character_ids=[KOGMAW])
    cores = champion_investigation(db, KOG, WINDOW)["cores"]
    # Same committed boards as the carry stats and the cores: tank-only Kog'Maw and the other window excluded.
    assert section["carry_boards"] == stat.commitment_games == cores["carry_boards"] == 14 + 11 + 10 + 9 + 12 + 10 + 10 + 10
    assert section["available"] is True and section["evidence"] == "observed"


def test_a_board_counts_once_with_two_committed_carry_copies(db: Database) -> None:
    boards, _names, _costs = committed_carry_boards(db, KOGMAW, WINDOW, roster=ROSTER)
    double = [b for b in boards if b.level == 7]
    assert len(double) == 10  # 10 boards, not 20
    assert all(b.lineup.count(KOGMAW) == 2 and b.carry_tier == 3 for b in double)
    level7 = _rows(_section(db), 7)
    assert [r["boards"] for r in level7] == [10]
    assert [u["character_id"] for u in level7[0]["units"]][:2] == [KOGMAW, KOGMAW]  # both copies shown, carry first


def test_denominator_is_same_level_committed_carry_boards(db: Database) -> None:
    section = _section(db)
    observed = {g["level"]: g["carry_boards_at_level"] for g in section["levels_observed"]}
    assert observed == {7: 10, 8: 14 + 11 + 10 + 9, 9: 12 + 10}  # never every board in the window
    a8 = _by_key(section, 8)["+".join(_lineup(*A))]
    assert a8["carry_boards_at_level"] == 44 and a8["share_of_level"] == pytest.approx(14 / 44)
    assert section["boards_with_level"] == 44 + 22 + 10 and section["carry_boards"] == 86
    assert section["level_coverage"] == pytest.approx(76 / 86)


def test_unreported_level_never_becomes_a_level_group(db: Database) -> None:
    section = _section(db)
    assert {g["level"] for g in section["levels_observed"]} == {7, 8, 9}
    assert 0 not in {g["level"] for g in section["levels"]}
    assert not valid_level(0) and not valid_level(None) and valid_level(1) and valid_level(MAX_PLAYER_LEVEL)


# ---------------------------------------------------------------- identity and grouping


def test_same_lineup_at_level_eight_and_nine_is_never_pooled(db: Database) -> None:
    section = _section(db)
    key = "+".join(_lineup(*A))
    a8, a9 = _by_key(section, 8)[key], _by_key(section, 9)[key]
    assert (a8["boards"], a8["top4_rate"], a8["avg_placement"]) == (14, 0.0, pytest.approx(89 / 14))
    assert (a9["boards"], a9["win_rate"], a9["avg_placement"]) == (12, 1.0, 1.0)
    # No row anywhere carries the pooled 26 boards or a blended placement.
    assert all(r["boards"] != 26 for g in section["levels"] for r in g["boards"])


def test_identical_lineups_group_and_different_lineups_stay_apart(db: Database) -> None:
    level8 = _by_key(_section(db), 8)
    assert set(level8) == {"+".join(_lineup(*A)), "+".join(_lineup(*B)), "+".join(_lineup(*A_DOUBLE))}


def test_duplicate_champions_are_part_of_the_identity(db: Database) -> None:
    level8 = _by_key(_section(db), 8)
    double = level8["+".join(_lineup(*A_DOUBLE))]
    assert double["unit_ids"].count(DRAVEN) == 2 and double["unit_count"] == 5 and double["boards"] == 10
    assert level8["+".join(_lineup(*A))]["boards"] == 14  # the single-Draven lineup is not merged into it
    assert [u["character_id"] for u in double["units"]].count(DRAVEN) == 2


def test_star_levels_do_not_change_identity() -> None:
    boards = [ObservedBoard(f"m{i}", 0, 4, 8, 2 if i % 2 else 3, tuple(sorted([KOGMAW, DRAVEN, KARMA])))
              for i in range(10)]
    evidence = group_boards(boards, min_boards=MIN_BOARDS)
    assert [lg.boards for g in evidence.levels for lg in g.lineups] == [10]
    # In the database: AD8 boards mix 1-star and 2-star teammates yet form one lineup of 10.
    assert MIN_BOARDS == 10


def test_carry_three_star_summary(db: Database) -> None:
    a8 = _by_key(_section(db), 8)["+".join(_lineup(*A))]
    assert a8["carry_three_star"] == {"boards": 4, "share": pytest.approx(4 / 14)}


def test_special_units_are_context_never_lineup_members(db: Database) -> None:
    b9 = _by_key(_section(db), 9)["+".join(_lineup(*B))]
    assert SUMMON not in b9["unit_ids"] and b9["unit_count"] == 4
    assert b9["special_units"] == [{"character_id": SUMMON, "name": b9["special_units"][0]["name"], "boards": 10}]
    assert _by_key(_section(db), 8)["+".join(_lineup(*B))]["special_units"] == []


def test_shop_identity_resolution_is_exact_or_uniquely_verified() -> None:
    assert GROMP == "DA_Gromp18_AP" and "DA_Gromp18_AD" not in ROSTER.champions
    # An alternate form the roster doesn't list resolves to the one shop champion with its normalized id.
    assert shop_champion_id(ROSTER, "DA_Gromp18_AD") == GROMP
    assert shop_champion_id(ROSTER, GROMP) == GROMP
    # Distinct shop forms keep their own exact ids (every Lux form is its own roster champion).
    lux_forms = [cid for cid in ROSTER.champions if ROSTER.is_shop_champion(cid) and "Lux" in cid]
    assert len(lux_forms) > 1 and all(shop_champion_id(ROSTER, cid) == cid for cid in lux_forms)
    # Known non-shop units and unmatched ids stay special.
    assert SUMMON in ROSTER.champions and shop_champion_id(ROSTER, SUMMON) is None
    assert shop_champion_id(ROSTER, "TFT18_NidaleeCougar") is None
    assert shop_champion_id(ROSTER, "") is None
    # Ambiguous: two shop champions share the normalized id -> no guess.
    fake = Roster(set_number=18, traits={}, champions={
        "DA_Foo18_AP": {"name": "Foo", "cost": 2, "traits": ["T1"]},
        "TFT18_Foo": {"name": "Foo", "cost": 3, "traits": ["T2"]},
    })
    assert shop_champion_id(fake, "DA_Foo18_AD") is None
    assert shop_champion_id(fake, "DA_Foo18_AP") == "DA_Foo18_AP"


def test_an_alternate_form_is_part_of_the_lineup_and_a_summon_is_not(tmp_path: Path) -> None:
    with Database(tmp_path / "forms.sqlite3") as db:
        for i in range(10):
            units = [make_unit(KOGMAW, tier=2, items=ITEMS), make_unit("DA_Gromp18_AD", tier=2),
                     make_unit(KARMA), make_unit(SUMMON)]
            db.ingest_match(make_match(f"F{i}", units=units, placement=3, level=8))
        section = _section(db)
    row, = _rows(section, 8)
    assert row["unit_ids"] == sorted([KOGMAW, GROMP, KARMA]) and row["unit_count"] == 3
    gromp = next(u for u in row["units"] if u["character_id"] == GROMP)
    assert gromp["name"] == "Gromp" and gromp["art_url"]
    assert [s["character_id"] for s in row["special_units"]] == [SUMMON]


# ---------------------------------------------------------------- promotion and ordering


def test_lineups_below_the_minimum_are_not_promoted(db: Database) -> None:
    section = _section(db)
    assert section["min_boards"] == ci.OBSERVED_BOARD_MIN_BOARDS == MIN_BOARDS == 10
    assert "+".join(_lineup(GROMP, KARMA)) not in _by_key(section, 8)  # 9 boards
    level8 = next(g for g in section["levels"] if g["level"] == 8)
    assert (level8["distinct_lineups"], level8["qualifying_lineups"]) == (4, 3)


def test_ordering_is_recurrence_first_never_placement(db: Database) -> None:
    level8 = _rows(_section(db), 8)
    # A (14 boards, all bottom four) stays ahead of B (11 boards, all wins).
    assert [r["boards"] for r in level8] == [14, 11, 10]
    assert level8[0]["lineup_key"] == "+".join(_lineup(*A)) and level8[0]["top4_rate"] == 0.0
    assert level8[1]["win_rate"] == 1.0


def test_ties_break_on_the_canonical_lineup() -> None:
    x, y = tuple(sorted([KOGMAW, VI, KARMA])), tuple(sorted([KOGMAW, DRAVEN, GROMP]))
    boards = [ObservedBoard(f"x{i}", 0, 8, 8, 2, x) for i in range(10)]
    boards += [ObservedBoard(f"y{i}", 0, 1, 8, 2, y) for i in range(10)]
    for order in (boards, list(reversed(boards))):
        lineups = [lg.lineup for g in group_boards(order, min_boards=10).levels for lg in g.lineups]
        assert lineups == sorted([x, y])


def test_levels_are_ascending_and_preview_takes_the_busiest_levels(db: Database) -> None:
    section = _section(db)
    assert [g["level"] for g in section["levels"]] == [7, 8, 9]
    # Preview: the top lineup of the two levels with the most carry boards (8: 44, 9: 22), by level.
    assert [(r["level"], r["boards"]) for r in section["preview"]] == [(8, 14), (9, 12)]


# ---------------------------------------------------------------- honest empty and unavailable states


def test_insufficient_evidence_is_an_honest_empty_state(tmp_path: Path) -> None:
    with Database(tmp_path / "thin.sqlite3") as db:
        for i in range(9):
            db.ingest_match(_board(f"T{i}", A, 1, 8))
        section = _section(db)
    assert section["available"] is True and section["levels"] == [] and section["preview"] == []
    assert section["levels_observed"] == [{"level": 8, "carry_boards_at_level": 9, "distinct_lineups": 1,
                                           "qualifying_lineups": 0}]


def test_no_reported_level_is_unavailable_not_level_zero(tmp_path: Path) -> None:
    with Database(tmp_path / "nolevel.sqlite3") as db:
        for i in range(12):
            db.ingest_match(_board(f"Z{i}", A, 3, 0))
        section = _section(db)
    assert section["available"] is False and section["levels"] == [] and section["boards_with_level"] == 0
    assert section["reason"] == ci.LEVEL_NOT_OBSERVED and section["carry_boards"] == 12


def test_no_carry_boards_means_no_board_section(db: Database) -> None:
    other = {"character_id": DRAVEN, "name": "Draven", "slug": "draven", "cost": 1, "art_url": None}
    assert champion_investigation(db, other, WINDOW)["observed_boards"] is None


# ---------------------------------------------------------------- API shape


def test_board_rows_carry_ui_refs_and_no_invented_positioning(db: Database) -> None:
    section = _section(db)
    row = _rows(section, 8)[0]
    assert set(row) >= {"level", "lineup_key", "unit_ids", "units", "boards", "carry_boards_at_level",
                        "share_of_level", "avg_placement", "top4_rate", "win_rate", "carry_three_star",
                        "evidence", "sample"}
    unit = row["units"][0]
    assert unit["is_carry"] is True and unit["character_id"] == KOGMAW
    assert set(unit) == {"character_id", "name", "cost", "slug", "art_url", "is_carry"}
    def keys(value):
        if isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from keys(v)
        elif isinstance(value, list):
            for v in value:
                yield from keys(v)

    assert not {k for k in keys(section) if any(p in k for p in ("position", "hex", "row", "col", "coord", "slot"))}
    text = json.dumps(section).lower()
    for absent in ("best", "optimal", "recommended", "score", "predicted"):
        assert absent not in text, absent
    assert "not cause" in section["definition"] and "player level" in section["ordering"]


def test_existing_investigation_fields_are_unchanged(db: Database) -> None:
    body = champion_investigation(db, KOG, WINDOW)
    for key in ("balance_window", "window", "evidence_type", "champion", "carry", "appearances", "window_average",
                "summary", "how_to_play", "items", "partners", "cores", "traits", "intrinsic_traits"):
        assert key in body, key
    assert body["cores"]["carry_boards"] == body["observed_boards"]["carry_boards"]
    assert body["how_to_play"] is not None and body["partners"]


def test_api_route_returns_observed_boards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "web.sqlite3"
    with Database(db_path) as database:
        _seed(database)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(db_path))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "demo.sqlite3"))
    from tftlab.webapp import create_app

    body = TestClient(create_app()).get("/api/champions/kogmaw", params={"balance_window": WINDOW}).json()
    assert body["observed_boards"]["available"] is True
    assert [g["level"] for g in body["observed_boards"]["levels"]] == [7, 8, 9]


# ---------------------------------------------------------------- public snapshots


def _snapshot(tmp_path: Path) -> Path:
    src = tmp_path / "source.sqlite3"
    build_live_ingest_like_source(src, 24)
    with Database.open_existing(src) as db:
        return ps.export_public_snapshot(db, tmp_path / "pub" / "public.sqlite3", prepare=False).path


def _make_old_style(path: Path) -> None:
    """What a snapshot exported before this change looks like: level 0 and
    no participant_level capability in its (otherwise identical) provenance."""
    conn = sqlite3.connect(path)
    prov = json.loads(conn.execute(f"SELECT value FROM {ps.METADATA_TABLE}").fetchone()[0])
    prov.pop(ps.PARTICIPANT_LEVEL_KEY)
    prov["sanitized_columns"]["participants"]["level"] = 0
    conn.execute(f"UPDATE {ps.METADATA_TABLE} SET value = ?", (json.dumps(prov, sort_keys=True),))
    conn.execute("UPDATE participants SET level = 0")
    conn.commit()
    conn.close()


def _top_carry(db: Database) -> dict:
    from tftlab.champion_investigation import champion_directory

    return max(champion_directory(db, "18.3"), key=lambda c: c["carry_games"])


def test_new_snapshots_preserve_level_and_say_so(tmp_path: Path) -> None:
    path = _snapshot(tmp_path)
    with Database.open_existing(path) as db:
        prov = ps.read_snapshot_provenance(db)
        assert prov[ps.PARTICIPANT_LEVEL_KEY] == "preserved" and prov["format_version"] == 1
        assert "level" not in prov["sanitized_columns"]["participants"]
        assert ps.snapshot_participant_level(db) == "preserved"
        levels = {r[0] for r in db.query_all("SELECT DISTINCT level FROM participants")}
        assert levels and levels <= set(range(1, 11))
        body = champion_investigation(db, _top_carry(db), "18.3")
        assert body["observed_boards"]["available"] is True
        assert body["observed_boards"]["boards_with_level"] == body["observed_boards"]["carry_boards"]
    checks = ps.verify_public_snapshot(path)["checks"]
    assert checks["participant_levels_plausible"] == "passed" and checks["no_augment_payloads"] == "passed"


def test_old_snapshots_still_load_and_report_boards_unavailable(tmp_path: Path, monkeypatch) -> None:
    path = _snapshot(tmp_path)
    with Database.open_existing(path) as db:
        before = champion_investigation(db, _top_carry(db), "18.3")
    _make_old_style(path)
    ps.verify_public_snapshot(path)  # still a valid, certified format-1 snapshot
    with Database.open_existing(path) as db:
        assert ps.snapshot_participant_level(db) == "sanitized"
        carry = _top_carry(db)
        after = champion_investigation(db, carry, "18.3")
    boards = after["observed_boards"]
    assert boards["available"] is False and boards["reason"] == ci.LEVEL_NOT_PRESERVED
    assert boards["levels"] == [] and boards["preview"] == [] and boards["levels_observed"] == []
    for key in ("carry", "items", "partners", "cores", "traits", "how_to_play"):
        assert after[key] == before[key], key  # everything else is untouched
    # The website serves it as before, with a 200 and the explanation.
    for key in ("DATABASE_URL", "RIOT_API_KEY", "TFT_STALE_AFTER_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TFT_DATA_SOURCE", "snapshot")
    monkeypatch.setenv("TFT_SNAPSHOT_PATH", str(path))
    monkeypatch.setenv("TFT_SNAPSHOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    client = TestClient(create_app())
    response = client.get(f"/api/champions/{carry['slug']}")
    assert response.status_code == 200
    assert response.json()["observed_boards"]["reason"].startswith("Final-board level context was not preserved")
    assert client.get("/api/source").json()["snapshot"]["participant_level"] is None


def test_snapshot_privacy_guarantees_hold_with_level_kept(tmp_path: Path) -> None:
    src = tmp_path / "source.sqlite3"
    payloads = build_live_ingest_like_source(src, 24)
    with Database.open_existing(src) as db:
        result = ps.export_public_snapshot(db, tmp_path / "pub" / "public.sqlite3", prepare=False)
    conn = sqlite3.connect(result.path)
    try:
        dump = "\n".join(conn.iterdump())
        assert conn.execute("SELECT DISTINCT augments_json FROM participants").fetchall() == [("[]",)]
        assert conn.execute("SELECT DISTINCT payload_json FROM matches").fetchall() == [("{}",)]
        columns = [c[1] for c in conn.execute("PRAGMA table_info(participants)")]
    finally:
        conn.close()
    assert columns == ["match_id", "participant_index", "placement", "level", "augments_json"]
    for payload in payloads:
        assert payload["metadata"]["match_id"] not in dump
        assert all(puuid not in dump for puuid in payload["metadata"]["participants"])


def test_implausible_levels_fail_verification(tmp_path: Path) -> None:
    path = _snapshot(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE participants SET level = 42 WHERE participant_index = 0")
    conn.commit()
    conn.close()
    with pytest.raises(ps.SnapshotExportError, match="participant_levels_plausible"):
        ps.verify_public_snapshot(path)


# ---------------------------------------------------------------- the page


def _js() -> str:
    return (Path(__file__).parents[1] / "src" / "tftlab" / "web" / "static" / "champion.js").read_text()


def test_page_has_a_boards_tab_in_the_documented_order() -> None:
    js = _js()
    tabs = js[js.index("const TABS = ["):js.index("];", js.index("const TABS = ["))]
    labels = [line.split("'")[3] for line in tabs.splitlines() if line.strip().startswith("['")]
    assert labels == ["How to play", "Boards", "Items", "Teammates", "Traits", "Evidence"]
    assert "panel('boards', active, boardsSection(inv))" in js
    assert 'data-tab="boards"' in js  # How to play links to the full tab


def test_page_labels_boards_as_observed_and_never_as_strength() -> None:
    js = _js()
    section = js[js.index("// ------------------------------------------------------------------ observed final boards"):
                 js.index("// ------------------------------------------------------------------ tabs")]
    for word in ("best", "optimal", "s-tier", "winning", "recommend", "strong", "support"):
        assert word not in section.lower(), word
    assert "ordered by how often they appeared, not by placement" in section
    assert "stamp('observed', 'Observed')" in section and "Level ${g.level}" in section
    assert "ob.reason" in section  # the unavailable state shows the API's own explanation
