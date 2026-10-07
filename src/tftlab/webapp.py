from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .analytics import (
    CANONICAL_UNIT_TIEBREAK_SQL,
    available_balance_windows,
    carry_commitment_stats,
    carry_partner_associations,
    default_balance_window,
    discover_candidates,
    discovery_candidate_for,
    discovery_population,
    item_package_stats,
    trait_count_associations,
)
from .carry import carry_commitment_sql
from .champion_investigation import (
    OBSERVED,
    champion_directory,
    champion_investigation,
    resolve_champion,
)
from .demo import generate_demo_matches
from .experiments import ExperimentNotFound, get_experiment, list_experiments, seed_demo_experiments
from .game_art import enrich_candidate, experiment_art, field_note_art
from .public_snapshot import read_snapshot_provenance
from .prepared_discovery import PreparedLookup, lookup_prepared, read_prepared_candidate, read_prepared_candidates
from .storage import Database
from .scout import comp_fingerprint
from .sources import scout_checklist
from .unreal_patch import UNRESOLVED_UNREAL_PATCH

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
STATIC_DIR = WEB_DIR / "static"


# ---------------------------------------------------------------- data source
#
# Where the public site's numbers come from. `TFT_DATA_SOURCE` selects it
# explicitly; unset keeps the original automatic behavior for local
# development. Every response says which one is active (`demo`, and
# `/api/source` in full), and synthetic data is never presented as observed
# Riot match evidence.
#
#   database  `DATABASE_URL` (production Postgres), opened read-only. Required
#             to be set; an unreachable one is a loud 503, never demo data.
#   snapshot  a sanitized public snapshot produced by `tftlab
#             export-public-snapshot` at `TFT_SNAPSHOT_PATH` (`.sqlite3`, or a
#             `.gz` of it, unpacked once to `TFT_SNAPSHOT_CACHE_DIR`). No cloud
#             database. It is served as observed evidence ONLY when it carries
#             the exporter's verified provenance (`tftlab.public_snapshot.
#             read_snapshot_provenance`); a missing, empty, unverified or
#             synthetic snapshot is a loud 503, never demo data.
#   demo      the deterministic SYNTHETIC demo dataset, labelled as such
#             everywhere. No database at all: the zero-cost review mode.
#   (unset)   automatic: `DATABASE_URL` if set, else a populated local SQLite
#             file (`TFT_DB_PATH`), else the demo dataset.
#
# `snapshot`/`demo` together with `DATABASE_URL` is refused (503): a
# configured production database is never silently ignored or replaced.

DATA_SOURCE_ENV = "TFT_DATA_SOURCE"
DATA_SOURCE_MODES = ("database", "snapshot", "demo")
DEFAULT_SNAPSHOT_PATH = "data/snapshot/theorylabs-snapshot.sqlite3"
#: An observed source whose latest indexed game is older than this is shown
#: as stale (it may not reflect the current patch).
DEFAULT_STALE_AFTER_DAYS = 14


@dataclass(frozen=True)
class DataSource:
    mode: str  # "database" | "snapshot" | "local" | "demo"
    label: str  # short, shown in the page ledger/footer
    observed: bool  # numbers are computed from real indexed Riot ranked matches
    synthetic: bool  # numbers are generated (the demo dataset)
    description: str
    configured: str  # "explicit" (TFT_DATA_SOURCE) or "automatic"


def _source(mode: str, configured: str) -> DataSource:
    if mode == "database":
        return DataSource("database", "indexed ranked matches", True, False,
                          "Statistics computed from historical ranked TFT matches indexed into the production database.",
                          configured)
    if mode == "snapshot":
        return DataSource("snapshot", "analytics snapshot", True, False,
                          "Statistics computed from a bundled, read-only snapshot of historical ranked TFT matches. "
                          "It does not update until a new snapshot is published.", configured)
    if mode == "local":
        return DataSource("local", "local database", True, False,
                          "Statistics computed from a local copy of indexed ranked TFT matches (development).",
                          configured)
    return DataSource("demo", "demo data (synthetic)", False, True,
                      "DEMO DATA: every match, statistic and date shown is synthetic, generated so the pages can be "
                      "tried without a database. None of it is observed Riot match evidence.", configured)


class DataSourceUnavailable(RuntimeError):
    """The selected data source cannot serve. Surfaced as a 503 with a
    generic `public_message` (never a path, DSN or credential), and never
    replaced by another source."""

    public_message = "the configured data source is unavailable"
    backend: str | None = None
    mode: str | None = None


