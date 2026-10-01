"""Prepared (precomputed) Discovery analytics.

Discovery's expensive part -- the window-wide carry population plus
partner/item/trait evidence for every carry -- depends only on the balance
window's match data and the analytics code, never on a request's cost,
sample, `top_n` or `limit` filters. So it is computed OFFLINE, once per
balance window, by `prepare_discovery` (the `tftlab prepare-discovery`
command, run by the live-ingest workflow after each ingest), and published
as rows the web application only reads and filters.

What one published run holds: every carry in the window (any cost,
`min_samples=1`), each as its complete `DiscoveryCandidate` -- the same
object `discover_candidates` builds -- with evidence lists kept to
`PREPARED_TOP_N` (the API's own `top_n` maximum). A request takes the rows
matching its costs and `min_samples`, ordered exactly like
`discover_candidates` (Opportunity Score descending, ties in population
order), truncates the evidence lists to its `top_n`, and applies `limit`.

Keys and freshness. A run is only served when all three match:

- `balance_window` -- never mixed;
- `analytics_version` -- `PREPARED_FORMAT` plus a digest of the analytics
  source files and data snapshots that define carries and evidence, so any
  change to that code or data (e.g. a deploy) makes older runs unusable
  automatically, without anyone remembering to bump a number;
- `source_fingerprint` -- the window's match count, latest and summed game
  timestamps, and the `schema_migrations` marker (in-place data migrations
  such as the completed-item recount). Ingestion only ever adds matches, so
  any new, moved or removed match in the window, or a data migration,
  changes it.

Publishing is one transaction: the run row and all of its candidate rows
are inserted together, so a reader sees a complete run or none. A failed or
interrupted preparation rolls back (and, best effort, leaves a `failed`
audit row that is never read as a result); the previous published run stays
the latest one. The fingerprint is read before and after computing; if
ingestion changed the window meanwhile, nothing is published for that
attempt. On Postgres, concurrent preparations of one window are serialized
with an advisory lock, and the second one skips if the first already
published a current run.

When no current run exists (none prepared yet, or new matches/new code made
it stale) the web application falls back to computing Discovery live, as it
did before prepared runs existed, and says so in the response: stale
evidence is never served as current.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Collection, Sequence

from .analytics.commitment import available_balance_windows
from .analytics.discovery import DiscoveryCandidate, discovery_population, population_candidates
from .storage import Database

#: Bump when the stored row/JSON layout changes. Analytics changes are
#: picked up automatically through the source digest below.
PREPARED_FORMAT = 1

#: Evidence kept per candidate: the API's `top_n` maximum (le=20 on both
#: Discovery endpoints), so every allowed request can be answered exactly.
PREPARED_TOP_N = 20

STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"

#: Published runs kept per balance window (older ones are pruned when a new
#: run is published); failed audit rows kept per window.
KEEP_PUBLISHED_RUNS = 3
KEEP_FAILED_RUNS = 5

#: Attempts per window when ingestion changes the source while computing.
MAX_ATTEMPTS = 3

_PACKAGE = Path(__file__).resolve().parent

#: Everything that decides which boards are carry boards and what evidence
#: and Opportunity Score a carry gets. A change to any of these makes older
#: prepared runs unusable.
ANALYTICS_SOURCES: tuple[Path, ...] = (
    _PACKAGE / "analytics" / "association.py",
    _PACKAGE / "analytics" / "commitment.py",
    _PACKAGE / "analytics" / "discovery.py",
    _PACKAGE / "analytics" / "item_packages.py",
    _PACKAGE / "analytics" / "partners.py",
    _PACKAGE / "analytics" / "traits.py",
    _PACKAGE / "carry.py",
    _PACKAGE / "items.py",
    # Equipped vs generated items (Thief's Gloves) and intrinsic traits.
    _PACKAGE / "itemization.py",
    _PACKAGE / "roster.py",
    _PACKAGE / "data" / "item_intent.json",
    _PACKAGE / "data" / "item_stats.json",
    _PACKAGE / "data" / "set_roster.json",
    Path(__file__).resolve(),
)


def analytics_version(sources: Sequence[Path] = ANALYTICS_SOURCES) -> str:
    digest = hashlib.sha256()
    for path in sources:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"v{PREPARED_FORMAT}-{digest.hexdigest()[:16]}"


ANALYTICS_VERSION = analytics_version()


class PreparedSourceChanged(RuntimeError):
    """The window's source data changed while it was being prepared."""


# ---------------------------------------------------------------- freshness


@dataclass(frozen=True)
class SourceFingerprint:
    matches: int
    latest_game_datetime: int
    fingerprint: str


def _table_exists(db: Database, table: str) -> bool:
    if db.dialect == "postgres":
        row = db.query_one("SELECT to_regclass(?) IS NOT NULL", (table,))
        return bool(row and row[0])
    row = db.query_one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    return row is not None


