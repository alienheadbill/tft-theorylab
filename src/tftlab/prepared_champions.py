"""Prepared (precomputed) Champion Investigation payloads.

A champion page (`/api/champions/<key>`) aggregates one champion's carry
boards several times over (carry statistics, item packages, partners and
cores, traits, exact boards and distributions) plus the window-wide "All
carries" average. None of it depends on anything but the balance window's
match data and the analytics code, so -- like prepared Discovery
(`tftlab.prepared_discovery`), whose run design this module follows -- it is
computed OFFLINE, once per balance window, by `prepare_champions` (the
`tftlab prepare-champions` command, and every public snapshot export), and
published as rows the web application only reads.

What one published run holds, for one balance window:

- `directory_json`: the champion picker's list (`champion_directory`), the
  body of `/api/champions`;
- one `champion_prepared_payloads` row per champion the page can resolve in
  the window (every manifest champion, every champion seen on a board): the
  complete `champion_investigation` body at `PREPARED_TOP_N`, the page's own
  `top_n`. Other `top_n` values are computed live (truncating a larger
  payload would not give the same cores, boards or "N more" counts).

A payload is built by the same function, from the same champion entry
(`resolve_champion`), as the live path, so a prepared response and a live
one are identical; the tests compare them.

Keys and freshness are prepared Discovery's: `balance_window`;
`analytics_version` (`PREPARED_FORMAT` plus a line-ending-normalized digest
of every source that shapes a champion page, so any change to that code or
data makes older runs unusable); and `source_fingerprint` (the window's
matches and data migrations). A run is served only while all three match;
otherwise the web application computes the page live and says so. Publishing
is one transaction, a failed preparation leaves a `failed` audit row that is
never read, and the fingerprint is checked again after computing.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .analytics import available_balance_windows, carry_board_average
from .champion_investigation import (
    _observed_character_ids,
    champion_directory,
    champion_investigation,
    resolve_champion,
)
from .prepared_discovery import (
    ANALYTICS_SOURCES as DISCOVERY_SOURCES,
    KEEP_FAILED_RUNS,
    KEEP_PUBLISHED_RUNS,
    MAX_ATTEMPTS,
    STATUS_FAILED,
    STATUS_PUBLISHED,
    PreparedSourceChanged,
    SourceFingerprint,
    _table_exists,
    analytics_version as _digest_version,
    source_fingerprint,
)
from .storage import Database

#: Bump when the stored row/JSON layout changes. Analytics changes are
#: picked up automatically through the source digest below.
PREPARED_FORMAT = 1

#: The `top_n` the champion page requests (champion.js) and the API default.
PREPARED_TOP_N = 6

_PACKAGE = Path(__file__).resolve().parent

#: Everything a champion page is computed from: Discovery's carry analytics,
#: every analytics module, the page's own view model and the static data it
#: names and draws. A change to any of these makes older runs unusable.
ANALYTICS_SOURCES: tuple[Path, ...] = tuple(dict.fromkeys((
    *DISCOVERY_SOURCES,
    *sorted((_PACKAGE / "analytics").glob("*.py")),
    _PACKAGE / "champion_investigation.py",
    _PACKAGE / "how_to_play.py",
    _PACKAGE / "game_art.py",
    _PACKAGE / "public_snapshot.py",  # how a snapshot's player levels are read
    _PACKAGE / "research_report.py",  # Discovery's minimum-sample constant
    _PACKAGE / "scout.py",  # the LOW SAMPLE threshold
    _PACKAGE / "data" / "game_art_manifest.json",
    Path(__file__).resolve(),
)))


def analytics_version(sources: Sequence[Path] = ANALYTICS_SOURCES) -> str:
    """`c<format>-<digest>`: the same normalized digest as prepared Discovery."""
    return f"c{PREPARED_FORMAT}-{_digest_version(sources).split('-', 1)[1]}"


ANALYTICS_VERSION = analytics_version()


def _json_default(value: Any) -> Any:
    # Postgres aggregates can come back as Decimal; FastAPI sends them as floats.
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=_json_default)


# ---------------------------------------------------------------- reading


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    balance_window: str
    analytics_version: str
    source_fingerprint: str
    source_matches: int
    champions: int
    top_n: int
    published_at: int


@dataclass(frozen=True)
class PreparedLookup:
    """`status` is `current` (serve `run`), `stale` (a run exists but its
    source or analytics version no longer matches), or `missing`."""

    status: str
    run: PreparedRun | None = None

    def describe(self, *, served: bool = True, reason: str | None = None) -> dict[str, Any]:
        """The additive `prepared` block of the champion responses. `served`
        is False when a current run exists but this response was computed
        live anyway (`reason`: "top_n" or "champion"); it then never says
        `current`."""
        status = self.status if served or self.status != "current" else "missing"
        out: dict[str, Any] = {"status": status, "analytics_version": ANALYTICS_VERSION}
        if status == "current" and self.run is not None:
            out.update(run_id=self.run.run_id, prepared_at=self.run.published_at,
                       source_matches=self.run.source_matches)
        elif reason is not None and self.status == "current":
            out["reason"] = reason
        return out


_RUN_COLUMNS = (
    "run_id, balance_window, analytics_version, source_fingerprint, source_matches, champions, top_n, published_at"
)


def _latest_published(db: Database, balance_window: str, version: str | None) -> PreparedRun | None:
    sql = f"SELECT {_RUN_COLUMNS} FROM champion_prepared_runs WHERE balance_window = ? AND status = ?"
    params: list[Any] = [balance_window, STATUS_PUBLISHED]
    if version is not None:
        sql += " AND analytics_version = ?"
        params.append(version)
    row = db.query_one(sql + " ORDER BY published_at DESC, run_id DESC LIMIT 1", params)
    if row is None:
        return None
    return PreparedRun(
        run_id=str(row[0]), balance_window=str(row[1]), analytics_version=str(row[2]),
        source_fingerprint=str(row[3]), source_matches=int(row[4]), champions=int(row[5]), top_n=int(row[6]),
        published_at=int(row[7]),
    )


def lookup_prepared(db: Database, balance_window: str) -> PreparedLookup:
    """Read-only: is there a published run that is current for this window?"""
    if not _table_exists(db, "champion_prepared_runs"):
        return PreparedLookup("missing")
    run = _latest_published(db, balance_window, ANALYTICS_VERSION)
    if run is None:
        older = _latest_published(db, balance_window, None)
        return PreparedLookup("stale" if older is not None else "missing")
    if run.source_fingerprint != source_fingerprint(db, balance_window).fingerprint:
        return PreparedLookup("stale", run)
    return PreparedLookup("current", run)


def read_prepared_directory(db: Database, run: PreparedRun) -> list[dict[str, Any]] | None:
    row = db.query_one("SELECT directory_json FROM champion_prepared_runs WHERE run_id = ?", (run.run_id,))
    return json.loads(row[0]) if row and row[0] else None


def read_prepared_champion(db: Database, run: PreparedRun, champion: dict[str, Any]) -> dict[str, Any] | None:
    """The prepared body for `champion` (a `resolve_champion` entry), or None
    when the run has none or it was built for a different entry."""
    row = db.query_one(
        "SELECT payload_json FROM champion_prepared_payloads WHERE run_id = ? AND character_id = ?",
        (run.run_id, champion["character_id"]),
    )
    if row is None:
        return None
    body = json.loads(row[0])
    expected = {k: champion[k] for k in ("character_id", "name", "slug", "cost", "art_url")}
    return body if body.get("champion") == expected else None


# ---------------------------------------------------------------- preparing


@dataclass(frozen=True)
class PrepareResult:
    balance_window: str
    status: str  # "published" | "skipped" (already current)
    run_id: str | None
    champions: int
    source_matches: int
    seconds: float


def _lock_key(balance_window: str) -> int:
    digest = hashlib.sha256(f"tftlab:prepare-champions:{balance_window}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _compute(db: Database, balance_window: str) -> tuple[str, list[tuple[str, str]]]:
    """(directory JSON, [(character_id, payload JSON)]), every payload built
    exactly as the live route builds it."""
    directory = champion_directory(db, balance_window)
    average = carry_board_average(db, balance_window)
    ids = list(dict.fromkeys([*(e["character_id"] for e in directory),
                              *sorted(_observed_character_ids(db, balance_window))]))
    payloads: dict[str, str] = {}
    for character_id in ids:
        champion = resolve_champion(db, balance_window, character_id)
        if champion is None or champion["character_id"] in payloads:
            continue
        body = champion_investigation(
            db, champion, balance_window, top_n=PREPARED_TOP_N,
            window_average=lambda _db, _w: dict(average) if average is not None else None,
        )
        payloads[champion["character_id"]] = _dumps(body)
    return _dumps(directory), list(payloads.items())


def _publish(db: Database, balance_window: str, fingerprint: SourceFingerprint, directory: str,
             payloads: Sequence[tuple[str, str]], started_at: int) -> str:
    run_id = uuid.uuid4().hex
    try:
        db.execute(
            """INSERT INTO champion_prepared_runs (
                run_id, balance_window, analytics_version, source_fingerprint, source_matches,
                source_latest_game_datetime, champions, top_n, directory_json, status, started_at, published_at,
                failure
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                run_id, balance_window, ANALYTICS_VERSION, fingerprint.fingerprint, fingerprint.matches,
                fingerprint.latest_game_datetime, len(payloads), PREPARED_TOP_N, directory, STATUS_PUBLISHED,
                started_at, int(time.time() * 1000),
            ),
        )
        db.executemany(
            "INSERT INTO champion_prepared_payloads VALUES (?, ?, ?)",
            [(run_id, character_id, payload) for character_id, payload in payloads],
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
            "SELECT run_id FROM champion_prepared_runs WHERE balance_window = ? AND status = ? "
            "ORDER BY started_at DESC, run_id DESC",
            (balance_window, status),
        )
        doomed += [str(r[0]) for r in rows[keep:]]
    for run_id in doomed:
        db.execute("DELETE FROM champion_prepared_payloads WHERE run_id = ?", (run_id,))
        db.execute("DELETE FROM champion_prepared_runs WHERE run_id = ?", (run_id,))