class ProductionDatabaseUnavailable(DataSourceUnavailable):
    """`DATABASE_URL` is configured but the database could not be reached.

    Deliberately distinct from "no production database configured, use
    demo data" -- once an operator has pointed the app at a real database,
    a connection failure there is a production incident to surface loudly
    (see the exception handler below), not something to paper over with
    the synthetic demo dataset.
    """

    public_message = "DATABASE_URL is configured but the database is unreachable"
    backend = "postgres"
    mode = "database"


class SnapshotUnavailable(DataSourceUnavailable):
    """`TFT_DATA_SOURCE=snapshot` but the snapshot file is missing, empty or
    not a valid read-only TheoryLabs database."""

    public_message = "the configured analytics snapshot is unavailable"
    backend = "sqlite"
    mode = "snapshot"


class DataSourceMisconfigured(DataSourceUnavailable):
    """Contradictory or invalid data-source settings (operator error)."""

    public_message = "the data source is misconfigured"


def _sqlite_db_path() -> Path:
    return Path(os.getenv("TFT_DB_PATH", "data/tftlab.sqlite3"))


def _snapshot_path() -> Path:
    return Path(os.getenv("TFT_SNAPSHOT_PATH") or DEFAULT_SNAPSHOT_PATH)


#: Unpacked `.gz` snapshots, keyed by (path, size, mtime): one decompression
#: per deployed file, serialized like the demo build.
_SNAPSHOT_UNPACKED: dict[tuple[str, int, int], Path] = {}
_SNAPSHOT_LOCK = threading.Lock()


