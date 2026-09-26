from __future__ import annotations

import json
import time
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .balance_window import resolve_balance_window
from .experiments import EXPERIMENT_INDEXES_SQL, EXPERIMENT_TABLES_SQL, FIELD_NOTE_COLUMN_MIGRATIONS
from .items import completed_item_count, component_ids_version
from .normalize import CostLookup, normalize_match
from .patch import patch_from_game_version
from .unreal_patch import UNRESOLVED_UNREAL_PATCH, is_masked_unreal_version

# Written once, uniformly, using SQLite's `?` placeholder style. The Postgres
# path translates `?` to `%s` before executing (see `Database._translate`),
# so analytics/ingest code never needs to branch on backend.
#
# Table and index DDL are kept separate (and run in that order, with column
# migrations in between -- see `_init_schema`) because on an *existing*
# database, `CREATE TABLE IF NOT EXISTS` is a no-op: a `CREATE INDEX` on a
# column added after that table's original release would fail with
# "column does not exist" if it ran before the migration that adds it.
TABLES_SQL = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    game_datetime BIGINT,
    game_version TEXT,
    patch TEXT,
    balance_window TEXT,
    game_type TEXT,
    queue_id INTEGER,
    set_number INTEGER,
    set_core_name TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    match_id TEXT NOT NULL,
    participant_index INTEGER NOT NULL,
    placement INTEGER NOT NULL,
    level INTEGER NOT NULL,
    augments_json TEXT NOT NULL,
    PRIMARY KEY (match_id, participant_index),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS units (
    match_id TEXT NOT NULL,
    participant_index INTEGER NOT NULL,
    -- This unit's position on its participant's board (0-based, in Match-V1
    -- payload order) -- NOT `character_id` -- is what makes a unit row
    -- unique: a real board can field more than one instance of the same
    -- champion (e.g. via clone/duplication effects), which the old
    -- (match_id, participant_index, character_id) key rejected outright.
    -- `character_id` remains a normal, indexed column below.
    unit_index INTEGER NOT NULL,
    character_id TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    cost INTEGER,
    tier INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    completed_item_count INTEGER NOT NULL,
    PRIMARY KEY (match_id, participant_index, unit_index),
    FOREIGN KEY (match_id, participant_index)
        REFERENCES participants(match_id, participant_index)
);

CREATE TABLE IF NOT EXISTS traits (
    match_id TEXT NOT NULL,
    participant_index INTEGER NOT NULL,
    trait_name TEXT NOT NULL,
    num_units INTEGER NOT NULL,
    style INTEGER,
    tier_current INTEGER,
    tier_total INTEGER,
    PRIMARY KEY (match_id, participant_index, trait_name),
    FOREIGN KEY (match_id, participant_index)
        REFERENCES participants(match_id, participant_index)
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_units_character ON units(character_id);
CREATE INDEX IF NOT EXISTS idx_units_cost ON units(cost);
CREATE INDEX IF NOT EXISTS idx_participants_placement ON participants(placement);
CREATE INDEX IF NOT EXISTS idx_matches_patch ON matches(patch);
CREATE INDEX IF NOT EXISTS idx_matches_balance_window ON matches(balance_window);
"""

# Sampling provenance (additive, `CREATE ... IF NOT EXISTS`, never touches
# match data). Rank here is *how a lobby was discovered* -- the ladder cohort
# a seed player was on when their history was read -- never the rank of the
# match or of its participants, which stay unlabelled.
#
# `seed_samples` is the rotation ledger: one row per seed whose history
# request succeeded in a run (an empty in-window history included). Only the
# PUUID is kept -- no Riot ID, name or summoner id.
#
# `match_discoveries` is many-to-many provenance: one row per (stored match,
# run, seed) that surfaced it. The match itself stays one canonical row in
# `matches`; a lobby found by several seeds/cohorts just has several rows
# here. No foreign key to `matches`, so existing maintenance that clears
# match tables is unaffected; rows are only written for stored matches.
SAMPLING_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS seed_samples (
    run_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    cohort TEXT NOT NULL,
    sampled_at BIGINT NOT NULL,
    PRIMARY KEY (run_id, puuid)
);

CREATE TABLE IF NOT EXISTS match_discoveries (
    match_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    cohort TEXT NOT NULL,
    discovered_at BIGINT NOT NULL,
    PRIMARY KEY (match_id, run_id, puuid)
);
"""

# Run completion. `seed_samples` rows only count for seed rotation once
# their run is `completed` here (see `Database.seed_last_sampled`), so an
# interrupted run -- or ledger rows written before this table existed --
# never advances rotation. Nothing is ever deleted: incomplete runs stay as
# audit evidence.
INGEST_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    started_at BIGINT NOT NULL,
    completed_at BIGINT,
    status TEXT NOT NULL,
    failure TEXT
);
"""

# One row per applied data migration that must run exactly once per database
# (keyed by what it depends on). Only initializing connections read/write it.
SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_key TEXT PRIMARY KEY,
    applied_at BIGINT NOT NULL,
    rows_changed INTEGER NOT NULL
);
"""

