"""`tftlab experiment-note`, `experiment-scout` and `scout-sources`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tftlab.cli import app
from tftlab.experiments import create_experiment, get_experiment
from tftlab.storage import Database

from _helpers import make_match, make_unit

runner = CliRunner()


def _run(*args: str):
    return runner.invoke(app, list(args), terminal_width=200)


def _setup(tmp_path: Path, games: int = 6) -> str:
    db = str(tmp_path / "cli.sqlite3")
    with Database(db) as database:
        for i in range(games):
            database.ingest_match(make_match(
                f"K{i}", placement=(i % 8) + 1,
                units=[make_unit("DA_18_KhaZix", name="Kha'Zix", tier=2, items=["TFT_Item_InfinityEdge", "TFT_Item_LastWhisper"])],
                traits=[{"name": "DA_18_Slayer", "num_units": 6, "style": 2, "tier_current": 2, "tier_total": 3}],
            ))
        create_experiment(database, {
            "title": "6 Ravager Kha'Zix", "carry_name": "Kha'Zix",
            "comp": {"core_units": ["Kha'Zix"], "target_traits": ["6 Ravager"], "reroll_level": 7},
        })
    return db


def test_experiment_note_flags(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    result = _run(
        "experiment-note", "6-ravager-khazix", "--kind", "scout_report", "--source", "TFT Academy",
        "--url", "https://tftacademy.com/tierlist/comps", "--body", "No sufficiently similar listed comp found.",
        "--label", "no public match found", "--noted-at", "2026-09-22", "--db", db,
    )
    assert result.exit_code == 0, result.output
    assert "SCOUT REPORT · TFT Academy" in result.output and "No public match found" in result.output
    with Database(db) as database:
        (note,) = get_experiment(database, "6-ravager-khazix").field_notes
    assert (note["source_key"], note["noted_at"], note["research_label"]) == (
        "tft_academy", "2026-09-22T00:00:00Z", "NO_PUBLIC_MATCH_FOUND"
    )


def test_experiment_note_from_json(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    path = tmp_path / "note.json"
    path.write_text(json.dumps({
        "kind": "mechanic_note", "source": "Little Buddy Bot", "url": "https://example.com/odds",
        "body": "Relevant shop mechanic affects practical reroll odds.", "data": {"odds_at_7": [0.19, 0.3]},
    }))
    result = _run("experiment-note", "6-ravager-khazix", "--from-json", str(path), "--db", db)
    assert result.exit_code == 0, result.output
    with Database(db) as database:
        (note,) = get_experiment(database, "6-ravager-khazix").field_notes
    assert note["kind"] == "mechanic_note" and note["data"] == {"odds_at_7": [0.19, 0.3]}

    path.write_text(json.dumps({"kind": "my_note", "body": "x", "sauce": "typo"}))
    assert _run("experiment-note", "6-ravager-khazix", "--from-json", str(path), "--db", db).exit_code != 0


def test_experiment_note_errors_save_nothing(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    cases = [
        ("--kind", "scout_report", "--body", "x", "--url", "javascript:alert(1)"),
        ("--kind", "riot_evidence", "--body", "made-up numbers"),
        ("--kind", "scout_report", "--body", "x", "--label", "NO_PUBLIC_MATCH_FOUND"),
        ("--kind", "scout_report", "--body", "x", "--status", "OBSERVED"),
        ("--body", "no kind"),
    ]
    for args in cases:
        result = _run("experiment-note", "6-ravager-khazix", *args, "--db", db)
        assert result.exit_code == 1, (args, result.output)
    assert _run("experiment-note", "nope", "--kind", "my_note", "--body", "x", "--db", db).exit_code == 1
    with Database(db) as database:
        assert get_experiment(database, "6-ravager-khazix").field_notes == []


def test_experiment_scout_reports_and_lists_unchecked_sources(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    result = _run("experiment-scout", "6-ravager-khazix", "--db", db)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "SCOUT: 6 Ravager Kha'Zix" in out
    assert "Carry: Kha'Zix" in out and "Trait target: 6 Ravager" in out and "Reroll level: 7" in out
    assert "Committed games: 6" in out and "LOW SAMPLE" in out
    assert "With 6 Ravager active: 6 of 6 committed games" in out
    assert "External checks still needed (not checked by this command)" in out
    for source in ("TFT Academy", "MetaTFT", "tactics.tools", "Mechanics check", "Community sightings"):
        assert f"- {source}" in out
    assert "Nothing saved" in out
    with Database(db) as database:
        assert get_experiment(database, "6-ravager-khazix").field_notes == []


def test_experiment_scout_save_appends_riot_evidence(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    _run("experiment-note", "6-ravager-khazix", "--kind", "scout_report", "--source", "MetaTFT", "--body", "x", "--db", db)
    result = _run("experiment-scout", "6-ravager-khazix", "--save", "--db", db)
    assert result.exit_code == 0, result.output
    assert "- MetaTFT" not in result.output  # now has a note
    assert "Saved a riot_evidence field note" in result.output
    with Database(db) as database:
        notes = get_experiment(database, "6-ravager-khazix").field_notes
    assert [n["kind"] for n in notes] == ["scout_report", "riot_evidence"]
    assert notes[1]["data"]["commitment_games"] == 6


def test_experiment_scout_json_and_minimal_idea(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    data = json.loads(_run("experiment-scout", "6-ravager-khazix", "--json", "--db", db).output)
    assert data["fingerprint"]["signature"] == "carry=khazix;core=khazix;traits=ravager@6;reroll_level=7"
    assert data["riot_evidence"]["commitment_games"] == 6 and data["saved_note"] is None
    with Database(db) as database:
        create_experiment(database, {"title": "Kha'Zix + 6 Ravager. Try rerolling him."})
    out = _run("experiment-scout", "khazix-6-ravager-try-rerolling-him", "--db", db).output
    assert "nothing structured yet" in out and "doesn't name a carry" in out
    assert _run("experiment-scout", "nope", "--db", db).exit_code == 1


def test_show_prints_notes_and_checklist(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    _run("experiment-note", "6-ravager-khazix", "--kind", "my_note", "--body", "hunch", "--db", db)
    out = _run("experiment-show", "6-ravager-khazix", "--db", db).output
    assert "MY NOTE · My note" in out and "hunch" in out
    assert "[ ] TFT Academy" in out and "[ ] Our Riot data" in out


def test_scout_sources_lists_the_vocabulary() -> None:
    out = _run("scout-sources").output
    for text in ("Our Riot data", "CommunityDragon", "TFT Academy", "MetaTFT", "tactics.tools", "Little Buddy Bot",
                 "Community / Reddit", "Tournament / high-Elo", "My note", "NO PUBLIC MATCH FOUND", "EMERGING"):
        assert text in out
