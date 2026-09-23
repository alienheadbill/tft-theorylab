"""Comp Scout v1: fingerprints, internal Riot evidence, field notes and the
research checklist. Runs on SQLite and (when configured) real Postgres."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tftlab import experiments as ex
from tftlab.analytics import discover_candidates, discovery_candidate_for
from tftlab.scout import comp_fingerprint, evidence_summary, record_riot_evidence, riot_evidence, scout
from tftlab.sources import resolve_source, scout_checklist
from tftlab.storage import TABLES_SQL, Database

from _helpers import make_match, make_unit

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
requires_postgres = pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")

WINDOW = "14.6"  # what _helpers.make_match's default game_version resolves to
KHA = "DA_18_KhaZix"
ITEMS = ["TFT_Item_InfinityEdge", "TFT_Item_LastWhisper"]


def _clean_postgres() -> Database:
    db = Database(POSTGRES_TEST_URL)
    for table in ("experiment_field_notes", "experiment_tags", "experiments", "traits", "units", "participants", "matches"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=requires_postgres)])
def db(request, tmp_path: Path):
    database = Database(tmp_path / "scout.sqlite3") if request.param == "sqlite" else _clean_postgres()
    try:
        yield database
    finally:
        database.close()


def _kha_game(i: int, *, placement: int, with_leona: bool, ravager_units: int) -> dict:
    units = [make_unit(KHA, name="Kha'Zix", tier=2, items=ITEMS)]
    if with_leona:
        units.append(make_unit("DA_18_Leona", name="Leona", tier=2))
    traits = [{"name": "DA_18_Slayer", "num_units": ravager_units, "style": 2, "tier_current": 2, "tier_total": 3}] if ravager_units else []
    return make_match(f"KHA_{i}", units=units, placement=placement, traits=traits)


def _seed_kha(db: Database, n: int = 10) -> None:
    # Game i: placement 1..8 cycling; Leona in even games; 6 Ravager in the first 4.
    for i in range(n):
        db.ingest_match(_kha_game(i, placement=(i % 8) + 1, with_leona=i % 2 == 0, ravager_units=6 if i < 4 else (2 if i < 6 else 0)))


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_is_deterministic_and_normalized() -> None:
    a = ex.Experiment(
        experiment_id="exp_a", slug="a", title="A", carry_character_id=None, carry_name="Kha'Zix",
        evidence_status="THEORYCRAFTED", lifecycle="idea", summary=None, author_notes=None,
        comp=ex.normalize_comp({
            "core_units": ["Leona", "Kha'Zix", "leona"],
            "optional_units": ["Sejuani", "Kha'Zix"],
            "target_traits": ["6 ravager", "2 Vanguard"],
            "carry_items": ["Last Whisper", "Infinity Edge", "TFT_Item_InfinityEdge"],
            "reroll_level": 7, "roll_timing": "  Slow   roll at 7 ",
        }),
        tags=[], origin="manual", created_at="", updated_at="",
    )
    b = ex.Experiment(
        **{**a.__dict__, "carry_name": None, "carry_character_id": KHA,
           "comp": ex.normalize_comp({
               "core_units": [{"name": "KHA'ZIX"}, "Leona"],
               "optional_units": ["sejuani"],
               "target_traits": [{"name": "Vanguard", "breakpoint": 2}, "6 Ravager"],
               "carry_items": ["TFT_Item_LastWhisper", "Infinity Edge"],
               "reroll_level": 7, "roll_timing": "slow roll at 7",
           })}
    )
    fa, fb = comp_fingerprint(a), comp_fingerprint(b)
    assert fa["signature"] == fb["signature"] == (
        "carry=khazix;core=khazix,leona;optional=sejuani;traits=ravager@6,vanguard@2;"
        "items=infinityedge,lastwhisper;reroll_level=7"
    )
    assert comp_fingerprint(a) == fa  # same input, same output
    assert fa["primary_carry"] == {"key": "khazix", "name": "Kha'Zix", "character_id": KHA}
    assert [t["trait_ids"] for t in fa["target_traits"]] == [["DA_18_Slayer"], ["DA_18_Vanguard"]]
    assert fa["roll_timing"] == "slow roll at 7"


def test_minimal_entries_fingerprint(db: Database) -> None:
    title_only = ex.create_experiment(db, {"title": "Kha'Zix + 6 Ravager. Try rerolling him."})
    fp = comp_fingerprint(title_only)
    assert fp["signature"] == "" and fp["specified"] == [] and fp["primary_carry"] is None

    carry_and_trait = ex.create_experiment(
        db, {"title": "6 Ravager Kha'Zix", "carry_name": "Kha'Zix", "comp": {"target_traits": ["6 Ravager"]}}
    )
    fp = comp_fingerprint(carry_and_trait)
    assert fp["signature"] == "carry=khazix;traits=ravager@6"
    assert fp["specified"] == ["carry", "traits"]
    assert fp["core_units"] == []  # no full board required or invented


# ------------------------------------------------------------------ our Riot data


def test_riot_evidence_reuses_discovery_analytics(db: Database) -> None:
    _seed_kha(db)
    e = ex.create_experiment(db, {"title": "k", "carry_name": "Kha'Zix", "comp": {"target_traits": ["6 Ravager"]}})
    ev = riot_evidence(db, e)
    cand = discovery_candidate_for(db, KHA, balance_window=WINDOW, top_n=3)
    assert ev["status"] == "ok" and ev["balance_window"] == WINDOW and ev["character_id"] == KHA
    for field in ("commitment_games", "appearance_rate", "commitment_rate", "avg_placement", "top4_rate",
                  "win_rate", "hit_3star_rate", "opportunity_score"):
        assert ev[field] == getattr(cand, field), field
    assert [p["label"] for p in ev["best_partners"]] == [p.label for p in cand.best_partners[:3]]


def test_core_units_and_trait_targets_are_counted_with_the_committed_carry(db: Database) -> None:
    _seed_kha(db)
    e = ex.create_experiment(db, {
        "title": "k", "carry_name": "Kha'Zix",
        "comp": {"core_units": ["Kha'Zix", "Leona", "Sejuani"], "target_traits": ["6 Ravager", "Ravager", "Nonsense Trait"]},
    })
    ev = riot_evidence(db, e)
    assert ev["commitment_games"] == 10
    assert ev["core_units"] == [
        {"name": "Leona", "games": 5, "top4_rate": 0.6},  # games 0,2,4,6,8 -> placements 1,3,5,7,1
        {"name": "Sejuani", "games": 0, "top4_rate": None},
    ]
    assert ev["core_together"] == {"units": ["Leona", "Sejuani"], "games": 0, "top4_rate": None}
    by_name = {(t["name"], t["breakpoint"]): t for t in ev["trait_targets"]}
    assert by_name[("Ravager", 6)]["games"] == 4          # six Ravager units, not trait tier 6
    assert by_name[("Ravager", None)]["games"] == 6        # any active Ravager
    assert by_name[("Nonsense Trait", None)] == {
        "name": "Nonsense Trait", "breakpoint": None, "known_trait": False, "games": 0, "top4_rate": None,
    }


def test_low_sample_is_explicit(db: Database) -> None:
    _seed_kha(db, n=5)
    e = ex.create_experiment(db, {"title": "k", "carry_character_id": KHA})
    ev = riot_evidence(db, e)
    assert ev["low_sample"] is True and ev["commitment_games"] == 5
    assert "LOW SAMPLE" in evidence_summary(ev)


def test_evidence_without_data_or_carry_says_so(db: Database) -> None:
    no_carry = ex.create_experiment(db, {"title": "just a thought"})
    assert riot_evidence(db, no_carry)["status"] == "no_data"
    _seed_kha(db, n=2)
    assert riot_evidence(db, no_carry)["status"] == "no_carry"
    cait = ex.create_experiment(db, {"title": "c", "carry_name": "Caitlyn"})
    ev = riot_evidence(db, cait)
    assert ev["status"] == "no_committed_games" and ev["commitment_games"] == 0
    assert "Caitlyn has no committed games" in evidence_summary(ev)


def test_external_notes_never_change_the_opportunity_score(db: Database) -> None:
    _seed_kha(db)
    e = ex.create_experiment(db, {"title": "k", "carry_name": "Kha'Zix"})
    before = [(c.character_id, c.opportunity_score) for c in discover_candidates(db, min_samples=1, max_cost=5)]
    ex.add_field_note(db, e.slug, kind="scout_report", source="MetaTFT", source_url="https://www.metatft.com/x",
                      body="Top-4 rate 80% on their site", data={"their_top4": 0.8, "their_games": 5000})
    record_riot_evidence(db, e, riot_evidence(db, e))
    after = [(c.character_id, c.opportunity_score) for c in discover_candidates(db, min_samples=1, max_cost=5)]
    assert before == after


# ------------------------------------------------------------------ field notes


def test_field_note_round_trip_and_source_resolution(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    note = ex.add_field_note(
        db, e.slug, kind="scout_report", source="tft academy", source_url="https://tftacademy.com/tierlist",
        body="No sufficiently similar listed comp.", research_label="no public match found",
        noted_at="2026-09-20", data={"checked_pages": 2},
    )
    assert note["source_key"] == "tft_academy" and note["source_name"] == "TFT Academy"
    assert note["research_label"] == "NO_PUBLIC_MATCH_FOUND"
    assert note["research_label_text"] == "No public match found"
    assert note["noted_at"] == "2026-09-20T00:00:00Z" and note["data"] == {"checked_pages": 2}
    other = ex.add_field_note(db, e.slug, kind="community_sighting", source="Some Discord", body="seen once")
    assert (other["source_key"], other["source_name"]) == ("other", "Some Discord")
    mine = ex.add_field_note(db, e.slug, kind="my_note", body="hunch")
    assert mine["source_key"] == "user"
    assert [n["kind"] for n in ex.get_experiment(db, e.slug).field_notes] == ["scout_report", "community_sighting", "my_note"]
    assert ex.get_experiment(db, e.slug).evidence_status == "THEORYCRAFTED"  # notes never promote


@pytest.mark.parametrize("kind", ["scout_report", "mechanic_note", "community_sighting", "tournament_sighting", "my_note"])
def test_every_manual_kind_is_accepted(db: Database, kind: str) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    assert ex.add_field_note(db, e.slug, kind=kind, body="x")["kind"] == kind


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"kind": "gossip", "body": "x"}, "kind must be one of"),
        ({"kind": "riot_evidence", "body": "fake numbers"}, "written by Theory Lab itself"),
        ({"kind": "status_change", "body": "promoted!"}, "written by Theory Lab itself"),
        ({"kind": "my_note", "body": "  "}, "needs a body"),
        ({"kind": "scout_report", "body": "x", "evidence_status": "OBSERVED"}, "OBSERVED"),
        ({"kind": "scout_report", "body": "x", "research_label": "UNIQUE"}, "research label"),
        ({"kind": "mechanic_note", "body": "x", "research_label": "KNOWN"}, "can't go on"),
        ({"kind": "scout_report", "body": "x", "research_label": "NO_PUBLIC_MATCH_FOUND"}, "must name the source"),
        ({"kind": "my_note", "body": "x", "noted_at": "yesterday-ish"}, "noted_at"),
        ({"kind": "my_note", "body": "x", "noted_at": (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()}, "future"),
        ({"kind": "my_note", "body": "x", "data": ["not", "an", "object"]}, "JSON object"),
    ],
)
def test_invalid_field_notes_are_rejected(db: Database, kwargs: dict, message: str) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    with pytest.raises(ex.ExperimentError, match=message):
        ex.add_field_note(db, e.slug, **kwargs)
    assert ex.get_experiment(db, e.slug).field_notes == []


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "ftp://files.example.com/x", "https://", "https://localhost/x", "notaurl",
     "https://user:pw@example.com/x", "https://example.com/a b", "https://example.com/" + "a" * 2100],
)
def test_bad_source_urls_are_rejected(db: Database, url: str) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    with pytest.raises(ex.ExperimentError, match="url"):
        ex.add_field_note(db, e.slug, kind="scout_report", body="x", source_url=url)


def test_good_source_urls_are_kept(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    for url in ("https://tactics.tools/team-compositions", "http://example.com/path?q=1#frag"):
        assert ex.add_field_note(db, e.slug, kind="scout_report", body="x", source_url=url)["source_url"] == url


def test_field_notes_are_chronological(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    for when, body in (("2026-09-22", "second"), ("2026-09-20", "first"), ("2026-09-22", "third"), ("2026-09-23T08:00:00Z", "fourth")):
        ex.add_field_note(db, e.slug, kind="my_note", body=body, noted_at=when)
    assert [n["body"] for n in ex.get_experiment(db, e.slug).field_notes] == ["first", "second", "third", "fourth"]


def test_status_changes_are_logged_but_lifecycle_changes_are_not(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "k"})
    ex.update_experiment(db, e.slug, {"lifecycle": "testing"})
    ex.update_experiment(db, e.slug, {"evidence_status": "VARIANT"})
    ex.update_experiment(db, e.slug, {"evidence_status": "VARIANT"})  # no-op, no second note
    notes = ex.get_experiment(db, e.slug).field_notes
    assert [(n["kind"], n["evidence_status"], n["body"]) for n in notes] == [
        ("status_change", "VARIANT", "Evidence status THEORYCRAFTED → VARIANT"),
    ]


# ------------------------------------------------------------------ checklist / scout run


def test_checklist_only_ticks_sources_with_notes(db: Database) -> None:
    _seed_kha(db)
    e = ex.create_experiment(db, {"title": "k", "carry_name": "Kha'Zix"})

    def ticked():
        return {c["key"] for c in scout_checklist(ex.get_experiment(db, e.slug).field_notes) if c["checked"]}

    assert ticked() == set()
    report = scout(db, e)
    assert ticked() == set()  # scouting alone writes nothing
    assert report["external_checks_needed"] == [
        "TFT Academy", "MetaTFT", "tactics.tools", "Mechanics check", "Community sightings",
        "High-Elo / tournament sightings",
    ]
    record_riot_evidence(db, e, report["riot_evidence"])
    assert ticked() == {"riot"}
    ex.add_field_note(db, e.slug, kind="scout_report", source="Some random blog", body="x")
    ex.add_field_note(db, e.slug, kind="my_note", body="I think TFT Academy has this")
    assert ticked() == {"riot"}  # neither is a note *from* a checklist source
    ex.add_field_note(db, e.slug, kind="scout_report", source="tactics.tools", body="x")
    ex.add_field_note(db, e.slug, kind="mechanic_note", source="Some wiki", body="x")
    ex.add_field_note(db, e.slug, kind="tournament_sighting", body="x")
    assert ticked() == {"riot", "tactics_tools", "mechanics", "tournament"}
    assert "tactics.tools" not in scout(db, ex.get_experiment(db, e.slug))["external_checks_needed"]


def test_recorded_riot_evidence_note(db: Database) -> None:
    _seed_kha(db, n=4)
    e = ex.create_experiment(db, {"title": "k", "carry_name": "Kha'Zix"})
    note = record_riot_evidence(db, e, riot_evidence(db, e))
    assert (note["kind"], note["source_key"], note["source_name"]) == ("riot_evidence", "riot", "Our Riot data")
    assert note["evidence_status"] is None  # our data never stamps an idea OBSERVED by itself
    assert note["data"]["commitment_games"] == 4 and note["data"]["low_sample"] is True
    assert "LOW SAMPLE" in note["body"]


def test_resolve_source_is_name_only() -> None:
    assert resolve_source("TFT Academy").key == resolve_source("tftacademy").key == "tft_academy"
    assert resolve_source("Reddit").key == "community"
    assert resolve_source("https://tftacademy.com") is None  # never guessed from a URL
    assert resolve_source("some blog") is None


# ------------------------------------------------------------------ migration


def test_field_note_columns_are_added_to_an_older_notebook(tmp_path: Path) -> None:
    """PR #12 databases have experiment_field_notes without source_key /
    research_label; connecting adds them without touching existing rows."""
    path = tmp_path / "pr12.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(TABLES_SQL)
    conn.executescript(
        """CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
             carry_character_id TEXT, carry_name TEXT, evidence_status TEXT NOT NULL DEFAULT 'THEORYCRAFTED',
             lifecycle TEXT NOT NULL DEFAULT 'idea', summary TEXT, author_notes TEXT, comp_json TEXT NOT NULL DEFAULT '{}',
             origin TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
           CREATE TABLE experiment_field_notes (note_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, noted_at TEXT NOT NULL,
             kind TEXT NOT NULL, evidence_status TEXT, body TEXT, source_name TEXT, source_url TEXT,
             data_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
           INSERT INTO experiments VALUES ('exp_1', 'old', 'Old idea', NULL, NULL, 'THEORYCRAFTED', 'idea', NULL, NULL, '{}',
             'manual', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z');
           INSERT INTO experiment_field_notes VALUES ('n1', 'exp_1', '2026-09-21T00:00:00Z', 'my_note', NULL, 'kept', NULL, NULL,
             '{}', '2026-09-21T00:00:00Z');"""
    )
    conn.commit()
    conn.close()
    for _ in range(2):  # idempotent
        with Database(path) as db:
            notes = ex.get_experiment(db, "old").field_notes
            assert [(n["body"], n["source_key"], n["research_label"]) for n in notes][0] == ("kept", None, None)
    with Database(path) as db:
        ex.add_field_note(db, "old", kind="scout_report", source="MetaTFT", body="new")
        assert ex.get_experiment(db, "old").field_notes[-1]["source_key"] == "metatft"


@requires_postgres
def test_postgres_field_note_columns_are_added_to_an_older_notebook() -> None:
    db = _clean_postgres()
    try:
        db.execute("ALTER TABLE experiment_field_notes DROP COLUMN source_key")
        db.execute("ALTER TABLE experiment_field_notes DROP COLUMN research_label")
        db.commit()
    finally:
        db.close()
    for _ in range(2):
        db = Database(POSTGRES_TEST_URL)
        try:
            e = ex.create_experiment(db, {"title": "after migration"})
            note = ex.add_field_note(db, e.slug, kind="scout_report", source="TFT Academy", body="x",
                                     research_label="KNOWN")
            assert (note["source_key"], note["research_label"]) == ("tft_academy", "KNOWN")
        finally:
            db.close()