def _record_failure(db: Database, balance_window: str, started_at: int, exc: BaseException) -> None:
    """Best-effort audit row; only the exception type is stored."""
    try:
        db.conn.rollback()
        db.execute(
            """INSERT INTO champion_prepared_runs (
                run_id, balance_window, analytics_version, source_fingerprint, source_matches,
                source_latest_game_datetime, champions, top_n, directory_json, status, started_at, published_at,
                failure
            ) VALUES (?, ?, ?, '', 0, NULL, 0, ?, NULL, ?, ?, NULL, ?)""",
            (uuid.uuid4().hex, balance_window, ANALYTICS_VERSION, PREPARED_TOP_N, STATUS_FAILED, started_at,
             type(exc).__name__),
        )
        db.commit()
    except Exception:
        db.conn.rollback()


def prepare_window(db: Database, balance_window: str, *, force: bool = False) -> PrepareResult:
    """Compute and publish one window's prepared champion run (or skip it
    when its latest run is already current). Requires an initialized
    (writable) database."""
    if db.read_only:
        raise RuntimeError("prepare-champions needs a writable, initialized database")
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
                return PrepareResult(balance_window, "skipped", current.run_id, current.champions,
                                     current.source_matches, time.perf_counter() - t0)
            directory, payloads = _compute(db, balance_window)
            db.commit()  # never hold a read transaction open while publishing
            if source_fingerprint(db, balance_window).fingerprint != before.fingerprint:
                db.commit()
                continue  # ingestion changed the window meanwhile: never publish a mixed result
            run_id = _publish(db, balance_window, before, directory, payloads, started_at)
            return PrepareResult(balance_window, STATUS_PUBLISHED, run_id, len(payloads), before.matches,
                                 time.perf_counter() - t0)
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


def prepare_champions(
    db: Database, *, balance_windows: Sequence[str] | None = None, force: bool = False
) -> list[PrepareResult]:
    """`prepare_window` for the given windows (default: every window in the
    store, latest first). Stops at the first failure; earlier windows'
    publications stand."""
    windows = list(balance_windows) if balance_windows else [w for w, _n, _t in available_balance_windows(db)]
    return [prepare_window(db, w, force=force) for w in windows]
