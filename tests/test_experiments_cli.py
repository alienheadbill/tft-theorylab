"""`tftlab experiment-*` commands: the only write path into the notebook."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tftlab.cli import app
from tftlab.experiments import get_experiment
from tftlab.storage import Database

runner = CliRunner()


def _run(*args: str):
    return runner.invoke(app, list(args))


def test_add_minimal_then_show(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    result = _run("experiment-add", "--title", "Kha'Zix + 6 Ravager. Try rerolling him.", "--db", db)
    assert result.exit_code == 0, result.output
    assert "Saved to the sqlite file" in result.output
    assert "[THEORYCRAFTED]" in result.output

    shown = _run("experiment-show", "khazix-6-ravager-try-rerolling-him", "--json", "--db", db)
    assert shown.exit_code == 0
    data = json.loads(shown.output)
    assert data["evidence_status"] == "THEORYCRAFTED" and data["lifecycle"] == "idea"
    assert data["comp"]["core_units"] == []


def test_add_with_flags_builds_structured_comp(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    result = _run(
        "experiment-add", "--title", "Cass/Fiddle", "--carry", "Cassiopeia", "--carry-id", "DA_18_Cassiopeia",
        "--core", "Cassiopeia", "--core", "Fiddlesticks", "--optional", "Leona",
        "--trait", "6 Ravager", "--carry-item", "Blue Buff", "--carry-item", "Jeweled Gauntlet",
        "--tank-item", "Warmog's Armor", "--secondary-unit", "Fiddlesticks", "--secondary-item", "Morello",
        "--target-level", "7", "--reroll-level", "5", "--roll-timing", "stabilize at 5",
        "--positioning", "corner", "--augments", "reroll augments", "--summary", "double reroll",
        "--notes", "try it", "--lifecycle", "testing", "--status", "VARIANT", "--tag", "reroll", "--db", db,
    )
    assert result.exit_code == 0, result.output
    with Database(db) as database:
        e = get_experiment(database, "cass-fiddle")
    assert [u["name"] for u in e.comp["core_units"]] == ["Cassiopeia", "Fiddlesticks"]
    assert e.comp["target_traits"] == [{"name": "Ravager", "breakpoint": 6, "note": None}]
    assert e.comp["carry_items"] == ["Blue Buff", "Jeweled Gauntlet"]
    assert e.comp["secondary_carry"] == {"unit": "Fiddlesticks", "items": ["Morello"]}
    assert (e.comp["target_level"], e.comp["reroll_level"]) == (7, 5)
    assert (e.evidence_status, e.lifecycle, e.tags) == ("VARIANT", "testing", ["reroll"])
    assert e.carry_character_id == "DA_18_Cassiopeia"


def test_add_from_json_with_flag_override(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    entry = tmp_path / "entry.json"
    entry.write_text(json.dumps({
        "title": "from a file",
        "summary": "written by an assistant",
        "comp": {"core_units": ["Caitlyn"], "target_traits": ["2 Sniper"]},
        "tags": ["reroll"],
    }))
    result = _run("experiment-add", "--from-json", str(entry), "--summary", "flag wins", "--db", db)
    assert result.exit_code == 0, result.output
    with Database(db) as database:
        e = get_experiment(database, "from-a-file")
    assert e.summary == "flag wins"
    assert e.comp["target_traits"] == [{"name": "Sniper", "breakpoint": 2, "note": None}]


def test_update_changes_only_given_fields(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    _run("experiment-add", "--title", "idea", "--core", "Caitlyn", "--carry-item", "IE", "--tag", "reroll", "--db", db)
    result = _run(
        "experiment-update", "idea", "--lifecycle", "watching", "--carry-item", "Last Whisper",
        "--add-tag", "odd", "--remove-tag", "reroll", "--db", db,
    )
    assert result.exit_code == 0, result.output
    with Database(db) as database:
        e = get_experiment(database, "idea")
    assert e.lifecycle == "watching"
    assert e.comp["carry_items"] == ["Last Whisper"]
    assert [u["name"] for u in e.comp["core_units"]] == ["Caitlyn"]
    assert e.tags == ["odd"]
    assert e.evidence_status == "THEORYCRAFTED"


def test_show_json_round_trips_through_update_from_json(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    _run("experiment-add", "--title", "round trip", "--trait", "6 Ravager", "--db", db)
    exported = tmp_path / "export.json"
    exported.write_text(_run("experiment-show", "round-trip", "--json", "--db", db).output)

    data = json.loads(exported.read_text())
    data["comp"]["carry_items"] = ["Infinity Edge"]
    data["author_notes"] = "edited in a text editor"
    exported.write_text(json.dumps(data))

    result = _run("experiment-update", "round-trip", "--from-json", str(exported), "--db", db)
    assert result.exit_code == 0, result.output
    with Database(db) as database:
        e = get_experiment(database, "round-trip")
    assert e.comp["carry_items"] == ["Infinity Edge"]
    assert e.comp["target_traits"][0]["breakpoint"] == 6
    assert e.author_notes == "edited in a text editor"


def test_list_filters_and_json(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    _run("experiment-add", "--title", "one", "--tag", "reroll", "--db", db)
    _run("experiment-add", "--title", "two", "--lifecycle", "archived", "--db", db)
    table = _run("experiment-list", "--db", db)
    assert table.exit_code == 0 and "one" in table.output and "two" in table.output
    filtered = json.loads(_run("experiment-list", "--tag", "reroll", "--json", "--db", db).output)
    assert [e["slug"] for e in filtered] == ["one"]
    empty = _run("experiment-list", "--status", "VARIANT", "--db", db)
    assert "No experiments match" in empty.output


def test_errors_exit_nonzero_and_save_nothing(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    observed = _run("experiment-add", "--title", "claims too much", "--status", "OBSERVED", "--db", db)
    assert observed.exit_code == 1 and "OBSERVED" in observed.output
    no_title = _run("experiment-add", "--summary", "no title", "--db", db)
    assert no_title.exit_code == 1 and "title" in no_title.output
    bad_json = tmp_path / "bad.json"
    bad_json.write_text(json.dumps({"title": "x", "comp": {"core_unitz": ["typo"]}}))
    typo = _run("experiment-add", "--from-json", str(bad_json), "--db", db)
    assert typo.exit_code == 1 and "unknown field" in typo.output
    assert _run("experiment-show", "nope", "--db", db).exit_code == 1
    assert _run("experiment-update", "nope", "--title", "x", "--db", db).exit_code == 1
    assert json.loads(_run("experiment-list", "--json", "--db", db).output) == []