def _snapshot_file() -> Path:
    """The SQLite file to open: `TFT_SNAPSHOT_PATH` itself, or for a `.gz`
    snapshot its one-time decompression into `TFT_SNAPSHOT_CACHE_DIR`
    (gzip's CRC check rejects a corrupt archive)."""
    path = _snapshot_path()
    if not path.is_file():
        raise SnapshotUnavailable("snapshot file not found")
    if path.suffix != ".gz":
        return path
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _SNAPSHOT_LOCK:
        cached = _SNAPSHOT_UNPACKED.get(key)
        if cached is not None and cached.is_file():
            return cached
        cache_dir = Path(os.getenv("TFT_SNAPSHOT_CACHE_DIR") or "data/snapshot-cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"{path.stem}.{stat.st_size}.{stat.st_mtime_ns}"
        tmp = target.with_name(target.name + ".tmp")
        try:
            import gzip
            import shutil

            with gzip.open(path, "rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.replace(tmp, target)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise SnapshotUnavailable(f"snapshot archive unreadable ({type(exc).__name__})") from exc
        _SNAPSHOT_UNPACKED[key] = target
        return target


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


#: The demo dataset is built on first use. A page fires several API requests
#: at once, so the build/check is serialized: concurrent first requests must
#: never race to delete or half-build the same file.
_DEMO_LOCK = threading.Lock()


def _build_demo_db() -> Database:
    demo_path = Path(os.getenv("TFT_DEMO_DB_PATH", "data/web-demo.sqlite3"))
    with _DEMO_LOCK:
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


def _configured_mode() -> str:
    mode = (os.getenv(DATA_SOURCE_ENV) or "").strip().lower()
    if mode and mode not in DATA_SOURCE_MODES:
        raise DataSourceMisconfigured(f"{DATA_SOURCE_ENV} must be one of {', '.join(DATA_SOURCE_MODES)} (or unset)")
    if mode in ("snapshot", "demo") and os.getenv("DATABASE_URL"):
        raise DataSourceMisconfigured(
            f"{DATA_SOURCE_ENV}={mode} cannot be combined with DATABASE_URL; remove DATABASE_URL to run without a "
            "cloud database")
    if mode == "database" and not os.getenv("DATABASE_URL"):
        raise DataSourceMisconfigured(f"{DATA_SOURCE_ENV}=database requires DATABASE_URL")
    return mode


def _resolve_source() -> tuple[Database, DataSource]:
    """Open the active data source (see the section comment above).

    When `DATABASE_URL` is set, it is *always* used, opened read-only via
    `Database.open_existing` (no schema setup from a request) -- connecting
    successfully is enough, even with zero matches so far (a fresh
    production database before first ingest is a normal, honest state, not
    something to disguise as demo data). If it's configured but unreachable,
    this raises `ProductionDatabaseUnavailable` rather than silently falling
    through to demo data; the exception handler registered on the app turns
    that into an explicit 503, per the same "never quietly pretend
    everything is fine" rule. A selected snapshot that cannot be opened is
    the same kind of loud failure. Callers must close the returned
    `Database`.
    """
    mode = _configured_mode()
    configured = "explicit" if mode else "automatic"
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # Read-only: a request never creates, migrates, indexes or backfills
        # anything (schema setup belongs to the CLI/ingest, which call
        # `Database(...)`). A missing/incompatible schema is a production
        # incident surfaced as 503, never silently created from a request.
        try:
            db = Database.open_existing(database_url)
        except Exception as exc:
            raise ProductionDatabaseUnavailable(
                f"DATABASE_URL is configured but unavailable ({type(exc).__name__})"
            ) from exc
        return db, _source("database", configured)

    if mode == "snapshot":
        path = _snapshot_file()
        try:
            db = Database.open_existing(path)  # SQLite mode=ro: never created or written
        except Exception as exc:
            raise SnapshotUnavailable(f"snapshot unreadable ({type(exc).__name__})") from exc
        try:
            read_snapshot_provenance(db)  # fail closed: only an exporter-certified, observed snapshot
            empty = not _has_participants(db)
        except Exception as exc:
            db.close()
            raise SnapshotUnavailable(f"snapshot not certified as observed data ({type(exc).__name__})") from exc
        if empty:
            db.close()
            raise SnapshotUnavailable("snapshot holds no matches")
        return db, _source("snapshot", configured)

    if mode == "demo":
        return _build_demo_db(), _source("demo", configured)

    sqlite_path = _sqlite_db_path()
    if sqlite_path.exists():
        db = _open_if_live(sqlite_path)
        if db is not None:
            return db, _source("local", configured)

    return _build_demo_db(), _source("demo", configured)


def _resolve_database() -> tuple[Database, bool]:
    """(database, is the synthetic demo dataset) -- see `_resolve_source`."""
    db, source = _resolve_source()
    return db, source.synthetic


#: Provenance fields shown by `/api/source` for a snapshot (all
#: non-identifying; the full record stays inside the file).
SNAPSHOT_PUBLIC_FIELDS = ("format", "format_version", "source_kind", "exported_at", "balance_windows", "matches",
                          "boards", "latest_game", "code_version", "exclusions")


def _snapshot_info(db: Database) -> dict[str, Any]:
    prov = read_snapshot_provenance(db)  # already certified when the source was resolved
    return {key: prov.get(key) for key in SNAPSHOT_PUBLIC_FIELDS}


def _stale_after_days() -> int:
    try:
        return max(1, int(os.getenv("TFT_STALE_AFTER_DAYS", DEFAULT_STALE_AFTER_DAYS)))
    except ValueError:
        return DEFAULT_STALE_AFTER_DAYS


def source_status(db: Database, source: DataSource, *, now_ms: int | None = None) -> dict[str, Any]:
    """Everything the UI needs to say where the numbers come from and how
    fresh they are. Synthetic data never gets a freshness verdict: its dates
    are generated."""
    matches, latest = db.query_one("SELECT COUNT(*), MAX(game_datetime) FROM matches")
    boards = db.query_one("SELECT COUNT(*) FROM participants")[0]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    stale_after = _stale_after_days()
    age_days = None
    stale = None
    if source.observed and latest:
        age_days = round((now_ms - int(latest)) / 86_400_000, 1)
        stale = age_days > stale_after
    return {
        "ok": True,
        **asdict(source),
        "demo": source.synthetic,
        "backend": db.dialect,
        "matches": matches,
        "boards": boards,
        "latest_game_datetime": latest,
        "latest_game_date_is_synthetic": source.synthetic,
        "default_balance_window": default_balance_window(db),
        "age_days": age_days,
        "stale": stale,
        "stale_after_days": stale_after,
        "snapshot": _snapshot_info(db) if source.mode == "snapshot" else None,
    }


#: Riot site verification (`/riot.txt`): the exact string Riot provides, set
#: by the operator on the host. Never the Riot API key.
RIOT_SITE_VERIFICATION_ENV = "RIOT_SITE_VERIFICATION"


def riot_site_verification() -> str | None:
    """The configured verification string, or None (not configured, or
    refused because it looks like a Riot API key)."""
    value = (os.getenv(RIOT_SITE_VERIFICATION_ENV) or "").strip()
    if not value:
        return None
    api_key = (os.getenv("RIOT_API_KEY") or "").strip()
    if value.upper().startswith("RGAPI-") or (api_key and value == api_key):
        return None  # an API key must never be published, whatever variable it was put in
    return value


# ---------------------------------------------------------------- Discovery population cache
#
# Discovery is normally served from prepared runs (`tftlab.prepared_discovery`,
# published by `tftlab prepare-discovery` after each ingest): a request only
# checks the run is current for its window and reads/filters rows. The cache
# below backs the LIVE fallback used when no current prepared run exists.
#
# Every live Discovery request needs the window-wide carry population (the
# Opportunity Score baseline and the superset candidates are filtered from).
# It is one expensive aggregate (seconds on a patch-sized window) and is
# byte-for-byte the same for every cost / min-games filter and every working
# notes click, until new matches arrive. So it -- and only it -- is kept in
# process memory:
#   key       : (database target, balance window)
#   freshness : the window's match count and latest game time, read with one
#               cheap query on every request; any new or removed match in the
#               window changes it and forces a recompute. Nothing else is
#               cached (per-carry evidence is always computed fresh).
#   size      : a handful of windows; the oldest entry is dropped first.
_POPULATION_CACHE: dict[tuple[str, str], tuple[tuple[int, int], list]] = {}
_POPULATION_CACHE_SIZE = 8
# Concurrent requests for a population that isn't cached yet wait for the
# first one to compute it instead of all running the same aggregate at once.
_POPULATION_LOCK = threading.Lock()


def _database_target() -> str:
    return os.getenv("DATABASE_URL") or str(_sqlite_db_path())


def cached_discovery_population(db: Database, balance_window: str) -> list:
    row = db.query_one(
        "SELECT COUNT(*), MAX(game_datetime) FROM matches WHERE balance_window = ?", (balance_window,)
    )
    fingerprint = (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
    key = (_database_target(), balance_window)
    with _POPULATION_LOCK:
        hit = _POPULATION_CACHE.get(key)
        if hit is not None and hit[0] == fingerprint:
            return hit[1]
        population = discovery_population(db, balance_window)
        _POPULATION_CACHE.pop(key, None)
        _POPULATION_CACHE[key] = (fingerprint, population)
        while len(_POPULATION_CACHE) > _POPULATION_CACHE_SIZE:
            _POPULATION_CACHE.pop(next(iter(_POPULATION_CACHE)))
        return population


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
    eligible_sql, eligible_params = carry_commitment_sql("c")
    return db.query_all(
        f"""
        WITH carry_games AS (
            SELECT DISTINCT c.match_id, c.participant_index
            FROM units c
            JOIN matches m ON m.match_id = c.match_id
            WHERE c.character_id = ? AND {eligible_sql} AND m.balance_window = ?
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
        (character_id, *eligible_params, balance_window, character_id),
    )


def carry_item_sets(db: Database, character_id: str, balance_window: str) -> list[tuple[Any, ...]]:
    """A board can field more than one committed instance of `character_id`
    in the same game; `ranked`/`rn = 1` picks exactly one canonical instance
    per game (`CANONICAL_UNIT_TIEBREAK_SQL`) so that game contributes one
    item-set observation, not one per instance."""
    eligible_sql, eligible_params = carry_commitment_sql("u")
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
            WHERE u.character_id = ? AND {eligible_sql}
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
        (character_id, *eligible_params, balance_window),
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
    app = FastAPI(title="TheoryLabs", version="0.2.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(DataSourceUnavailable)
    async def _data_source_unavailable(request: Request, exc: DataSourceUnavailable) -> JSONResponse:
        # Deliberately only the class's fixed public message -- never the
        # exception detail, a path, the DATABASE_URL or any credential.
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "demo": False,
                "backend": exc.backend,
                "source_mode": exc.mode,
                "status": "error",
                "error": exc.public_message,
            },
        )

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    # Static information pages (no data source needed: they always render).
    for route, page in (("/about", "about.html"), ("/methodology", "methodology.html"), ("/data", "methodology.html"),
                        ("/privacy", "privacy.html"), ("/terms", "terms.html")):
        app.add_api_route(route, (lambda page=page: FileResponse(WEB_DIR / page)), methods=["GET"],
                          include_in_schema=False)

    @app.get("/riot.txt", include_in_schema=False)
    def riot_txt() -> PlainTextResponse:
        """Riot site verification: exactly the operator-configured string,
        as plain text with nothing before or after it. 404 when none is
        configured -- never a placeholder."""
        value = riot_site_verification()
        headers = {"Cache-Control": "no-store"}
        if value is None:
            return PlainTextResponse("Not Found", status_code=404, headers=headers)
        return PlainTextResponse(value, headers=headers)

    @app.get("/api/source")
    def data_source() -> dict[str, object]:
        db, source = _resolve_source()
        with db:
            return source_status(db, source)

    @app.get("/champions", include_in_schema=False)
    @app.get("/champions/{key}", include_in_schema=False)
    def champions_page(key: str | None = None) -> FileResponse:
        # One page for the champion picker and each investigation;
        # champion.js reads the path and calls the read-only API below.
        return FileResponse(WEB_DIR / "champion.html")

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
        db, source = _resolve_source()
        with db:
            participants = db.query_one("SELECT COUNT(*) FROM participants")[0]
            matches = db.query_one("SELECT COUNT(*) FROM matches")[0]
            balance_window = default_balance_window(db)
        return {
            "ok": True,
            "demo": source.synthetic,
            "source": source.mode,
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
            associations = trait_count_associations(
                db, character_id, resolved_window, min_games=min_games
            )
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "character_id": character_id,
            "traits": [asdict(a) for a in associations],
        }

    @app.get("/api/champions")
    def champions(balance_window: str | None = Query(None)) -> dict[str, object]:
        """Every current-set champion with its carry-game count in one
        balance window, so a player can pick one by name."""
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            directory = champion_directory(db, resolved_window)
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "evidence_type": OBSERVED,
            "champions": directory,
        }

    @app.get("/api/champions/{key}")
    def champion_detail(
        key: str,
        balance_window: str | None = Query(None),
        top_n: int = Query(6, ge=1, le=20, description="Rows kept per evidence list"),
    ) -> dict[str, object]:
        """One champion's carry investigation in one balance window. `key`
        is a name or slug ("khazix", "Kha'Zix") or a Riot id. Unknown
        champions are 404; a known champion with no carry games in the window
        is a normal 200 with `carry: null` (the page shows its empty state)."""
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            champion = resolve_champion(db, resolved_window, key)
            if champion is None:
                raise HTTPException(status_code=404, detail="Champion not found")
            body = champion_investigation(db, champion, resolved_window, top_n=top_n)
        return {"demo": demo, "backend": db.dialect, **body}

    @app.get("/api/discovery")
    def discovery(
        max_cost: int = Query(3, ge=1, le=5),
        min_samples: int = Query(10, ge=1, le=100000),
        balance_window: str | None = Query(None),
        top_n: int = Query(5, ge=1, le=20, description="Best partners/items/traits kept per candidate"),
        limit: int = Query(20, ge=1, le=200),
        costs: str | None = Query(
            None,
            description="Exact costs, comma-separated (e.g. '4' or '1,3,5'); overrides max_cost. Only these "
            "carries get partner/item/trait evidence built.",
            pattern=r"^[1-5](,[1-5])*$",
        ),
    ) -> dict[str, object]:
        selected = sorted({int(c) for c in costs.split(",")}) if costs else list(range(1, max_cost + 1))
        db, demo = _resolve_database()
        with db:
            resolved_window = balance_window or default_balance_window(db)
            prepared = lookup_prepared(db, resolved_window) if resolved_window else PreparedLookup("missing")
            if prepared.status == "current" and prepared.run is not None:
                # Steady state: no aggregation, just the run's matching rows.
                window_carries = prepared.run.window_carries
                candidates = read_prepared_candidates(
                    db, prepared.run, costs=selected, min_samples=min_samples, top_n=top_n, limit=limit
                )
            else:
                # No current prepared run (never prepared, or new matches /
                # new analytics code since): compute live, never serve stale.
                population = cached_discovery_population(db, resolved_window) if resolved_window else []
                window_carries = len(population)
                candidates = [
                    asdict(c)
                    for c in discover_candidates(
                        db,
                        balance_window=resolved_window,
                        costs=selected,
                        min_samples=min_samples,
                        top_n=top_n,
                        population=population,
                    )[:limit]
                ]
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "costs": selected,
            "min_samples": min_samples,
            # Carries of any cost in the window before the cost/min filters:
            # 0 means the window genuinely has no carry data; otherwise an
            # empty `candidates` list means the filters matched nothing.
            "window_carries": window_carries,
            # Where these numbers came from: a current prepared run, or live
            # computation because the latest run is "stale" or "missing".
            "prepared": prepared.describe(),
            "candidates": [enrich_candidate(c) for c in candidates],
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
            prepared = PreparedLookup("missing")
            if resolved_window is not None:
                prepared = lookup_prepared(db, resolved_window)
                if prepared.status == "current" and prepared.run is not None:
                    candidate = read_prepared_candidate(db, prepared.run, character_id, top_n=top_n)
                else:
                    live = discovery_candidate_for(
                        db, character_id, balance_window=resolved_window, top_n=top_n,
                        population=cached_discovery_population(db, resolved_window),
                    )
                    candidate = asdict(live) if live is not None else None
        if candidate is None:
            raise HTTPException(status_code=404, detail="Carry not found")
        return {
            "demo": demo,
            "backend": db.dialect,
            "balance_window": resolved_window,
            "prepared": prepared.describe(),
            "candidate": enrich_candidate(candidate),
        }

    return app


app = create_app()
