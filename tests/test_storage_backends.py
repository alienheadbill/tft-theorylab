"""Storage-abstraction tests.

The SQLite tests always run. The Postgres tests only run when a real,
reachable Postgres instance is provided via `TFTLAB_TEST_DATABASE_URL` (or
`DATABASE_URL`) -- e.g. a disposable local/CI database -- and are skipped
otherwise. No credentials are hardcoded here; nothing is committed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tftlab.analytics import (
    carry_commitment_stats,
    carry_partner_associations,
    default_balance_window,
    discover_candidates,
    item_package_stats,
    trait_breakpoint_associations,
)
from tftlab.demo import generate_demo_matches
from tftlab.storage import Database
from tftlab.webapp import carry_item_sets, carry_partners

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="Set TFTLAB_TEST_DATABASE_URL to a reachable Postgres instance to run these tests",
)


def test_sqlite_backend_dialect(tmp_path: Path) -> None:
    with Database(tmp_path / "dialect.sqlite3") as db:
        assert db.dialect == "sqlite"


def test_sqlite_uses_qmark_placeholders_directly(tmp_path: Path) -> None:
    with Database(tmp_path / "placeholders.sqlite3") as db:
        db.ingest_many(generate_demo_matches(5, seed=2))
        row = db.query_one("SELECT COUNT(*) FROM participants WHERE placement = ?", (1,))
        assert row is not None


def test_migration_backfills_balance_window_for_pre_existing_rows(tmp_path: Path) -> None:
    """`ALTER TABLE ADD COLUMN` doesn't compute values for existing rows, so a
    database that had matches before `balance_window` existed must have them
    backfilled on the next connect, not stuck at NULL forever."""
    from _helpers import make_match, make_unit

    db_path = tmp_path / "legacy.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(
            make_match(
                "LEGACY_1",
                game_version="Version 18.2.100.1 (Sep 1 2026) [PUBLIC] <Releases/18.2>",
                game_datetime=1_789_000_000_000,  # before the registered 18.2 cutover
                units=[make_unit("TFT18_Legacy", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )

    # Simulate a database migrated from before this column existed: the
    # column is present, but this row's value was never computed.
    with Database(db_path) as db:
        db.execute("UPDATE matches SET balance_window = NULL WHERE match_id = ?", ("LEGACY_1",))
        db.commit()

    # Simply reconnecting must self-heal it via the migration backfill.
    with Database(db_path) as db:
        row = db.query_one("SELECT balance_window FROM matches WHERE match_id = ?", ("LEGACY_1",))
    assert row is not None
    assert row[0] == "18.2a"


_OLD_UNITS_SCHEMA_SQL = """
CREATE TABLE matches (
    match_id TEXT PRIMARY KEY, game_datetime BIGINT, game_version TEXT, patch TEXT,
    balance_window TEXT, game_type TEXT, queue_id INTEGER, set_number INTEGER,
    set_core_name TEXT, payload_json TEXT NOT NULL
);
CREATE TABLE participants (
    match_id TEXT NOT NULL, participant_index INTEGER NOT NULL, placement INTEGER NOT NULL,
    level INTEGER NOT NULL, augments_json TEXT NOT NULL,
    PRIMARY KEY (match_id, participant_index)
);
CREATE TABLE units (
    match_id TEXT NOT NULL, participant_index INTEGER NOT NULL, character_id TEXT NOT NULL,
    unit_name TEXT NOT NULL, cost INTEGER, tier INTEGER NOT NULL, items_json TEXT NOT NULL,
    completed_item_count INTEGER NOT NULL,
    PRIMARY KEY (match_id, participant_index, character_id)
);
CREATE TABLE traits (
    match_id TEXT NOT NULL, participant_index INTEGER NOT NULL, trait_name TEXT NOT NULL,
    num_units INTEGER NOT NULL, style INTEGER, tier_current INTEGER, tier_total INTEGER,
    PRIMARY KEY (match_id, participant_index, trait_name)
);
"""


def test_sqlite_migrates_units_table_from_old_schema_preserving_data(tmp_path: Path) -> None:
    """Regression test for the live-ingest failure this milestone fixes: a
    database created before `unit_index` existed used
    `(match_id, participant_index, character_id)` as the `units` primary
    key, which rejects a genuine duplicate-champion board outright
    (`UniqueViolation`/`IntegrityError`). Reconnecting via `Database` must
    upgrade the table in place -- preserving existing matches/participants/
    units, never wiping/recreating the database -- and then accept exactly
    the kind of board that used to fail."""
    import sqlite3

    from _helpers import make_match, make_unit

    db_path = tmp_path / "old_units_schema.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_UNITS_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("OLD_1", 1_790_000_000_000, "Version 14.6.1", "14.6", "14.6", "standard", 1100, 14, "TFTSet14", "{}"),
    )
    conn.execute("INSERT INTO participants VALUES (?,?,?,?,?)", ("OLD_1", 0, 3, 8, "[]"))
    conn.execute(
        "INSERT INTO units VALUES (?,?,?,?,?,?,?,?)",
        ("OLD_1", 0, "TFT14_Legacy", "Legacy", 4, 2, "[]", 0),
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        # Existing data survived the migration untouched.
        assert db.query_one("SELECT match_id FROM matches WHERE match_id = ?", ("OLD_1",)) is not None
        rows = db.query_all(
            "SELECT unit_index, character_id FROM units WHERE match_id = ? ORDER BY unit_index", ("OLD_1",)
        )
        assert rows == [(0, "TFT14_Legacy")]

        # The actual point: a genuine duplicate-champion board, which the
        # old key raised UniqueViolation on, must now succeed.
        assert db.ingest_match(
            make_match(
                "NEW_DUP",
                units=[
                    make_unit("TFT14_Legacy", tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit("TFT14_Legacy", tier=1, items=[]),
                ],
            )
        )
        dup_rows = db.query_all(
            "SELECT unit_index, character_id FROM units WHERE match_id = ? ORDER BY unit_index", ("NEW_DUP",)
        )
        assert dup_rows == [(0, "TFT14_Legacy"), (1, "TFT14_Legacy")]

    # Reconnecting again must be a no-op (idempotent), not an error.
    with Database(db_path) as db:
        assert db.query_one("SELECT COUNT(*) FROM units")[0] == 3


def _clean_postgres_db() -> Database:
    # Only called from tests already guarded by @requires_postgres.
    db = Database(POSTGRES_TEST_URL)
    for table in ("traits", "units", "participants", "matches"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db


@requires_postgres
def test_postgres_backend_dialect() -> None:
    db = _clean_postgres_db()
    try:
        assert db.dialect == "postgres"
    finally:
        db.close()


@requires_postgres
def test_postgres_and_sqlite_agree_on_ingest_and_query(tmp_path: Path) -> None:
    """Identical inputs through the same `Database` API must yield identical
    analytics results regardless of backend -- proving analytics code isn't
    coupled to SQLite-specific behavior."""
    matches = generate_demo_matches(40, seed=11)

    with Database(tmp_path / "parity.sqlite3") as sqlite_db:
        sqlite_db.ingest_many(matches)
        sqlite_stats = carry_commitment_stats(sqlite_db, min_samples=1)

    postgres_db = _clean_postgres_db()
    try:
        postgres_db.ingest_many(matches)
        postgres_stats = carry_commitment_stats(postgres_db, min_samples=1)
    finally:
        postgres_db.close()

    sqlite_by_id = {s.character_id: s for s in sqlite_stats}
    postgres_by_id = {s.character_id: s for s in postgres_stats}
    assert sqlite_by_id.keys() == postgres_by_id.keys()
    for character_id, sqlite_stat in sqlite_by_id.items():
        postgres_stat = postgres_by_id[character_id]
        assert sqlite_stat.commitment_games == postgres_stat.commitment_games
        assert sqlite_stat.avg_placement == pytest.approx(postgres_stat.avg_placement)
        assert sqlite_stat.hit_3star_rate == pytest.approx(postgres_stat.hit_3star_rate)


@requires_postgres
def test_postgres_carry_partners_query_has_no_having_alias_bug() -> None:
    """Regression test: `webapp.carry_partners`'s `HAVING` used to reference a
    SELECT alias (`together`), which SQLite tolerates but Postgres rejects
    with `UndefinedColumn`. This must succeed against real Postgres."""
    from _helpers import make_match, make_unit

    db = _clean_postgres_db()
    try:
        for i in range(3):
            db.ingest_match(
                make_match(
                    f"PARTNER_{i}",
                    units=[
                        make_unit("TFT14_Main", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                        make_unit("TFT14_Buddy", tier=2, items=[]),
                    ],
                )
            )
        balance_window = default_balance_window(db)
        assert balance_window is not None

        partners = carry_partners(db, "TFT14_Main", balance_window)
        assert [p[0] for p in partners] == ["TFT14_Buddy"]  # unit_name, together >= 3

        item_sets = carry_item_sets(db, "TFT14_Main", balance_window)
        assert len(item_sets) == 1
        assert item_sets[0][1] == 3  # games
    finally:
        db.close()


@requires_postgres
def test_postgres_trait_upsert_is_idempotent() -> None:
    from _helpers import make_match, make_unit

    db = _clean_postgres_db()
    try:
        payload = make_match(
            "PG_TRAIT_TEST",
            units=[make_unit("TFT14_Foo", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            traits=[{"name": "Juggernaut", "num_units": 2, "style": 1, "tier_current": 1, "tier_total": 3}],
        )
        assert db.ingest_match(payload) is True
        assert db.ingest_match(payload) is False  # already ingested; no duplicate/upsert error
        rows = db.query_all("SELECT trait_name, num_units FROM traits WHERE match_id = ?", ("PG_TRAIT_TEST",))
        assert rows == [("Juggernaut", 2)]
    finally:
        db.close()


@requires_postgres
def test_postgres_discovery_and_association_queries() -> None:
    """The partner/item/trait LEFT JOIN queries (association.py's callers)
    weren't exercised against Postgres by the parity test above (which only
    calls carry_commitment_stats); run them for real here."""
    from _helpers import make_match, make_unit

    db = _clean_postgres_db()
    try:
        for i in range(5):
            db.ingest_match(
                make_match(
                    f"HIT_{i}",
                    placement=1,
                    units=[
                        make_unit("TFT14_Carry", tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                        make_unit("TFT14_Partner", tier=2, items=[]),
                    ],
                    traits=[{"name": "Juggernaut", "num_units": 4, "style": 3, "tier_current": 2, "tier_total": 3}],
                )
            )
        for i in range(5):
            db.ingest_match(
                make_match(
                    f"MISS_{i}",
                    placement=7,
                    units=[make_unit("TFT14_Carry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )

        balance_window = default_balance_window(db)
        assert balance_window is not None

        partners = carry_partner_associations(db, "TFT14_Carry", balance_window, min_games=1)
        assert [p.key for p in partners] == ["TFT14_Partner"]
        assert partners[0].top4_rate == 1.0
        assert partners[0].top4_rate_without == 0.0

        item_stats = item_package_stats(db, "TFT14_Carry", balance_window)
        assert len(item_stats["items"]) == 2  # BlueBuff, Deathcap

        traits = trait_breakpoint_associations(db, "TFT14_Carry", balance_window, min_games=1)
        assert [t.key for t in traits] == ["Juggernaut:2"]

        candidates = discover_candidates(db, min_cost=1, max_cost=3, min_samples=1)
        assert any(c.character_id == "TFT14_Carry" for c in candidates)
    finally:
        db.close()


@requires_postgres
def test_postgres_migrates_units_table_from_old_schema_preserving_data() -> None:
    """Postgres counterpart of the SQLite migration test above: production
    may already contain real match data under the OLD `units` primary key.
    Reconnecting via `Database` must upgrade it in place -- preserving
    existing rows -- and then accept a genuine duplicate-champion board the
    old key would have raised `UniqueViolation` on. Also a regression test
    for a real bug this migration introduced and then fixed: Postgres's
    `ALTER TABLE ADD COLUMN` always appends `unit_index` as the table's
    LAST physical column, so `ingest_match`'s `INSERT INTO units` must use
    an explicit column list, not positional `VALUES`, or it silently
    inserts into the wrong columns on a migrated (but not freshly created)
    table."""
    import psycopg

    from _helpers import make_match, make_unit

    setup_conn = psycopg.connect(POSTGRES_TEST_URL)
    setup_conn.execute("DROP TABLE IF EXISTS traits, units, participants, matches CASCADE")
    for statement in _OLD_UNITS_SCHEMA_SQL.strip().split(";"):
        if statement.strip():
            setup_conn.execute(statement)
    setup_conn.execute(
        "INSERT INTO matches VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("OLD_PG_1", 1_790_000_000_000, "Version 14.6.1", "14.6", "14.6", "standard", 1100, 14, "TFTSet14", "{}"),
    )
    setup_conn.execute("INSERT INTO participants VALUES (%s,%s,%s,%s,%s)", ("OLD_PG_1", 0, 3, 8, "[]"))
    setup_conn.execute(
        "INSERT INTO units VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("OLD_PG_1", 0, "TFT14_Legacy", "Legacy", 4, 2, "[]", 0),
    )
    setup_conn.commit()
    setup_conn.close()

    db = Database(POSTGRES_TEST_URL)
    try:
        assert db.query_one("SELECT match_id FROM matches WHERE match_id = ?", ("OLD_PG_1",)) is not None
        rows = db.query_all(
            "SELECT unit_index, character_id FROM units WHERE match_id = ? ORDER BY unit_index", ("OLD_PG_1",)
        )
        assert rows == [(0, "TFT14_Legacy")]

        assert db.ingest_match(
            make_match(
                "NEW_PG_DUP",
                units=[
                    make_unit("TFT14_Legacy", tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit("TFT14_Legacy", tier=1, items=[]),
                ],
            )
        )
        dup_rows = db.query_all(
            "SELECT unit_index, character_id, tier FROM units WHERE match_id = ? ORDER BY unit_index",
            ("NEW_PG_DUP",),
        )
        assert dup_rows == [(0, "TFT14_Legacy", 3), (1, "TFT14_Legacy", 1)]
    finally:
        db.close()


@requires_postgres
def test_postgres_and_sqlite_agree_on_duplicate_champion_stats(tmp_path: Path) -> None:
    """SQLite-vs-Postgres parity for a board fielding two instances of the
    same champion -- the exact real-world shape that broke the second live
    ingest run (see tests/test_duplicate_units.py for the full single-
    backend regression coverage)."""
    from _helpers import make_match, make_unit

    def _seed(db: Database) -> None:
        db.ingest_match(
            make_match(
                "DUP",
                placement=2,
                units=[
                    make_unit("TFT14_Dup", tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"]),
                    make_unit("TFT14_Dup", tier=1, items=[]),
                ],
            )
        )
        for i, placement in enumerate([5, 6, 7]):
            db.ingest_match(
                make_match(
                    f"SOLO_{i}",
                    placement=placement,
                    units=[make_unit("TFT14_Dup", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )

    with Database(tmp_path / "dup_parity.sqlite3") as sqlite_db:
        _seed(sqlite_db)
        sqlite_stat = next(
            s
            for s in carry_commitment_stats(sqlite_db, min_cost=1, max_cost=5, min_samples=1)
            if s.character_id == "TFT14_Dup"
        )

    postgres_db = _clean_postgres_db()
    try:
        _seed(postgres_db)
        postgres_stat = next(
            s
            for s in carry_commitment_stats(postgres_db, min_cost=1, max_cost=5, min_samples=1)
            if s.character_id == "TFT14_Dup"
        )
    finally:
        postgres_db.close()

    assert sqlite_stat.appearances == postgres_stat.appearances == 4
    assert sqlite_stat.commitment_games == postgres_stat.commitment_games == 4
    assert sqlite_stat.avg_placement == pytest.approx(postgres_stat.avg_placement)
    assert sqlite_stat.top4_rate == pytest.approx(postgres_stat.top4_rate)
