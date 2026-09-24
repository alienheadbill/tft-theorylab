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


_UNREAL_FALLBACK_VERSION = "TFT Unreal Version ?.?.?.?"


def _seed_pre_fix_unreal_row(db_path: Path, match_id: str, game_datetime: int) -> None:
    """A row shaped exactly like what the OLD (buggy) `patch_from_game_version`
    produced: `patch` and `balance_window` both set to the raw masked
    `game_version` string verbatim, since that fallback treated any
    unparseable string (Unreal-masked or not) as its own single-value patch."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            match_id,
            game_datetime,
            _UNREAL_FALLBACK_VERSION,
            _UNREAL_FALLBACK_VERSION,
            _UNREAL_FALLBACK_VERSION,
            "standard",
            1100,
            18,
            "TFTSet18",
            "{}",
        ),
    )
    conn.commit()
    conn.close()


def test_backfill_only_touches_rows_from_the_masked_unreal_fallback(tmp_path: Path) -> None:
    """Requirement: only rows whose existing `patch` came from the masked
    Unreal fallback are touched; a row with an already-valid parsed patch
    (even an unusual-looking one) must never be modified."""
    from _helpers import make_match, make_unit

    db_path = tmp_path / "unreal_backfill.sqlite3"
    with Database(db_path) as db:
        db.ingest_match(
            make_match(
                "NORMAL_1",
                game_version="Version 14.6.1 (Sep 10 2024) [PUBLIC] <Releases/14.6>",
                units=[make_unit("TFT14_Foo", tier=2, items=[])],
            )
        )
    # 2026-09-23T00:00:00Z: inside the real registry's deliberate 18.2/18.3
    # gap (the reported early-NA-18.3 rollout window), so this stays
    # unresolved even against the now-populated production registry.
    _seed_pre_fix_unreal_row(db_path, "UNREAL_OLD", game_datetime=1_790_121_600_000)

    with Database(db_path) as db:
        normal_row = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("NORMAL_1",))
        unreal_row = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_OLD",))

    # Untouched: already had a genuinely parsed patch.
    assert normal_row == ("14.6", "14.6")
    # Corrected: was the masked-Unreal fallback string; now the explicit
    # unresolved sentinel with balance_window left unset -- this timestamp
    # falls in the registry's deliberate gap, not any known-safe window.
    assert unreal_row == ("unreal-unresolved", None)


def test_unreal_backfill_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "unreal_idempotent.sqlite3"
    # Create the schema first (a fresh DB already has unit_index etc.), then
    # seed a raw pre-fix row directly, bypassing normalize/ingest entirely.
    with Database(db_path):
        pass
    # 2026-09-23T00:00:00Z: inside the real registry's deliberate gap.
    _seed_pre_fix_unreal_row(db_path, "UNREAL_1", game_datetime=1_790_121_600_000)

    with Database(db_path) as db:
        first = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_1",))
    with Database(db_path) as db:
        second = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_1",))

    assert first == second == ("unreal-unresolved", None)


def test_unreal_backfill_self_heals_once_a_verified_window_is_registered(tmp_path: Path) -> None:
    """Regression test: tightening `is_masked_unreal_version` to the exact
    "?.?.?.?" placeholder shape (so a future genuinely-parseable
    "TFT Unreal Version 18.3.1234" isn't misclassified) must not also break
    self-healing -- a row already holding the UNRESOLVED_UNREAL_PATCH
    sentinel (which doesn't match that shape) must still be re-examined and
    correctly reclassified once a real, verified+sourced window covering
    its timestamp is added to the registry, with no separate re-migration
    step."""
    import tftlab.unreal_patch as unreal_patch_module
    from tftlab.unreal_patch import UnrealPatchWindow

    db_path = tmp_path / "self_heal.sqlite3"
    with Database(db_path):
        pass
    _seed_pre_fix_unreal_row(db_path, "UNREAL_1", game_datetime=1_500_000)

    with Database(db_path):
        pass  # first connect: backfills to the unresolved sentinel

    original_registry = unreal_patch_module.UNREAL_PATCH_REGISTRY
    try:
        unreal_patch_module.UNREAL_PATCH_REGISTRY = (
            UnrealPatchWindow(
                client_patch="18.2",
                starts_at=1_000_000,
                ends_at=2_000_000,
                verified=True,
                source="test fixture",
            ),
        )
        with Database(db_path) as db:
            healed = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_1",))
    finally:
        unreal_patch_module.UNREAL_PATCH_REGISTRY = original_registry

    # balance_window.py's own registered mid-patch cutover for 18.2 (see
    # BALANCE_WINDOW_REGISTRY) is far later than this test's timestamp, so
    # the resolved patch composes into the "a" half -- proving the two
    # registries still compose correctly after self-healing, not just that
    # a bare client patch comes back.
    assert healed == ("18.2", "18.2a")


def test_real_populated_registry_self_heals_a_production_style_sentinel_row(tmp_path: Path) -> None:
    """The actual deliverable of this milestone: a row shaped exactly like
    production's 47 affected matches (raw masked-Unreal fallback value,
    timestamp inside the real conservative 18.2 window) must self-heal to
    a real patch/balance_window against the now-populated, real
    UNREAL_PATCH_REGISTRY -- no fabricated/injected registry involved."""
    # 2026-09-17T21:47:59.589Z -- production's actual reported earliest
    # masked-Unreal game_datetime, which falls inside the real 18.2 window.
    production_earliest = 1_789_681_679_589

    db_path = tmp_path / "real_self_heal.sqlite3"
    with Database(db_path):
        pass
    _seed_pre_fix_unreal_row(db_path, "PROD_STYLE_1", game_datetime=production_earliest)

    with Database(db_path) as db:
        healed = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("PROD_STYLE_1",))

    assert healed[0] == "18.2"
    assert healed[1] in ("18.2a", "18.2b")


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


@requires_postgres
def test_postgres_unreal_backfill_matches_sqlite_and_is_idempotent() -> None:
    """Postgres counterpart of the SQLite masked-Unreal backfill tests
    above: only rows from the pre-fix fallback are touched, and
    reconnecting repeatedly produces the same (idempotent) result."""
    db = _clean_postgres_db()
    try:
        db.execute(
            "INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "UNREAL_PG_1",
                1_790_121_600_000,  # 2026-09-23T00:00:00Z: inside the real registry's deliberate gap
                _UNREAL_FALLBACK_VERSION,
                _UNREAL_FALLBACK_VERSION,
                _UNREAL_FALLBACK_VERSION,
                "standard",
                1100,
                18,
                "TFTSet18",
                "{}",
            ),
        )
        db.commit()
    finally:
        db.close()

    # Reconnecting runs _backfill_unreal_patches.
    db = Database(POSTGRES_TEST_URL)
    try:
        first = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_PG_1",))
    finally:
        db.close()

    db = Database(POSTGRES_TEST_URL)
    try:
        second = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_PG_1",))
    finally:
        db.close()

    assert first == second == ("unreal-unresolved", None)


@requires_postgres
def test_postgres_real_registry_resolves_a_production_style_sentinel_row() -> None:
    """Postgres counterpart of
    test_real_populated_registry_self_heals_a_production_style_sentinel_row:
    a row shaped like production's affected matches, with a timestamp
    inside the real conservative 18.2 window, must resolve identically on
    Postgres -- proving SQLite/Postgres parity for the actual populated
    registry, not just a fabricated one."""
    production_earliest = 1_789_681_679_589  # 2026-09-17T21:47:59.589Z

    db = _clean_postgres_db()
    try:
        db.execute(
            "INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "PROD_STYLE_PG_1",
                production_earliest,
                _UNREAL_FALLBACK_VERSION,
                _UNREAL_FALLBACK_VERSION,
                _UNREAL_FALLBACK_VERSION,
                "standard",
                1100,
                18,
                "TFTSet18",
                "{}",
            ),
        )
        db.commit()
    finally:
        db.close()

    db = Database(POSTGRES_TEST_URL)
    try:
        healed = db.query_one(
            "SELECT patch, balance_window FROM matches WHERE match_id = ?", ("PROD_STYLE_PG_1",)
        )
    finally:
        db.close()

    assert healed[0] == "18.2"
    assert healed[1] in ("18.2a", "18.2b")


@requires_postgres
def test_postgres_and_sqlite_agree_on_unexpected_missing_balance_window(tmp_path: Path) -> None:
    """SQLite-vs-Postgres parity for the intentional-Unreal-gap vs.
    genuinely-unexpected missing-balance-window distinction: a mix of
    intentionally unresolved rows and one genuinely broken row (NULL
    game_version) must produce identical unexpected_missing_balance_window/
    is_severe results on both backends."""
    from tftlab.validate import validate_live_data

    from _helpers import make_match, make_unit

    def _seed(db: Database) -> None:
        for i in range(3):
            db.ingest_match(
                make_match(
                    f"GAP_{i}",
                    game_version="TFT Unreal Version ?.?.?.?",
                    game_datetime=1_790_121_600_000 + i,  # inside the registry's deliberate gap
                    units=[make_unit("TFT18_Foo", tier=2, items=[])],
                )
            )
        db.ingest_match(
            make_match("BROKEN_1", game_version=None, units=[make_unit("TFT14_Foo", tier=2, items=[])])
        )

    with Database(tmp_path / "unexpected_parity.sqlite3") as sqlite_db:
        _seed(sqlite_db)
        sqlite_report = validate_live_data(sqlite_db, balance_window="doesnt-matter")

    postgres_db = _clean_postgres_db()
    try:
        _seed(postgres_db)
        postgres_report = validate_live_data(postgres_db, balance_window="doesnt-matter")
    finally:
        postgres_db.close()

    assert sqlite_report.matches_missing_balance_window == postgres_report.matches_missing_balance_window == 4
    assert sqlite_report.unresolved_unreal_matches == postgres_report.unresolved_unreal_matches == 3
    assert (
        sqlite_report.unexpected_missing_balance_window
        == postgres_report.unexpected_missing_balance_window
        == 1
    )
    assert sqlite_report.is_severe is postgres_report.is_severe is True


def _all_rows(db: Database) -> dict[str, list[tuple]]:
    """Every stored row except the raw payload, sorted, for backend comparison."""
    return {
        "matches": sorted(db.query_all(
            "SELECT match_id, game_datetime, patch, balance_window, queue_id, set_number FROM matches"
        )),
        "participants": sorted(db.query_all("SELECT * FROM participants")),
        "units": sorted(db.query_all(
            "SELECT match_id, participant_index, unit_index, character_id, unit_name, cost, tier, "
            "items_json, completed_item_count FROM units"
        )),
        "traits": sorted(db.query_all(
            "SELECT match_id, participant_index, trait_name, num_units, style, tier_current, tier_total FROM traits"
        )),
    }


def _batched_insert_fixture() -> list[dict]:
    """Full 8-player demo matches plus a payload that lists the same trait
    twice for one participant (the later entry must win, as before)."""
    from _helpers import make_match, make_unit

    repeated = make_match(
        "REPEATED_TRAIT",
        units=[make_unit("TFT14_Foo", tier=2, items=["TFT_Item_BlueBuff"]), make_unit("TFT14_Foo", tier=1, items=[])],
        traits=[
            {"name": "Juggernaut", "num_units": 2, "style": 1, "tier_current": 1, "tier_total": 3},
            {"name": "Juggernaut", "num_units": 4, "style": 2, "tier_current": 2, "tier_total": 3},
            {"name": "", "num_units": 9},
        ],
    )
    return [*generate_demo_matches(6, seed=3), repeated]


def test_batched_ingest_stores_every_row_once_on_sqlite(tmp_path: Path) -> None:
    matches = _batched_insert_fixture()
    with Database(tmp_path / "batch.sqlite3") as db:
        assert db.ingest_many(matches) == len(matches)
        rows = _all_rows(db)
    assert len(rows["participants"]) == 6 * 8 + 1
    expected_units = sum(len(p["units"]) for m in matches for p in m["info"]["participants"])
    assert len(rows["units"]) == expected_units
    repeated = [r for r in rows["traits"] if r[0] == "REPEATED_TRAIT"]
    assert repeated == [("REPEATED_TRAIT", 0, "Juggernaut", 4, 2, 2, 3)]


@requires_postgres
def test_postgres_and_sqlite_store_identical_rows_with_batched_inserts(tmp_path: Path) -> None:
    matches = _batched_insert_fixture()
    with Database(tmp_path / "batch_parity.sqlite3") as sqlite_db:
        sqlite_db.ingest_many(matches)
        sqlite_rows = _all_rows(sqlite_db)

    postgres_db = _clean_postgres_db()
    try:
        postgres_db.ingest_many(matches)
        postgres_rows = _all_rows(postgres_db)
    finally:
        postgres_db.close()

    assert postgres_rows == sqlite_rows
