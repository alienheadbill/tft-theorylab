"""Sanitized public website snapshots (`tftlab export-public-snapshot`).

A public snapshot is a SQLite file the read-only website can serve with
`TFT_DATA_SOURCE=snapshot` and no cloud database. It is built from a real
TheoryLabs database (local SQLite or a restored Postgres backup), never by
copying that database:

- Only what the current public routes read is exported: the match tables in
  `Database.REQUIRED_READ_SCHEMA` (matches / participants / units / traits),
  the operator's own (non-demo) Experiments notebook, and prepared Discovery
  runs computed on the snapshot itself. Collection-only tables
  (`seed_samples`, `match_discoveries`, `ingest_runs`) are absent, and
  storage columns no public route reads are sanitized (raw Match-V1
  `payload_json` -> `{}`, `augments_json` -> `[]`, ...); see
  `SANITIZED_COLUMNS`.
- Riot match ids are replaced by opaque snapshot-local ids (`S0000001`, ...)
  numbered by (game time, source id), consistently in every table. The
  mapping is never stored; only a one-way fingerprint of the whole source
  population is.
- The snapshot carries versioned provenance (`public_snapshot_metadata`).
  The website treats a snapshot as observed Riot evidence ONLY when that
  provenance was written by this exporter after the source passed the
  real-ingestion checks below (`read_snapshot_provenance`); anything else
  fails closed.

Real-ingestion checks (fail closed; every one must hold): the source has
completed live-ingest runs (`ingest_runs`) whose discovery ledger
(`match_discoveries`) links to exported matches, and every exported match is
a ranked (queue 1100) Riot Match-V1 record -- a regional Riot match id, the
stored raw payload's `metadata.match_id` / `data_version` / participant list
matching it, and a trusted numeric patch balance window. The deterministic
demo dataset (`generate_demo_matches`) fails all of these. The checks guard
against mistakes, not against someone deliberately forging a database.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .riot import RANKED_TFT_QUEUE_ID
from .storage import REQUIRED_READ_SCHEMA, Database

SNAPSHOT_FORMAT = "theorylabs-public-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
SOURCE_KIND = "riot-match-v1-ranked-tft"
METADATA_TABLE = "public_snapshot_metadata"
PROVENANCE_KEY = "provenance"
EXPORTER = "tftlab export-public-snapshot"

#: Riot platform routing values that prefix Match-V1 match ids (e.g. NA1_...).
RIOT_PLATFORMS = ("BR1", "EUN1", "EUW1", "JP1", "KR", "LA1", "LA2", "ME1", "NA1", "OC1", "PH2", "RU", "SG2",
                  "TH2", "TR1", "TW2", "VN2")
_RIOT_MATCH_ID_RE = re.compile(rf"^(?:{'|'.join(RIOT_PLATFORMS)})_\d{{6,}}$")
#: A trusted analytical balance window: a numeric patch, optionally with a
#: mid-patch suffix (18.3, 18.2b). Never "Version DEMO" or the unresolved
#: Unreal sentinel.
_TRUSTED_WINDOW_RE = re.compile(r"^\d{1,3}\.\d{1,3}[a-z]?$")
#: Riot PUUIDs are 78-character url-safe base64 strings.
_PUUID_LIKE_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{78}(?![A-Za-z0-9_-])")
_SECRET_MARKERS = ("RGAPI-", "postgres://", "postgresql://")

#: Columns copied into the snapshot, per table (the order of the INSERTs).
EXPORTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "matches": ("match_id", "game_datetime", "game_version", "patch", "balance_window", "game_type", "queue_id",
                "set_number", "set_core_name", "payload_json"),
    "participants": ("match_id", "participant_index", "placement", "level", "augments_json"),
    "units": ("match_id", "participant_index", "unit_index", "character_id", "unit_name", "cost", "tier",
              "items_json", "completed_item_count"),
    "traits": ("match_id", "participant_index", "trait_name", "num_units", "style", "tier_current", "tier_total"),
}
#: Storage columns no public route reads (see `REQUIRED_READ_SCHEMA`), and
#: the fixed value each one gets in the snapshot. `game_version` and
#: `queue_id` ARE kept: non-identifying, and they let a reader check the
#: rows are ranked Riot matches classified by patch.
SANITIZED_COLUMNS: dict[str, dict[str, Any]] = {
    "matches": {"payload_json": "{}", "game_type": None, "set_number": None, "set_core_name": None},
    "participants": {"augments_json": "[]", "level": 0},
}
#: Collection-only tables that must never be in a public snapshot.
EXCLUDED_TABLES = ("seed_samples", "match_discoveries", "ingest_runs")
EXPERIMENT_COLUMNS = {
    "experiments": REQUIRED_READ_SCHEMA["experiments"],
    "experiment_tags": REQUIRED_READ_SCHEMA["experiment_tags"],
    "experiment_field_notes": REQUIRED_READ_SCHEMA["experiment_field_notes"],
}
EXCLUSION_STATEMENT = (
    "Raw Riot Match-V1 payloads, augment payloads, player identifiers (PUUIDs, Riot IDs), Riot match ids, the "
    "seed/sampling ledger, match discovery provenance and ingestion-run records were excluded. Match ids are "
    "opaque snapshot-local ids; no mapping to Riot match ids is included.")
_CHUNK = 400


class SnapshotExportError(RuntimeError):
    """The source cannot be exported as observed public evidence, or the
    built snapshot failed verification. Nothing is written in either case."""


class SnapshotProvenanceError(RuntimeError):
    """A file is not a verified TheoryLabs public snapshot."""


@dataclass
class ExportResult:
    path: Path
    manifest_path: Path
    gzip_path: Path | None
    provenance: dict[str, Any]
    verification: dict[str, Any]
    files: dict[str, dict[str, Any]] = field(default_factory=dict)


def _noop(_: str) -> None:
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _table_exists(db: Database, table: str) -> bool:
    try:
        db.query_one(f"SELECT 1 FROM {table} WHERE 1 = 0")
        return True
    except Exception:
        if db.dialect == "postgres":
            db.conn.rollback()  # a failed statement aborts the read-only transaction
        return False


def code_version() -> dict[str, Any]:
    """The commit/run that produced a snapshot, where available: the GitHub
    Actions default variables, else the local git HEAD. Never secrets."""
    out = {key.lower(): os.environ[key] for key in ("GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
           if os.environ.get(key)}
    if "github_sha" not in out:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent, capture_output=True,
                                 text=True, timeout=5, check=True).stdout.strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                out["git_sha"] = sha
        except Exception:
            pass
    from .prepared_discovery import ANALYTICS_VERSION
    return {**out, "analytics_version": ANALYTICS_VERSION} if out else {"available": False,
                                                                         "analytics_version": ANALYTICS_VERSION}


# ---------------------------------------------------------------- source checks


def _check_source(db: Database, balance_windows: Sequence[str] | None) -> dict[str, Any]:
    """Select the exported population and run the real-ingestion checks.
    Returns the source facts the export needs; raises SnapshotExportError."""
    rows = db.query_all("SELECT DISTINCT balance_window FROM matches WHERE balance_window IS NOT NULL")
    present = sorted(r[0] for r in rows)
    windows = list(dict.fromkeys(balance_windows)) if balance_windows else present
    if not windows:
        raise SnapshotExportError("the source holds no classified matches (no balance window)")
    untrusted = [w for w in windows if not _TRUSTED_WINDOW_RE.fullmatch(str(w))]
    if untrusted:
        raise SnapshotExportError(f"not trusted patch balance windows: {untrusted} (synthetic/demo data is never "
                                  "exported as observed evidence)")
    missing = [w for w in windows if w not in present]
    if missing:
        raise SnapshotExportError(f"balance windows not present in the source: {missing}")

    # 1. A live-ingest database: completed runs and their discovery ledger.
    for table in ("ingest_runs", "match_discoveries"):
        if not _table_exists(db, table):
            raise SnapshotExportError(f"the source has no `{table}` table: it is not a TheoryLabs live-ingest "
                                      "database, so its matches cannot be labelled observed Riot evidence")
    completed = db.query_one("SELECT COUNT(*) FROM ingest_runs WHERE status = 'completed'")[0]
    if not completed:
        raise SnapshotExportError("the source has no completed live-ingest run")

    marks = ",".join("?" * len(windows))
    population = db.query_all(
        f"SELECT match_id, game_datetime, balance_window FROM matches WHERE balance_window IN ({marks}) "
        "ORDER BY game_datetime, match_id", windows)
    ids = [r[0] for r in population]
    linked = db.query_one(
        f"""SELECT COUNT(DISTINCT d.match_id) FROM match_discoveries d
            JOIN ingest_runs r ON r.run_id = d.run_id AND r.status = 'completed'
            JOIN matches m ON m.match_id = d.match_id
            WHERE m.balance_window IN ({marks})""", windows)[0]
    if not linked:
        raise SnapshotExportError("no exported match is linked to a completed live-ingest run in the discovery ledger")

    # 2. Every exported match is a ranked Riot Match-V1 record.
    failures: dict[str, int] = {}
    puuids: set[str] = set()

    def fail(reason: str) -> None:
        failures[reason] = failures.get(reason, 0) + 1

    boards = {m: n for m, n in db.query_all(
        f"""SELECT p.match_id, COUNT(*) FROM participants p JOIN matches m ON m.match_id = p.match_id
            WHERE m.balance_window IN ({marks}) GROUP BY p.match_id""", windows)}
    for chunk in _chunks(ids):
        q = ",".join("?" * len(chunk))
        for match_id, queue_id, patch, payload_json in db.query_all(
                f"SELECT match_id, queue_id, patch, payload_json FROM matches WHERE match_id IN ({q})", list(chunk)):
            if queue_id != RANKED_TFT_QUEUE_ID:
                fail("queue_id is not ranked TFT (1100)")
            if not _RIOT_MATCH_ID_RE.fullmatch(str(match_id)):
                fail("match id is not a regional Riot Match-V1 id")
            if not (patch and re.fullmatch(r"\d{1,3}\.\d{1,3}", str(patch))):
                fail("patch is not a numeric client patch")
            try:
                payload = json.loads(payload_json)
                meta, info = payload.get("metadata") or {}, payload.get("info") or {}
            except (TypeError, ValueError, AttributeError):
                fail("raw Match-V1 payload missing or unreadable")
                continue
            if meta.get("match_id") != match_id:
                fail("payload metadata.match_id does not match")
            if not meta.get("data_version"):
                fail("payload has no Match-V1 data_version")
            participants = meta.get("participants")
            if not (isinstance(participants, list) and participants and len(participants) == boards.get(match_id, -1)
                    and all(isinstance(p, str) and p for p in participants)):
                fail("payload participant list does not match the stored boards")
            else:
                puuids.update(participants)
            if info.get("queue_id") != RANKED_TFT_QUEUE_ID:
                fail("payload queue_id is not ranked TFT (1100)")
    if failures:
        detail = "; ".join(f"{reason}: {n}" for reason, n in sorted(failures.items()))
        raise SnapshotExportError(f"{sum(failures.values())} real-ingestion check failure(s) across "
                                  f"{len(ids)} matches -- {detail}")

    if _table_exists(db, "seed_samples"):
        puuids.update(r[0] for r in db.query_all("SELECT DISTINCT puuid FROM seed_samples"))
    puuids.update(r[0] for r in db.query_all("SELECT DISTINCT puuid FROM match_discoveries"))
    per_window = {w: {"matches": 0, "latest_game_datetime": None, "earliest_game_datetime": None} for w in windows}
    for _m, when, window in population:
        row = per_window[window]
        row["matches"] += 1
        row["earliest_game_datetime"] = when if row["earliest_game_datetime"] is None else min(row["earliest_game_datetime"], when)
        row["latest_game_datetime"] = when if row["latest_game_datetime"] is None else max(row["latest_game_datetime"], when)
    return {
        "windows": windows, "population": population, "match_ids": ids, "puuids": puuids, "boards": boards,
        "completed_ingest_runs": completed, "matches_linked_to_completed_runs": linked, "per_window": per_window,
        "fingerprint": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest(),
    }


# ---------------------------------------------------------------- build


def _copy_matches(source: Database, out: sqlite3.Connection, population: Sequence[tuple], mapping: dict[str, str]) -> dict:
    counts = {table: 0 for table in EXPORTED_COLUMNS}
    ids = [r[0] for r in population]
    for table, columns in EXPORTED_COLUMNS.items():
        sanitized = SANITIZED_COLUMNS.get(table, {})
        select = ", ".join(columns)
        insert = f"INSERT INTO {table} ({select}) VALUES ({', '.join('?' * len(columns))})"
        order = {"matches": "match_id", "participants": "match_id, participant_index",
                 "units": "match_id, participant_index, unit_index",
                 "traits": "match_id, participant_index, trait_name"}[table]
        for chunk in _chunks(ids):
            q = ",".join("?" * len(chunk))
            rows = source.query_all(f"SELECT {select} FROM {table} WHERE match_id IN ({q}) ORDER BY {order}", list(chunk))
            cleaned = []
            for row in rows:
                values = dict(zip(columns, row))
                values["match_id"] = mapping[values["match_id"]]
                values.update(sanitized)
                cleaned.append(tuple(values[c] for c in columns))
            out.executemany(insert, cleaned)
            counts[table] += len(cleaned)
    return counts


def _copy_experiments(source: Database, out: sqlite3.Connection) -> dict[str, int]:
    """The operator's own notebook (origin 'manual'), which the public site
    already shows; the demo notebook entries are never exported."""
    counts = {table: 0 for table in EXPERIMENT_COLUMNS}
    if not all(_table_exists(source, t) for t in EXPERIMENT_COLUMNS):
        return counts
    ids = [r[0] for r in source.query_all("SELECT experiment_id FROM experiments WHERE origin = 'manual' ORDER BY experiment_id")]
    for table, columns in EXPERIMENT_COLUMNS.items():
        select = ", ".join(columns)
        insert = f"INSERT INTO {table} ({select}) VALUES ({', '.join('?' * len(columns))})"
        for chunk in _chunks(ids):
            q = ",".join("?" * len(chunk))
            rows = source.query_all(f"SELECT {select} FROM {table} WHERE experiment_id IN ({q})", list(chunk))
            if table == "experiments":
                origin = columns.index("origin")
                rows = [r for r in rows if r[origin] == "manual"]
            out.executemany(insert, [tuple(r) for r in rows])
            counts[table] += len(rows)
    return counts


def export_public_snapshot(source: Database, out_path: Path | str, *, balance_windows: Sequence[str] | None = None,
                           compress: bool = False, prepare: bool = True, progress: Callable[[str], None] = _noop,
                           exported_at_ms: int | None = None, secrets: Sequence[str] = ()) -> ExportResult:
    """Build, verify and atomically publish a public snapshot of `source`
    (which is only read). Raises SnapshotExportError, leaving nothing behind,
    when the source fails the real-ingestion checks or the result fails
    verification. `secrets` (e.g. the source connection string) are values
    the finished file is scanned for, besides RIOT_API_KEY/DATABASE_URL."""
    from .prepared_discovery import prepare_discovery

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    progress("checking the source (real-ingestion provenance, per-match Match-V1 checks)")
    facts = _check_source(source, balance_windows)
    progress(f"source checks passed: {len(facts['match_ids'])} matches in {facts['windows']}")
    mapping = {match_id: f"S{i:07d}" for i, match_id in enumerate(facts["match_ids"], start=1)}

    build = out_path.with_name(out_path.name + ".building")
    for stale in (build, Path(str(build) + "-journal"), Path(str(build) + "-wal")):
        stale.unlink(missing_ok=True)
    try:
        # Full TheoryLabs schema on an EMPTY file (so no migration ever touches
        # the sanitized rows), then the collection-only tables are dropped.
        with Database(build) as db:
            progress("copying matches, boards, units and traits with opaque match ids")
            counts = _copy_matches(source, db.conn, facts["population"], mapping)
            counts.update(_copy_experiments(source, db.conn))
            db.commit()
            prepared = []
            if prepare:
                # An optimization only: a window whose preparation fails is
                # computed live by the website, exactly as without a run.
                progress("preparing Discovery runs on the snapshot")
                for window in facts["windows"]:
                    try:
                        prepared += [{"balance_window": r.balance_window, "status": r.status}
                                     for r in prepare_discovery(db, balance_windows=[window])]
                    except Exception as exc:
                        db.conn.rollback()
                        prepared.append({"balance_window": window, "status": "failed", "error": type(exc).__name__})
                        progress(f"  {window}: Discovery preparation failed ({type(exc).__name__}); served live instead")
            for table in EXCLUDED_TABLES:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            boards = sum(facts["boards"].values())
            latest = max((r[1] for r in facts["population"] if r[1] is not None), default=None)
            provenance = {
                "format": SNAPSHOT_FORMAT,
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "synthetic": False,
                "observed": True,
                "source_kind": SOURCE_KIND,
                "source_description": "Ranked TFT (queue 1100) matches retrieved from the Riot Games Match-V1 API by "
                                      "TheoryLabs live ingestion.",
                "exporter": EXPORTER,
                "exported_at": _iso(exported_at_ms if exported_at_ms is not None else int(time.time() * 1000)),
                "code_version": code_version(),
                "balance_windows": facts["windows"],
                "per_window": {w: {**v, "earliest_game": _iso(v["earliest_game_datetime"]),
                                   "latest_game": _iso(v["latest_game_datetime"])}
                               for w, v in facts["per_window"].items()},
                "matches": len(facts["match_ids"]),
                "boards": boards,
                "units": counts["units"],
                "traits": counts["traits"],
                "latest_game_datetime": latest,
                "latest_game": _iso(latest),
                "experiments": counts["experiments"],
                "prepared_discovery": prepared,
                "source_population_fingerprint": {
                    "algorithm": "sha256 of the sorted source match ids, newline-joined (one-way; no per-match mapping)",
                    "value": facts["fingerprint"]},
                "real_ingestion_checks": {
                    "passed": True,
                    "completed_ingest_runs": facts["completed_ingest_runs"],
                    "matches_linked_to_completed_runs": facts["matches_linked_to_completed_runs"],
                    "discovery_ledger_coverage": round(facts["matches_linked_to_completed_runs"] / len(facts["match_ids"]), 4),
                    "per_match": "queue 1100; regional Riot match id; raw payload metadata.match_id, data_version and "
                                 "participant list consistent with the stored boards; numeric client patch; trusted "
                                 "balance window",
                },
                "tables": sorted([*EXPORTED_COLUMNS, *EXPERIMENT_COLUMNS, "discovery_prepared_runs",
                                  "discovery_prepared_candidates", "schema_migrations", METADATA_TABLE]),
                "sanitized_columns": SANITIZED_COLUMNS,
                "excluded_tables": list(EXCLUDED_TABLES),
                "match_id_scheme": "opaque snapshot-local ids S0000001.. numbered by (game_datetime, source match id); "
                                   "the mapping is not stored",
                "exclusions": EXCLUSION_STATEMENT,
            }
            db.execute(f"CREATE TABLE {METADATA_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(f"INSERT INTO {METADATA_TABLE} (key, value) VALUES (?, ?)",
                       (PROVENANCE_KEY, json.dumps(provenance, sort_keys=True)))
            db.commit()
            # One self-contained, compact file: no WAL/-shm sidecars to ship.
            db.conn.execute("PRAGMA journal_mode=DELETE")
            db.conn.execute("VACUUM")
        progress("verifying the snapshot")
        verification = verify_public_snapshot(build, source_match_ids=facts["match_ids"], puuids=facts["puuids"],
                                              expected_counts={"matches": len(facts["match_ids"]), "participants": boards,
                                                               "units": counts["units"], "traits": counts["traits"]},
                                              expected_windows=facts["windows"],
                                              secrets=[*secrets, *(os.environ.get(k) or "" for k in ("RIOT_API_KEY", "DATABASE_URL"))])
    except BaseException:
        for leftover in (build, *(Path(str(build) + s) for s in ("-wal", "-shm", "-journal"))):
            leftover.unlink(missing_ok=True)
        raise
    for sidecar in ("-wal", "-shm", "-journal"):
        Path(str(build) + sidecar).unlink(missing_ok=True)
        Path(str(out_path) + sidecar).unlink(missing_ok=True)
    os.replace(build, out_path)
    files = {out_path.name: {"bytes": out_path.stat().st_size, "sha256": _sha256_file(out_path)}}
    gz_path = None
    if compress:
        gz_path = out_path.with_name(out_path.name + ".gz")
        tmp = gz_path.with_name(gz_path.name + ".tmp")
        with out_path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, gz_path)
        files[gz_path.name] = {"bytes": gz_path.stat().st_size, "sha256": _sha256_file(gz_path)}
    manifest_path = out_path.with_name(out_path.name + ".manifest.json")
    manifest = {"provenance": provenance, "verification": verification, "files": files}
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    progress(f"snapshot verified and written: {out_path} ({files[out_path.name]['bytes']} bytes)")
    return ExportResult(out_path, manifest_path, gz_path, provenance, verification, files)


# ---------------------------------------------------------------- reading and verification


def read_snapshot_provenance(db: Database) -> dict[str, Any]:
    """The provenance of a verified public snapshot. Raises
    SnapshotProvenanceError unless it was written by this exporter's format,
    says observed and non-synthetic, and records passed real-ingestion
    checks -- a structurally valid database alone is never enough."""
    try:
        row = db.query_one(f"SELECT value FROM {METADATA_TABLE} WHERE key = ?", (PROVENANCE_KEY,))
    except Exception as exc:
        raise SnapshotProvenanceError("no snapshot provenance (not produced by the public snapshot exporter)") from exc
    if not row:
        raise SnapshotProvenanceError("no snapshot provenance record")
    try:
        prov = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise SnapshotProvenanceError("snapshot provenance is unreadable") from exc
    checks = prov.get("real_ingestion_checks") if isinstance(prov, dict) else None
    if not (isinstance(prov, dict) and prov.get("format") == SNAPSHOT_FORMAT
            and prov.get("format_version") == SNAPSHOT_FORMAT_VERSION and prov.get("synthetic") is False
            and prov.get("observed") is True and prov.get("source_kind") == SOURCE_KIND
            and isinstance(checks, dict) and checks.get("passed") is True):
        raise SnapshotProvenanceError("snapshot provenance does not certify observed, non-synthetic Riot match data")
    return prov


def _text_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        for col in conn.execute(f"PRAGMA table_info({table})"):
            if col[2].upper() in ("TEXT", "") or "CHAR" in col[2].upper():
                out.append((table, col[1]))
    return out


def verify_public_snapshot(path: Path | str, *, source_match_ids: Iterable[str] = (), puuids: Iterable[str] = (),
                           expected_counts: dict[str, int] | None = None,
                           expected_windows: Sequence[str] | None = None,
                           secrets: Iterable[str] | None = None) -> dict[str, Any]:
    """Integrity checks of a built snapshot; raises SnapshotExportError on
    the first failing check, else returns what was checked. Usable on its
    own (`tftlab verify-public-snapshot`) without the source; the
    source-dependent checks run when their inputs are given."""
    path = Path(path)
    checks: dict[str, Any] = {}

    def require(name: str, ok: bool, detail: str) -> None:
        if not ok:
            raise SnapshotExportError(f"snapshot verification failed ({name}): {detail}")
        checks[name] = "passed"

    # The website's own read path: read-only open + schema + provenance.
    from .analytics import available_balance_windows
    with Database.open_existing(path) as db:  # SQLite mode=ro; raises SchemaUnavailable
        checks["read_schema"] = "passed"
        prov = read_snapshot_provenance(db)
        checks["provenance"] = "passed"
        windows = [w for w, _n, _t in available_balance_windows(db)]
        checks["public_read_path"] = "passed"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        require("no_collection_tables", not tables & set(EXCLUDED_TABLES), f"present: {sorted(tables & set(EXCLUDED_TABLES))}")
        columns = {t: {c[1] for c in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
        require("no_puuid_columns", not any("puuid" in c.lower() for cols in columns.values() for c in cols),
                "a puuid column is present")
        raw = conn.execute("SELECT COUNT(*) FROM matches WHERE payload_json <> '{}'").fetchone()[0]
        require("no_raw_payloads", raw == 0, f"{raw} matches keep a raw payload")
        aug = conn.execute("SELECT COUNT(*) FROM participants WHERE augments_json <> '[]'").fetchone()[0]
        require("no_augment_payloads", aug == 0, f"{aug} boards keep raw augments")
        bad_ids = conn.execute("SELECT COUNT(*) FROM matches WHERE match_id NOT GLOB 'S[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'").fetchone()[0]
        require("opaque_match_ids", bad_ids == 0, f"{bad_ids} match ids are not opaque snapshot ids")
        orphans = sum(conn.execute(sql).fetchone()[0] for sql in (
            "SELECT COUNT(*) FROM participants p LEFT JOIN matches m ON m.match_id = p.match_id WHERE m.match_id IS NULL",
            "SELECT COUNT(*) FROM units u LEFT JOIN participants p ON p.match_id = u.match_id "
            "AND p.participant_index = u.participant_index WHERE p.match_id IS NULL",
            "SELECT COUNT(*) FROM traits t LEFT JOIN participants p ON p.match_id = t.match_id "
            "AND p.participant_index = t.participant_index WHERE p.match_id IS NULL",
            "SELECT COUNT(*) FROM matches m WHERE NOT EXISTS (SELECT 1 FROM participants p WHERE p.match_id = m.match_id)"))
        require("referential_consistency", orphans == 0, f"{orphans} orphaned or empty rows after remapping")
        present = sorted(r[0] for r in conn.execute("SELECT DISTINCT balance_window FROM matches"))
        require("trusted_windows_only", all(w is not None and _TRUSTED_WINDOW_RE.fullmatch(w) for w in present),
                f"untrusted windows {present}")
        require("windows_match_provenance", present == sorted(prov["balance_windows"]) == sorted(windows),
                f"{present} vs provenance {prov['balance_windows']}")
        if expected_windows is not None:
            require("requested_windows_only", present == sorted(expected_windows), f"{present} vs {sorted(expected_windows)}")
        actual = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in EXPORTED_COLUMNS}
        require("counts_match_provenance", actual["matches"] == prov["matches"] and actual["participants"] == prov["boards"],
                f"{actual} vs provenance")
        if expected_counts is not None:
            require("counts_reconcile_with_source", all(actual[k] == v for k, v in expected_counts.items()),
                    f"{actual} vs source {expected_counts}")

        # Leak scan over every text value in the file.
        source_ids = set(source_match_ids)
        known_puuids = set(puuids)
        secret_values = [s for s in (secrets if secrets is not None else
                                     (os.environ.get("RIOT_API_KEY"), os.environ.get("DATABASE_URL"))) if s and len(s) >= 8]
        leaks: dict[str, int] = {}
        for table, column in _text_columns(conn):
            for (value,) in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
                if not isinstance(value, str) or not value:
                    continue
                if any(marker in value for marker in _SECRET_MARKERS) or any(s in value for s in secret_values):
                    leaks["secret"] = leaks.get("secret", 0) + 1
                if value in known_puuids or _PUUID_LIKE_RE.search(value):  # any 78-char PUUID-shaped token
                    leaks["puuid"] = leaks.get("puuid", 0) + 1
                if source_ids and (value in source_ids or any(tok in source_ids for tok in re.findall(r"[A-Z0-9]+_\d+", value))):
                    leaks["source_match_id"] = leaks.get("source_match_id", 0) + 1
        require("no_secrets_or_identifiers", not leaks, f"{leaks}")
    finally:
        conn.close()
    return {"checks": checks, "counts": actual, "balance_windows": present}
