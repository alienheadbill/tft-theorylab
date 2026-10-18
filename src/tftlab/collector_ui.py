"""TheoryLabs Collector: a local-only control panel for the local collector.

A small browser dashboard so the owner can run the zero-cost local collector
(`tftlab.local_collector`) without a terminal: status, the Riot key, "Collect
Data", progress, results and "Create Public Snapshot".

This is a SEPARATE FastAPI application from the public website
(`tftlab.webapp`); nothing here is imported or mounted there, and Render
never runs it. It is privileged (it writes `.env`, uses the Riot key, writes
the local database), so:

  * it binds 127.0.0.1 only (there is no host option), and every request
    must come from a loopback client with a `Host` of 127.0.0.1/localhost
    on the served port (DNS-rebinding protection);
  * every state-changing request is a POST carrying a per-start random token
    (in a custom header) that only the served page knows, so other websites
    open in the same browser cannot trigger actions (CSRF);
  * the Riot key is accepted only in a POST JSON body, written with the
    existing `.env` writer, and never returned, logged or rendered;
  * it offers no shell, filesystem or database-URL inputs: the database is
    the local collector's own (`local_collector.resolve_local_db`).

Collections and snapshots run through the exact same code as the CLI
(`cli.run_local_collection` / `cli.run_local_snapshot`) in ONE background
worker thread managed by `JobManager`: one job at a time, never parallel,
state survives page reloads, and every outcome (including crashes) releases
the slot. Progress comes from the collector's own preflight/step callbacks
plus the live Riot request counter; no percentage is invented.

Run with `tftlab collector-ui` (or `python -m tftlab.collector_ui`); the
Windows launcher `TheoryLabs Collector.cmd` starts it without a console
window and opens the browser. A second launch just reopens the browser.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import local_collector as lc

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ENV = "TFT_COLLECTOR_UI_PORT"
APP_ID = "theorylabs-collector"
TOKEN_HEADER = "X-Collector-Token"
UI_DIR = Path(__file__).resolve().parent / "collector_web"
PUBLIC_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
STATE_FILE_NAME = ".collector-ui.json"
LOG_FILE_NAME = "collector-ui.log"
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
#: Files the dashboard cannot work without (resolved from this package's own
#: location, never a machine-specific path).
REQUIRED_UI_FILES = (UI_DIR / "index.html", UI_DIR / "collector.js", UI_DIR / "collector.css",
                     PUBLIC_STATIC_DIR / "app.css")
_KEY_PATTERN = re.compile(r"RGAPI-[0-9A-Za-z-]{8,}")

log = logging.getLogger("tftlab.collector_ui")


# ---------------------------------------------------------------- secrecy


def redact(text: str, *extra: str | None) -> str:
    """Remove anything key-shaped (and the given values) from `text`."""
    text = _KEY_PATTERN.sub("[redacted key]", text)
    for value in extra:
        if value and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    return text


def describe_exception(exc: BaseException) -> str:
    """A sanitized one-line description for logs and technical details: the
    exception type and message (which includes the missing path for file
    errors), with anything key-shaped removed. Never the Riot key."""
    text = redact(f"{type(exc).__name__}: {exc}", os.environ.get("RIOT_API_KEY"))
    return text if len(text) <= 500 else text[:497] + "..."


def missing_ui_files() -> list[str]:
    """Required dashboard files that are not where this package expects them
    (e.g. the TheoryLabs folder was moved or is incomplete). Checked on every
    page load, not cached, so a move while the app runs is noticed."""
    return [str(path) for path in REQUIRED_UI_FILES if not path.is_file()]


def _working_folder_ok() -> bool:
    """False when the folder the app was started from no longer exists
    (moved or renamed while the app was running)."""
    try:
        return Path(os.getcwd()).is_dir()
    except OSError:
        return False


class _RedactingFilter(logging.Filter):
    """Belt and braces for the log file: no key-shaped string survives."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message, os.environ.get("RIOT_API_KEY"))
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)),
                                     os.environ.get("RIOT_API_KEY"))
            record.exc_info = None
        return True


