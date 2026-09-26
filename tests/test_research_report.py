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
    assert change["lost_unit_instances"] == 12 and change["gained_unit_instances"] == 0
    package = change["top_lost_packages"][0]
    assert package["items"] == [VISAGE, STEADFAST] and package["unit_instances"] == 12  # sorted ids
    assert package["current_intents"] == {STEADFAST: "known_unlisted", VISAGE: "tank"}
    assert package["pr21_defensive"] == {STEADFAST: False, VISAGE: True}  # PR #21 read Steadfast as offensive


def test_dataset_summary_counts_and_integrity(store: Path) -> None:
    with Database.open_existing(store) as db:
        d = build_report(db, baseline=False)["dataset"]
    assert (d["window_matches"], d["window_participants"]) == (29, 29)
    assert (d["window_unit_observable_participants"], d["window_source_empty_participants"]) == (28, 1)
    assert d["store_duplicate_match_ids"] == 0 and d["window_malformed_placements"] == 0
    assert d["store_matches_without_balance_window"] == 0
    assert d["window_matches_not_8_distinct_placements"] == 29  # single-participant fixtures: flagged, as intended
    assert sum(d["store_balance_window_distribution"].values()) == d["store_matches"] == 29


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