#: `ingest_runs.status` values. Only COMPLETED counts for rotation.
RUN_STARTED = "started"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"

#: Every table/column the read-only web application can SELECT -- the routes
#: in `tftlab.webapp`, the `tftlab.analytics` helpers they call, and the
#: experiment reads (`experiments._SELECT`, tags, `list_field_notes`).
#: `Database.open_existing` verifies all of them before returning, so an
#: incompatible schema fails up front (503) instead of mid-request. Columns
#: only written by ingest/admin code (e.g. `matches.payload_json`,
#: `participants.augments_json`) are deliberately not required. Keep this in
#: sync when a web read path starts selecting a new column.
REQUIRED_READ_SCHEMA: dict[str, tuple[str, ...]] = {
    # balance-window listing (game_datetime), health (patch), analytics joins.
    "matches": ("match_id", "game_datetime", "patch", "balance_window"),
    "participants": ("match_id", "participant_index", "placement"),
    # commitment/partners/items/traits analytics and the canonical-unit
    # tiebreak (completed_item_count, tier, unit_index).
    "units": (
        "match_id", "participant_index", "unit_index", "character_id", "unit_name",
        "cost", "tier", "items_json", "completed_item_count",
    ),
    "traits": ("match_id", "participant_index", "trait_name", "num_units", "style", "tier_current", "tier_total"),
    # experiments._SELECT, exactly.
    "experiments": (
        "experiment_id", "slug", "title", "carry_character_id", "carry_name", "evidence_status",
        "lifecycle", "summary", "author_notes", "comp_json", "origin", "created_at", "updated_at",
    ),
    "experiment_tags": ("experiment_id", "tag"),
    # list_field_notes: selected columns plus its WHERE/ORDER BY columns.
    "experiment_field_notes": (
        "note_id", "experiment_id", "noted_at", "kind", "evidence_status", "body", "source_key",
        "source_name", "source_url", "research_label", "data_json", "created_at",
    ),
}


class SchemaUnavailable(RuntimeError):
    """An existing database is missing tables/columns a read-only consumer
    needs. Raised instead of creating or migrating anything."""


