from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .normalize import normalize_match


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    game_datetime INTEGER,
    game_version TEXT,
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
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def has_match(self, match_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        return row is not None

    def ingest_match(self, payload: dict[str, Any]) -> bool:
        metadata = payload.get("metadata", {})
        info = payload.get("info", {})
        match_id = str(metadata.get("match_id") or "")
        if not match_id:
            raise ValueError("Match payload has no metadata.match_id")
        if self.has_match(match_id):
            return False

        participants = normalize_match(payload)
        with self.conn:
            self.conn.execute(
                """INSERT INTO matches(
                    match_id, game_datetime, game_version, game_type, queue_id,
                    set_number, set_core_name, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    info.get("game_datetime"),
                    info.get("game_version"),
                    info.get("tft_game_type"),
                    info.get("queue_id"),
                    info.get("tft_set_number"),
                    info.get("tft_set_core_name"),
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

            for p in participants:
                self.conn.execute(
                    "INSERT INTO participants VALUES (?, ?, ?, ?, ?)",
                    (
                        p.match_id,
                        p.participant_index,
                        p.placement,
                        p.level,
                        json.dumps(p.augments),
                    ),
                )
                for u in p.units:
                    self.conn.execute(
                        """INSERT INTO units VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    self.conn.execute(
                        "INSERT OR REPLACE INTO traits VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        return True

    def ingest_many(self, payloads: Iterable[dict[str, Any]]) -> int:
        inserted = 0
        for payload in payloads:
            inserted += int(self.ingest_match(payload))
        return inserted