# ---------------------------------------------------------------- user-facing messages

KEY_EXPIRED_MESSAGE = ("Your Riot development key has expired (or is not valid). Get a new key from the Riot "
                       "Developer Portal (developer.riotgames.com) and paste it into the “Riot development key” "
                       "box on this page.")
BUSY_MESSAGE = "A collection or snapshot is already running. Wait for it to finish."
CLI_BUSY_MESSAGE = ("Another collection is already running on this database (perhaps from a terminal). Wait for it "
                    "to finish, then try again.")
VALIDATION_FAILED_MESSAGE = ("The collected data failed validation. The matches were kept, but do NOT create a "
                             "public snapshot until this has been reviewed.")
UNEXPECTED_MESSAGE = ("Something unexpected went wrong. Nothing was published. The details below can help when "
                      "asking for help.")
STALE_PAGE_MESSAGE = ("This page was opened before the TheoryLabs Collector app was last restarted, so it cannot "
                      "send commands. Reload the page (press F5) and try again.")
FOLDER_MISSING_MESSAGE = ("The TheoryLabs folder this collector app was started from is no longer available (was it "
                          "moved or renamed?). Stop the collector app (Stop TheoryLabs Collector.cmd) and start it "
                          "again from the TheoryLabs folder.")
UI_FILES_MISSING_MESSAGE = ("Some of the TheoryLabs Collector's own files are missing, so the dashboard cannot work "
                            "properly. This usually means the TheoryLabs folder was moved, renamed or is incomplete "
                            "while the collector app was running. Stop the collector app (Stop TheoryLabs "
                            "Collector.cmd), make sure the folder is complete, and start it again.")


def _friendly_collect_message(report: Any) -> str:
    if report.outcome == lc.SUCCESS:  # the CLI's wording, pointed at this panel's button instead
        return report.message.replace("Ready for `tftlab local-snapshot` when you want a public website snapshot.",
                                      "You can now click “Create Public Snapshot” whenever you want one for the "
                                      "website.")
    if report.message == lc.EXPIRED_KEY_MESSAGE or "development key expired during the run" in report.message:
        return KEY_EXPIRED_MESSAGE + (" Matches saved before it expired were kept." if
                                      report.outcome == lc.COLLECTION_FAILED else " Nothing was collected.")
    if report.message.startswith(lc.NO_WINDOW_MESSAGE):  # the patch calendar's own explanation
        return report.message[len(lc.NO_WINDOW_MESSAGE):].strip() or report.message
    if report.outcome == lc.VALIDATION_FAILED:
        return VALIDATION_FAILED_MESSAGE
    return report.message  # the collector's own plain-language explanation


# ---------------------------------------------------------------- jobs

#: Which phase a collector check (`local_collector.Check.name`) belongs to.
_PHASE_OF_CHECK = {
    "Local database": "Checking configuration", "DATABASE_URL": "Checking configuration",
    "Disk space": "Checking configuration", "Riot routing": "Checking configuration",
    "Current time (UTC)": "Checking configuration", "Trusted patch window": "Checking configuration",
    "Patch schedule": "Checking Riot's patch schedule",
    "Riot key configured": "Validating Riot key", "Riot key valid": "Validating Riot key",
    "Champion cost data": "Loading champion data", "Existing data": "Reading existing data",
    "Backup": "Backing up the database",
}


