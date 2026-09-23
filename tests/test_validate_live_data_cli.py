"""CLI-level tests for `tftlab validate-live-data`'s reporting of
intentionally-unresolved Unreal-era matches vs. genuinely unexpected
missing balance windows (see IntegrityReport.unexpected_missing_balance_window).
"""

from pathlib import Path

from typer.testing import CliRunner

from tftlab.cli import app
from tftlab.storage import Database

from _helpers import make_match, make_unit


def test_production_shaped_db_with_only_intentional_unresolved_rows_is_healthy(
    tmp_path: Path,
) -> None:
    """Mirrors the exact reported production state: some matches resolved
    to 18.2, the rest sitting in the Unreal registry's deliberate rollout
    gap. This must print both counts clearly and end healthy -- a
    production database in this exact shape must never fail CI/validation
    just because the gap rows exist."""
    db_path = tmp_path / "prod_shaped.sqlite3"
    with Database(db_path) as db:
        for i in range(10):
            db.ingest_match(
                make_match(
                    f"RESOLVED_{i}",
                    game_version="TFT Unreal Version ?.?.?.?",
                    game_datetime=1_789_681_679_589 + i * 1000,  # inside the real 18.2 window
                    units=[make_unit("TFT18_Foo", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )
        for i in range(37):
            db.ingest_match(
                make_match(
                    f"GAP_{i}",
                    game_version="TFT Unreal Version ?.?.?.?",
                    game_datetime=1_790_035_223_978 + i * 1000,  # inside the deliberate gap
                    units=[make_unit("TFT18_Foo", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )

    result = CliRunner().invoke(app, ["validate-live-data", "--db", str(db_path), "--no-check-metadata"])

    assert result.exit_code == 0
    assert "Matches missing balance_window: 37" in result.output
    assert "Expected Unreal rollout-gap unresolved: 37" in result.output
    assert "Unexpected missing balance_window: 0" in result.output
    assert "intentional and safe" in result.output
    assert "No severe integrity issues detected." in result.output


def test_unexpected_missing_balance_window_still_fails_validation(tmp_path: Path) -> None:
    """A genuinely broken row (no game_version at all) alongside intentional
    Unreal gap rows must still fail validation -- the two counts must not
    be conflated."""
    db_path = tmp_path / "mixed_broken.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(
            make_match(
                "GAP_0",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=1_790_035_223_978,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        db.ingest_match(
            make_match("BROKEN_0", game_version=None, units=[make_unit("TFT14_Foo", tier=2, items=[])])
        )

    result = CliRunner().invoke(
        app, ["validate-live-data", "--db", str(db_path), "--balance-window", "doesnt-matter", "--no-check-metadata"]
    )

    assert result.exit_code == 1
    assert "Matches missing balance_window: 2" in result.output
    assert "Expected Unreal rollout-gap unresolved: 1" in result.output
    assert "Unexpected missing balance_window: 1" in result.output
    assert "SEVERE integrity issues detected." in result.output
