"""Source-empty participants: validation semantics and analytics denominators.

A participant Riot itself sent with no units (the production case:
NA1_5648101147, participant 1, placement 2, `units: []`) is faithfully
stored and must warn, not fail validation; a participant whose units were
lost between Riot and storage must still fail it.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tftlab.analytics import carry_commitment_stats
from tftlab.demo import generate_demo_matches
from tftlab.storage import Database
from tftlab.validate import validate_live_data

from _helpers import make_match, make_unit


def _eight_player_match(match_id: str = "NA1_TEST") -> dict:
    """A realistic 8-participant match (demo generator shape)."""
    payload = copy.deepcopy(generate_demo_matches(1, seed=21)[0])
    payload["metadata"]["match_id"] = match_id
    return payload


def _production_shaped(match_id: str = "NA1_5648101147_FIXTURE") -> dict:
    """Mirrors the diagnosed production match: participant 1 placed 2nd
    with `units: []` while the other seven boards are intact."""
    payload = _eight_player_match(match_id)
    participants = payload["info"]["participants"]
    for i, p in enumerate(participants):
        p["placement"] = i + 1
    participants[1]["units"] = []
    return payload


def _counts(report) -> tuple[int, int, int, bool]:
    return (
        report.participants_without_units,
        report.source_empty_participants,
        report.unexpected_participants_without_units,
        report.is_severe,
    )


def test_empty_units_list_is_source_empty_warning(tmp_path: Path) -> None:
    with Database(tmp_path / "a.sqlite3") as db:
        db.ingest_match(_production_shaped())
        report = validate_live_data(db)
    assert _counts(report) == (1, 1, 0, False)


def test_missing_units_key_is_source_empty_warning(tmp_path: Path) -> None:
    payload = _eight_player_match()
    del payload["info"]["participants"][4]["units"]
    with Database(tmp_path / "b.sqlite3") as db:
        db.ingest_match(payload)
        report = validate_live_data(db)
    assert _counts(report) == (1, 1, 0, False)


def test_units_lost_after_ingest_are_unexpected_and_severe(tmp_path: Path) -> None:
    payload = _eight_player_match()
    assert payload["info"]["participants"][2]["units"]  # Riot sent units
    with Database(tmp_path / "c.sqlite3") as db:
        db.ingest_match(payload)
        db.execute("DELETE FROM units WHERE match_id = ? AND participant_index = 2", ("NA1_TEST",))
        db.commit()
        report = validate_live_data(db)
    assert _counts(report) == (1, 0, 1, True)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: p.update({"info": {"participants": p["info"]["participants"][:3]}}),  # participant count differs
        lambda p: p["info"]["participants"][2].update({"placement": 7}),  # placement doesn't line up
        lambda p: p["info"]["participants"][2].update({"units": None}),  # units not a list
        lambda p: p.pop("info"),  # unreadable shape
    ],
)
def test_unmappable_participant_is_unexpected_and_severe(tmp_path: Path, corrupt) -> None:
    """If the stored participant can't be matched to its raw entry, it is
    never assumed to be source-empty."""
    payload = _eight_player_match()
    with Database(tmp_path / "d.sqlite3") as db:
        db.ingest_match(payload)
        db.execute("DELETE FROM units WHERE match_id = ? AND participant_index = 2", ("NA1_TEST",))
        stored_placement = db.query_one(
            "SELECT placement FROM participants WHERE match_id = ? AND participant_index = 2", ("NA1_TEST",)
        )[0]
        raw = copy.deepcopy(payload)
        raw["info"]["participants"][2]["placement"] = stored_placement
        raw["info"]["participants"][2]["units"] = []  # would be source-empty if the mapping held
        corrupt(raw)
        db.execute("UPDATE matches SET payload_json = ? WHERE match_id = ?", (json.dumps(raw), "NA1_TEST"))
        db.commit()
        report = validate_live_data(db)
    assert _counts(report) == (1, 0, 1, True)


def test_source_empty_alone_is_not_severe_but_unexpected_still_is(tmp_path: Path) -> None:
    with Database(tmp_path / "e.sqlite3") as db:
        db.ingest_match(_production_shaped("SOURCE_EMPTY"))
        assert validate_live_data(db).is_severe is False
        lost = _eight_player_match("LOST")
        db.ingest_match(lost)
        db.execute("DELETE FROM units WHERE match_id = ? AND participant_index = 0", ("LOST",))
        db.commit()
        report = validate_live_data(db)
    assert _counts(report) == (2, 1, 1, True)


def test_source_empty_participant_is_kept_as_stored(tmp_path: Path) -> None:
    """Nothing is deleted, fabricated or rewritten."""
    with Database(tmp_path / "f.sqlite3") as db:
        db.ingest_match(_production_shaped("KEEP"))
        validate_live_data(db)
        row = db.query_one("SELECT placement, level FROM participants WHERE match_id = ? AND participant_index = 1", ("KEEP",))
        units = db.query_one("SELECT COUNT(*) FROM units WHERE match_id = ? AND participant_index = 1", ("KEEP",))[0]
        participants = db.query_one("SELECT COUNT(*) FROM participants WHERE match_id = ?", ("KEEP",))[0]
    assert row[0] == 2 and units == 0 and participants == 8


def test_validation_agrees_with_the_production_diagnostic_script(tmp_path: Path) -> None:
    """The read-only diagnostic (scripts/diagnostics) and validation must
    classify the same fixture the same way."""
    script = Path(__file__).parent.parent / "scripts" / "diagnostics" / "participants_without_units.py"
    spec = importlib.util.spec_from_file_location("diag_for_validation", script)
    diag = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = diag
    spec.loader.exec_module(diag)

    payload = _production_shaped("CONSISTENT")
    with Database(tmp_path / "g.sqlite3") as db:
        db.ingest_match(payload)
        report = validate_live_data(db)
        stored = {
            (r[0]): (r[1], r[2])
            for r in db.query_all(
                "SELECT p.participant_index, p.placement, "
                "(SELECT COUNT(*) FROM units u WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index) "
                "FROM participants p WHERE p.match_id = ?",
                ("CONSISTENT",),
            )
        }
    raw_rows = [
        diag.RawParticipant(
            "CONSISTENT", i, str(p["placement"]), "units" in p, "array", len(p.get("units", [])),
            stored[i][1], stored[i][0], None, None, None, None, None, None, None,
        )
        for i, p in enumerate(payload["info"]["participants"])
    ]
    verdict = diag.classify([("CONSISTENT", 1)], raw_rows, {"CONSISTENT": 8})
    assert verdict == diag.CASE_A
    assert report.source_empty_participants == 1 and not report.is_severe


# ---------------------------------------------------------------- denominators


def _rates_scenario(db: Database, *, with_source_empty: bool) -> None:
    """Two boards with the carry (both committed), two without it."""
    items = ["TFT_Item_BlueBuff", "TFT_Item_JeweledGauntlet"]
    for i in range(2):
        db.ingest_match(make_match(f"CARRY_{i}", placement=2, units=[make_unit("TFT14_Foo", items=items)]))
    for i in range(2):
        db.ingest_match(make_match(f"OTHER_{i}", placement=5, units=[make_unit("TFT14_Bar")]))
    if with_source_empty:
        db.ingest_match(make_match("EMPTY", placement=2, units=[]))


def test_source_empty_participant_does_not_depress_rates(tmp_path: Path) -> None:
    with Database(tmp_path / "base.sqlite3") as base_db:
        _rates_scenario(base_db, with_source_empty=False)
        (base,) = carry_commitment_stats(base_db, min_samples=1)
    with Database(tmp_path / "empty.sqlite3") as db:
        _rates_scenario(db, with_source_empty=True)
        (stat,) = carry_commitment_stats(db, min_samples=1)
        participants = db.query_one("SELECT COUNT(*) FROM participants")[0]

    assert participants == 5  # the source-empty participant is still stored
    assert stat.appearance_rate == base.appearance_rate == pytest.approx(2 / 4)
    assert stat.commitment_rate == base.commitment_rate == pytest.approx(2 / 4)
    assert stat.usage_rate == stat.commitment_rate
    # Placement-based results only ever come from the carry's own games.
    assert (stat.commitment_games, stat.avg_placement, stat.top4_rate) == (base.commitment_games, base.avg_placement, base.top4_rate)


def test_rates_for_normal_data_are_unchanged(tmp_path: Path) -> None:
    """With no source-empty boards, unit-observable == all participants."""
    with Database(tmp_path / "normal.sqlite3") as db:
        _rates_scenario(db, with_source_empty=False)
        total = db.query_one("SELECT COUNT(*) FROM participants")[0]
        (stat,) = carry_commitment_stats(db, min_samples=1)
    assert stat.appearance_rate == pytest.approx(stat.appearances / total)
    assert stat.commitment_rate == pytest.approx(stat.commitment_games / total)


_POSTGRES_TEST_URL = __import__("os").environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not _POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_postgres_validation_splits_source_empty_from_unexpected() -> None:
    db = Database(_POSTGRES_TEST_URL)
    try:
        for table in ("traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.ingest_match(_production_shaped("PG_SOURCE_EMPTY"))
        first = validate_live_data(db)
        lost = _eight_player_match("PG_LOST")
        db.ingest_match(lost)
        db.execute("DELETE FROM units WHERE match_id = ? AND participant_index = 3", ("PG_LOST",))
        db.commit()
        second = validate_live_data(db)
        stats = carry_commitment_stats(db, min_samples=1, max_cost=5)
        observable = db.query_one(
            "SELECT COUNT(*) FROM participants p WHERE EXISTS "
            "(SELECT 1 FROM units u WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index)"
        )[0]
    finally:
        db.close()
    assert _counts(first) == (1, 1, 0, False)
    assert _counts(second) == (2, 1, 1, True)
    assert observable == 14
    assert stats
    for stat in stats:
        assert stat.appearance_rate == pytest.approx(stat.appearances / observable)
        assert stat.commitment_rate == pytest.approx(stat.commitment_games / observable)
