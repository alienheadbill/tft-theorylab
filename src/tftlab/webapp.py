from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analytics import carry_commitment_stats, default_patch
from .demo import generate_demo_matches
from .storage import Database

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"


def _sqlite_db_path() -> Path:
    return Path(os.getenv("TFT_DB_PATH", "data/tftlab.sqlite3"))


def _has_participants(db: Database) -> bool:
    row = db.query_one("SELECT COUNT(*) FROM participants")
    return bool(row and row[0])


def _open_if_live(target: Path | str) -> Database | None:
    """Open `target` and return it only if it's reachable and has real data."""
    try:
        db = Database(target)
    except Exception:
        return None
    try:
        if _has_participants(db):
            return db
    except Exception:
        pass
    db.close()
    return None


def _build_demo_db() -> Database:
    demo_path = Path(os.getenv("TFT_DEMO_DB_PATH", "data/web-demo.sqlite3"))
    rebuild = True
    if demo_path.exists():
        try:
            with Database(demo_path) as existing:
                rebuild = not _has_participants(existing)
        except Exception:
            rebuild = True
    if rebuild:
        if demo_path.exists():
            demo_path.unlink()
        with Database(demo_path) as db:
            db.ingest_many(generate_demo_matches(180))
    return Database(demo_path)


def _resolve_database() -> tuple[Database, bool]:
    """Pick the active data source.

    Prefers a populated `DATABASE_URL` (production Postgres), then a
    populated local SQLite file, and only falls back to the deterministic
    demo dataset when neither has real match data. Callers must close the
    returned `Database`.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        db = _open_if_live(database_url)
        if db is not None:
            return db, False

    sqlite_path = _sqlite_db_path()
    if sqlite_path.exists():
        db = _open_if_live(sqlite_path)
        if db is not None:
            return db, False

    return _build_demo_db(), True


def create_app() -> FastAPI:
    app = FastAPI(title="TFT Theory Lab", version="0.2.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            participants = db.query_one("SELECT COUNT(*) FROM participants")[0]
            matches = db.query_one("SELECT COUNT(*) FROM matches")[0]
            patch = default_patch(db)
        return {
            "ok": True,
            "demo": demo,
            "backend": db.dialect,
            "patch": patch,
            "matches": matches,
            "participants": participants,
        }

    @app.get("/api/carries")
    def carries(
        max_cost: int = Query(3, ge=1, le=5),
        min_samples: int = Query(10, ge=1, le=100000),
        patch: str | None = Query(None, description="Restrict to one balance patch; defaults to the most-played patch in the store."),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_patch = patch or default_patch(db)
            stats = carry_commitment_stats(
                db,
                patch=resolved_patch,
                min_cost=1,
                max_cost=max_cost,
                min_samples=min_samples,
            )
            matches = db.query_one("SELECT COUNT(*) FROM matches")[0]
            participants = db.query_one("SELECT COUNT(*) FROM participants")[0]
        return {
            "demo": demo,
            "backend": db.dialect,
            "patch": resolved_patch,
            "matches": matches,
            "participants": participants,
            "carries": [asdict(s) for s in stats],
        }

    @app.get("/api/carries/{character_id}")
    def carry_detail(character_id: str, patch: str | None = Query(None)) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_patch = patch or default_patch(db)
            stats = carry_commitment_stats(
                db, patch=resolved_patch, min_cost=1, max_cost=5, min_samples=1
            )
            selected = next((s for s in stats if s.character_id == character_id), None)
            if selected is None:
                raise HTTPException(status_code=404, detail="Carry not found")

            rows = db.query_all(
                """
                SELECT f.unit_name, f.cost, COUNT(*) AS together,
                       AVG(p.placement * 1.0) AS avg_place,
                       AVG(CASE WHEN p.placement <= 4 THEN 1.0 ELSE 0.0 END) AS top4
                FROM units c
                JOIN participants p
                  ON p.match_id = c.match_id AND p.participant_index = c.participant_index
                JOIN units f
                  ON f.match_id = c.match_id AND f.participant_index = c.participant_index
                JOIN matches m
                  ON m.match_id = c.match_id
                WHERE c.character_id = ?
                  AND c.completed_item_count >= 2
                  AND f.character_id <> c.character_id
                  AND m.patch = ?
                GROUP BY f.character_id, f.unit_name, f.cost
                HAVING together >= 3
                ORDER BY top4 DESC, together DESC
                LIMIT 8
                """,
                (character_id, resolved_patch),
            )

            item_rows = db.query_all(
                """
                SELECT items_json, COUNT(*) AS games,
                       AVG(p.placement * 1.0) AS avg_place,
                       AVG(CASE WHEN p.placement <= 4 THEN 1.0 ELSE 0.0 END) AS top4
                FROM units u
                JOIN participants p
                  ON p.match_id = u.match_id AND p.participant_index = u.participant_index
                JOIN matches m
                  ON m.match_id = u.match_id
                WHERE u.character_id = ? AND u.completed_item_count >= 2
                  AND m.patch = ?
                GROUP BY items_json
                ORDER BY games DESC, top4 DESC
                LIMIT 5
                """,
                (character_id, resolved_patch),
            )

        return {
            "demo": demo,
            "backend": db.dialect,
            "patch": resolved_patch,
            "carry": asdict(selected),
            "partners": [
                {
                    "name": r[0],
                    "cost": r[1],
                    "games": r[2],
                    "avg_placement": r[3],
                    "top4_rate": r[4],
                }
                for r in rows
            ],
            "item_sets": [
                {
                    "items": json.loads(r[0]),
                    "games": r[1],
                    "avg_placement": r[2],
                    "top4_rate": r[3],
                }
                for r in item_rows
            ],
        }

    return app


app = create_app()