SAMPLING_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_seed_samples_puuid ON seed_samples(puuid, sampled_at);
CREATE INDEX IF NOT EXISTS idx_match_discoveries_cohort ON match_discoveries(cohort);
"""

# Columns added to `matches` after its initial release. A fresh database
# already has these via SCHEMA_SQL above; this only matters for upgrading an
# older database in place (see `_run_migrations`).
_MATCHES_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("patch", "TEXT"),
    ("balance_window", "TEXT"),
)

_TRAIT_UPSERT_SQL = {
    "sqlite": "INSERT OR REPLACE INTO traits VALUES (?, ?, ?, ?, ?, ?, ?)",
    "postgres": """
        INSERT INTO traits (
            match_id, participant_index, trait_name, num_units, style, tier_current, tier_total
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (match_id, participant_index, trait_name) DO UPDATE SET
            num_units = EXCLUDED.num_units,
            style = EXCLUDED.style,
            tier_current = EXCLUDED.tier_current,
            tier_total = EXCLUDED.tier_total
    """,
}


def _is_postgres_url(value: str) -> bool:
    return value.startswith("postgres://") or value.startswith("postgresql://")


class Database:
    """Backend-agnostic TFT match store.

    Pass a filesystem path (or `Path`) for local/demo SQLite, or a
    `postgres://`/`postgresql://` URL (e.g. from a `DATABASE_URL` env var) for
    production Postgres. Every query in this codebase is written with `?`
    placeholders; Postgres connections translate them internally, so
    analytics code never has to know which backend it's talking to.
    """

    def __init__(self, target: Path | str, *, initialize_schema: bool = True) -> None:
        """Open `target` and, by default, create/migrate its schema.

        `initialize_schema=True` is for CLI/ingest/admin and local SQLite:
        it runs CREATE TABLE / ALTER TABLE / backfills / CREATE INDEX. Code
        serving requests against an existing database uses
        `Database.open_existing` instead, which never does.
        """
        target_str = str(target)
        self.read_only = not initialize_schema
        #: Unit rows whose stored completed_item_count this connection
        #: corrected (see `_backfill_completed_item_counts`); None when the
        #: backfill did not run on this connection.
        self.completed_item_count_backfill: int | None = None
        if _is_postgres_url(target_str):
            self.dialect = "postgres"
            self.path: Path | None = None
            self.conn = self._connect_postgres(target_str, read_only=self.read_only)
        else:
            self.dialect = "sqlite"
            self.path = Path(target)
            self.conn = self._connect_sqlite(self.path, read_only=self.read_only)
        if initialize_schema:
            self._init_schema()

    @classmethod
    def open_existing(cls, target: Path | str) -> "Database":
        """Connect to an already-initialized database for reading only.

        Runs no CREATE / ALTER / CREATE INDEX / backfill / migration and no
        other write. Postgres sessions are opened with
        `default_transaction_read_only=on` and SQLite files in read-only
        mode, so an accidental write fails loudly instead of happening. A
        missing or incompatible schema raises `SchemaUnavailable` rather
        than being created.
        """
        db = cls(target, initialize_schema=False)
        try:
            db.verify_read_schema()
        except Exception:
            db.close()
            raise
        return db

    def verify_read_schema(self) -> None:
        for table, columns in REQUIRED_READ_SCHEMA.items():
            try:
                self.query_all(f"SELECT {', '.join(columns)} FROM {table} WHERE 1 = 0")
            except Exception as exc:
                raise SchemaUnavailable(f"required table/columns missing: {table}") from exc

    @staticmethod
    def _connect_sqlite(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            # Never creates the file (or its directory) and refuses writes.
            return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _connect_postgres(url: str, *, read_only: bool = False) -> Any:
        import psycopg  # optional dependency; only required in production

        if read_only:
            return psycopg.connect(url, options="-c default_transaction_read_only=on")
        return psycopg.connect(url)

    def _init_schema(self) -> None:
        # Order matters: tables, then column migrations, then indexes -- an
        # index on a column added by a migration must not run before that
        # migration, or it fails against a database where the table already
        # existed pre-migration (see the TABLES_SQL/INDEXES_SQL split above).
        self._execute_script(TABLES_SQL)
        # Notebook tables are brand new and additive (CREATE ... IF NOT
        # EXISTS), so creating them on connect is the whole migration: it's
        # idempotent and never touches match data.
        self._execute_script(EXPERIMENT_TABLES_SQL)
        self._execute_script(SAMPLING_TABLES_SQL)
        self._execute_script(INGEST_RUNS_SQL)
        self._execute_script(SCHEMA_MIGRATIONS_SQL)
        self._run_migrations()
        self._execute_script(INDEXES_SQL)
        self._execute_script(EXPERIMENT_INDEXES_SQL)
        self._execute_script(SAMPLING_INDEXES_SQL)

    def _execute_script(self, sql: str) -> None:
        if self.dialect == "sqlite":
            self.conn.executescript(sql)
        else:
            with self.conn.cursor() as cur:
                cur.execute(sql)
        self.conn.commit()

    def _run_migrations(self) -> None:
        """Bring an older database up to the current schema.

        A fresh database already has every column via `TABLES_SQL` above; this
        only matters for a `matches` table created before a given column
        existed (e.g. a local `data/tftlab.sqlite3` from an earlier build).
        """
        for table, columns in (
            ("matches", _MATCHES_COLUMN_MIGRATIONS),
            ("experiment_field_notes", FIELD_NOTE_COLUMN_MIGRATIONS),
        ):
            for column, column_type in columns:
                self._add_column_if_missing(table, column, column_type)

        # Must run before _backfill_balance_window: it corrects `patch` for
        # rows still holding the pre-fix masked-Unreal fallback value, so
        # that routine sees an up-to-date `patch` (and, for still-unresolved
        # rows, an already-NULL `balance_window` it must leave alone).
        self._backfill_unreal_patches()
        self._backfill_balance_window()
        self._migrate_units_unit_index()
        self._backfill_completed_item_counts()

    def _backfill_completed_item_counts(self) -> None:
        """Recompute every stored `units.completed_item_count` from its
        `items_json` with the current component recognition
        (`tftlab.items.component_ids`) and update only rows that differ.

        Needed because rows ingested before a set's own component ids were
        recognized (Set 18's `DA_Component_*`) counted those components as
        completed items. Runs once per database per component set: the key
        includes `component_ids_version()`, so it is skipped on later
        connects and re-runs automatically if the recognized set changes.
        One transaction (updates + marker); `items_json` and every other
        column are untouched. Only initializing connections get here --
        `Database.open_existing` (the read-only web path) never does.
        """
        key = f"completed_item_count:{component_ids_version()}"
        if self.query_one("SELECT 1 FROM schema_migrations WHERE migration_key = ?", (key,)) is not None:
            return
        updates: list[tuple[int, str, int, int]] = []
        try:
            rows = self.query_all(
                "SELECT match_id, participant_index, unit_index, items_json, completed_item_count FROM units"
            )
            for match_id, participant_index, unit_index, items_json, stored in rows:
                try:
                    items = json.loads(items_json)
                except (TypeError, ValueError):
                    continue  # never guess: leave an unreadable row as stored
                if not isinstance(items, list):
                    continue
                corrected = completed_item_count([str(i) for i in items if i])
                if corrected != stored:
                    updates.append((corrected, match_id, participant_index, unit_index))
            self.executemany(
                "UPDATE units SET completed_item_count = ? "
                "WHERE match_id = ? AND participant_index = ? AND unit_index = ?",
                updates,
            )
            self.execute(
                "INSERT INTO schema_migrations (migration_key, applied_at, rows_changed) VALUES (?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (key, int(time.time() * 1000), len(updates)),
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        self.completed_item_count_backfill = len(updates)

    def _add_column_if_missing(self, table: str, column: str, column_type: str) -> None:
        if self.dialect == "sqlite":
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                self.conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        else:
            with self.conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}")
            self.conn.commit()

    def _units_needs_unit_index_migration(self) -> bool:
        if self.dialect == "sqlite":
            columns = [row[1] for row in self.conn.execute("PRAGMA table_info(units)").fetchall()]
            return "unit_index" not in columns
        row = self.query_one(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'units' AND column_name = 'unit_index'"
        )
        return row is None

    def _migrate_units_unit_index(self) -> None:
        """Upgrade an older `units` table from the
        `(match_id, participant_index, character_id)` primary key to
        `(match_id, participant_index, unit_index)`.

        Real TFT boards can field more than one instance of the same
        champion in one game; the old key rejected the second instance
        outright with a unique-constraint violation on ingest.
        `unit_index` is backfilled deterministically from each row's
        physical insertion order (SQLite `rowid` / Postgres `ctid`), which
        matches the order units were originally enumerated from the
        Match-V1 payload during ingest (see `normalize.normalize_match`) --
        `ingest_match` has always inserted a participant's units in that
        same order and never updates a units row afterward, so that
        physical order is a reliable, deterministic stand-in for the
        original per-participant unit ordering.

        Idempotent (skips entirely once `unit_index` already exists), so
        this runs safely on every `Database` connect, including against a
        production database that already has real match data -- no
        downtime, no wipe/recreate, no data loss.
        """
        if not self._units_needs_unit_index_migration():
            return

        try:
            if self.dialect == "sqlite":
                self.conn.execute(
                    """
                    CREATE TABLE units_new (
                        match_id TEXT NOT NULL,
                        participant_index INTEGER NOT NULL,
                        unit_index INTEGER NOT NULL,
                        character_id TEXT NOT NULL,
                        unit_name TEXT NOT NULL,
                        cost INTEGER,
                        tier INTEGER NOT NULL,
                        items_json TEXT NOT NULL,
                        completed_item_count INTEGER NOT NULL,
                        PRIMARY KEY (match_id, participant_index, unit_index),
                        FOREIGN KEY (match_id, participant_index)
                            REFERENCES participants(match_id, participant_index)
                    )
                    """
                )
                self.conn.execute(
                    """
                    INSERT INTO units_new (
                        match_id, participant_index, unit_index, character_id,
                        unit_name, cost, tier, items_json, completed_item_count
                    )
                    SELECT
                        match_id, participant_index,
                        ROW_NUMBER() OVER (
                            PARTITION BY match_id, participant_index ORDER BY rowid
                        ) - 1,
                        character_id, unit_name, cost, tier, items_json, completed_item_count
                    FROM units
                    """
                )
                self.conn.execute("DROP TABLE units")
                self.conn.execute("ALTER TABLE units_new RENAME TO units")
            else:
                with self.conn.cursor() as cur:
                    cur.execute("ALTER TABLE units ADD COLUMN unit_index INTEGER")
                    cur.execute(
                        """
                        UPDATE units
                        SET unit_index = ranked.rn
                        FROM (
                            SELECT ctid, ROW_NUMBER() OVER (
                                PARTITION BY match_id, participant_index ORDER BY ctid
                            ) - 1 AS rn
                            FROM units
                        ) AS ranked
                        WHERE units.ctid = ranked.ctid
                        """
                    )
                    cur.execute("ALTER TABLE units ALTER COLUMN unit_index SET NOT NULL")
                    cur.execute("ALTER TABLE units DROP CONSTRAINT units_pkey")
                    cur.execute(
                        "ALTER TABLE units ADD PRIMARY KEY (match_id, participant_index, unit_index)"
                    )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _backfill_balance_window(self) -> None:
        """Populate `balance_window` for rows a column migration left NULL.

        `ALTER TABLE ... ADD COLUMN` doesn't compute values for existing rows,
        so a database migrated from before this column existed would
        otherwise have its historical matches permanently unclassifiable by
        balance window -- exactly the "historical matches must stay
        classifiable" requirement this feature exists for.
        """
        rows = self.query_all(
            "SELECT match_id, patch, game_datetime FROM matches WHERE balance_window IS NULL AND patch IS NOT NULL"
        )
        if not rows:
            return
        for match_id, patch, game_datetime in rows:
            if patch == UNRESOLVED_UNREAL_PATCH:
                # Intentionally left unresolved by _backfill_unreal_patches
                # (see resolve_unreal_patch) -- never silently combined into
                # a fake shared balance window just because this generic
                # backfill doesn't recognize the sentinel as "no patch".
                continue
            window = resolve_balance_window(patch, game_datetime)
            if window is not None:
                self.execute("UPDATE matches SET balance_window = ? WHERE match_id = ?", (window, match_id))
        self.commit()

    def _backfill_unreal_patches(self) -> None:
        """Recompute `patch`/`balance_window` for rows whose `patch` column
        still holds the pre-fix masked-Unreal fallback value.

        Before `tftlab.unreal_patch` existed, `patch_from_game_version`
        stored the raw, unparseable `game_version` string itself (e.g.
        `"TFT Unreal Version ?.?.?.?"`) as the "patch" for any match it
        couldn't parse a version out of -- collapsing every such match,
        regardless of its real client patch, into one shared fake balance
        window. Only rows whose CURRENT `patch` value still looks
        Unreal-masked are touched here; a row that already has a genuinely
        parsed patch (e.g. `"14.6"`) is left completely alone, per the
        production-safety requirement that this backfill never touch
        historical rows that were already correct.

        Idempotent and self-healing: a row already holding
        `UNRESOLVED_UNREAL_PATCH` is explicitly re-examined too (not just
        rows with the raw pre-fix masked string), and re-resolved with the
        *current* `UNREAL_PATCH_REGISTRY` on every connect. That's
        deliberate: once real, verified cutover windows are added to the
        registry, previously unresolved production rows correctly pick up
        their real patch on the very next connect, with no separate
        one-off re-migration step needed. A row already holding a real
        resolved patch (e.g. `"14.6"`) is never revisited -- it matches
        neither the masked-placeholder shape nor the sentinel.
        """
        rows = self.query_all("SELECT match_id, patch, game_version, game_datetime FROM matches WHERE patch IS NOT NULL")
        for match_id, patch, game_version, game_datetime in rows:
            if not (is_masked_unreal_version(patch) or patch == UNRESOLVED_UNREAL_PATCH):
                continue
            new_patch = patch_from_game_version(game_version, game_datetime)
            new_window = (
                None if new_patch == UNRESOLVED_UNREAL_PATCH else resolve_balance_window(new_patch, game_datetime)
            )
            self.execute(
                "UPDATE matches SET patch = ?, balance_window = ? WHERE match_id = ?",
                (new_patch, new_window, match_id),
            )
        self.commit()

    def _translate(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one statement and return its cursor. Callers should not commit
        themselves for writes made through `ingest_match`/`ingest_many`; use
        `commit()` directly for standalone writes."""
        translated = self._translate(sql)
        if self.dialect == "sqlite":
            return self.conn.execute(translated, params)
        cur = self.conn.cursor()
        cur.execute(translated, params)
        return cur

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement for many parameter rows. On Postgres, psycopg
        pipelines these into a few round trips instead of one per row, which
        is what keeps remote-database ingestion time bounded. Same rows,
        same order, same transaction as calling `execute` per row."""
        if not rows:
            return
        translated = self._translate(sql)
        if self.dialect == "sqlite":
            self.conn.executemany(translated, rows)
            return
        with self.conn.cursor() as cur:
            cur.executemany(translated, rows)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        return self.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self.execute(sql, params).fetchall()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def seed_last_sampled(self) -> dict[str, int]:
        """Sampling ledger: PUUID -> when it was last sampled (epoch ms),
        counting only runs that completed (`ingest_runs.status =
        'completed'`). Rows from an interrupted run, or with no
        `ingest_runs` row at all, are kept but ignored."""
        rows = self.query_all(
            "SELECT s.puuid, MAX(s.sampled_at) FROM seed_samples s "
            "JOIN ingest_runs r ON r.run_id = s.run_id "
            "WHERE r.status = ? AND r.completed_at IS NOT NULL GROUP BY s.puuid",
            (RUN_COMPLETED,),
        )
        return {p: int(t) for p, t in rows}

    def start_ingest_run(self, run_id: str, started_at: int) -> None:
        """Register `run_id` as started (not yet counted for rotation). A
        run id is used once; reusing one is an error."""
        if self.query_one("SELECT 1 FROM ingest_runs WHERE run_id = ?", (run_id,)) is not None:
            raise ValueError(f"ingest run {run_id!r} already exists")
        self.execute(
            "INSERT INTO ingest_runs (run_id, started_at, status) VALUES (?, ?, ?)",
            (run_id, started_at, RUN_STARTED),
        )
        self.commit()

    def finalize_ingest_run(
        self,
        run_id: str,
        seeds: Sequence[tuple[str, str]],
        discoveries: Sequence[tuple[str, str, str]],
        *,
        sampled_at: int,
        completed_at: int,
    ) -> None:
        """Atomically write the run's ledger rows and provenance rows and
        mark it completed -- one transaction, so either all of it is
        visible to seed rotation or none of it is."""
        try:
            self._insert_seed_samples(run_id, seeds, sampled_at)
            self._insert_match_discoveries(run_id, discoveries, sampled_at)
            updated = self.execute(
                "UPDATE ingest_runs SET status = ?, completed_at = ? WHERE run_id = ? AND status = ?",
                (RUN_COMPLETED, completed_at, run_id, RUN_STARTED),
            ).rowcount
            if updated != 1:
                raise RuntimeError(f"ingest run {run_id!r} is not in the started state")
        except Exception:
            self.conn.rollback()
            raise
        self.commit()

    def mark_ingest_run_failed(self, run_id: str, failure: str) -> None:
        """Best effort: record that `run_id` failed (exception type only).
        It stays incomplete either way; this never raises."""
        try:
            self.conn.rollback()
            self.execute(
                "UPDATE ingest_runs SET status = ?, failure = ? WHERE run_id = ? AND status = ?",
                (RUN_FAILED, failure[:200], run_id, RUN_STARTED),
            )
            self.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass

    def _insert_seed_samples(self, run_id: str, seeds: Sequence[tuple[str, str]], sampled_at: int) -> None:
        self.executemany(
            "INSERT INTO seed_samples (run_id, puuid, cohort, sampled_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [(run_id, puuid, cohort, sampled_at) for puuid, cohort in seeds],
        )

    def _insert_match_discoveries(
        self, run_id: str, discoveries: Sequence[tuple[str, str, str]], discovered_at: int
    ) -> None:
        self.executemany(
            "INSERT INTO match_discoveries (match_id, run_id, puuid, cohort, discovered_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [(match_id, run_id, puuid, cohort, discovered_at) for match_id, puuid, cohort in discoveries],
        )

    def record_seed_samples(self, run_id: str, seeds: Sequence[tuple[str, str]], sampled_at: int) -> None:
        """Ledger rows for `(puuid, cohort)` seeds sampled in `run_id`
        (standalone; ingest uses `finalize_ingest_run`)."""
        self._insert_seed_samples(run_id, seeds, sampled_at)
        self.commit()

    def record_match_discoveries(
        self, run_id: str, discoveries: Sequence[tuple[str, str, str]], discovered_at: int
    ) -> None:
        """Provenance rows for `(match_id, puuid, cohort)`: which seed (and
        its cohort) surfaced which stored match in `run_id` (standalone;
        ingest uses `finalize_ingest_run`)."""
        self._insert_match_discoveries(run_id, discoveries, discovered_at)
        self.commit()

    def has_match(self, match_id: str) -> bool:
        return self.query_one("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)) is not None

    def ingest_match(self, payload: dict[str, Any], *, cost_lookup: CostLookup | None = None) -> bool:
        normalized = normalize_match(payload, cost_lookup=cost_lookup)
        if self.has_match(normalized.match_id):
            return False

        try:
            self.execute(
                """INSERT INTO matches(
                    match_id, game_datetime, game_version, patch, balance_window, game_type,
                    queue_id, set_number, set_core_name, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized.match_id,
                    normalized.game_datetime,
                    normalized.game_version,
                    normalized.patch,
                    normalized.balance_window,
                    normalized.game_type,
                    normalized.queue_id,
                    normalized.set_number,
                    normalized.set_core_name,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

            participant_rows: list[tuple[Any, ...]] = []
            unit_rows: list[tuple[Any, ...]] = []
            trait_rows: list[tuple[Any, ...]] = []
            for p in normalized.participants:
                participant_rows.append(
                    (p.match_id, p.participant_index, p.placement, p.level, json.dumps(p.augments))
                )
                for u in p.units:
                    unit_rows.append(
                        (
                            p.match_id,
                            p.participant_index,
                            u.unit_index,
                            u.character_id,
                            u.name,
                            u.cost,
                            u.tier,
                            json.dumps(u.items),
                            u.completed_item_count,
                        )
                    )
                for trait in p.traits:
                    name = str(trait.get("name") or "")
                    if not name:
                        continue
                    trait_rows.append(
                        (
                            p.match_id,
                            p.participant_index,
                            name,
                            int(trait.get("num_units") or 0),
                            trait.get("style"),
                            trait.get("tier_current"),
                            trait.get("tier_total"),
                        )
                    )
            # Batched per table (participants before the units/traits that
            # reference them), still inside this match's one transaction.
            self.executemany("INSERT INTO participants VALUES (?, ?, ?, ?, ?)", participant_rows)
            self.executemany(
                # Explicit column list, not positional VALUES: a Postgres
                # database migrated from the old schema (see
                # `_migrate_units_unit_index`) has `unit_index` as its LAST
                # physical column (Postgres's `ALTER TABLE ADD COLUMN` always
                # appends), not third -- positional VALUES would silently
                # insert into the wrong columns on a migrated table even
                # though it works on a freshly created one.
                """INSERT INTO units (
                    match_id, participant_index, unit_index, character_id,
                    unit_name, cost, tier, items_json, completed_item_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                unit_rows,
            )
            # Row by row within the batch, so a trait repeated for the same
            # participant still upserts exactly as before.
            self.executemany(_TRAIT_UPSERT_SQL[self.dialect], trait_rows)
        except Exception:
            self.conn.rollback()
            raise

        self.commit()
        return True

    def ingest_many(self, payloads: Iterable[dict[str, Any]], *, cost_lookup: CostLookup | None = None) -> int:
        inserted = 0
        for payload in payloads:
            inserted += int(self.ingest_match(payload, cost_lookup=cost_lookup))
        return inserted
