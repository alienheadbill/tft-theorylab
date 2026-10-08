"""Install the configured public snapshot before the website starts.

`tftlab fetch-snapshot` is the deployment bridge from a published, verified
sanitized snapshot (a GitHub Release asset of this public repository) to the
free Render web service, with no database service. Configuration is three
environment variables (no credentials: the asset is public):

  TFT_SNAPSHOT_URL     https URL of the `.sqlite3.gz` asset (immutable release)
  TFT_SNAPSHOT_SHA256  its expected SHA-256 (64 hex characters)
  TFT_SNAPSHOT_PATH    where the website reads it (must end in `.gz`)

Only when `TFT_DATA_SOURCE=snapshot`. In any other mode (e.g. `demo`) it
does nothing and needs nothing, so the same Render commands work before and
after the switch.

Installation fails closed, and never leaves a bad file active:

  1. if the active file already has the expected SHA-256, nothing to do
     (no network: a restart after a build that already installed it);
  2. otherwise download into a temporary file next to the target
     (redirects followed; HTTP errors, short reads and oversize refused;
     transient failures retried a bounded number of times);
  3. compare its SHA-256 with TFT_SNAPSHOT_SHA256;
  4. decompress it to a scratch file and run the existing public-snapshot
     verification (`public_snapshot.verify_public_snapshot`, which also
     requires the exporter's observed-data provenance);
  5. only then atomically rename it into TFT_SNAPSHOT_PATH.

Any failure raises `SnapshotFetchError` (exit 1 from the CLI) and removes
the temporary files; the website itself still refuses anything that is not
an exporter-certified snapshot (503, never demo data).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

SNAPSHOT_URL_ENV = "TFT_SNAPSHOT_URL"
SNAPSHOT_SHA256_ENV = "TFT_SNAPSHOT_SHA256"
SNAPSHOT_PATH_ENV = "TFT_SNAPSHOT_PATH"
DATA_SOURCE_ENV = "TFT_DATA_SOURCE"
#: The verified snapshot is ~46 MB; anything far larger is not ours.
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAYS_S = (2.0, 5.0)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SnapshotFetchError(RuntimeError):
    """The configured snapshot could not be installed. Messages never
    include secrets, query strings or environment values beyond the
    public host/path."""


@dataclass(frozen=True)
class FetchResult:
    status: str  # "not-snapshot-mode" | "already-installed" | "installed" | "verified-local"
    path: Path | None = None
    bytes: int = 0
    sha256: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def _public_location(url: str) -> str:
    """Host and path only: never a query string or fragment (which could
    carry a token)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_snapshot_archive(path: Path, *, scratch_dir: Path) -> dict[str, Any]:
    """Decompress a `.sqlite3.gz` snapshot (gzip's CRC rejects corruption)
    and run the existing public-snapshot verification on it, including the
    observed-data provenance the website requires. Returns a public summary;
    raises SnapshotFetchError."""
    from .public_snapshot import SnapshotExportError, SnapshotProvenanceError, read_snapshot_provenance, \
        verify_public_snapshot
    from .storage import Database

    scratch_dir.mkdir(parents=True, exist_ok=True)
    plain = scratch_dir / f".verify-{os.getpid()}-{time.monotonic_ns()}.sqlite3"
    try:
        try:
            with gzip.open(path, "rb") as src, plain.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise SnapshotFetchError(f"the snapshot archive is corrupt ({type(exc).__name__})") from exc
        try:
            checked = verify_public_snapshot(plain)
            with Database.open_existing(plain) as db:
                provenance = read_snapshot_provenance(db)
        except (SnapshotExportError, SnapshotProvenanceError) as exc:
            raise SnapshotFetchError(f"the snapshot failed verification: {exc}") from exc
        except Exception as exc:
            raise SnapshotFetchError(f"the snapshot is not a valid public snapshot ({type(exc).__name__})") from exc
        return {
            "balance_windows": provenance.get("balance_windows"),
            "matches": provenance.get("matches"),
            "boards": provenance.get("boards"),
            "units": provenance.get("units"),
            "traits": provenance.get("traits"),
            "latest_game": provenance.get("latest_game"),
            "exported_at": provenance.get("exported_at"),
            "checks": len(checked.get("checks", {})),
        }
    finally:
        plain.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(plain) + suffix).unlink(missing_ok=True)


