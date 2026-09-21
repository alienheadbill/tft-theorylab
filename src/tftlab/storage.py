from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .normalize import CostLookup, normalize_match

# Written once, uniformly, using SQLite's `?` placeholder style. The Postgres
# path translates `?` to `%s` before executing (see `Database._translate`),
# so analytics/ingest code never needs to branch on backend.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    game_datetime BIGINT,
    game_version TEXT,
    patch TEXT,
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
    character_id TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    cost INTEGER,
    tier INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    completed_item_count INTEGER NOT NULL,
    PRIMARY KEY (match_id, participant_index, character_id),
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

CREATE INDEX IF NOT EXISTS idx_units_character ON units(character_id);
CREATE INDEX IF NOT EXISTS idx_units_cost ON units(cost);
CREATE INDEX IF NOT EXISTS idx_participants_placement ON participants(placement);
CREATE INDEX IF NOT EXISTS idx_matches_patch ON matches(patch);
"""

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

    def __init__(self, target: Path | str) -> None:
        target_str = str(target)
        if _is_postgres_url(target_str):
            self.dialect = "postgres"
            self.path: Path | None = None
            self.conn = self._connect_postgres(target_str)
        else:
            self.dialect = "sqlite"
            self.path = Path(target)
            self.conn = self._connect_sqlite(self.path)
        self._init_schema()

    @staticmethod
    def _connect_sqlite(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _connect_postgres(url: str) -> Any:
        import psycopg  # optional dependency; only required in production

        return psycopg.connect(url)

    def _init_schema(self) -> None:
        if self.dialect == "sqlite":
            self.conn.executescript(SCHEMA_SQL)
        else:
            with self.conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        self.conn.commit()
        self._run_migrations()

    def _run_migrations(self) -> None:
        """Bring an older database up to the current schema.

        A fresh database already has every column via `SCHEMA_SQL` above; this
        only matters for a `matches` table created before the `patch` column
        existed (e.g. a local `data/tftlab.sqlite3` from an earlier build).
        """
        if self.dialect == "sqlite":
            try:
                self.conn.execute("ALTER TABLE matches ADD COLUMN patch TEXT")
                self.conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        else:
            with self.conn.cursor() as cur:
                cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS patch TEXT")
            self.conn.commit()

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

    def has_match(self, match_id: str) -> bool:
        return self.query_one("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)) is not None

    def ingest_match(self, payload: dict[str, Any], *, cost_lookup: CostLookup | None = None) -> bool:
        normalized = normalize_match(payload, cost_lookup=cost_lookup)
        if self.has_match(normalized.match_id):
            return False

        try:
            self.execute(
                """INSERT INTO matches(
                    match_id, game_datetime, game_version, patch, game_type, queue_id,
                    set_number, set_core_name, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized.match_id,
                    normalized.game_datetime,
                    normalized.game_version,
                    normalized.patch,
                    normalized.game_type,
                    normalized.queue_id,
                    normalized.set_number,
                    normalized.set_core_name,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

            for p in normalized.participants:
                self.execute(
                    "INSERT INTO participants VALUES (?, ?, ?, ?, ?)",
                    (p.match_id, p.participant_index, p.placement, p.level, json.dumps(p.augments)),
                )
                for u in p.units:
                    self.execute(
                        "INSERT INTO units VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            p.match_id,
                            p.participant_index,
                            u.character_id,
                            u.name,
                            u.cost,
                            u.tier,
                            json.dumps(u.items),
                            u.completed_item_count,
                        ),
                    )
                for trait in p.traits:
                    name = str(trait.get("name") or "")
                    if not name:
                        continue
                    self.execute(
                        _TRAIT_UPSERT_SQL[self.dialect],
                        (
                            p.match_id,
                            p.participant_index,
                            name,
                            int(trait.get("num_units") or 0),
                            trait.get("style"),
                            trait.get("tier_current"),
                            trait.get("tier_total"),
                        ),
                    )
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
