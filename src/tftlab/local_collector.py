"""The local collector: TheoryLabs data collection on one personal computer.

One SQLite file (default `data/local/theorylabs.sqlite3`) is the private,
raw working database: Riot Match-V1 payloads, the seed-sampling ledger, ingest
runs, match-discovery provenance and prepared analytics all stay on this
computer. No Postgres server, Docker or cloud database is involved, and the
collector structurally refuses to write anywhere else: `resolve_local_db`
rejects database URLs and `DATABASE_URL` is never read. The public website
gets data only through `tftlab.public_snapshot` (sanitized export), never
this file.

`run_local_collect` is orchestration only. Every step reuses the existing
implementation instead of duplicating it:

  1. preflight -- local SQLite target, disk space, the current trusted
     window (`unreal_patch.current_trusted_window`; never a hardcoded
     patch), the Riot key (`riot.check_riot_key`, the `verify-riot` check)
     and CommunityDragon costs -- all before the database is modified;
  2. a consistent pre-run backup (SQLite's online backup API) with
     retention;
  3. bounded collection: `ingest.ingest_ladder` with the production
     cohort defaults, bounded to the trusted window, with its own rate
     limiting, seed rotation, deduplication and run finalization;
  4. `validate.validate_live_data` for the window's balance window(s);
  5. `prepared_discovery.prepare_window` for those windows only;
  6. a plain-language report (no PUUIDs, match ids or secrets).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .ingest import DEFAULT_MAX_LADDER_PAGES, ingest_ladder
from .prepared_discovery import lookup_prepared, prepare_window
from .riot import RiotApiError, check_riot_key, classify_riot_error, is_fatal_riot_error
from .storage import Database
from .unreal_patch import NoCurrentTrustedWindow, UnrealPatchWindow, current_trusted_window
from .validate import validate_live_data

#: Everything the local collector writes lives under here (git-ignored).
DEFAULT_LOCAL_DIR = Path("data/local")
DEFAULT_LOCAL_DB = DEFAULT_LOCAL_DIR / "theorylabs.sqlite3"
#: Optional override of the raw database path. Deliberately NOT
#: `DATABASE_URL` (cloud) or `TFT_DB_PATH` (the generic commands' default).
LOCAL_DB_ENV = "TFT_LOCAL_DB_PATH"
BACKUP_DIRNAME = "backups"
PUBLIC_DIRNAME = "public"
REPORTS_DIRNAME = "reports"
PUBLIC_SNAPSHOT_NAME = "theorylabs-public-snapshot.sqlite3"

#: The bounded settings the scheduled production ingest used (live-ingest.yml).
DEFAULT_SEED_ALLOCATION: Mapping[str, int] = {
    "challenger": 15, "grandmaster": 15, "master": 20, "diamond": 25, "platinum": 25,
}
DEFAULT_MATCHES_PER_SEED = 10
#: Operator policy cap on top of Riot's advertised limits, as in production.
DEFAULT_RATE_CEILING = "10:10"
DEFAULT_KEEP_BACKUPS = 7
#: Free space required before collecting, besides room for one backup.
MIN_FREE_BYTES = 512 * 1024 * 1024
#: `.env.example`'s placeholder, which is "not configured".
PLACEHOLDER_KEYS = frozenset({"", "RGAPI-your-key-here"})
#: A lock older than this is from a run that died without cleaning up.
LOCK_STALE_SECONDS = 6 * 3600

BACKUP_NAME_RE = re.compile(r"^theorylabs-(\d{8}T\d{6}Z)(?:-(\d+))?\.sqlite3$")

EXPIRED_KEY_MESSAGE = (
    "Riot development key is expired (or invalid). Riot development keys expire about every 24 hours: "
    "refresh it in the Riot Developer Portal (developer.riotgames.com), update RIOT_API_KEY in .env "
    "(or run `tftlab local-set-key`), and run this command again. No collection was performed."
)
NO_WINDOW_MESSAGE = (
    "TheoryLabs does not currently have a verified collection window. No Riot data was collected. "
    "A new patch window has to be verified and registered (tftlab.unreal_patch.UNREAL_PATCH_REGISTRY) "
    "before collection can continue; TheoryLabs never guesses patch boundaries."
)
KEY_NOT_SET_MESSAGE = (
    "RIOT_API_KEY is not set. Copy your development key from the Riot Developer Portal "
    "(developer.riotgames.com) into the RIOT_API_KEY line of .env (or run `tftlab local-set-key`). "
    "No collection was performed."
)


class LocalTargetError(ValueError):
    """The local collector was pointed at something other than a local SQLite file."""


class CollectorBusy(RuntimeError):
    """Another local collection holds the lock."""


def now_ms() -> int:
    """Wall-clock time in epoch ms (patched in tests)."""
    return int(time.time() * 1000)


def iso(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def human_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} bytes"  # pragma: no cover


# ---------------------------------------------------------------- the target


def _is_sqlite_file(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(16) == b"SQLite format 3\x00"


def resolve_local_db(value: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """The local raw database path: `value`, else `TFT_LOCAL_DB_PATH`, else
    `data/local/theorylabs.sqlite3`. Never `DATABASE_URL`.

    Refuses anything that is not a local SQLite file -- a `postgres://` (or
    any other `scheme://`) URL, a directory, or an existing non-SQLite file
    -- with `LocalTargetError`. The refused value is never echoed (a URL
    could carry a password)."""
    env = os.environ if env is None else env
    raw = str(value) if value else (env.get(LOCAL_DB_ENV) or str(DEFAULT_LOCAL_DB))
    if "://" in raw or raw.lower().startswith(("postgres:", "postgresql:")):
        raise LocalTargetError(
            "The local collector only writes to a SQLite file on this computer and refuses database URLs "
            "(postgres://..., cloud databases). Use a file path such as data/local/theorylabs.sqlite3."
        )
    path = Path(raw).expanduser()
    if path.is_dir():
        raise LocalTargetError(f"{path} is a folder, not a SQLite database file.")
    if path.exists() and path.stat().st_size and not _is_sqlite_file(path):
        raise LocalTargetError(f"{path} exists but is not a SQLite database; refusing to use it.")
    return path.resolve()


def local_dirs(db_path: Path) -> dict[str, Path]:
    base = db_path.parent
    return {"backups": base / BACKUP_DIRNAME, "public": base / PUBLIC_DIRNAME, "reports": base / REPORTS_DIRNAME}


def riot_key_configured(api_key: str | None) -> bool:
    return bool(api_key) and api_key.strip() not in PLACEHOLDER_KEYS


# ---------------------------------------------------------------- read-only facts


def _window_balance_windows(db: Database, window: UnrealPatchWindow) -> list[str]:
    """Balance windows of stored matches played inside the trusted window
    (usually exactly one, named after the patch; more if a mid-patch cutover is
    registered), latest first."""
    rows = db.query_all(
        "SELECT balance_window, MAX(game_datetime) FROM matches WHERE balance_window IS NOT NULL "
        "AND game_datetime >= ? AND game_datetime < ? GROUP BY balance_window",
        (window.starts_at, window.ends_at),
    )
    return [str(r[0]) for r in sorted(rows, key=lambda r: int(r[1] or 0), reverse=True)]


def window_facts(db: Database, window: UnrealPatchWindow) -> dict[str, Any]:
    """Counts for the trusted window's classified matches. Aggregates only."""
    bounds = (window.starts_at, window.ends_at)
    where = "m.balance_window IS NOT NULL AND m.game_datetime >= ? AND m.game_datetime < ?"
    matches, latest = db.query_one(f"SELECT COUNT(*), MAX(m.game_datetime) FROM matches m WHERE {where}", bounds)
    boards = db.query_one(
        f"SELECT COUNT(*) FROM participants p JOIN matches m ON m.match_id = p.match_id WHERE {where}", bounds
    )[0]
    return {
        "matches": int(matches or 0),
        "boards": int(boards or 0),
        "latest_game": int(latest) if latest is not None else None,
        "balance_windows": _window_balance_windows(db, window),
    }