def _download(url: str, dest: Path, *, transport: httpx.BaseTransport | None, timeout: float,
              max_bytes: int) -> tuple[int, str]:
    """Stream `url` into `dest`, returning (bytes, sha256). Raises
    httpx.HTTPError (retryable network/5xx) or SnapshotFetchError (final)."""
    digest = hashlib.sha256()
    written = 0
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "theorylabs-snapshot-fetch/1"}) as client:
        with client.stream("GET", url) as response:
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(f"server error {response.status_code}", request=response.request,
                                            response=response)
            if response.status_code != 200:
                raise SnapshotFetchError(f"download failed: HTTP {response.status_code} from {_public_location(url)}")
            expected = response.headers.get("Content-Length")
            if expected is not None and int(expected) > max_bytes:
                raise SnapshotFetchError(f"refusing a {expected}-byte download (limit {max_bytes})")
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 20):
                    written += len(chunk)
                    if written > max_bytes:
                        raise SnapshotFetchError(f"download exceeded the {max_bytes}-byte limit")
                    digest.update(chunk)
                    fh.write(chunk)
            if expected is not None and written != int(expected):
                raise SnapshotFetchError(f"incomplete download: received {written} of {expected} bytes")
    return written, digest.hexdigest()


def fetch_configured_snapshot(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    progress: Callable[[str], None] = lambda _line: None,
    attempts: int = DOWNLOAD_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 120.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> FetchResult:
    """Install (or confirm) the configured snapshot; see the module docstring."""
    env = os.environ if env is None else env
    mode = (env.get(DATA_SOURCE_ENV) or "").strip().lower()
    if mode != "snapshot":
        return FetchResult("not-snapshot-mode")
    if env.get("DATABASE_URL"):
        raise SnapshotFetchError("TFT_DATA_SOURCE=snapshot cannot be combined with DATABASE_URL; remove DATABASE_URL "
                                 "(the snapshot site needs no database)")

    url = (env.get(SNAPSHOT_URL_ENV) or "").strip()
    raw_path = (env.get(SNAPSHOT_PATH_ENV) or "").strip()
    if not url:
        # A snapshot shipped by other means: it must already be there and valid.
        if not raw_path:
            raise SnapshotFetchError(f"snapshot mode needs {SNAPSHOT_URL_ENV} (+ {SNAPSHOT_SHA256_ENV}) or an existing "
                                     f"file at {SNAPSHOT_PATH_ENV}")
        path = Path(raw_path)
        if not path.is_file():
            raise SnapshotFetchError(f"no snapshot file at {path} and no {SNAPSHOT_URL_ENV} to download one from")
        if path.suffix != ".gz":
            raise SnapshotFetchError(f"{SNAPSHOT_PATH_ENV} must be a .sqlite3.gz snapshot for this check")
        summary = verify_snapshot_archive(path, scratch_dir=path.parent)
        return FetchResult("verified-local", path, path.stat().st_size, sha256_file(path), summary)

    if not url.lower().startswith("https://"):
        raise SnapshotFetchError(f"{SNAPSHOT_URL_ENV} must be an https:// URL")
    expected_sha = (env.get(SNAPSHOT_SHA256_ENV) or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected_sha):
        raise SnapshotFetchError(f"{SNAPSHOT_SHA256_ENV} must be the snapshot's 64-character hex SHA-256 "
                                 "(a download is never trusted without it)")
    if not raw_path:
        raise SnapshotFetchError(f"{SNAPSHOT_PATH_ENV} must be set (e.g. data/snapshot/theorylabs-public-snapshot."
                                 "sqlite3.gz) so the website reads the file this installs")
    path = Path(raw_path)
    if not path.name.endswith(".gz"):
        raise SnapshotFetchError(f"{SNAPSHOT_PATH_ENV} must end in .gz (the published snapshot is gzip-compressed)")

    if path.is_file() and sha256_file(path) == expected_sha:
        progress(f"snapshot already installed at {path} (sha256 matches); nothing downloaded")
        return FetchResult("already-installed", path, path.stat().st_size, expected_sha)

    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{os.getpid()}.part")
    location = _public_location(url)
    try:
        for attempt in range(1, max(1, attempts) + 1):
            progress(f"downloading {location} (attempt {attempt}/{attempts})")
            try:
                size, actual_sha = _download(url, part, transport=transport, timeout=timeout, max_bytes=max_bytes)
                break
            except httpx.HTTPError as exc:
                part.unlink(missing_ok=True)
                if attempt >= attempts:
                    raise SnapshotFetchError(f"download failed after {attempts} attempts ({type(exc).__name__}) "
                                             f"from {location}") from exc
                sleep(RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)])
        progress(f"downloaded {size} bytes; checking sha256")
        if actual_sha != expected_sha:
            raise SnapshotFetchError(f"sha256 mismatch: downloaded {actual_sha}, expected {expected_sha}; "
                                     "the file was NOT installed")
        progress("sha256 matches; verifying the snapshot (decompress + public-snapshot checks)")
        summary = verify_snapshot_archive(part, scratch_dir=path.parent)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)
    return FetchResult("installed", path, size, expected_sha, summary)