def source_fingerprint(db: Database, balance_window: str) -> SourceFingerprint:
    """The window's match population, plus applied in-place data migrations."""
    row = db.query_one(
        "SELECT COUNT(*), MAX(game_datetime), SUM(game_datetime) FROM matches WHERE balance_window = ?",
        (balance_window,),
    )
    count, latest, total = (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)) if row else (0, 0, 0)
    migrations = "none"
    if _table_exists(db, "schema_migrations"):
        m = db.query_one("SELECT COUNT(*), MAX(applied_at) FROM schema_migrations")
        migrations = f"{int(m[0] or 0)}:{int(m[1] or 0)}" if m else "none"
    return SourceFingerprint(
        matches=count,
        latest_game_datetime=latest,
        fingerprint=f"matches={count};latest={latest};sum={total};migrations={migrations}",
    )


# ---------------------------------------------------------------- reading


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    balance_window: str
    analytics_version: str
    source_fingerprint: str
    source_matches: int
    window_carries: int
    published_at: int


@dataclass(frozen=True)
class PreparedLookup:
    """`status` is `current` (serve `run`), `stale` (a run exists but its
    source or analytics version no longer matches), or `missing`."""

    status: str
    run: PreparedRun | None = None

    def describe(self) -> dict[str, Any]:
        """The additive `prepared` block of the Discovery API responses."""
        out: dict[str, Any] = {"status": self.status, "analytics_version": ANALYTICS_VERSION}
        if self.status == "current" and self.run is not None:
            out.update(
                run_id=self.run.run_id,
                prepared_at=self.run.published_at,
                source_matches=self.run.source_matches,
            )
        return out


_RUN_COLUMNS = (
    "run_id, balance_window, analytics_version, source_fingerprint, source_matches, window_carries, published_at"
)


def _latest_published(db: Database, balance_window: str, version: str | None) -> PreparedRun | None:
    sql = f"SELECT {_RUN_COLUMNS} FROM discovery_prepared_runs WHERE balance_window = ? AND status = ?"
    params: list[Any] = [balance_window, STATUS_PUBLISHED]
    if version is not None:
        sql += " AND analytics_version = ?"
        params.append(version)
    row = db.query_one(sql + " ORDER BY published_at DESC, run_id DESC LIMIT 1", params)
    if row is None:
        return None
    return PreparedRun(
        run_id=str(row[0]), balance_window=str(row[1]), analytics_version=str(row[2]),
        source_fingerprint=str(row[3]), source_matches=int(row[4]), window_carries=int(row[5]),
        published_at=int(row[6]),
    )


def lookup_prepared(db: Database, balance_window: str) -> PreparedLookup:
    """Read-only: is there a published run that is current for this window?"""
    if not _table_exists(db, "discovery_prepared_runs"):
        return PreparedLookup("missing")
    run = _latest_published(db, balance_window, ANALYTICS_VERSION)
    if run is None:
        older = _latest_published(db, balance_window, None)
        return PreparedLookup("stale" if older is not None else "missing")
    if run.source_fingerprint != source_fingerprint(db, balance_window).fingerprint:
        return PreparedLookup("stale", run)
    return PreparedLookup("current", run)


def _truncate(candidate: dict[str, Any], top_n: int) -> dict[str, Any]:
    for key in ("best_partners", "best_item_packages", "best_trait_breakpoints"):
        candidate[key] = candidate[key][:top_n]
    return candidate


def read_prepared_candidates(
    db: Database,
    run: PreparedRun,
    *,
    costs: Collection[int],
    min_samples: int,
    top_n: int,
    limit: int,
) -> list[dict[str, Any]]:
    """The run's candidates for these filters, as `asdict(DiscoveryCandidate)`
    dicts in `discover_candidates` order (score descending, ties in
    population order). No aggregation: an indexed read of at most `limit`
    rows."""
    wanted = sorted({int(c) for c in costs})
    if not wanted:
        return []
    rows = db.query_all(
        f"""
        SELECT candidate_json FROM discovery_prepared_candidates
        WHERE run_id = ? AND cost IN ({", ".join("?" for _ in wanted)}) AND commitment_games >= ?
        ORDER BY opportunity_score DESC, population_rank ASC
        LIMIT ?
        """,
        (run.run_id, *wanted, min_samples, limit),
    )
    return [_truncate(json.loads(r[0]), top_n) for r in rows]


def read_prepared_candidate(db: Database, run: PreparedRun, character_id: str, *, top_n: int) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT candidate_json FROM discovery_prepared_candidates WHERE run_id = ? AND character_id = ?",
        (run.run_id, character_id),
    )
    return _truncate(json.loads(row[0]), top_n) if row else None


# ---------------------------------------------------------------- preparing


@dataclass(frozen=True)
class PrepareResult:
    balance_window: str
    status: str  # "published" | "skipped" (already current)
    run_id: str | None
    window_carries: int
    source_matches: int
    seconds: float