@dataclass
class Job:
    kind: str  # "collect" | "snapshot"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "running"  # running | succeeded | failed
    phase: str = "Starting"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    steps: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    detail: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    live: dict[str, Any] = field(default_factory=dict)
    client: Any = None  # the live Riot client, for request counters only

    def failed_step(self) -> dict[str, str] | None:
        """The collector step that reported a failure, if any."""
        return next((dict(step) for step in reversed(self.steps) if step.get("status") == "fail"), None)

    def public(self) -> dict[str, Any]:
        live = dict(self.live)
        telemetry = getattr(self.client, "telemetry", None)
        if telemetry is not None and self.state == "running":
            live.update(riot_requests=int(getattr(telemetry, "requests", 0) or 0),
                        rate_limited=int(getattr(telemetry, "rate_limited", 0) or 0))
        end = self.finished_at or time.time()
        return {"kind": self.kind, "id": self.id, "state": self.state, "phase": self.phase,
                "started_at": int(self.started_at * 1000),
                "finished_at": int(self.finished_at * 1000) if self.finished_at else None,
                "elapsed_s": round(end - self.started_at, 1), "steps": list(self.steps), "message": self.message,
                "detail": self.detail, "result": dict(self.result), "live": live,
                "failed_step": self.failed_step()}


class JobBusy(RuntimeError):
    pass


