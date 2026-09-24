from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .analytics import (
    CANONICAL_UNIT_TIEBREAK_SQL,
    available_balance_windows,
    carry_commitment_stats,
    carry_partner_associations,
    default_balance_window,
    discover_candidates,
    discovery_candidate_for,
    item_package_stats,
    trait_breakpoint_associations,
)
from .demo import generate_demo_matches
from .experiments import ExperimentNotFound, get_experiment, list_experiments, seed_demo_experiments
from .game_art import enrich_candidate, experiment_art, field_note_art
from .storage import Database
from .scout import comp_fingerprint
from .sources import scout_checklist
from .unreal_patch import UNRESOLVED_UNREAL_PATCH

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"


class ProductionDatabaseUnavailable(RuntimeError):
    """`DATABASE_URL` is configured but the database could not be reached.

    Deliberately distinct from "no production database configured, use
    demo data" -- once an operator has pointed the app at a real database,
    a connection failure there is a production incident to surface loudly
    (see the exception handler below), not something to paper over with
    the synthetic demo dataset.
    """


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
    db = Database(demo_path)
    # Example notebook entries live only in this local demo database, never in
    # a real one. No-op once any experiment exists.
    seed_demo_experiments(db)
    return db


def _resolve_database() -> tuple[Database, bool]:
    """Pick the active data source.

    When `DATABASE_URL` is set, it is *always* used -- connecting
    successfully is enough, even with zero matches so far (a fresh
    production database before first ingest is a normal, honest state, not
    something to disguise as demo data). If it's configured but unreachable,
    this raises `ProductionDatabaseUnavailable` rather than silently falling
    through to demo data; the exception handler registered on the app turns
    that into an explicit 503, per the same "never quietly pretend
    everything is fine" rule.

    Only when `DATABASE_URL` is unset at all does this fall back to a
    populated local SQLite file, and finally the deterministic demo
    dataset -- both of which are fine for local development, where there
    was never a production database to fail. Callers must close the
    returned `Database`.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            db = Database(database_url)
        except Exception as exc:
            raise ProductionDatabaseUnavailable(
                f"DATABASE_URL is configured but unreachable ({type(exc).__name__})"
            ) from exc
        return db, False

    sqlite_path = _sqlite_db_path()
    if sqlite_path.exists():
        db = _open_if_live(sqlite_path)
        if db is not None:
            return db, False

    return _build_demo_db(), True


def carry_partners(db: Database, character_id: str, balance_window: str) -> list[tuple[Any, ...]]:
    """Champions that most often finish on the same board as a committed `character_id`.

    Extracted from the route handler so the Postgres-vs-SQLite `HAVING`
    behavior (Postgres rejects a SELECT alias there; SQLite allows it) has a
    unit test independent of the FastAPI/env-based database resolution.

    A board can field more than one committed instance of `character_id`, or
    more than one instance of a given partner, in the same game; both CTEs
    below `DISTINCT`-deduplicate to one row per (game, partner) pair first,
    so `COUNT(*)`/the placement averages reflect games, not raw unit-row
    combinations -- a naive join here would otherwise double- (or more-)
    count a game for every extra instance on either side.
    """
    return db.query_all(
        """
        WITH carry_games AS (
            SELECT DISTINCT c.match_id, c.participant_index
            FROM units c
            JOIN matches m ON m.match_id = c.match_id
            WHERE c.character_id = ? AND c.completed_item_count >= 2 AND m.balance_window = ?
        ),
        partner_games AS (
            SELECT DISTINCT cg.match_id, cg.participant_index, f.character_id, f.unit_name, f.cost
            FROM carry_games cg
            JOIN units f ON f.match_id = cg.match_id AND f.participant_index = cg.participant_index
            WHERE f.character_id <> ?
        )
        SELECT pg.unit_name, pg.cost, COUNT(*) AS together,
               AVG(p.placement * 1.0) AS avg_place,
               AVG(CASE WHEN p.placement <= 4 THEN 1.0 ELSE 0.0 END) AS top4
        FROM partner_games pg
        JOIN participants p
          ON p.match_id = pg.match_id AND p.participant_index = pg.participant_index
        GROUP BY pg.character_id, pg.unit_name, pg.cost
        HAVING COUNT(*) >= 3
        ORDER BY top4 DESC, together DESC
        LIMIT 8
        """,
        (character_id, balance_window, character_id),
    )


def carry_item_sets(db: Database, character_id: str, balance_window: str) -> list[tuple[Any, ...]]:
    """A board can field more than one committed instance of `character_id`
    in the same game; `ranked`/`rn = 1` picks exactly one canonical instance
    per game (`CANONICAL_UNIT_TIEBREAK_SQL`) so that game contributes one
    item-set observation, not one per instance."""
    return db.query_all(
        f"""
        WITH ranked AS (
            SELECT
                u.items_json, p.placement,
                ROW_NUMBER() OVER (
                    PARTITION BY u.match_id, u.participant_index
                    ORDER BY {CANONICAL_UNIT_TIEBREAK_SQL}
                ) AS rn
            FROM units u
            JOIN participants p
              ON p.match_id = u.match_id AND p.participant_index = u.participant_index
            JOIN matches m
              ON m.match_id = u.match_id
            WHERE u.character_id = ? AND u.completed_item_count >= 2
              AND m.balance_window = ?
        )
        SELECT items_json, COUNT(*) AS games,
               AVG(placement * 1.0) AS avg_place,
               AVG(CASE WHEN placement <= 4 THEN 1.0 ELSE 0.0 END) AS top4
        FROM ranked
        WHERE rn = 1
        GROUP BY items_json
        ORDER BY games DESC, top4 DESC
        LIMIT 5
        """,
        (character_id, balance_window),
    )


def _experiment_with_art(entry, *, include_field_notes: bool = False) -> dict[str, Any]:
    """An experiment's API body plus local game-art URLs (additive keys;
    every URL is under /static/game/ or None)."""
    body = entry.to_api(include_field_notes=include_field_notes)
    art = experiment_art(entry)
    body["art"] = art
    if body["carry"] is not None:
        body["carry"]["art_url"] = art["carry"]
    for note in body.get("field_notes") or []:
        note["art"] = field_note_art(note)
    return body


def create_app() -> FastAPI:
    app = FastAPI(title="TFT Theory Lab", version="0.2.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(ProductionDatabaseUnavailable)
    async def _production_database_unavailable(request: Request, exc: ProductionDatabaseUnavailable) -> JSONResponse:
        # Deliberately no exception detail beyond the exception's own generic
        # message (which never includes the DATABASE_URL itself, only the
        # failing exception's type) -- never echo connection strings/credentials.
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "demo": False,
                "backend": "postgres",
                "status": "error",
                "error": "DATABASE_URL is configured but the database is unreachable",
            },
        )

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/experiments", include_in_schema=False)
    @app.get("/experiments/{key}", include_in_schema=False)
    def experiments_page(key: str | None = None) -> FileResponse:
        # One page for both views; experiments.js reads the path and fetches
        # the list or a single entry from the read-only API below.
        return FileResponse(WEB_DIR / "experiments.html")

    # The notebook is read-only on the web: there are no accounts yet, so
    # entries are only written through the owner's `tftlab experiment-*` CLI.
    @app.get("/api/experiments")
    def experiments(
        status: str | None = Query(None, description="THEORYCRAFTED, VARIANT or OBSERVED"),
        lifecycle: str | None = Query(None, description="idea, testing, watching or archived"),
        carry: str | None = Query(None, description="Carry name or character_id"),
        tag: str | None = Query(None),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            entries = list_experiments(db, evidence_status=status, lifecycle=lifecycle, carry=carry, tag=tag)
        return {"demo": demo, "count": len(entries), "experiments": [_experiment_with_art(e) for e in entries]}

    @app.get("/api/experiments/{key}")
    def experiment_detail(key: str) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            try:
                entry = get_experiment(db, key)
            except ExperimentNotFound:
                raise HTTPException(status_code=404, detail="Experiment not found") from None
        body = _experiment_with_art(entry, include_field_notes=True)
        # Read-only extras for the page: the normalized fingerprint and which
        # sources the research log actually has notes from.
        body["fingerprint"] = comp_fingerprint(entry)
        body["scout_checklist"] = scout_checklist(entry.field_notes)
        return {"demo": demo, "experiment": body}

    @app.get("/api/health")
    def health() -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            participants = db.query_one("SELECT COUNT(*) FROM participants")[0]
            matches = db.query_one("SELECT COUNT(*) FROM matches")[0]
            balance_window = default_balance_window(db)
        return {
            "ok": True,
            "demo": demo,
            "backend": db.dialect,
            "balance_window": balance_window,
            "matches": matches,
            "participants": participants,
        }

    @app.get("/api/balance-windows")
    def balance_windows() -> dict[str, object]:
        """Every balance window actually present in the store, so the
        frontend's window selector only ever offers real, resolvable
        windows -- never the Unreal-unresolved sentinel, which always has a
        `NULL` balance_window and is therefore already excluded by
        `available_balance_windows`'s own `WHERE balance_window IS NOT NULL`.

        `unresolved_unreal_matches` is exposed separately, purely so the UI
        can show a small, honest note when intentionally-unresolved
        transition-period matches exist -- it must never be offered as a
        selectable window itself.
        """
        db, demo = _resolve_database()
        with db:
            windows = available_balance_windows(db)
            unresolved_unreal_matches = db.query_one(
                "SELECT COUNT(*) FROM matches WHERE patch = ?", (UNRESOLVED_UNREAL_PATCH,)
            )[0]
        return {
            "demo": demo,
            "backend": db.dialect,
            "default_balance_window": windows[0][0] if windows else None,
            "windows": [
                {"balance_window": w, "matches": n, "latest_game_datetime": latest}
                for w, n, latest in windows
            ],
            "unresolved_unreal_matches": unresolved_unreal_matches,
        }

    @app.get("/api/carries")
    def carries(
        max_cost: int = Query(3, ge=1, le=5),
        min_samples: int = Query(10, ge=1, le=100000),
        balance_window: str | None = Query(
            None, description="Restrict to one balance window; defaults to the latest one in the store."
        ),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            stats = carry_commitment_stats(
                db,
                balance_window=resolved_window,
                min_cost=1,
                max_cost=max_cost,
                min_samples=min_samples,
            )
            matches = db.query_one("SELECT COUNT(*) FROM matches")[0]
            participants = db.query_one("SELECT COUNT(*) FROM participants")[0]
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "matches": matches,
            "participants": participants,
            "carries": [asdict(s) for s in stats],
        }

    @app.get("/api/carries/{character_id}")
    def carry_detail(character_id: str, balance_window: str | None = Query(None)) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            stats = carry_commitment_stats(
                db, balance_window=resolved_window, min_cost=1, max_cost=5, min_samples=1
            )
            selected = next((s for s in stats if s.character_id == character_id), None)
            if selected is None:
                raise HTTPException(status_code=404, detail="Carry not found")

            rows = carry_partners(db, character_id, resolved_window)
            item_rows = carry_item_sets(db, character_id, resolved_window)

        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
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

    def _require_carry(db: Database, character_id: str, balance_window: str) -> None:
        stats = carry_commitment_stats(db, balance_window=balance_window, min_cost=1, max_cost=5, min_samples=1)
        if not any(s.character_id == character_id for s in stats):
            raise HTTPException(status_code=404, detail="Carry not found")

    @app.get("/api/carries/{character_id}/partners")
    def carry_partners_endpoint(
        character_id: str,
        balance_window: str | None = Query(None),
        min_games: int = Query(1, ge=1),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            if resolved_window is None:
                raise HTTPException(status_code=404, detail="No data available")
            _require_carry(db, character_id, resolved_window)
            associations = carry_partner_associations(db, character_id, resolved_window, min_games=min_games)
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "character_id": character_id,
            "partners": [asdict(a) for a in associations],
        }

    @app.get("/api/carries/{character_id}/items")
    def carry_items_endpoint(
        character_id: str,
        balance_window: str | None = Query(None),
        min_pair_games: int = Query(2, ge=1),
        min_package_games: int = Query(2, ge=1),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            if resolved_window is None:
                raise HTTPException(status_code=404, detail="No data available")
            _require_carry(db, character_id, resolved_window)
            stats = item_package_stats(
                db,
                character_id,
                resolved_window,
                min_pair_games=min_pair_games,
                min_package_games=min_package_games,
            )
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "character_id": character_id,
            "items": [asdict(a) for a in stats["items"]],
            "pairs": [asdict(a) for a in stats["pairs"]],
            "packages": [asdict(a) for a in stats["packages"]],
        }

    @app.get("/api/carries/{character_id}/traits")
    def carry_traits_endpoint(
        character_id: str,
        balance_window: str | None = Query(None),
        min_games: int = Query(2, ge=1),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            if resolved_window is None:
                raise HTTPException(status_code=404, detail="No data available")
            _require_carry(db, character_id, resolved_window)
            associations = trait_breakpoint_associations(
                db, character_id, resolved_window, min_games=min_games
            )
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "character_id": character_id,
            "traits": [asdict(a) for a in associations],
        }

    @app.get("/api/discovery")
    def discovery(
        max_cost: int = Query(3, ge=1, le=5),
        min_samples: int = Query(10, ge=1, le=100000),
        balance_window: str | None = Query(None),
        top_n: int = Query(5, ge=1, le=20, description="Best partners/items/traits kept per candidate"),
        limit: int = Query(20, ge=1, le=200),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            candidates = discover_candidates(
                db,
                balance_window=resolved_window,
                min_cost=1,
                max_cost=max_cost,
                min_samples=min_samples,
                top_n=top_n,
            )[:limit]
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "candidates": [enrich_candidate(asdict(c)) for c in candidates],
        }

    @app.get("/api/discovery/{character_id}")
    def discovery_detail(
        character_id: str,
        balance_window: str | None = Query(None),
        top_n: int = Query(8, ge=1, le=20),
    ) -> dict[str, object]:
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            candidate = None
            if resolved_window is not None:
                candidate = discovery_candidate_for(
                    db, character_id, balance_window=resolved_window, top_n=top_n
                )
        if candidate is None:
            raise HTTPException(status_code=404, detail="Carry not found")
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "candidate": enrich_candidate(asdict(candidate)),
        }

    return app


app = create_app()