def store_facts(db: Database) -> dict[str, Any]:
    """Store-wide aggregates. No identifiers."""
    def count(sql: str) -> int:
        try:
            return int(db.query_one(sql)[0] or 0)
        except Exception:
            return 0

    runs = {
        status: count(f"SELECT COUNT(*) FROM ingest_runs WHERE status = '{status}'")
        for status in ("completed", "started", "failed")
    }
    latest = db.query_one("SELECT MAX(game_datetime) FROM matches")[0]
    return {
        "matches": count("SELECT COUNT(*) FROM matches"),
        "boards": count("SELECT COUNT(*) FROM participants"),
        "latest_game": int(latest) if latest is not None else None,
        "runs_completed": runs["completed"],
        "runs_incomplete": runs["started"] + runs["failed"],
        "ledger_rows": count("SELECT COUNT(*) FROM seed_samples"),
    }


# ---------------------------------------------------------------- backups


@dataclass(frozen=True)
class BackupResult:
    path: Path | None
    bytes: int = 0
    removed: tuple[str, ...] = ()
    note: str = ""


class BackupFailed(RuntimeError):
    pass


def list_backups(backup_dir: Path) -> list[Path]:
    """Collector backups in `backup_dir`, newest first."""
    if not backup_dir.is_dir():
        return []
    found = []
    for path in backup_dir.iterdir():
        match = BACKUP_NAME_RE.match(path.name)
        if match and path.is_file():
            found.append(((match.group(1), int(match.group(2) or 0)), path))
    return [p for _key, p in sorted(found, reverse=True)]