def _lock_key(balance_window: str) -> int:
    digest = hashlib.sha256(f"tftlab:prepare-discovery:{balance_window}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _compute(db: Database, balance_window: str) -> tuple[list[DiscoveryCandidate], int]:
    population = discovery_population(db, balance_window)
    return population_candidates(db, balance_window, population, top_n=PREPARED_TOP_N), len(population)


def _publish(
    db: Database,
    balance_window: str,
    fingerprint: SourceFingerprint,
    candidates: Sequence[DiscoveryCandidate],
    window_carries: int,
    started_at: int,
) -> str:
    run_id = uuid.uuid4().hex
    try:
        db.execute(
            """INSERT INTO discovery_prepared_runs (
                run_id, balance_window, analytics_version, source_fingerprint, source_matches,
                source_latest_game_datetime, window_carries, status, started_at, published_at, failure
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                run_id, balance_window, ANALYTICS_VERSION, fingerprint.fingerprint, fingerprint.matches,
                fingerprint.latest_game_datetime, window_carries, STATUS_PUBLISHED, started_at,
                int(time.time() * 1000),
            ),
        )
        db.executemany(
            "INSERT INTO discovery_prepared_candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, c.character_id, c.cost, c.commitment_games, rank, c.opportunity_score,
                    json.dumps(asdict(c), separators=(",", ":")),
                )
                for rank, c in enumerate(candidates)
            ],
        )
        _prune(db, balance_window)
    except Exception:
        db.conn.rollback()
        raise
    db.commit()
    return run_id


def _prune(db: Database, balance_window: str) -> None:
    """Drop all but the newest published and failed runs of this window
    (inside the publishing transaction)."""
    doomed: list[str] = []
    for status, keep in ((STATUS_PUBLISHED, KEEP_PUBLISHED_RUNS), (STATUS_FAILED, KEEP_FAILED_RUNS)):
        rows = db.query_all(
            "SELECT run_id FROM discovery_prepared_runs WHERE balance_window = ? AND status = ? "
            "ORDER BY started_at DESC, run_id DESC",
            (balance_window, status),
        )
        doomed += [str(r[0]) for r in rows[keep:]]
    for run_id in doomed:
        db.execute("DELETE FROM discovery_prepared_candidates WHERE run_id = ?", (run_id,))
        db.execute("DELETE FROM discovery_prepared_runs WHERE run_id = ?", (run_id,))


def _record_failure(db: Database, balance_window: str, started_at: int, exc: BaseException) -> None:
    """Best-effort audit row. Only the exception type is stored (never its
    message, which could echo connection details)."""
    try:
        db.conn.rollback()
        db.execute(
            """INSERT INTO discovery_prepared_runs (
                run_id, balance_window, analytics_version, source_fingerprint, source_matches,
                source_latest_game_datetime, window_carries, status, started_at, published_at, failure
            ) VALUES (?, ?, ?, '', 0, NULL, 0, ?, ?, NULL, ?)""",
            (uuid.uuid4().hex, balance_window, ANALYTICS_VERSION, STATUS_FAILED, started_at, type(exc).__name__),
        )
        db.commit()
    except Exception:
        db.conn.rollback()


def prepare_window(db: Database, balance_window: str, *, force: bool = False) -> PrepareResult:
    """Compute and publish one window's prepared Discovery run (or skip it
    when its latest run is already current). Requires an initialized
    (writable) database."""
    if db.read_only:
        raise RuntimeError("prepare-discovery needs a writable, initialized database")
    t0 = time.perf_counter()
    started_at = int(time.time() * 1000)
    locked = False
    try:
        if db.dialect == "postgres":
            db.query_one("SELECT pg_advisory_lock(?)", (_lock_key(balance_window),))
            locked = True
        for _attempt in range(MAX_ATTEMPTS):
            before = source_fingerprint(db, balance_window)
            current = _latest_published(db, balance_window, ANALYTICS_VERSION)
            if not force and current is not None and current.source_fingerprint == before.fingerprint:
                db.commit()  # end the read transaction
                return PrepareResult(
                    balance_window, "skipped", current.run_id, current.window_carries,
                    current.source_matches, time.perf_counter() - t0,
                )
            candidates, window_carries = _compute(db, balance_window)
            # End the read transaction now: an idle open transaction would
            # hold table locks that a concurrent ingest's schema setup waits on.
            db.commit()
            if source_fingerprint(db, balance_window).fingerprint != before.fingerprint:
                db.commit()
                continue  # ingestion changed the window meanwhile: never publish a mixed result
            run_id = _publish(db, balance_window, before, candidates, window_carries, started_at)
            return PrepareResult(
                balance_window, STATUS_PUBLISHED, run_id, window_carries, before.matches, time.perf_counter() - t0,
            )
        raise PreparedSourceChanged(
            f"balance window {balance_window!r} kept changing during {MAX_ATTEMPTS} preparation attempts"
        )
    except BaseException as exc:
        _record_failure(db, balance_window, started_at, exc)
        raise
    finally:
        if locked:
            try:
                db.query_one("SELECT pg_advisory_unlock(?)", (_lock_key(balance_window),))
                db.commit()
            except Exception:
                db.conn.rollback()


def prepare_discovery(
    db: Database, *, balance_windows: Sequence[str] | None = None, force: bool = False
) -> list[PrepareResult]:
    """`prepare_window` for the given windows (default: every window in the
    store, latest first). Stops at the first failure; earlier windows'
    publications stand."""
    windows = list(balance_windows) if balance_windows else [w for w, _n, _t in available_balance_windows(db)]
    return [prepare_window(db, w, force=force) for w in windows]
