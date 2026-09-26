"""Component recognition (legacy TFT_Item_* + the current set's snapshot-tagged
components such as Set 18's DA_Component_*) and the one-time backfill that
corrects stored units.completed_item_count."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path

import pytest

from tftlab.analytics import carry_commitment_stats, item_package_stats
from tftlab.carry import is_carry_observation
from tftlab.demo import generate_demo_matches
from tftlab.items import ITEM_STATS_PATH, component_ids, component_ids_version, completed_item_count, is_component
from tftlab.storage import Database

from _helpers import make_match, make_unit

DA_COMPONENTS = {
    "DA_Component_BFSword", "DA_Component_ChainVest", "DA_Component_FryingPan", "DA_Component_GiantsBelt",
    "DA_Component_NeedlesslyLargeRod", "DA_Component_NegatronCloak", "DA_Component_RecurveBow",
    "DA_Component_SparringGloves", "DA_Component_Spatula", "DA_Component_TearOfTheGoddess",
}
LEGACY = {
    "TFT_Item_BFSword", "TFT_Item_ChainVest", "TFT_Item_GiantsBelt", "TFT_Item_NeedlesslyLargeRod",
    "TFT_Item_NegatronCloak", "TFT_Item_RecurveBow", "TFT_Item_SparringGloves", "TFT_Item_Spatula",
    "TFT_Item_TearOfTheGoddess", "TFT_Item_FryingPan", "TFT_Item_EmptyBag",
}
WARMOG, GARGOYLE, GUINSOO, TITAN = "DA_WarmogsArmor", "DA_GargoyleStoneplate", "DA_GuinsoosRageblade", "DA_TitansResolve"

CASES = [
    (["DA_Component_BFSword", "DA_Component_RecurveBow"], 0, False),
    ([WARMOG, "DA_Component_ChainVest"], 1, False),
    ([WARMOG, GARGOYLE, "DA_Component_ChainVest"], 2, False),  # both completed items defensive
    ([GUINSOO, "DA_Component_RecurveBow"], 1, False),
    ([GUINSOO, TITAN, "DA_Component_RecurveBow"], 2, True),
    (["TFT_Item_BFSword", "TFT_Item_RecurveBow"], 0, False),  # legacy components unchanged
    (["TFT_Item_InfinityEdge", "TFT_Item_LastWhisper", "TFT_Item_ChainVest"], 2, True),
    (["DA_BrandNewThing", WARMOG], 2, True),  # unknown non-component: still a completed/special item
]


def test_recognized_component_ids_are_legacy_plus_set18_da_components() -> None:
    assert component_ids() == frozenset(LEGACY | DA_COMPONENTS)
    assert not is_component("DA_GargoyleStoneplate") and not is_component("DA_18_EmblemSlayer")
    assert not is_component("DA_Component_NotInTheSnapshot")  # metadata decides, not the prefix


def test_every_snapshot_component_is_recognized() -> None:
    """Guard: a future set's component namespace, once tagged "component" in
    the committed snapshot, can never be counted as completed equipment."""
    items = json.loads(ITEM_STATS_PATH.read_text())["items"]
    tagged = {i for i, m in items.items() if "component" in (m.get("tags") or [])}
    assert DA_COMPONENTS <= tagged
    assert all(is_component(i) for i in tagged)


@pytest.mark.parametrize("items, completed, carry", CASES)
def test_corrected_completed_item_count_and_carry(items, completed, carry) -> None:
    assert completed_item_count(items) == completed
    assert is_carry_observation(items) is carry


def _board(match_id: str, character_id: str, items: list[str], placement: int = 3) -> dict:
    return make_match(match_id, placement=placement, units=[make_unit(character_id, rarity=1, items=items)])


def test_new_ingest_stores_corrected_counts_and_analytics_follow(tmp_path: Path) -> None:
    with Database(tmp_path / "ingest.sqlite3") as db:
        for i, (items, _, _) in enumerate(CASES):
            db.ingest_match(_board(f"C{i}", f"TFT99_U{i}", items))
        stored = dict(db.query_all("SELECT character_id, completed_item_count FROM units"))
        committed = {s.character_id for s in carry_commitment_stats(db, min_samples=1, max_cost=5)}
    assert stored == {f"TFT99_U{i}": completed for i, (_, completed, _) in enumerate(CASES)}
    assert committed == {f"TFT99_U{i}" for i, (_, _, carry) in enumerate(CASES) if carry}


def test_components_never_appear_in_item_package_evidence(tmp_path: Path) -> None:
    with Database(tmp_path / "pkg.sqlite3") as db:
        for i in range(3):
            db.ingest_match(_board(f"P{i}", "TFT99_Carry", [GUINSOO, TITAN, "DA_Component_RecurveBow"], placement=2))
            db.ingest_match(_board(f"Q{i}", "TFT99_Carry", [GUINSOO, "DA_Deathblade", "DA_Component_ChainVest"], placement=4))
        window = db.query_one("SELECT balance_window FROM matches LIMIT 1")[0]
        packages = item_package_stats(db, "TFT99_Carry", window, min_pair_games=1, min_package_games=1)
    keys = {a.key for group in packages.values() for a in group}
    assert keys and not any("Component" in k for k in keys)
    assert GUINSOO in keys


# ---------------------------------------------------------------- the backfill migration


def _stale_database(db: Database) -> dict:
    """Simulate rows written with the OLD component logic: DA_ components
    counted as completed items, and no migration marker yet."""
    payloads = [
        _board("OLD_TANK", "TFT99_Tank", [WARMOG, GARGOYLE, "DA_Component_ChainVest"], placement=6),
        _board("OLD_BENCH", "TFT99_Bench", ["DA_Component_BFSword", "DA_Component_RecurveBow"], placement=5),
        _board("OLD_CARRY", "TFT99_Carry", [GUINSOO, TITAN, "DA_Component_RecurveBow"], placement=2),
        _board("OLD_HALF", "TFT99_Half", [GUINSOO, "DA_Component_RecurveBow"], placement=4),
        copy.deepcopy(generate_demo_matches(1, seed=5)[0]),  # untouched legacy rows
    ]
    for payload in payloads:
        db.ingest_match(payload)
    old_counts = {"TFT99_Tank": 3, "TFT99_Bench": 2, "TFT99_Carry": 3, "TFT99_Half": 2}
    for character_id, count in old_counts.items():
        db.execute("UPDATE units SET completed_item_count = ? WHERE character_id = ?", (count, character_id))
    db.execute("DELETE FROM schema_migrations")
    db.commit()
    return old_counts


def _snapshot(db: Database) -> tuple:
    units = db.query_all(
        "SELECT match_id, participant_index, unit_index, character_id, unit_name, cost, tier, items_json, completed_item_count "
        "FROM units ORDER BY match_id, participant_index, unit_index"
    )
    participants = db.query_all("SELECT * FROM participants ORDER BY match_id, participant_index")
    matches = db.query_all("SELECT match_id, game_datetime, patch, balance_window, payload_json FROM matches ORDER BY match_id")
    return units, participants, matches


def _check_backfill(open_db, reopen_initializing, open_read_only) -> None:
    db = open_db()
    try:
        old_counts = _stale_database(db)
        stale_units, participants, matches = _snapshot(db)
        stale_committed = {s.character_id for s in carry_commitment_stats(db, min_samples=1, max_cost=5)}
    finally:
        db.close()
    assert {"TFT99_Tank", "TFT99_Bench", "TFT99_Carry", "TFT99_Half"} <= stale_committed  # the inflated state

    # The read-only web path never migrates.
    ro = open_read_only()
    try:
        assert ro.completed_item_count_backfill is None
        assert _snapshot(ro)[0] == stale_units
        assert ro.query_one("SELECT COUNT(*) FROM schema_migrations")[0] == 0
    finally:
        ro.close()

    fixed = reopen_initializing()
    try:
        assert fixed.completed_item_count_backfill == 4
        units, participants_after, matches_after = _snapshot(fixed)
        committed = {s.character_id for s in carry_commitment_stats(fixed, min_samples=1, max_cost=5)}
        marker = fixed.query_one("SELECT migration_key, rows_changed FROM schema_migrations")
    finally:
        fixed.close()
    assert (participants_after, matches_after) == (participants, matches)  # otherwise unchanged
    before = {(u[0], u[1], u[2]): u for u in stale_units}
    corrected = {"TFT99_Tank": 2, "TFT99_Bench": 0, "TFT99_Carry": 2, "TFT99_Half": 1}
    for u in units:
        old = before[(u[0], u[1], u[2])]
        assert u[:8] == old[:8]  # items_json and every other column unchanged
        assert u[8] == corrected.get(u[3], old[8])
    assert committed & set(old_counts) == {"TFT99_Carry"}  # analytics now use the corrected counts
    assert marker == (f"completed_item_count:{component_ids_version()}", 4)

    again = reopen_initializing()  # idempotent: marker present, nothing rescanned or changed
    try:
        assert again.completed_item_count_backfill is None
        assert _snapshot(again)[0] == units
    finally:
        again.close()


def test_backfill_corrects_stale_rows_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "stale.sqlite3"
    _check_backfill(lambda: Database(path), lambda: Database(path), lambda: Database.open_existing(path))


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_backfill_corrects_stale_rows_on_postgres() -> None:
    def fresh() -> Database:
        db = Database(POSTGRES_TEST_URL)
        for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        return db

    _check_backfill(fresh, lambda: Database(POSTGRES_TEST_URL), lambda: Database.open_existing(POSTGRES_TEST_URL))


def test_backfill_leaves_unreadable_items_json_alone(tmp_path: Path) -> None:
    path = tmp_path / "odd.sqlite3"
    with Database(path) as db:
        db.ingest_match(_board("ODD", "TFT99_Odd", [WARMOG, "DA_Component_ChainVest"]))
        db.execute("UPDATE units SET items_json = 'not json', completed_item_count = 7")
        db.execute("DELETE FROM schema_migrations")
        db.commit()
    with Database(path) as db:
        assert db.completed_item_count_backfill == 0
        assert db.query_one("SELECT completed_item_count FROM units")[0] == 7


def test_ingest_report_shows_the_backfill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from test_db_concurrency import _cli
    from test_ingest_riot import _StubRiotClient

    path = tmp_path / "cli.sqlite3"
    with Database(path) as db:
        _stale_database(db)
    client = _StubRiotClient(puuids=["p0"], match_ids_by_puuid={}, matches={})
    result = _cli(monkeypatch, tmp_path, client, ["--challenger-seeds", "1"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Stored completed-item counts corrected on connect (component recognition): 4 unit rows" in text