def prune_backups(backup_dir: Path, keep: int, *, protect: tuple[Path, ...] = ()) -> list[Path]:
    """Delete all but the newest `keep` (at least 1) collector backups.
    Only files named like collector backups are ever considered, and the
    protected paths (the active database, the backup just written) never."""
    protected = {p.resolve() for p in protect}
    removed = []
    for path in list_backups(backup_dir)[max(1, keep):]:
        if path.resolve() in protected:
            continue
        path.unlink()
        removed.append(path)
    return removed


def backup_local_db(db_path: Path, backup_dir: Path, *, at_ms: int, keep: int = DEFAULT_KEEP_BACKUPS) -> BackupResult:
    """A consistent copy of the live database via SQLite's online backup API
    (safe with WAL, unlike copying the file), written as
    `theorylabs-<UTC stamp>.sqlite3`, checked with `PRAGMA quick_check`,
    then older backups beyond `keep` are deleted. Nothing is deleted unless
    the new backup completed. A missing database needs no backup."""
    if not db_path.exists():
        return BackupResult(None, note="first run: there was no database yet, so no pre-run backup was needed")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupFailed(f"could not create the backup folder {backup_dir} ({type(exc).__name__})") from exc
    stamp = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"theorylabs-{stamp}.sqlite3"
    n = 1
    while target.exists():
        target = backup_dir / f"theorylabs-{stamp}-{n}.sqlite3"
        n += 1
    partial = target.with_name(target.name + ".partial")
    try:
        source = sqlite3.connect(db_path)
        try:
            dest = sqlite3.connect(partial)
            try:
                source.backup(dest)
                dest.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
                verdict = dest.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                dest.close()
        finally:
            source.close()
        if verdict != "ok":
            raise BackupFailed(f"the backup copy failed its integrity check ({verdict})")
        os.replace(partial, target)
    except BackupFailed:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise BackupFailed(f"could not back up the local database ({type(exc).__name__}: {exc})") from exc
    removed = prune_backups(backup_dir, keep, protect=(db_path, target))
    return BackupResult(target, target.stat().st_size, tuple(p.name for p in removed))


# ---------------------------------------------------------------- lock


@contextmanager
def collector_lock(db_path: Path, *, now: Callable[[], float] = time.time) -> Iterator[Path]:
    """One collection per database at a time. A lock left behind by a run
    that died (older than LOCK_STALE_SECONDS) is replaced."""
    lock = db_path.parent / f".{db_path.name}.collect.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = now() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > LOCK_STALE_SECONDS:
                lock.unlink(missing_ok=True)
                continue
            raise CollectorBusy(
                f"Another collection is already running on this database (lock file {lock}). Wait for it to "
                "finish. If you are sure none is running (e.g. the computer restarted mid-run), delete that file."
            ) from None
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "started_at": int(now())}))
        break
    else:  # pragma: no cover - lost two races in a row
        raise CollectorBusy(f"Could not take the collection lock {lock}.")
    try:
        yield lock
    finally:
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------- collection


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str


@dataclass
class CollectConfig:
    db_path: Path
    backup_dir: Path
    keep_backups: int = DEFAULT_KEEP_BACKUPS
    seed_allocation: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SEED_ALLOCATION))
    matches_per_seed: int = DEFAULT_MATCHES_PER_SEED
    max_ladder_pages: int = DEFAULT_MAX_LADDER_PAGES


