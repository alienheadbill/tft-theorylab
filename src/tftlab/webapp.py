from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analytics import carry_commitment_stats
from .demo import generate_demo_matches
from .storage import Database

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"


def _db_path() -> Path:
    return Path(os.getenv("TFT_DB_PATH", "data/tftlab.sqlite3"))


def _ensure_demo_db(path: Path) -> tuple[Path, bool]:
    """Use live DB when it has participants; otherwise create a deterministic demo DB."""
    try:
        if path.exists():
            with Database(path) as db:
                n = db.conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
                if n:
                    return path, False
    except Exception:
        pass

    demo = Path(os.getenv("TFT_DEMO_DB_PATH", "data/web-demo.sqlite3"))
    rebuild = True
    if demo.exists():
        try:
            with Database(demo) as db:
                rebuild = db.conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
        except Exception:
            rebuild = True
    if rebuild:
        if demo.exists():
            demo.unlink()
        with Database(demo) as db:
            db.ingest_many(generate_demo_matches(180))
    return demo, True


def create_app() -> FastAPI:
    app = FastAPI(title="TFT Theory Lab", version="0.2.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        path, demo = _ensure_demo_db(_db_path())
        with Database(path) as db:
            participants = db.conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
            matches = db.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        return {"ok": True, "demo": demo, "matches": matches, "participants": participants}

    @app.get("/api/carries")
    def carries(
        max_cost: int = Query(3, ge=1, le=5),
        min_samples: int = Query(10, ge=1, le=100000),
    ) -> dict[str, object]:
        path, demo = _ensure_demo_db(_db_path())
        with Database(path) as db:
            stats = carry_commitment_stats(
                db.conn,
                min_cost=1,
                max_cost=max_cost,
                min_samples=min_samples,
            )
            matches = db.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            participants = db.conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        return {
            "demo": demo,
            "matches": matches,
            "participants": participants,
            "carries": [asdict(s) for s in stats],
        }

    @app.get("/api/carries/{character_id}")
    def carry_detail(character_id: str) -> dict[str, object]:
        path, demo = _ensure_demo_db(_db_path())
        with Database(path) as db:
            stats = carry_commitment_stats(db.conn, min_cost=1, max_cost=5, min_samples=1)
            selected = next((s for s in stats if s.character_id == character_id), None)
            if selected is None:
                raise HTTPException(status_code=404, detail="Carry not found")

            rows = db.conn.execute(
                """
                SELECT f.unit_name, f.cost, COUNT(*) AS together,
                       AVG(p.placement * 1.0) AS avg_place,
                       AVG(CASE WHEN p.placement <= 4 THEN 1.0 ELSE 0.0 END) AS top4
                FROM units c
                JOIN participants p
                  ON p.match_id = c.match_id AND p.participant_index = c.participant_index
                JOIN units f
                  ON f.match_id = c.match_id AND f.participant_index = c.participant_index
                WHERE c.character_id = ?
                  AND c.completed_item_count >= 2
                  AND f.character_id <> c.character_id
                GROUP BY f.character_id, f.unit_name, f.cost
                HAVING together >= 3
                ORDER BY top4 DESC, together DESC
                LIMIT 8
                """,
                (character_id,),
            ).fetchall()

            item_rows = db.conn.execute(
                """
                SELECT items_json, COUNT(*) AS games,
                       AVG(p.placement * 1.0) AS avg_place,
                       AVG(CASE WHEN p.placement <= 4 THEN 1.0 ELSE 0.0 END) AS top4
                FROM units u
                JOIN participants p
                  ON p.match_id = u.match_id AND p.participant_index = u.participant_index
                WHERE u.character_id = ? AND u.completed_item_count >= 2
                GROUP BY items_json
                ORDER BY games DESC, top4 DESC
                LIMIT 5
                """,
                (character_id,),
            ).fetchall()

        import json
        return {
            "demo": demo,
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