class JobManager:
    """At most one job (collection OR snapshot) at a time, in one worker
    thread. Status reads never start anything; the slot is always released
    when the job ends, however it ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.last: dict[str, Job] = {}

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.current is not None

    def start(self, kind: str, work: Callable[[Job], None]) -> Job:
        with self._lock:
            if self.current is not None:
                raise JobBusy(BUSY_MESSAGE)
            job = Job(kind)
            self.current = job
        log.info("%s job %s requested", kind, job.id)
        thread = threading.Thread(target=self._run, args=(job, work), name=f"collector-ui-{kind}", daemon=True)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self.current = None
            raise
        return job

    def _run(self, job: Job, work: Callable[[Job], None]) -> None:
        log.info("%s job %s started", job.kind, job.id)
        try:
            work(job)
            if job.state == "running":
                job.state = "succeeded"
        except BaseException as exc:  # never leave the slot taken
            job.state = "failed"
            job.message = job.message or UNEXPECTED_MESSAGE
            job.detail = describe_exception(exc)
            log.exception("%s job %s crashed during '%s': %s", job.kind, job.id, job.phase, job.detail)
        finally:
            job.finished_at = time.time()
            job.client = None
            if job.phase not in ("Complete", "Failed"):
                job.phase = "Complete" if job.state == "succeeded" else "Failed"
            elapsed = job.finished_at - job.started_at
            if job.state == "succeeded":
                log.info("%s job %s completed in %.0fs: %s", job.kind, job.id, elapsed,
                         redact(job.message, os.environ.get("RIOT_API_KEY")))
            else:
                step = job.failed_step()
                log.warning("%s job %s failed after %.0fs%s: %s", job.kind, job.id, elapsed,
                            f" at step '{step['name']}'" if step else "",
                            redact(job.message or job.detail, os.environ.get("RIOT_API_KEY")))
            with self._lock:
                self.last[job.kind] = job
                self.current = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            current, last = self.current, dict(self.last)
        return {"current": current.public() if current else None,
                "last": {kind: job.public() for kind, job in last.items()}}


# ---------------------------------------------------------------- the work (existing code paths only)


def _collect_work(job: Job) -> None:
    from . import cli

    db_path = lc.resolve_local_db(None)  # TFT_LOCAL_DB_PATH or data/local/...; never DATABASE_URL
    config = lc.CollectConfig(db_path=db_path, backup_dir=lc.local_dirs(db_path)["backups"])
    job.live["settings"] = (f"{', '.join(f'{c} {n}' for c, n in config.seed_allocation.items())} seed players x "
                            f"{config.matches_per_seed} recent matches")

    def progress(check: lc.Check) -> None:
        job.steps.append({"name": check.name, "status": check.status,
                          "detail": redact(check.detail, os.environ.get("RIOT_API_KEY"))})
        if check.name == "Collection":
            job.phase = "Collecting matches from Riot" if check.status == "info" else "Validating the data"
        elif check.name == "Validation":
            job.phase = "Preparing Discovery"
        elif check.name == "Discovery preparation":
            job.phase = "Finishing"
        elif check.name in _PHASE_OF_CHECK:
            job.phase = _PHASE_OF_CHECK[check.name]

    def tracked_client(api_key: str, *, platform: str, region: str):
        client = cli._local_riot_client(api_key, platform=platform, region=region)
        job.client = client
        return client

    try:
        report, report_path = cli.run_local_collection(db_path, config, progress=progress,
                                                       client_factory=tracked_client)
    except lc.CollectorBusy:
        job.state, job.phase, job.message = "failed", "Failed", CLI_BUSY_MESSAGE
        return
    after, before, ingest, riot = (report.after or {}).get("window", {}), (report.before or {}).get("window", {}), \
        report.ingest or {}, report.riot or {}
    job.result = {
        "outcome": report.outcome, "patch": report.patch, "window_start": report.window_start,
        "window_end": report.window_end,
        "balance_windows": after.get("balance_windows") or before.get("balance_windows") or [],
        "matches_before": before.get("matches"), "new_matches": ingest.get("inserted"),
        "duplicates_skipped": ingest.get("duplicates_skipped"), "patch_matches": after.get("matches"),
        "boards": after.get("boards"), "latest_game": after.get("latest_game"),
        "seeds": ingest.get("seeds") or {}, "riot_requests": riot.get("requests"),
        "rate_limited": riot.get("rate_limited"), "waited_s": riot.get("waited_s"), "elapsed_s": riot.get("elapsed_s"),
        "validation": {bw: {"is_severe": v["is_severe"], "severe": v["severe"], "warnings": v["warnings"]}
                       for bw, v in (report.validation or {}).items()},
        "prepared": lc.describe_prepared(report.prepared) if report.prepared else None,
        "backup": (report.backup or {}).get("path") or (report.backup or {}).get("note"),
        "database_ok": report.database_ok, "ready_for_snapshot": report.ready_for_snapshot,
        "report_path": str(report_path) if report_path else None,
    }
    job.message = _friendly_collect_message(report)
    if report.outcome != lc.SUCCESS:
        job.state, job.phase = "failed", "Failed"
        job.detail = redact(report.message, os.environ.get("RIOT_API_KEY"))
    else:
        job.phase = "Complete"


def _snapshot_work(job: Job) -> None:
    from . import cli
    from .public_snapshot import SnapshotExportError

    db_path = lc.resolve_local_db(None)
    job.phase = "Exporting and verifying the sanitized snapshot"

    def progress(line: str) -> None:
        job.steps.append({"name": "Snapshot", "status": "info",
                          "detail": redact(line, os.environ.get("RIOT_API_KEY"))})

    try:
        result, verification = cli.run_local_snapshot(db_path, progress=progress)
    except lc.CollectorBusy:
        job.state, job.phase, job.message = "failed", "Failed", CLI_BUSY_MESSAGE
        return
    except (SnapshotExportError, lc.LocalSnapshotError) as exc:
        job.state, job.phase = "failed", "Failed"
        job.message = "The public snapshot was NOT created. Nothing was uploaded."
        job.detail = redact(str(exc), os.environ.get("RIOT_API_KEY"))
        return
    prov = result.provenance
    gz = result.gzip_path
    job.result = {
        "balance_windows": prov.get("balance_windows"), "matches": prov.get("matches"), "boards": prov.get("boards"),
        "latest_game": prov.get("latest_game"), "folder": str(result.path.parent),
        "gz_path": str(gz) if gz else None,
        "gz_bytes": result.files.get(gz.name, {}).get("bytes") if gz else None,
        "gz_sha256": result.files.get(gz.name, {}).get("sha256") if gz else None,
        "manifest_path": str(result.manifest_path), "checks_passed": len(verification.get("checks", {})),
    }
    job.message = ("Public snapshot created and verified on this computer. Nothing was uploaded or deployed; "
                   "publishing it is a separate, deliberate step.")
    job.phase = "Complete"


# ---------------------------------------------------------------- status


@dataclass
class KeyStatus:
    state: str = "unchecked"  # unchecked | valid | invalid | error
    detail: str = ""
    checked_at: int | None = None


def _latest_report(db_path: Path) -> dict[str, Any] | None:
    reports = sorted(lc.local_dirs(db_path)["reports"].glob("local-collect-*.json"))
    if not reports:
        return None
    try:
        data = json.loads(reports[-1].read_text())
    except (OSError, ValueError):
        return None
    after = (data.get("after") or {}).get("window") or {}
    return {"file": reports[-1].name, "outcome": data.get("outcome"),
            "message": redact(str(data.get("message") or ""), os.environ.get("RIOT_API_KEY")),
            "patch": data.get("patch"), "new_matches": (data.get("ingest") or {}).get("inserted"),
            "patch_matches": after.get("matches"), "boards": after.get("boards"),
            "latest_game": after.get("latest_game")}


def _collector_status(at_ms: int) -> dict[str, Any]:
    if not _working_folder_ok():
        return {"error": FOLDER_MISSING_MESSAGE}
    try:
        db_path = lc.resolve_local_db(None)
    except lc.LocalTargetError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        log.warning("cannot resolve the local database: %s", describe_exception(exc))
        return {"error": FOLDER_MISSING_MESSAGE}
    try:
        status = lc.local_status(db_path, at_ms=at_ms)
    except Exception as exc:  # unreadable database etc.: show it, never crash the page
        status = {"db_path": str(db_path), "exists": db_path.exists(), "read_error": type(exc).__name__}
    status["last_report"] = _latest_report(db_path)
    status["backup_dir"] = str(lc.local_dirs(db_path)["backups"])
    return status


# ---------------------------------------------------------------- the app


class KeyBody(BaseModel):
    api_key: str


def create_collector_app(*, root: Path | None = None, port: int = DEFAULT_PORT, token: str | None = None,
                         on_shutdown: Callable[[], None] | None = None,
                         jobs: JobManager | None = None) -> FastAPI:
    """The local control panel. `root` is the TheoryLabs folder (where .env
    lives; default: the current directory)."""
    root = (root or Path.cwd()).resolve()
    token = token or secrets.token_urlsafe(32)
    #: Not a secret: identifies this run of the app so a page left open across a
    #: restart can tell (its control token is then stale) and say so.
    instance = uuid.uuid4().hex[:12]
    jobs = jobs or JobManager()
    key_status = KeyStatus()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = FastAPI(title="TheoryLabs Collector (local)", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs, app.state.token, app.state.instance = jobs, token, instance
    # check_dir=False: a missing folder must not crash the app at start-up; the page explains it instead.
    app.mount("/static", StaticFiles(directory=PUBLIC_STATIC_DIR, check_dir=False), name="static")  # style only
    app.mount("/ui", StaticFiles(directory=UI_DIR, check_dir=False), name="ui")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        client = request.client.host if request.client else None
        if client not in _LOOPBACK_CLIENTS:
            return JSONResponse({"error": "The TheoryLabs Collector only accepts requests from this computer."},
                                status_code=403)
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return JSONResponse({"error": "Unexpected host."}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and origin.lower() not in {f"http://{h}" for h in allowed_hosts}:
                return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
            if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
                log.info("refused %s %s: missing or stale control token (page opened before a restart?)",
                         request.method, request.url.path)
                return JSONResponse({"error": STALE_PAGE_MESSAGE, "code": "stale_page"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                       "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        return response

    def _settings():
        from .config import Settings

        return Settings.from_env()

    def _key_view() -> dict[str, Any]:
        configured = lc.riot_key_configured(_settings().riot_api_key)
        state = key_status.state if configured else "not_configured"
        return {"state": state, "configured": configured, "detail": key_status.detail if configured else "",
                "checked_at": key_status.checked_at if configured else None}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        missing = missing_ui_files()
        if missing:
            log.error("dashboard files missing: %s", ", ".join(missing))
            return HTMLResponse(_missing_files_page(missing), status_code=500)
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{CONTROL_TOKEN}}", token).replace("{{INSTANCE_ID}}", instance))

    @app.get("/api/ping")
    def ping() -> dict[str, str]:
        return {"app": APP_ID}

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        """Read-only: never starts, changes or contacts anything."""
        at_ms = lc.now_ms()
        missing = missing_ui_files()
        folder_ok = _working_folder_ok()
        return {"app": APP_ID, "instance": instance, "now": at_ms, "root": str(root),
                "env_file_exists": folder_ok and (root / ".env").exists(),
                "app_files": {"ok": not missing and folder_ok, "missing": missing,
                              "message": FOLDER_MISSING_MESSAGE if not folder_ok else
                              (UI_FILES_MISSING_MESSAGE if missing else "")},
                "collector": _collector_status(at_ms), "key": _key_view(), "jobs": jobs.snapshot()}

    @app.post("/api/key")
    def save_key(body: KeyBody) -> dict[str, Any]:
        key = body.api_key.strip()
        if not lc.RIOT_KEY_RE.fullmatch(key):
            raise HTTPException(422, "That does not look like a Riot API key (they look like RGAPI-xxxxxxxx-xxxx-...). "
                                     "Nothing was saved.")
        if not (root / ".env").exists():
            lc.init_local(root)  # folders + .env from .env.example; never overwrites
        lc.set_env_key(root / ".env", key)
        os.environ["RIOT_API_KEY"] = key  # this process uses the new key from now on
        key_status.state, key_status.detail, key_status.checked_at = "unchecked", "Saved. Click “Check key”.", None
        log.info("Riot key saved to .env (value not logged)")
        return {"ok": True, "key": _key_view()}

    @app.post("/api/key/validate")
    def validate_key() -> dict[str, Any]:
        from . import cli
        from .riot import check_riot_key

        if jobs.busy:
            raise HTTPException(409, BUSY_MESSAGE)
        settings = _settings()
        if not lc.riot_key_configured(settings.riot_api_key):
            raise HTTPException(409, "No Riot key is saved yet. Paste your development key above and save it.")
        try:
            with cli._local_riot_client(settings.riot_api_key, platform=settings.platform,
                                        region=settings.region) as client:
                check = check_riot_key(client)
        except Exception as exc:
            check, problem = None, describe_exception(exc)
            log.warning("key check could not run: %s", problem)
        else:
            problem = ""
        key_status.checked_at = lc.now_ms()
        if check is not None and check.ok:
            key_status.state, key_status.detail = "valid", "Riot accepted the key."
        elif check is not None and check.category in ("unauthorized", "forbidden"):
            key_status.state, key_status.detail = "invalid", KEY_EXPIRED_MESSAGE
        else:
            key_status.state = "error"
            key_status.detail = {"rate_limited": "Riot is rate limiting this key right now; try again in a few minutes.",
                                 "network_error": "Could not reach Riot (network problem). Check the internet "
                                                  "connection and try again."}.get(
                getattr(check, "category", None), "The key could not be checked right now; try again later.")
            if check is not None and not check.ok:
                log.warning("key check failed: %s", check.category or "unknown error")
            if problem.startswith("FileNotFoundError"):
                key_status.detail = ("The collector app is missing a file it needs, so the key could not be "
                                     "checked. If the TheoryLabs folder was moved while the app was running, stop "
                                     "the collector app and start it again.")
            elif problem:
                key_status.detail = f"The key could not be checked because of an unexpected problem ({problem})."
        return {"ok": key_status.state == "valid", "key": _key_view()}

    def collect_and_note_key(job: Job) -> None:
        """Run the collection; what its own Riot key step found also updates the key status
        (a collection that hit an expired key must not leave the key showing 'not checked')."""
        _collect_work(job)
        key_step = next((s for s in job.steps if s.get("name") == "Riot key valid"), None)
        if key_step is None:
            return
        key_status.checked_at = lc.now_ms()
        if key_step.get("status") == "ok":
            key_status.state, key_status.detail = "valid", "Riot accepted the key."
        elif key_step.get("detail") in ("unauthorized", "forbidden"):
            key_status.state, key_status.detail = "invalid", KEY_EXPIRED_MESSAGE

    @app.post("/api/collect")
    def collect() -> dict[str, Any]:
        try:
            job = jobs.start("collect", collect_and_note_key)
        except JobBusy as exc:
            log.info("collection request refused: another job is running")
            raise HTTPException(409, str(exc)) from None
        return {"ok": True, "job": job.public()}

    @app.post("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        try:
            job = jobs.start("snapshot", _snapshot_work)
        except JobBusy as exc:
            log.info("snapshot request refused: another job is running")
            raise HTTPException(409, str(exc)) from None
        return {"ok": True, "job": job.public()}

    @app.post("/api/shutdown")
    def shutdown() -> dict[str, Any]:
        if jobs.busy:
            raise HTTPException(409, "A collection or snapshot is running. Stopping now would interrupt it; wait "
                                     "for it to finish first.")
        if on_shutdown is not None:
            threading.Timer(0.3, on_shutdown).start()
        return {"ok": True, "message": "The TheoryLabs Collector is stopping. You can close this tab."}

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 echoes the submitted input, which could be a key: never do that.
        return JSONResponse({"error": "The request was not understood. Reload the page and try again."},
                            status_code=422)

    return app


def _missing_files_page(missing: list[str]) -> str:
    from html import escape

    items = "".join(f"<li><code>{escape(path)}</code></li>" for path in missing)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>TheoryLabs Collector: files missing"
            "</title><meta name='viewport' content='width=device-width, initial-scale=1'></head>"
            "<body style='font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;"
            "line-height:1.5;background:#151618;color:#ece7dc'>"
            "<h1>TheoryLabs Collector cannot show its dashboard</h1>"
            f"<p>{escape(UI_FILES_MISSING_MESSAGE)}</p>"
            f"<p>Missing files:</p><ul>{items}</ul>"
            "<p>Your collected data, backups and settings are not affected.</p></body></html>")


# ---------------------------------------------------------------- running it


def _state_file(root: Path) -> Path:
    return root / lc.DEFAULT_LOCAL_DIR / STATE_FILE_NAME


def _read_state(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_state_file(root).read_text())
    except (OSError, ValueError):
        return None


def _ping(port: int, timeout: float = 1.5) -> bool:
    try:
        response = httpx.get(f"http://{HOST}:{port}/api/ping", timeout=timeout,
                             headers={"Host": f"{HOST}:{port}"}, trust_env=False)
        return response.status_code == 200 and response.json().get("app") == APP_ID
    except Exception:
        return False


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            # uvicorn binds with SO_REUSEADDR; without it a port still in TIME_WAIT right after
            # a restart would look busy and the app would needlessly move to the next port.
            # (Not on Windows, where SO_REUSEADDR means something else: sharing a bound port.)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def _configure_logging(root: Path) -> Path:
    log_dir = root / lc.DEFAULT_LOCAL_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_FILE_NAME
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactingFilter())
    root_logger = logging.getLogger()
    root_logger.handlers[:] = [handler]
    root_logger.setLevel(logging.INFO)
    return path


def stop_running(root: Path | None = None) -> str:
    """Ask a running control panel (started from `root`) to stop."""
    root = (root or Path.cwd()).resolve()
    info = _read_state(root)
    if not info or not _ping(int(info.get("port", 0))):
        return "The TheoryLabs Collector is not running."
    port = int(info["port"])
    try:
        response = httpx.post(f"http://{HOST}:{port}/api/shutdown", timeout=5, trust_env=False,
                              headers={TOKEN_HEADER: str(info.get("token", "")), "Host": f"{HOST}:{port}"})
    except Exception as exc:
        return f"Could not reach the TheoryLabs Collector ({type(exc).__name__})."
    if response.status_code == 200:
        return "The TheoryLabs Collector is stopping."
    return response.json().get("error", f"The TheoryLabs Collector refused to stop (HTTP {response.status_code}).")


def _refresh_patch_schedule() -> None:
    """Start-up check of Riot's patch schedule (one request at most, throttled
    to hourly; collection preflight checks again). Never blocks the page:
    `/api/state` only ever reads the saved copy."""
    from . import patch_calendar

    try:
        db_path = lc.resolve_local_db(None)
        result = patch_calendar.refresh_schedule(db_path.parent, now_ms=lc.now_ms())
        log.info("patch schedule at start-up: %s", result.message if result.attempted else "checked recently")
    except Exception as exc:
        log.warning("patch schedule check at start-up failed: %s", describe_exception(exc))


def serve(*, port: int | None = None, open_browser: bool = True, root: Path | None = None) -> int:
    """Start the control panel on 127.0.0.1 (or reopen an already running one)."""
    import uvicorn

    from . import cli

    root = (root or Path.cwd()).resolve()
    os.chdir(root)  # .env and data/local are relative to the TheoryLabs folder
    if sys.stdout is None or sys.stderr is None:  # pythonw: no console; keep logging working
        sys.stdout = sys.stderr = open(os.devnull, "w")
    log_path = _configure_logging(root)
    cli._load_dotenv(root / ".env")

    existing = _read_state(root)
    if existing and _ping(int(existing.get("port", 0)), timeout=3):
        url = f"http://{HOST}:{int(existing['port'])}/"
        log.info("already running at %s; opening the browser", url)
        if open_browser:
            webbrowser.open(url)
        return 0

    missing = missing_ui_files()
    if missing:
        log.error("dashboard files missing at start-up (the page will explain): %s", ", ".join(missing))
    requested = port or int(os.environ.get(PORT_ENV) or DEFAULT_PORT)
    chosen = next((p for p in range(requested, requested + 10) if _port_free(p)), None)
    if chosen is None:
        log.error("no free local port in %s-%s", requested, requested + 9)
        return 1
    token = secrets.token_urlsafe(32)
    config = uvicorn.Config(None, host=HOST, port=chosen, log_config=None, access_log=False)  # 127.0.0.1 only
    server = uvicorn.Server(config)
    config.app = create_collector_app(root=root, port=chosen, token=token,
                                      on_shutdown=lambda: setattr(server, "should_exit", True))
    state_file = _state_file(root)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_file, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"port": chosen, "pid": os.getpid(), "token": token}, fh)
    url = f"http://{HOST}:{chosen}/"
    log.info("TheoryLabs Collector starting at %s (log: %s)", url, log_path)

    def open_when_ready() -> None:
        for _ in range(100):
            if server.started:
                if open_browser:
                    webbrowser.open(url)
                return
            time.sleep(0.1)

    threading.Thread(target=open_when_ready, daemon=True).start()
    threading.Thread(target=_refresh_patch_schedule, name="patch-schedule", daemon=True).start()
    try:
        server.run()
    finally:
        info = _read_state(root)
        if info and info.get("token") == token:
            state_file.unlink(missing_ok=True)
        log.info("TheoryLabs Collector stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m tftlab.collector_ui",
                                     description="TheoryLabs Collector: local-only control panel (127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help=f"local port (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--stop", action="store_true", help="stop a running control panel")
    args = parser.parse_args(argv)
    if args.stop:
        print(stop_running())
        return 0
    return serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