#: Outcomes, in the order a run can reach them. Only SUCCESS exits 0.
SUCCESS = "success"
PREFLIGHT_FAILED = "preflight_failed"
COLLECTION_FAILED = "collection_failed"
VALIDATION_FAILED = "validation_failed"
PREPARE_FAILED = "prepare_failed"


@dataclass
class CollectReport:
    outcome: str = PREFLIGHT_FAILED
    message: str = ""
    checks: list[Check] = field(default_factory=list)
    db_path: str = ""
    patch: str | None = None
    window_start: int | None = None
    window_end: int | None = None
    window_source: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    backup: dict[str, Any] = field(default_factory=dict)
    ingest: dict[str, Any] = field(default_factory=dict)
    riot: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    prepared: dict[str, Any] = field(default_factory=dict)
    database_ok: bool | None = None
    ready_for_snapshot: bool = False

    @property
    def exit_code(self) -> int:
        return 0 if self.outcome == SUCCESS else 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _noop(_check: Check) -> None:
    return None


def _riot_failure_message(category: str | None) -> str:
    if category == "unauthorized":
        return EXPIRED_KEY_MESSAGE
    if category == "forbidden":
        return ("Riot refused the key (403 Forbidden): it is not allowed to use these endpoints or this region. "
                "Check TFT_PLATFORM/TFT_REGION in .env and the key in the Developer Portal. No collection was performed.")
    if category == "rate_limited":
        return "Riot is rate limiting this key right now (429). Wait a few minutes and try again. No collection was performed."
    if category == "network_error":
        return ("Could not reach the Riot API (network problem). Check the internet connection and try again. "
                "No collection was performed.")
    return f"The Riot key check failed ({category or 'unknown error'}). No collection was performed."


