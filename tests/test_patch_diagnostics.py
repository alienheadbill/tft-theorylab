"""CLI-level tests for `tftlab patch-diagnostics` -- a read-only, non-
ingesting diagnostic command meant to let an operator read off the real
game_datetime range for masked Unreal-era matches without running another
Riot ingest. See requirement: no Riot calls, no CommunityDragon calls, no
payload dumps, no deletion, no analytics/model changes.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tftlab.cli import app
from tftlab.storage import Database

from _helpers import make_match, make_unit


def test_patch_diagnostics_requires_no_riot_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike ingest-riot/verify-riot, this command must work with no
    RIOT_API_KEY configured at all -- it never talks to Riot."""
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    db_path = tmp_path / "diag.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(make_match("M1", units=[make_unit("TFT14_Foo", tier=2, items=[])]))

    result = CliRunner().invoke(app, ["patch-diagnostics", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "RIOT_API_KEY" not in result.output


def test_patch_diagnostics_reports_masked_unreal_range_without_ingesting(tmp_path: Path) -> None:
    db_path = tmp_path / "diag_unreal.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(
            make_match(
                "UNREAL_1",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=1_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        db.ingest_match(
            make_match(
                "UNREAL_2",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=5_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        db.ingest_match(
            make_match(
                "NORMAL_1",
                game_version="Version 14.6.1 (Sep 10 2024) [PUBLIC] <Releases/14.6>",
                units=[make_unit("TFT14_Foo", tier=2, items=[])],
            )
        )

    result = CliRunner().invoke(app, ["patch-diagnostics", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "Total matches (store-wide): 3" in result.output
    assert "Masked-Unreal (unresolved) matches: 2" in result.output
    assert "1000" in result.output  # earliest masked-Unreal game_datetime
    assert "5000" in result.output  # latest masked-Unreal game_datetime
    assert "'TFT Unreal Version ?.?.?.?': 2" in result.output
    # The real registry now has 2 usable (verified + sourced) windows
    # (18.2, 18.3); these fabricated game_datetimes (1_000/5_000) are
    # long before either, so they correctly stay unresolved regardless.
    assert "2/2 window(s) usable" in result.output


def test_patch_diagnostics_never_dumps_payloads(tmp_path: Path) -> None:
    db_path = tmp_path / "diag_payload.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(
            make_match(
                "M1",
                units=[make_unit("TFT14_Foo", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )

    result = CliRunner().invoke(app, ["patch-diagnostics", "--db", str(db_path)])

    assert result.exit_code == 0
    # A full payload dump would include "metadata" / "participants" keys.
    assert '"participants"' not in result.output
    assert '"metadata"' not in result.output


def test_patch_diagnostics_never_deletes_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "diag_no_delete.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(
            make_match(
                "UNREAL_1",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=1_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )

    CliRunner().invoke(app, ["patch-diagnostics", "--db", str(db_path)])

    with Database(db_path) as db:
        remaining = db.query_one("SELECT COUNT(*) FROM matches")[0]
    assert remaining == 1
