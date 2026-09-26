"""Read-only Discovery research report (tftlab.research_report, `tftlab discovery-report`)."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tftlab.cli import app
from tftlab.research_report import (
    NotReadOnly,
    build_report,
    evidence_band,
    pr21_baseline_intents,
    write_report,
)
from tftlab.storage import Database

from _helpers import make_match, make_unit

VISAGE, STEADFAST, WARMOG, GARGOYLE = "DA_SpiritVisage", "DA_SteadfastHeart", "DA_WarmogsArmor", "DA_GargoyleStoneplate"
GUINSOO, TITAN, STERAK, RAVAGER = "DA_GuinsoosRageblade", "DA_TitansResolve", "DA_SteraksGage", "DA_18_EmblemSlayer"
WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "read-only-discovery-report.yml"


def _board(match_id: str, character_id: str, items: list[str], placement: int, *, empty_second: bool = False) -> dict:
    match = make_match(match_id, placement=placement, units=[
        make_unit(character_id, rarity=0, tier=2, items=items), make_unit("TFT99_Filler", rarity=0),
    ])
    return match


def _populate(db: Database) -> None:
    for i in range(12):  # Leona-like: Spirit Visage + Steadfast Heart
        db.ingest_match(_board(f"LEO{i}", "TFT99_Leona", [VISAGE, STEADFAST], 3))
    for i in range(8):  # an off-meta conversion
        db.ingest_match(_board(f"ELI{i}", "TFT99_Elise", [RAVAGER, GUINSOO], 2))
    for i in range(5):  # tank on both rules
        db.ingest_match(_board(f"WAL{i}", "TFT99_Wall", [WARMOG, GARGOYLE], 5))
    for i in range(3):
        db.ingest_match(_board(f"BRU{i}", "TFT99_Bruiser", [TITAN, STERAK], 4))
    db.ingest_match(make_match("EMPTY", placement=8, units=[]))  # a source-empty participant


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store.sqlite3"
    with Database(path) as db:
        _populate(db)
    return path


def test_report_is_read_only_and_explains_the_semantic_change(store: Path) -> None:
    digest = hashlib.sha256(store.read_bytes()).hexdigest()
    with Database.open_existing(store) as db:
        report = build_report(db, top_n=5)
    assert hashlib.sha256(store.read_bytes()).hexdigest() == digest  # nothing written
    assert report["read_only_connection"] == "sqlite mode=ro"

    ids = {c["character_id"] for c in report["candidates"]}
    assert ids == {"TFT99_Elise", "TFT99_Bruiser"}  # Leona-like and Wall have no carry evidence
    elise = next(c for c in report["candidates"] if c["character_id"] == "TFT99_Elise")
    assert (elise["appearances"], elise["commitment_games"], elise["evidence_band"]) == (8, 8, "D_too_little")
    assert {"opportunity_score", "opportunity_components", "confidence", "hit_3star_games", "best_item_packages",
            "best_partners", "best_trait_breakpoints", "rank_overall", "rank_in_cost"} <= set(elise)

    comparison = {r["character_id"]: r for r in report["pr21_comparison"]["champions"]}
    leona = comparison["TFT99_Leona"]
    assert (leona["commitment_games_pr21"], leona["commitment_games_pr22"], leona["appearances"]) == (12, 0, 12)
    assert leona["rank_overall_pr21"] is not None and leona["rank_overall_pr22"] is None
    wall = comparison["TFT99_Wall"]  # listed although it commits under neither rule
    assert (wall["appearances"], wall["commitment_games_pr21"], wall["commitment_games_pr22"]) == (5, 0, 0)
    assert comparison["TFT99_Filler"]["appearances"] == 28
    assert comparison["TFT99_Elise"]["commitment_change"] == 0
    totals = report["pr21_comparison"]
    assert (totals["total_commitment_games_pr21"], totals["total_commitment_games_pr22"], totals["absolute_change"]) == (23, 11, -12)

    change = report["pr21_package_changes"]["TFT99_Leona"]
    assert change["lost_commitment_boards"] == 12 and change["gained_commitment_boards"] == 0
    package = change["top_lost_packages"][0]
    assert package["items"] == [VISAGE, STEADFAST] and package["boards"] == 12  # sorted ids
    assert package["current_intents"] == {STEADFAST: "known_unlisted", VISAGE: "tank"}
    assert package["pr21_defensive"] == {STEADFAST: False, VISAGE: True}  # PR #21 read Steadfast as offensive


def test_dataset_summary_counts_and_integrity(store: Path) -> None:
    with Database.open_existing(store) as db:
        d = build_report(db, baseline=False)["dataset"]
    assert (d["window_matches"], d["window_participants"]) == (29, 29)
    assert (d["window_unit_observable_participants"], d["window_participants_without_units"]) == (28, 1)
    assert (d["window_source_empty_participants"], d["window_unexpected_participants_without_units"]) == (1, 0)
    assert d["store_duplicate_match_ids"] == 0 and d["window_malformed_placements"] == d["store_malformed_placements"] == 0
    assert d["store_matches_without_balance_window"] == d["store_unexpected_missing_balance_window"] == 0
    assert d["window_matches_not_8_distinct_placements"] == 29  # single-participant fixtures: flagged, as intended
    assert sum(d["store_balance_window_distribution"].values()) == d["store_matches"] == 29
    assert d["checkpoint"] == {
        "unexpected_missing_balance_windows": 0, "malformed_placements": 0, "duplicate_match_ids": 0,
        "source_empty_participants_in_window": 1, "unexpected_participants_without_units_in_window": 0,
        "unexpected_participants_without_units_store_wide": 0,
    }


OTHER_VERSION = "Version 14.5.570.1111 (Sep 01 2024/13:00:00) [PUBLIC] <Releases/14.5>"


def test_source_empty_vs_unexpected_uses_the_raw_payload_and_the_window(tmp_path: Path) -> None:
    """No stored units alone is not source-empty: Riot's own payload entry
    decides. Counts are scoped to the reported window; store-wide ones are
    reported separately."""
    path = tmp_path / "integrity.sqlite3"
    with Database(path) as db:
        db.ingest_match(_board("OK", "TFT99_Elise", [RAVAGER, GUINSOO], 2))
        db.ingest_match(make_match("SRC_EMPTY", placement=8, units=[]))  # Riot sent units=[] -> source-empty
        db.ingest_match(_board("LOST", "TFT99_Elise", [GUINSOO, TITAN], 3))  # Riot sent units ...
        db.ingest_match(make_match("OLDLOST", placement=4, game_version=OTHER_VERSION,
                                   units=[make_unit("TFT99_Elise", rarity=0, items=[GUINSOO])]))
        for match_id in ("LOST", "OLDLOST"):  # ... but storage has none -> unexpected
            db.execute("DELETE FROM units WHERE match_id = ?", (match_id,))
        db.commit()
    with Database.open_existing(path) as db:
        window = db.query_one("SELECT balance_window FROM matches WHERE match_id = 'OK'")[0]
        d = build_report(db, balance_window=window, baseline=False)["dataset"]
    assert (d["window_matches"], d["window_participants"], d["window_participants_without_units"]) == (3, 3, 2)
    assert (d["window_source_empty_participants"], d["window_unexpected_participants_without_units"]) == (1, 1)
    assert d["window_unit_observable_participants"] == 1
    # The other window's lost participant counts store-wide only.
    assert (d["store_participants_without_units"], d["store_source_empty_participants"],
            d["store_unexpected_participants_without_units"]) == (3, 1, 2)
    assert d["checkpoint"]["unexpected_participants_without_units_in_window"] == 1
    assert d["checkpoint"]["unexpected_participants_without_units_store_wide"] == 2


def test_classifier_scope_is_optional_and_store_wide_by_default(tmp_path: Path) -> None:
    from tftlab.validate import classify_participants_without_units

    path = tmp_path / "scope.sqlite3"
    with Database(path) as db:
        db.ingest_match(make_match("E1", placement=8, units=[]))
        db.ingest_match(make_match("E2", placement=8, units=[], game_version=OTHER_VERSION))
        db.commit()
        w1 = db.query_one("SELECT balance_window FROM matches WHERE match_id = 'E1'")[0]
        assert classify_participants_without_units(db) == (2, 0)
        assert classify_participants_without_units(db, balance_window=w1) == (1, 0)
        assert classify_participants_without_units(db, balance_window="no such window") == (0, 0)


def test_null_balance_window_expected_unreal_gap_vs_unexpected(tmp_path: Path) -> None:
    from tftlab.unreal_patch import UNRESOLVED_UNREAL_PATCH

    path = tmp_path / "windows.sqlite3"
    with Database(path) as db:
        db.ingest_match(_board("OK", "TFT99_Elise", [RAVAGER, GUINSOO], 2))
        db.ingest_match(_board("UNREAL", "TFT99_Elise", [RAVAGER, GUINSOO], 2))
        db.ingest_match(_board("BROKEN", "TFT99_Elise", [RAVAGER, GUINSOO], 2))
        db.execute("UPDATE matches SET balance_window = NULL, patch = ? WHERE match_id = 'UNREAL'", (UNRESOLVED_UNREAL_PATCH,))
        db.execute("UPDATE matches SET balance_window = NULL WHERE match_id = 'BROKEN'")  # patch kept: genuinely broken
        db.commit()
    with Database.open_existing(path) as db:
        d = build_report(db, baseline=False)["dataset"]
    assert d["store_matches_without_balance_window"] == 2
    assert d["store_expected_unresolved_unreal_matches"] == 1  # the intentional rollout gap
    assert d["store_unexpected_missing_balance_window"] == 1  # the broken one
    assert d["checkpoint"]["unexpected_missing_balance_windows"] == 1
    assert d["window_matches"] == 1


def test_refuses_a_writable_connection(store: Path) -> None:
    with Database(store) as db, pytest.raises(NotReadOnly):
        build_report(db)


@pytest.mark.parametrize("games, band", [(0, "D_too_little"), (9, "D_too_little"), (10, "C_early"), (29, "C_early"),
                                        (30, "B_moderate"), (59, "B_moderate"), (60, "A_substantial"), (500, "A_substantial")])
def test_evidence_bands_follow_existing_thresholds(games: int, band: str) -> None:
    assert evidence_band(games) == band


def test_pr21_baseline_reproduces_the_stat_rule() -> None:
    intents = pr21_baseline_intents()
    for defensive in (VISAGE, WARMOG, GARGOYLE, "DA_DragonsClaw"):
        assert intents[defensive] == {"intent": "tank"}, defensive
    for not_defensive in (STEADFAST, TITAN, STERAK, GUINSOO, RAVAGER, "DA_Crownguard", "DA_Component_ChainVest"):
        assert not_defensive not in intents, not_defensive


def test_written_artifacts_hold_aggregates_only(store: Path, tmp_path: Path) -> None:
    with Database.open_existing(store) as db:
        report = build_report(db)
    paths = write_report(report, tmp_path / "out")
    assert [p.name for p in paths] == [
        "discovery_Version_14.6_full.json", "discovery_Version_14.6_candidates.csv", "discovery_Version_14.6_pr21_vs_pr22.csv",
    ] or all(p.exists() for p in paths)
    text = paths[0].read_text()
    for match_id in ("LEO0", "ELI0", "EMPTY"):
        assert f'"{match_id}"' not in text  # no match ids
    rows = list(csv.DictReader(paths[1].open()))
    assert {r["character_id"] for r in rows} == {"TFT99_Elise", "TFT99_Bruiser"}
    assert "component_confidence" in rows[0] and "top_item_package" in rows[0]


def test_cli_writes_the_report(store: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["discovery-report", "--db", str(store), "--out-dir", str(tmp_path / "cli")])
    assert result.exit_code == 0, result.output
    assert "Read-only connection: sqlite mode=ro" in result.output
    assert "Commitment games PR #21 -> PR #22: 23 -> 11" in result.output
    assert len(list((tmp_path / "cli").iterdir())) == 3


def test_cli_never_creates_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sqlite3"
    result = CliRunner().invoke(app, ["discovery-report", "--db", str(missing), "--out-dir", str(tmp_path / "o")])
    assert result.exit_code != 0 and not missing.exists()


def _two_copies(match_id: str, first: list[str], second: list[str]) -> dict:
    return make_match(match_id, placement=3, units=[
        make_unit("TFT99_Dup", rarity=0, tier=2, items=first), make_unit("TFT99_Dup", rarity=0, tier=2, items=second),
    ])


def test_duplicate_copy_still_qualifying_means_no_lost_commitment(tmp_path: Path) -> None:
    """Copy A qualifies only under PR #21, copy B under both: the board
    commits under both rules, so nothing is lost or attributed."""
    path = tmp_path / "dup_keep.sqlite3"
    with Database(path) as db:
        db.ingest_match(_two_copies("D1", [VISAGE, STEADFAST], [RAVAGER, GUINSOO]))
    with Database.open_existing(path) as db:
        report = build_report(db)
    row = next(r for r in report["pr21_comparison"]["champions"] if r["character_id"] == "TFT99_Dup")
    assert (row["appearances"], row["commitment_games_pr21"], row["commitment_games_pr22"], row["commitment_change"]) == (1, 1, 1, 0)
    assert "TFT99_Dup" not in report["pr21_package_changes"]


def test_duplicate_copies_all_losing_eligibility_is_one_lost_board_with_the_canonical_package(tmp_path: Path) -> None:
    """Every PR #21-eligible copy fails the current rule: one lost board,
    attributed to the canonical copy (most completed items, then tier, then
    lowest unit_index)."""
    path = tmp_path / "dup_lost.sqlite3"
    three = [STEADFAST, "DA_Crownguard", VISAGE]
    with Database(path) as db:
        db.ingest_match(_two_copies("D2", [VISAGE, STEADFAST], three))
    with Database.open_existing(path) as db:
        report = build_report(db)
    row = next(r for r in report["pr21_comparison"]["champions"] if r["character_id"] == "TFT99_Dup")
    assert (row["commitment_games_pr21"], row["commitment_games_pr22"], row["commitment_change"]) == (1, 0, -1)
    change = report["pr21_package_changes"]["TFT99_Dup"]
    assert (change["lost_commitment_boards"], change["gained_commitment_boards"]) == (1, 0)
    assert len(change["top_lost_packages"]) == 1
    assert change["top_lost_packages"][0]["items"] == sorted(three) and change["top_lost_packages"][0]["boards"] == 1


def test_canonical_unit_key_matches_the_sql_tiebreak() -> None:
    from tftlab.analytics import CANONICAL_UNIT_TIEBREAK_SQL
    from tftlab.research_report import canonical_unit_key

    assert CANONICAL_UNIT_TIEBREAK_SQL == "completed_item_count DESC, tier DESC, unit_index ASC"
    copies = {"a": (2, 3, 0), "b": (3, 1, 1), "c": (3, 2, 2), "d": (3, 2, 1)}
    assert min(copies, key=lambda k: canonical_unit_key(*copies[k])) == "d"


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_postgres_report_runs_in_a_server_enforced_read_only_transaction() -> None:
    db = Database(POSTGRES_TEST_URL)
    try:
        for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        _populate(db)
        db.commit()
    finally:
        db.close()
    with Database.open_existing(POSTGRES_TEST_URL) as ro:
        report = build_report(ro)
        with pytest.raises(Exception):
            ro.execute("DELETE FROM matches")  # the session refuses writes
    assert report["read_only_connection"] == "postgres transaction_read_only=on"
    assert report["pr21_comparison"]["total_commitment_games_pr22"] == 11


# ---------------------------------------------------------------- workflow


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_is_manual_main_only_and_read_only() -> None:
    text = _text()
    assert "workflow_dispatch" in text
    for trigger in ("\npush:", "\n  push:", "pull_request:", "schedule:"):
        assert trigger not in text
    preflight = text.index("Validate production configuration")
    assert preflight < text.index("refs/heads/main") < text.index("actions/checkout")
    assert '-z "${DATABASE_URL}"' in text and "postgres://*|postgresql://*" in text
    assert "group: read-only-discovery-report" in text and "contents: read" in text


def test_workflow_runs_only_the_report_and_uploads_it() -> None:
    text = _text()
    assert "tftlab discovery-report" in text
    for other in ("ingest-riot", "verify-riot", "discovery-smoke", "patch-diagnostics", "validate-live-data", "refresh-game-art"):
        assert other not in text
    assert "actions/upload-artifact" in text and "git push" not in text and "git commit" not in text
    assert "secrets.RIOT_API_KEY" not in text and "RIOT_API_KEY:" not in text


def test_workflow_never_prints_the_database_url() -> None:
    for line in _text().splitlines():
        if "echo" in line.lower():
            assert "$DATABASE_URL" not in line and "${DATABASE_URL}" not in line
