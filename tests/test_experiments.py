"""Theorycraft notebook ("experiments") model: persistence on SQLite and
Postgres, incomplete entries, structured round-trips, evidence-status rules,
the additive migration, and the example entries."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tftlab import experiments as ex
from tftlab.storage import TABLES_SQL, Database

from _helpers import make_match, make_unit

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="Set TFTLAB_TEST_DATABASE_URL to a reachable Postgres instance to run these tests",
)

FULL_ENTRY = {
    "title": "Cassiopeia / Fiddlesticks reroll",
    "carry_character_id": "DA_18_Cassiopeia",
    "carry_name": "Cassiopeia",
    "lifecycle": "testing",
    "summary": "Double reroll.",
    "author_notes": "unsure about the tank",
    "comp": {
        "core_units": [{"name": "Cassiopeia", "star": 3, "character_id": "DA_18_Cassiopeia"}, "Fiddlesticks"],
        "optional_units": ["Leona"],
        "target_traits": ["6 Ravager", {"name": "Bastion", "breakpoint": 2, "note": "if Leona"}, "Vanguard"],
        "carry_items": ["Blue Buff", "Jeweled Gauntlet"],
        "tank_items": "Warmog's Armor",
        "secondary_carry": {"unit": "Fiddlesticks", "items": ["Morellonomicon"]},
        "target_level": 7,
        "reroll_level": 5,
        "roll_timing": "stabilize at 5",
        "positioning_notes": "Cass corner",
        "augment_notes": "reroll augments",
    },
    "tags": ["Reroll", "#double-reroll", "reroll"],
}

EXPECTED_COMP = {
    "core_units": [
        {"name": "Cassiopeia", "character_id": "DA_18_Cassiopeia", "star": 3, "note": None},
        {"name": "Fiddlesticks", "character_id": None, "star": None, "note": None},
    ],
    "optional_units": [{"name": "Leona", "character_id": None, "star": None, "note": None}],
    "target_traits": [
        {"name": "Ravager", "breakpoint": 6, "note": None},
        {"name": "Bastion", "breakpoint": 2, "note": "if Leona"},
        {"name": "Vanguard", "breakpoint": None, "note": None},
    ],
    "carry_items": ["Blue Buff", "Jeweled Gauntlet"],
    "tank_items": ["Warmog's Armor"],
    "secondary_carry": {"unit": "Fiddlesticks", "items": ["Morellonomicon"]},
    "target_level": 7,
    "reroll_level": 5,
    "roll_timing": "stabilize at 5",
    "positioning_notes": "Cass corner",
    "augment_notes": "reroll augments",
}


def _clean_postgres() -> Database:
    db = Database(POSTGRES_TEST_URL)
    for table in ("experiment_field_notes", "experiment_tags", "experiments"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=requires_postgres)])
def db(request, tmp_path: Path):
    if request.param == "sqlite":
        database = Database(tmp_path / "notebook.sqlite3")
    else:
        database = _clean_postgres()
    try:
        yield database
    finally:
        database.close()


# ------------------------------------------------------------------ basics


def test_minimal_idea_is_a_valid_experiment(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "Kha'Zix + 6 Ravager. Try rerolling him."})
    assert e.slug == "khazix-6-ravager-try-rerolling-him"
    assert e.evidence_status == "THEORYCRAFTED"
    assert e.lifecycle == "idea"
    assert e.carry_character_id is None and e.carry_name is None and e.summary is None
    assert e.comp == ex.empty_comp()
    assert e.tags == []
    assert e.origin == "manual"
    assert e.created_at == e.updated_at


def test_structured_entry_round_trips(db: Database) -> None:
    created = ex.create_experiment(db, FULL_ENTRY)
    fetched = ex.get_experiment(db, created.slug)
    assert fetched.comp == EXPECTED_COMP
    assert fetched.tags == ["double-reroll", "reroll"]
    assert fetched.carry_name == "Cassiopeia"
    assert fetched.lifecycle == "testing"
    # Lookup by id works too.
    assert ex.get_experiment(db, created.experiment_id).slug == created.slug


def test_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "persist.sqlite3"
    with Database(path) as db:
        ex.create_experiment(db, FULL_ENTRY)
    with Database(path) as db:
        (only,) = ex.list_experiments(db)
        assert only.comp == EXPECTED_COMP


@requires_postgres
def test_postgres_persists_across_connections() -> None:
    first = _clean_postgres()
    try:
        ex.create_experiment(first, FULL_ENTRY)
    finally:
        first.close()
    second = Database(POSTGRES_TEST_URL)
    try:
        (only,) = ex.list_experiments(second)
        assert only.comp == EXPECTED_COMP
        assert only.tags == ["double-reroll", "reroll"]
    finally:
        second.close()


def test_duplicate_titles_get_unique_slugs(db: Database) -> None:
    a = ex.create_experiment(db, {"title": "Caitlyn reroll"})
    b = ex.create_experiment(db, {"title": "Caitlyn reroll"})
    assert (a.slug, b.slug) == ("caitlyn-reroll", "caitlyn-reroll-2")
    with pytest.raises(ex.ExperimentError, match="already taken"):
        ex.create_experiment(db, {"title": "x", "slug": "caitlyn-reroll"})


# ------------------------------------------------------------------ evidence rules


def test_evidence_status_defaults_to_theorycrafted_and_variant_is_allowed(db: Database) -> None:
    assert ex.create_experiment(db, {"title": "a"}).evidence_status == "THEORYCRAFTED"
    assert ex.create_experiment(db, {"title": "b", "evidence_status": "variant"}).evidence_status == "VARIANT"


def test_observed_cannot_be_set_by_hand(db: Database) -> None:
    with pytest.raises(ex.ExperimentError, match="OBSERVED"):
        ex.create_experiment(db, {"title": "a", "evidence_status": "OBSERVED"})
    e = ex.create_experiment(db, {"title": "b"})
    with pytest.raises(ex.ExperimentError, match="OBSERVED"):
        ex.update_experiment(db, e.slug, {"evidence_status": "observed"})
    assert ex.get_experiment(db, e.slug).evidence_status == "THEORYCRAFTED"


def test_updates_never_promote_evidence_status(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "a"})
    updated = ex.update_experiment(db, e.slug, {"lifecycle": "testing", "comp": {"target_traits": ["6 Ravager"]}})
    assert updated.evidence_status == "THEORYCRAFTED"


def test_database_rejects_unknown_status_values(db: Database) -> None:
    ex.create_experiment(db, {"title": "a"})
    with pytest.raises(Exception):
        db.execute("UPDATE experiments SET evidence_status = 'PROVEN'")
    db.conn.rollback()


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "entry, message",
    [
        ({}, "title"),
        ({"title": "   "}, "title"),
        ({"title": "a", "lifecycle": "someday"}, "lifecycle"),
        ({"title": "a", "comp": {"core_unit": ["typo"]}}, "unknown field"),
        ({"title": "a", "comp": {"target_level": 15}}, "level"),
        ({"title": "a", "comp": {"core_units": [{"star": 3}]}}, "needs a name"),
        ({"title": "a", "comp": {"target_traits": [{"name": "Ravager", "breakpoint": "six"}]}}, "breakpoint"),
        ({"title": "a", "winrate": 0.9}, "unknown field"),
        ({"title": "a", "slug": "Not A Slug"}, "slug"),
    ],
)
def test_invalid_input_is_rejected_with_a_clear_message(db: Database, entry: dict, message: str) -> None:
    with pytest.raises(ex.ExperimentError, match=message):
        ex.create_experiment(db, entry)
    assert ex.list_experiments(db) == []


# ------------------------------------------------------------------ updates


def test_update_merges_comp_and_leaves_other_fields_alone(db: Database) -> None:
    e = ex.create_experiment(db, FULL_ENTRY)
    updated = ex.update_experiment(db, e.slug, {"comp": {"carry_items": ["Rabadon's Deathcap"]}})
    assert updated.comp["carry_items"] == ["Rabadon's Deathcap"]
    assert updated.comp["core_units"] == EXPECTED_COMP["core_units"]
    assert updated.comp["target_traits"] == EXPECTED_COMP["target_traits"]
    assert updated.summary == "Double reroll." and updated.lifecycle == "testing"
    assert updated.updated_at >= e.updated_at


def test_update_can_clear_a_field_and_edit_tags(db: Database) -> None:
    e = ex.create_experiment(db, FULL_ENTRY)
    updated = ex.update_experiment(
        db, e.slug, {"summary": None, "comp": {"secondary_carry": None}}, add_tags=["cass"], remove_tags=["#reroll"]
    )
    assert updated.summary is None
    assert updated.comp["secondary_carry"] is None
    assert updated.tags == ["cass", "double-reroll"]


def test_update_rename_slug_and_unknown_key(db: Database) -> None:
    e = ex.create_experiment(db, {"title": "a"})
    renamed = ex.update_experiment(db, e.slug, {"slug": "better-name"})
    assert ex.get_experiment(db, "better-name").experiment_id == renamed.experiment_id
    with pytest.raises(ex.ExperimentNotFound):
        ex.get_experiment(db, "a")
    with pytest.raises(ex.ExperimentNotFound):
        ex.update_experiment(db, "does-not-exist", {"title": "x"})


def test_to_input_round_trips_through_update(db: Database) -> None:
    e = ex.create_experiment(db, FULL_ENTRY)
    same = ex.update_experiment(db, e.slug, e.to_input())
    assert same.to_input() == e.to_input()


# ------------------------------------------------------------------ listing


def test_list_filters(db: Database) -> None:
    ex.create_experiment(db, FULL_ENTRY)
    ex.create_experiment(db, {"title": "Kha idea", "carry_name": "Kha'Zix", "tags": ["ravager"]})
    ex.create_experiment(db, {"title": "variant one", "evidence_status": "VARIANT", "lifecycle": "archived"})

    def slugs(**kw):
        return sorted(e.slug for e in ex.list_experiments(db, **kw))

    assert slugs() == ["cassiopeia-fiddlesticks-reroll", "kha-idea", "variant-one"]
    assert slugs(evidence_status="variant") == ["variant-one"]
    assert slugs(lifecycle="archived") == ["variant-one"]
    assert slugs(carry="kha'zix") == ["kha-idea"]
    assert slugs(carry="DA_18_Cassiopeia") == ["cassiopeia-fiddlesticks-reroll"]
    assert slugs(tag="#Reroll") == ["cassiopeia-fiddlesticks-reroll"]
    assert slugs(tag="nothing") == []


# ------------------------------------------------------------------ migration


def test_notebook_tables_are_added_to_an_existing_match_database(tmp_path: Path) -> None:
    """A database created before this feature (match tables only, real
    rows in them) gains the notebook tables on the next connect, and its
    match data is untouched."""
    path = tmp_path / "older.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(TABLES_SQL)
    conn.commit()
    conn.close()
    with Database(path) as db:
        db.ingest_match(make_match("M1", units=[make_unit("TFT14_Foo", tier=2, items=[])]))
    with Database(path) as db:
        assert db.query_one("SELECT COUNT(*) FROM matches")[0] == 1
        assert ex.list_experiments(db) == []
        ex.create_experiment(db, {"title": "fits right in"})


@requires_postgres
def test_postgres_notebook_tables_are_added_to_an_existing_match_database() -> None:
    db = _clean_postgres()
    try:
        db.execute("DELETE FROM traits")
        db.execute("DELETE FROM units")
        db.execute("DELETE FROM participants")
        db.execute("DELETE FROM matches")
        db.execute("DROP TABLE experiment_field_notes")
        db.execute("DROP TABLE experiment_tags")
        db.execute("DROP TABLE experiments")
        db.commit()
        db.ingest_match(make_match("PG_OLD_1", units=[make_unit("TFT14_Foo", tier=2, items=[])]))
    finally:
        db.close()
    reopened = Database(POSTGRES_TEST_URL)
    try:
        assert reopened.query_one("SELECT COUNT(*) FROM matches")[0] == 1
        assert ex.list_experiments(reopened) == []
        ex.create_experiment(reopened, {"title": "fits right in"})
    finally:
        reopened.close()


def test_migration_is_idempotent(db: Database) -> None:
    ex.create_experiment(db, FULL_ENTRY)
    # Re-running schema setup (what every connect does) changes nothing.
    db._init_schema()
    db._init_schema()
    (only,) = ex.list_experiments(db)
    assert only.comp == EXPECTED_COMP


# ------------------------------------------------------------------ examples

_STAT_WORDS = ("rate", "placement", "games", "score", "winrate", "top4", "top_4", "sample", "confidence")


def test_example_entries_are_theorycraft_only(db: Database) -> None:
    assert ex.seed_demo_experiments(db) == len(ex.DEMO_EXPERIMENTS)
    assert ex.seed_demo_experiments(db) == 0  # never seeds twice

    entries = ex.list_experiments(db)
    assert len(entries) == len(ex.DEMO_EXPERIMENTS)
    titles = {e.title for e in entries}
    assert {"6 Ravager Kha'Zix", "Cassiopeia / Fiddlesticks reroll"} <= titles
    for e in entries:
        assert e.evidence_status == "THEORYCRAFTED"
        assert e.origin == "demo" and e.to_api()["is_example"] is True
        assert "example" in e.tags
        assert ex.get_experiment(db, e.slug).field_notes == []
        body = e.to_api(include_field_notes=True)
        keys = set(body) | set(body["comp"])
        assert not any(word in key for key in keys for word in _STAT_WORDS), keys


def test_examples_are_not_seeded_into_a_notebook_with_entries(db: Database) -> None:
    ex.create_experiment(db, {"title": "mine"})
    assert ex.seed_demo_experiments(db) == 0
    assert [e.title for e in ex.list_experiments(db)] == ["mine"]


def test_detail_reads_field_notes_table(db: Database) -> None:
    """Nothing writes field notes yet, but the read path already serves
    whatever the future scouting work appends, oldest first."""
    e = ex.create_experiment(db, {"title": "a"})
    assert ex.get_experiment(db, e.slug).field_notes == []
    db.execute(
        """INSERT INTO experiment_field_notes
           (note_id, experiment_id, noted_at, kind, evidence_status, body, source_name, source_url, data_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("n1", e.experiment_id, "2026-09-25T00:00:00Z", "sighting", None, "seen once", "a site",
         "https://example.com", '{"similarity": 0.4}', "2026-09-25T00:00:00Z"),
    )
    db.commit()
    (note,) = ex.get_experiment(db, e.slug).field_notes
    assert note["kind"] == "sighting" and note["data"] == {"similarity": 0.4}