def _quick_check(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except Exception:
        return False


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def run_local_collect(
    config: CollectConfig,
    *,
    api_key: str | None,
    platform: str,
    region: str,
    at_ms: int,
    client_factory: Callable[..., Any],
    fetch_metadata: Callable[[], Any],
    ingest: Callable[..., Any] = ingest_ladder,
    validate: Callable[..., Any] = validate_live_data,
    prepare: Callable[..., Any] = prepare_window,
    database_url_set: bool = False,
    progress: Callable[[Check], None] = _noop,
) -> CollectReport:
    """Preflight, back up, collect, validate, prepare, report (see module docstring).

    `client_factory(api_key, platform=..., region=...)` returns a context-
    managed Riot client (normally `RiotClient` with the production rate
    ceiling); `fetch_metadata()` returns CommunityDragon `SetMetadata`.
    Never raises for an expected failure: the report's `outcome`/`message`
    say what happened and what was (not) changed."""
    report = CollectReport(db_path=str(config.db_path))

    def check(name: str, status: str, detail: str) -> Check:
        item = Check(name, status, detail)
        report.checks.append(item)
        progress(item)
        return item

    def stop(message: str, outcome: str = PREFLIGHT_FAILED) -> CollectReport:
        report.outcome, report.message = outcome, message
        return report

    # 1. configuration: a local SQLite file, never a cloud database.
    db_path = config.db_path
    if "://" in str(db_path):  # resolve_local_db already refuses these; never trust a caller
        raise LocalTargetError("the local collector only accepts a local SQLite file path")
    exists = db_path.exists()
    check("Local database", "ok", f"{db_path} ({'exists, ' + human_bytes(db_path.stat().st_size) if exists else 'new file'})")
    if database_url_set:
        check("DATABASE_URL", "info", "set in the environment but ignored: the local collector never uses it")
    free = _free_bytes(db_path.parent)
    need = MIN_FREE_BYTES + (db_path.stat().st_size if exists else 0)
    if free < need:
        check("Disk space", "fail", f"{human_bytes(free)} free; need at least {human_bytes(need)}")
        return stop(f"Not enough free disk space ({human_bytes(free)} free, {human_bytes(need)} needed). "
                     "No collection was performed.")
    check("Disk space", "ok", f"{human_bytes(free)} free")
    check("Riot routing", "ok", f"platform {platform}, region {region}")
    check("Current time (UTC)", "info", iso(at_ms))

    # 2. the current trusted patch window (from the registry; never guessed).
    try:
        window = current_trusted_window(at_ms)
    except NoCurrentTrustedWindow as exc:
        check("Trusted patch window", "fail", f"none: {exc}")
        return stop(NO_WINDOW_MESSAGE)
    report.patch, report.window_start, report.window_end = window.client_patch, window.starts_at, window.ends_at
    report.window_source = window.source
    check("Trusted patch window", "ok",
          f"patch {window.client_patch}: {iso(window.starts_at)} to {iso(window.ends_at)} (now inside it)")

    # 3. the Riot key -- before the database is opened at all.
    if not riot_key_configured(api_key):
        check("Riot key configured", "fail", "RIOT_API_KEY is missing or still the placeholder")
        return stop(KEY_NOT_SET_MESSAGE)
    check("Riot key configured", "ok", "yes (the key itself is never shown)")

    with client_factory(api_key, platform=platform, region=region) as client:
        key = check_riot_key(client)
        if not key.ok:
            check("Riot key valid", "fail", key.category or "failed")
            report.riot = _riot_telemetry(client)
            return stop(_riot_failure_message(key.category))
        check("Riot key valid", "ok", f"yes ({key.challenger_entries} Challenger players listed)")

        # 4. CommunityDragon shop costs, the same as ingest-riot's default.
        try:
            metadata = fetch_metadata()
        except Exception as exc:
            check("Champion cost data", "fail", f"CommunityDragon unavailable ({type(exc).__name__})")
            return stop("CommunityDragon (champion cost data) could not be reached. TheoryLabs will not guess "
                        "shop costs. Try again later. No collection was performed.")
        check("Champion cost data", "ok", f"CommunityDragon set {getattr(metadata, 'set_number', '?')}")

        # 5. what is already stored (read-only).
        report.before = {"window": {"matches": 0, "boards": 0, "latest_game": None, "balance_windows": []},
                         "store": {}}
        if exists:
            try:
                with Database.open_existing(db_path) as ro:
                    report.before = {"window": window_facts(ro, window), "store": store_facts(ro)}
            except Exception as exc:
                check("Existing data", "warn", f"could not read it read-only ({type(exc).__name__}); "
                      "the schema will be brought up to date when collection opens it")
            else:
                store = report.before["store"]
                check("Existing data", "ok",
                      f"{report.before['window']['matches']} matches in patch {window.client_patch}; "
                      f"{store['matches']} matches in total; {store['runs_completed']} completed collection runs")
        else:
            check("Existing data", "ok", "none yet (first collection)")

        # 6. a consistent backup before anything is modified.
        try:
            backup = backup_local_db(db_path, config.backup_dir, at_ms=at_ms, keep=config.keep_backups)
        except BackupFailed as exc:
            check("Backup", "fail", str(exc))
            return stop(f"The pre-run backup failed ({exc}). No collection was performed.")
        report.backup = {"path": str(backup.path) if backup.path else None, "bytes": backup.bytes,
                         "removed": list(backup.removed), "note": backup.note}
        check("Backup", "ok", f"{backup.path.name} ({human_bytes(backup.bytes)})" if backup.path else backup.note)

        # 7. bounded collection with the existing ingest implementation.
        check("Collection", "info",
              f"bounded: {', '.join(f'{c} {n}' for c, n in config.seed_allocation.items())} seeds x "
              f"{config.matches_per_seed} recent matches, patch {window.client_patch} only (this can take 10-25 minutes)")
        # Always `local-<ms>`: never derived from CI variables (GITHUB_RUN_ID would give
        # every run in one job the same id), and unique per run on one computer.
        run_id = f"local-{at_ms}"
        with Database(db_path) as db:
            if db.dialect != "sqlite":  # pragma: no cover - resolve_local_db makes this unreachable
                raise LocalTargetError("the local collector only writes to SQLite")
            try:
                result = ingest(
                    client, db,
                    seed_allocation=dict(config.seed_allocation),
                    matches_per_player=config.matches_per_seed,
                    max_ladder_pages=config.max_ladder_pages,
                    cost_lookup=metadata.cost_for_champion,
                    history_start_time=window.starts_at // 1000,
                    history_end_time=window.ends_at // 1000,
                    run_id=run_id,
                    now_ms=at_ms,
                )
            except Exception as exc:
                report.riot = _riot_telemetry(client)
                report.ingest = {"run_id": run_id, "run_status": "failed", "error": type(exc).__name__}
                db.close()
                report.database_ok = _quick_check(db_path)
                _fill_after(report, db_path, window)
                if isinstance(exc, RiotApiError) and is_fatal_riot_error(exc) \
                        and classify_riot_error(str(exc)) == "unauthorized":
                    reason = "the Riot development key expired during the run (refresh it and run again)"
                elif isinstance(exc, RiotApiError):
                    reason = f"Riot requests kept failing ({classify_riot_error(str(exc))})"
                else:
                    reason = f"an unexpected error ({type(exc).__name__})"
                check("Collection", "fail", reason)
                return stop(
                    f"Collection stopped early: {reason}. "
                    + ("The local database is still valid: " if report.database_ok else
                       "WARNING: the database integrity check did not pass; restore the backup above before "
                       "collecting again. ")
                    + "matches stored before the failure were saved completely (one at a time), the run is "
                    "recorded as incomplete, and seed rotation did not advance, so the next run will retry "
                    "those players.",
                    COLLECTION_FAILED,
                )
            report.riot = _riot_telemetry(client)
            report.ingest = {
                "run_id": result.run_id, "run_status": result.run_status,
                "inserted": result.matches_inserted, "duplicates_skipped": result.duplicates_skipped,
                "fetched": result.matches_fetched, "failed_match_requests": result.failed_requests,
                "non_ranked_skipped": result.non_target_matches_skipped,
                "seeds": {c: {"selected": r.selected, "requested": r.requested}
                          for c, r in result.cohort_reports.items()},
                "seed_players": result.seed_players, "failed_histories": result.failed_history_requests,
                "empty_histories": result.seeds_with_empty_history,
                "seed_ledger_rows": result.seed_ledger_rows, "discovery_rows": result.discovery_rows,
            }
            check("Collection", "ok", f"{result.matches_inserted} new matches, {result.duplicates_skipped} already stored")
            if result.seed_players and result.failed_history_requests == result.seed_players:
                db.close()
                report.database_ok = _quick_check(db_path)
                _fill_after(report, db_path, window)
                return stop("Riot did not return any match history for any player (network or Riot trouble). "
                            "Nothing new was collected; the database is unchanged apart from the run record. "
                            "Try again later.", COLLECTION_FAILED)

            # 8. validation of the window's balance window(s).
            windows = _window_balance_windows(db, window)
            severe = False
            for bw in windows:
                integrity = validate(db, balance_window=bw, metadata=metadata)
                severe = severe or integrity.is_severe
                report.validation[bw] = _validation_summary(integrity)
            if not windows:
                check("Validation", "warn", f"skipped: no patch {window.client_patch} matches stored yet")
            elif severe:
                check("Validation", "fail", "severe integrity problems: " + "; ".join(
                    f"{bw}: {', '.join(v['severe']) or 'see validate-live-data'}"
                    for bw, v in report.validation.items() if v["is_severe"]))
            else:
                warnings = [w for v in report.validation.values() for w in v["warnings"]]
                check("Validation", "warn" if warnings else "ok",
                      "passed" + (f" with warnings: {'; '.join(warnings)}" if warnings else ""))

            # 9. prepared Discovery for the current window only.
            prepare_failed = False
            if severe:
                check("Discovery preparation", "warn", "skipped because validation failed")
            for bw in ([] if severe else windows):
                try:
                    prepared = prepare(db, bw)
                except Exception as exc:
                    prepare_failed = True
                    report.prepared[bw] = {"status": "failed", "error": type(exc).__name__}
                    db.conn.rollback()
                    continue
                report.prepared[bw] = {"status": prepared.status, "carries": prepared.window_carries,
                                       "matches": prepared.source_matches, "seconds": round(prepared.seconds, 1)}
            if report.prepared:
                status = "fail" if prepare_failed else "ok"
                check("Discovery preparation", status, describe_prepared(report.prepared))

    report.database_ok = _quick_check(db_path)
    _fill_after(report, db_path, window)
    after = report.after.get("window", {})
    if severe:
        return stop("Collection completed, but validation found severe integrity problems. The collected matches "
                    "were kept (nothing was deleted). Do NOT create a public snapshot until this is reviewed "
                    "(run `tftlab validate-live-data --db <database>` for details).", VALIDATION_FAILED)
    if prepare_failed:
        return stop("Collection and validation completed, but preparing Discovery analytics failed. The data is safe; "
                    "run `tftlab local-collect` again later (it retries preparation).", PREPARE_FAILED)
    if not report.database_ok:
        return stop("Collection completed but the database integrity check did not pass. Restore the backup "
                    "before collecting again.", VALIDATION_FAILED)
    report.ready_for_snapshot = bool(after.get("matches")) and bool(windows)
    report.outcome = SUCCESS
    report.message = (
        f"Collection complete: {report.ingest['inserted']} new patch {window.client_patch} matches."
        + (" Ready for `tftlab local-snapshot` when you want a public website snapshot."
           if report.ready_for_snapshot else " No current-patch matches are stored yet, so no snapshot can be made.")
    )
    return report


_PREPARED_WORDS = {"published": "prepared", "skipped": "already up to date", "failed": "FAILED"}


def describe_prepared(prepared: Mapping[str, Mapping[str, Any]]) -> str:
    return "; ".join(
        f"{bw}: {_PREPARED_WORDS.get(p['status'], p['status'])}"
        + (f" ({p['carries']} carries from {p['matches']} matches)" if "carries" in p else "")
        for bw, p in prepared.items()
    )


def _riot_telemetry(client: Any) -> dict[str, Any]:
    snapshot = getattr(client, "telemetry_snapshot", None)
    if snapshot is None:
        return {}
    data = snapshot()
    return {
        "requests": data.get("requests", 0), "successes": data.get("successes", 0),
        "rate_limited": data.get("rate_limited", 0), "transient_retries": data.get("transient_retries", 0),
        "network_retries": data.get("network_retries", 0), "elapsed_s": round(float(data.get("elapsed_s") or 0), 1),
        "waited_s": round(float(data.get("pacing_sleep_s") or 0) + float(data.get("rate_limit_sleep_s") or 0)
                          + float(data.get("backoff_sleep_s") or 0), 1),
    }


def _validation_summary(report: Any) -> dict[str, Any]:
    severe = [label for label, value in (
        ("unexpected missing balance window", report.unexpected_missing_balance_window),
        ("malformed placements", report.malformed_placements),
        ("duplicate match ids", report.duplicate_match_ids),
        ("boards lost between Riot and storage", report.unexpected_participants_without_units),
    ) if value]
    warnings = []
    if report.source_empty_participants:
        warnings.append(f"{report.source_empty_participants} board(s) Riot sent without units (kept, a known Riot quirk)")
    for label, values in (("champion", report.unknown_champion_ids), ("item", report.unknown_item_ids),
                          ("trait", report.unknown_trait_ids)):
        if values:
            warnings.append(f"{len(values)} {label} id(s) not in CommunityDragon yet")
    return {"is_severe": bool(report.is_severe), "severe": severe, "warnings": warnings,
            "matches": report.total_matches, "boards": report.total_participants}


def _fill_after(report: CollectReport, db_path: Path, window: UnrealPatchWindow) -> None:
    try:
        with Database.open_existing(db_path) as ro:
            report.after = {"window": window_facts(ro, window), "store": store_facts(ro)}
    except Exception:
        report.after = {}


def write_report(report: CollectReport, reports_dir: Path, *, at_ms: int) -> Path:
    """The run's report as JSON (aggregates only: no key, PUUIDs or match ids)."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = reports_dir / f"local-collect-{stamp}.json"
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str) + "\n")
    return path


# ---------------------------------------------------------------- status


def local_status(db_path: Path, *, at_ms: int, recent_windows: int = 3) -> dict[str, Any]:
    """Everything `tftlab local-status` shows. Read-only and network-free:
    no Riot, no CommunityDragon. Aggregates only -- no identifiers."""
    status: dict[str, Any] = {"db_path": str(db_path), "exists": db_path.exists(), "now": at_ms}
    try:
        window = current_trusted_window(at_ms)
        status["trusted_window"] = {"patch": window.client_patch, "starts_at": window.starts_at,
                                    "ends_at": window.ends_at, "inside": True}
    except NoCurrentTrustedWindow as exc:
        window = None
        status["trusted_window"] = {"patch": None, "inside": False, "reason": str(exc)}
    backups = list_backups(local_dirs(db_path)["backups"])
    status["backups"] = len(backups)
    status["latest_backup"] = (
        {"name": backups[0].name, "bytes": backups[0].stat().st_size, "modified": int(backups[0].stat().st_mtime * 1000)}
        if backups else None
    )
    if not status["exists"]:
        return status
    status["size_bytes"] = db_path.stat().st_size
    from .analytics import available_balance_windows

    try:
        db = Database.open_existing(db_path)
    except Exception as exc:
        status["read_error"] = type(exc).__name__
        return status
    with db:
        status["store"] = store_facts(db)
        windows = available_balance_windows(db)
        status["windows"] = [{"balance_window": w, "matches": n, "latest_game": t} for w, n, t in windows]
        if window is not None:
            status["current"] = window_facts(db, window)
        targets = list(dict.fromkeys(
            (status.get("current", {}).get("balance_windows") or []) + [w for w, _n, _t in windows[:recent_windows]]
        ))
        prepared = {}
        for bw in targets:
            try:
                prepared[bw] = lookup_prepared(db, bw).status
            except Exception:
                prepared[bw] = "unknown"
        status["prepared"] = prepared
    return status


# ---------------------------------------------------------------- public snapshot


class LocalSnapshotError(RuntimeError):
    pass


def default_snapshot_windows(db: Database, *, at_ms: int) -> list[str]:
    """The current trusted window's balance window(s) present in the store."""
    try:
        window = current_trusted_window(at_ms)
    except NoCurrentTrustedWindow:
        raise LocalSnapshotError(
            "There is no current trusted patch window, so there is no default to export. "
            "Name a stored, trusted window explicitly with --balance-window (e.g. the previous patch)."
        ) from None
    windows = _window_balance_windows(db, window)
    if not windows:
        raise LocalSnapshotError(f"No patch {window.client_patch} matches are stored yet; run `tftlab local-collect` first.")
    return windows


def local_snapshot(db_path: Path, out_path: Path, *, at_ms: int, balance_windows: list[str] | None = None,
                   secrets: tuple[str, ...] = (), progress: Callable[[str], None] = lambda _l: None
                   ) -> tuple[Any, dict[str, Any]]:
    """Export a sanitized public snapshot of the local database with the
    existing exporter (`public_snapshot.export_public_snapshot`, which runs
    the real-ingestion checks, sanitizes and verifies), gzip it, then verify
    the written file again independently. Only reads `db_path`; never
    uploads or deploys anything. Raises SnapshotExportError/LocalSnapshotError."""
    from .public_snapshot import export_public_snapshot, verify_public_snapshot

    if not db_path.exists():
        raise LocalSnapshotError(f"There is no local database at {db_path} yet; run `tftlab local-collect` first.")
    with Database.open_existing(db_path) as db:
        windows = list(balance_windows) if balance_windows else default_snapshot_windows(db, at_ms=at_ms)
        result = export_public_snapshot(db, out_path, balance_windows=windows, compress=True, prepare=True,
                                        progress=progress, secrets=[str(db_path), *[s for s in secrets if s]])
    verification = verify_public_snapshot(result.path, secrets=[s for s in secrets if s] or None)
    return result, verification


# ---------------------------------------------------------------- setup


def init_local(root: Path, *, env_example: Path | None = None) -> list[tuple[str, str]]:
    """Create the local folders and a `.env` from `.env.example`. Never
    overwrites an existing `.env` or database; never writes a key."""
    actions: list[tuple[str, str]] = []
    db_path = (root / DEFAULT_LOCAL_DB)
    for directory in (db_path.parent, *local_dirs(db_path).values()):
        if directory.is_dir():
            actions.append((str(directory), "already exists"))
        else:
            directory.mkdir(parents=True, exist_ok=True)
            actions.append((str(directory), "created"))
    env = root / ".env"
    example = env_example or root / ".env.example"
    if env.exists():
        actions.append((str(env), "already exists, left unchanged"))
    else:
        template = example.read_text() if example.exists() else (
            "RIOT_API_KEY=RGAPI-your-key-here\nTFT_PLATFORM=na1\nTFT_REGION=americas\n"
        )
        fd = os.open(env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(template)
        actions.append((str(env), "created from .env.example -- now paste your Riot key into it"))
    actions.append((str(db_path), "already exists, left unchanged" if db_path.exists()
                    else "will be created by the first `tftlab local-collect`"))
    return actions


RIOT_KEY_RE = re.compile(r"^RGAPI-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def set_env_key(env_path: Path, api_key: str) -> None:
    """Replace (or add) the RIOT_API_KEY line of `.env`, keeping every other
    line. Written atomically and, where supported, readable by the owner only."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("RIOT_API_KEY=") and not replaced:
            out.append(f"RIOT_API_KEY={api_key}")
            replaced = True
        elif not line.strip().startswith("RIOT_API_KEY="):
            out.append(line)
    if not replaced:
        out.append(f"RIOT_API_KEY={api_key}")
    tmp = env_path.with_name(env_path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, env_path)
