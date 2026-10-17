"""TFT patch calendar: new patch windows from Riot's published schedule.

Riot's official "Patch Schedule - Teamfight Tactics" support article
(`SOURCE_URL`) lists each patch and its scheduled date in Pacific Time,
including mid-patch balance updates such as "TFT 18.6B". The local collector
reads it so a new patch's trusted classification window no longer needs a
code change, while keeping every guarantee of `tftlab.unreal_patch`:

- **Fixed history.** `unreal_patch.UNREAL_PATCH_REGISTRY` (18.2, 18.3, 18.4)
  is never regenerated or moved; this module only adds windows after it.
- **One named, versioned rule** (`POLICY_VERSION`, `window_for`): a patch
  scheduled on Pacific date D whose next schedule entry is on Pacific date N
  is trusted from 00:00 America/Los_Angeles on D+1 until (exclusive) 17:00
  America/Los_Angeles on N-2. Both instants are computed with the IANA time
  zone, so daylight saving is handled by the time-zone database, never a
  fixed UTC offset. This reproduces the 18.3 and 18.4 windows exactly and
  18.2's end (18.2 started seven hours earlier, a historical exception).
  Every transition keeps a gap of about 55 hours that resolves to nothing.
- **Explicit ends only.** A window needs the NEXT schedule entry; the last
  listed patch gets no window until Riot lists its successor. There is no
  open-ended "latest patch".
- **Balance updates** ("18.6B": base patch 18.6 plus a suffix) are not new
  client patches and never create a window or a balance cutoff. They do end
  the preceding window like any other schedule entry, so collection pauses
  across them until the next ordinary patch's window starts.
- **Freeze.** A derived ("planned") window classifies nothing. The moment
  collection first uses it, it is written to a small append-only sidecar
  next to the local database (`FROZEN_NAME`) and never changes again, even
  if Riot later edits the schedule. Only frozen windows are installed into
  `unreal_patch` (`install_frozen`), so every window that ever classified a
  match is frozen. (A sidecar instead of a database migration: the windows
  are configuration of the classifier, not collected data, and are needed
  before the database is opened; the public website never needs them
  because stored rows already carry their patch.)
- **Fail closed.** Unexpected page structure, a schedule that conflicts with
  a frozen window, a cached schedule too old to start a new window, or no
  schedule at all: no new window, collection is NOT READY with a plain
  reason. A failed refresh never replaces a good cache.

Network: one public HTTPS GET of `SOURCE_URL` (no key), with a timeout, a
TheoryLabs user agent and a size limit, at most once per
`MIN_REFRESH_INTERVAL_MS`; only `refresh_schedule` touches the network, and
only the local collector (start-up and collection preflight) calls it.
Status reads (`calendar_state`) are read-only and network-free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import unreal_patch
from .patch import patch_sort_key
from .unreal_patch import (
    NoCurrentTrustedWindow,
    UnrealPatchWindow,
    current_trusted_window,
    install_frozen_windows,
)

log = logging.getLogger("tftlab.patch_calendar")

SOURCE_ID = "riot-support:tft-patch-schedule"
SOURCE_NAME = "Riot Games Player Support: Patch Schedule - Teamfight Tactics"
SOURCE_URL = "https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/"
SOURCE_HOST = "support.riotgames.com"
PACIFIC_TZ = "America/Los_Angeles"

CACHE_FORMAT = "theorylabs-patch-calendar"
CACHE_FORMAT_VERSION = 1
FROZEN_FORMAT = "theorylabs-frozen-patch-windows"
FROZEN_FORMAT_VERSION = 1
PARSER_VERSION = "riot-support-schedule-table/1"
#: The window rule. Change the name whenever the rule changes; frozen
#: windows keep the policy they were created with.
POLICY_VERSION = "pt-d+1-0000_to_n-2-1700/1"
START_DAYS_AFTER, START_HOUR = 1, 0
END_DAYS_BEFORE, END_HOUR = 2, 17
MIN_WINDOW_MS = 24 * 3600 * 1000

CACHE_NAME = "patch-calendar.json"
CHECK_NAME = "patch-calendar-check.json"
FROZEN_NAME = "patch-windows-frozen.json"
REJECTED_NAME = "patch-calendar-rejected.html"

HTTP_TIMEOUT_S = 15.0
MAX_PAGE_BYTES = 2 * 1024 * 1024
USER_AGENT = f"TheoryLabs-Collector/{__version__} (local patch-schedule check; at most hourly)"
#: No more than one request per hour, successful or not.
MIN_REFRESH_INTERVAL_MS = 3600 * 1000
#: A cached schedule older than this may keep an already-frozen window in
#: use but may not START a new one (Riot asks players to re-check a day or
#: two before a patch).
MAX_SCHEDULE_AGE_FOR_NEW_WINDOW_MS = 48 * 3600 * 1000

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_LABEL_RE = re.compile(r"^TFT\s+(\d{1,3})\.(\d{1,2})([A-Za-z])?$")
_DATE_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})(?:\s*\(\s*([A-Za-z]+)\s*\))?$")

ORDINARY, BALANCE = "patch", "balance"


class ScheduleError(ValueError):
    """The page or cache did not have the expected, validated structure."""


class CalendarNotReady(NoCurrentTrustedWindow):
    """No window may be used for collection right now (see `.state`)."""

    def __init__(self, message: str, state: "CalendarState") -> None:
        super().__init__(message)
        self.state = state


# ---------------------------------------------------------------- time


def _pacific():
    from zoneinfo import ZoneInfo  # tzdata on Windows (a declared dependency there)

    return ZoneInfo(PACIFIC_TZ)


def pacific_instant_ms(day: date, hour: int) -> int:
    """Epoch ms of `hour`:00 wall-clock time in America/Los_Angeles on `day`.

    Uses the IANA zone (PDT or PST as that date requires). The hours used by
    the policy (00:00 and 17:00) never fall in a DST transition (02:00), and
    the round trip below proves it."""
    tz = _pacific()
    local = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
    back = datetime.fromtimestamp(local.timestamp(), tz)
    if (back.year, back.month, back.day, back.hour, back.minute) != (day.year, day.month, day.day, hour, 0):
        raise ScheduleError(f"{day} {hour:02d}:00 is not a valid Pacific time")
    return int(local.timestamp()) * 1000


def iso_utc(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_pacific(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_pacific()).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------- events


@dataclass(frozen=True)
class ScheduleEvent:
    """One schedule row, normalized. `kind` is `"patch"` (a new client patch,
    e.g. 18.5) or `"balance"` (a suffix event, e.g. 18.6B = base patch 18.6
    plus balance update "B"). `scheduled_date` is the Pacific calendar date
    as written.

    When the row also names a weekday that the date does not fall on (the
    live page has "September 23, 2026 (Thursday)"; that date is a
    Wednesday), the row is *ambiguous*: `earliest_date`/`latest_date` span
    the written date and the nearest date on the named weekday, and the
    window rule uses whichever reading is more conservative. Otherwise both
    equal `scheduled_date`."""

    label: str
    base_patch: str
    suffix: str | None
    kind: str
    scheduled_date: date
    raw_patch: str = ""
    raw_date: str = ""
    earliest_date: date | None = None
    latest_date: date | None = None

    def __post_init__(self) -> None:
        if self.earliest_date is None:
            object.__setattr__(self, "earliest_date", self.scheduled_date)
        if self.latest_date is None:
            object.__setattr__(self, "latest_date", self.scheduled_date)

    @property
    def ambiguous(self) -> bool:
        return self.earliest_date != self.latest_date

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "base_patch": self.base_patch, "suffix": self.suffix, "kind": self.kind,
                "scheduled_date": self.scheduled_date.isoformat(), "timezone": PACIFIC_TZ,
                "earliest_date": self.earliest_date.isoformat(), "latest_date": self.latest_date.isoformat(),
                "ambiguous": self.ambiguous, "raw_patch": self.raw_patch, "raw_date": self.raw_date}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleEvent":
        try:
            event = parse_row(str(data["raw_patch"]), str(data["raw_date"]))
        except (KeyError, TypeError) as exc:
            raise ScheduleError(f"cached event is incomplete ({exc})") from None
        expected = {"label": event.label, "base_patch": event.base_patch, "suffix": event.suffix,
                    "kind": event.kind, "scheduled_date": event.scheduled_date.isoformat(),
                    "earliest_date": event.earliest_date.isoformat(), "latest_date": event.latest_date.isoformat()}
        if any(data.get(k) != v for k, v in expected.items()):
            raise ScheduleError(f"cached event {data.get('label')!r} does not match its raw text")
        return event


def parse_row(raw_patch: str, raw_date: str) -> ScheduleEvent:
    """One row: `"TFT 18.6B"` + `"November 11, 2026"`, optionally followed
    by a weekday such as `"(Thursday)"` (a mismatching weekday makes the row
    ambiguous; see `ScheduleEvent`). Anything else is a `ScheduleError` --
    never a guess."""
    patch_text, date_text = " ".join(raw_patch.split()), " ".join(raw_date.split())
    m = _LABEL_RE.match(patch_text)
    if not m:
        raise ScheduleError(f"unexpected patch label {patch_text!r}")
    major, minor, suffix = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper() or None
    d = _DATE_RE.match(date_text)
    if not d or d.group(1).lower() not in _MONTHS:
        raise ScheduleError(f"unexpected date {date_text!r} for {patch_text}")
    try:
        day = date(int(d.group(3)), _MONTHS.index(d.group(1).lower()) + 1, int(d.group(2)))
    except ValueError:
        raise ScheduleError(f"impossible date {date_text!r} for {patch_text}") from None
    if not 2020 <= day.year <= 2100:
        raise ScheduleError(f"implausible year in {date_text!r}")
    earliest = latest = day
    weekday = d.group(4)
    if weekday is not None:
        if weekday.lower() not in _WEEKDAYS:
            raise ScheduleError(f"unexpected annotation in {date_text!r} for {patch_text}")
        delta = (_WEEKDAYS.index(weekday.lower()) - day.weekday()) % 7
        if delta:
            other = day + timedelta(days=delta if delta <= 3 else delta - 7)
            earliest, latest = min(day, other), max(day, other)
    base = f"{major}.{minor}"
    return ScheduleEvent(label=base + (suffix or ""), base_patch=base, suffix=suffix,
                         kind=BALANCE if suffix else ORDINARY, scheduled_date=day,
                         raw_patch=patch_text, raw_date=date_text, earliest_date=earliest, latest_date=latest)


class _TableCollector(HTMLParser):
    """Collects every <table> as rows of cell texts (header cells included)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif self._cell is not None and tag == "br":
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth = max(0, self._depth - 1)
        elif self._depth == 1 and tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_schedule_html(html: str) -> list[ScheduleEvent]:
    """The validated schedule from the support article's HTML.

    Expects exactly one table whose header is `Patch | Scheduled Date
    (Pacific Time)` and whose every other row is one `parse_row` row.
    Raises `ScheduleError` for anything else (missing/extra table, another
    time zone, an extra column, a row like "TBD")."""
    collector = _TableCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception as exc:  # pragma: no cover - HTMLParser is lenient
        raise ScheduleError(f"unreadable HTML ({type(exc).__name__})") from None
    candidates = [t for t in collector.tables if t and _is_schedule_header(t[0])]
    if len(candidates) != 1:
        raise ScheduleError(f"expected exactly one patch schedule table, found {len(candidates)}")
    rows = candidates[0][1:]
    events = []
    for row in rows:
        if len(row) != 2:
            raise ScheduleError(f"unexpected row shape ({len(row)} cells): {row!r}")
        events.append(parse_row(row[0], row[1]))
    validate_events(events)
    return events


def _is_schedule_header(cells: list[str]) -> bool:
    if len(cells) != 2:
        return False
    first, second = cells[0].lower(), cells[1].lower()
    return first == "patch" and "scheduled date" in second and "pacific" in second


def validate_events(events: list[ScheduleEvent]) -> None:
    """Whole-schedule checks: enough rows, unique labels, strictly later
    dates, strictly increasing client patches, and every balance event
    belonging to the client patch just before it."""
    if len(events) < 3:
        raise ScheduleError(f"only {len(events)} schedule rows; expected a full schedule")
    labels = [e.label for e in events]
    if len(set(labels)) != len(labels):
        raise ScheduleError("duplicate patch labels")
    for before, after in zip(events, events[1:]):
        if after.earliest_date <= before.latest_date:
            raise ScheduleError(f"{after.label} is not scheduled after {before.label}")
    latest: ScheduleEvent | None = None
    for event in events:
        if event.kind == ORDINARY:
            if latest is not None and patch_sort_key(event.base_patch) <= patch_sort_key(latest.base_patch):
                raise ScheduleError(f"patch {event.label} does not come after {latest.label}")
            latest = event
        elif latest is None or event.base_patch != latest.base_patch:
            raise ScheduleError(f"balance update {event.label} does not follow patch {event.base_patch}")
    if latest is None:
        raise ScheduleError("no ordinary patches listed")


def events_fingerprint(events: list[ScheduleEvent]) -> str:
    canonical = json.dumps([e.as_dict() for e in events], sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------- the rule


@dataclass(frozen=True)
class DerivedWindow:
    """A window computed by the policy from two consecutive schedule entries.
    `scheduled_date`/`until_date` are the dates the rule used (for an
    ambiguous row, its more conservative reading)."""

    client_patch: str
    starts_at: int
    ends_at: int
    scheduled_date: date
    until_label: str
    until_date: date
    policy_version: str = POLICY_VERSION

    def as_window(self) -> UnrealPatchWindow:
        return UnrealPatchWindow(self.client_patch, self.starts_at, self.ends_at, True, SOURCE_URL)

    def as_dict(self) -> dict[str, Any]:
        return {"client_patch": self.client_patch, "starts_at": self.starts_at, "ends_at": self.ends_at,
                "starts_at_utc": iso_utc(self.starts_at), "ends_at_utc": iso_utc(self.ends_at),
                "starts_at_pacific": iso_pacific(self.starts_at), "ends_at_pacific": iso_pacific(self.ends_at),
                "scheduled_date": self.scheduled_date.isoformat(), "until_label": self.until_label,
                "until_date": self.until_date.isoformat(), "policy_version": self.policy_version}


def window_for(scheduled: date, next_scheduled: date) -> tuple[int, int]:
    """`POLICY_VERSION`: [00:00 PT on scheduled+1, 17:00 PT on next-2)."""
    return (pacific_instant_ms(scheduled + timedelta(days=START_DAYS_AFTER), START_HOUR),
            pacific_instant_ms(next_scheduled - timedelta(days=END_DAYS_BEFORE), END_HOUR))


def derive_windows(events: list[ScheduleEvent]) -> list[DerivedWindow]:
    """One window per ordinary patch that has a following schedule entry
    (ordinary or balance) and leaves at least `MIN_WINDOW_MS` of trust.
    Pure and deterministic: the same events always give the same windows."""
    windows = []
    for event, successor in zip(events, events[1:]):
        if event.kind != ORDINARY:
            continue
        # Conservative reading of an ambiguous row: start after its latest
        # possible date, end before the successor's earliest possible date.
        starts_at, ends_at = window_for(event.latest_date, successor.earliest_date)
        if ends_at - starts_at < MIN_WINDOW_MS:
            continue
        windows.append(DerivedWindow(event.base_patch, starts_at, ends_at, event.latest_date,
                                     successor.label, successor.earliest_date))
    return windows


# ---------------------------------------------------------------- files


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class Schedule:
    """A validated schedule plus its provenance."""

    events: tuple[ScheduleEvent, ...]
    fetched_at: int
    content_sha256: str
    events_sha256: str
    source_url: str = SOURCE_URL
    parser_version: str = PARSER_VERSION

    def as_cache(self) -> dict[str, Any]:
        return {"format": CACHE_FORMAT, "format_version": CACHE_FORMAT_VERSION,
                "source": {"id": SOURCE_ID, "name": SOURCE_NAME, "url": self.source_url},
                "parser_version": self.parser_version, "policy_version": POLICY_VERSION,
                "fetched_at": self.fetched_at, "fetched_at_utc": iso_utc(self.fetched_at),
                "content_sha256": self.content_sha256, "events_sha256": self.events_sha256,
                "events": [e.as_dict() for e in self.events],
                "derived_windows": [w.as_dict() for w in derive_windows(list(self.events))]}


def load_cache(local_dir: Path) -> Schedule | None:
    """The cached schedule, re-validated. None if there is none; ScheduleError if it is unusable."""
    path = Path(local_dir) / CACHE_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScheduleError(f"the cached schedule could not be read ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT \
            or data.get("format_version") != CACHE_FORMAT_VERSION:
        raise ScheduleError("the cached schedule has an unknown format")
    if (data.get("source") or {}).get("url") != SOURCE_URL:
        raise ScheduleError("the cached schedule is not from Riot's official schedule page")
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        raise ScheduleError("the cached schedule has no events")
    events = [ScheduleEvent.from_dict(e) for e in raw_events]
    validate_events(events)
    if events_fingerprint(events) != data.get("events_sha256"):
        raise ScheduleError("the cached schedule's fingerprint does not match its events")
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, int):
        raise ScheduleError("the cached schedule has no fetch time")
    return Schedule(tuple(events), fetched_at, str(data.get("content_sha256") or ""), data["events_sha256"],
                    parser_version=str(data.get("parser_version") or ""))


def load_check(local_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(local_dir) / CHECK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class FrozenWindow:
    """A derived window as it was when collection first used it. Never edited."""

    client_patch: str
    starts_at: int
    ends_at: int
    source: str
    policy_version: str
    frozen_at: int
    scheduled_date: str
    until_label: str
    until_date: str
    events_sha256: str
    schedule_fetched_at: int

    def as_window(self) -> UnrealPatchWindow:
        return UnrealPatchWindow(self.client_patch, self.starts_at, self.ends_at, True, self.source)

    def as_dict(self) -> dict[str, Any]:
        return {"client_patch": self.client_patch, "starts_at": self.starts_at, "ends_at": self.ends_at,
                "starts_at_utc": iso_utc(self.starts_at), "ends_at_utc": iso_utc(self.ends_at),
                "source": self.source, "policy_version": self.policy_version, "frozen_at": self.frozen_at,
                "frozen_at_utc": iso_utc(self.frozen_at), "scheduled_date": self.scheduled_date,
                "until_label": self.until_label, "until_date": self.until_date,
                "events_sha256": self.events_sha256, "schedule_fetched_at": self.schedule_fetched_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FrozenWindow":
        try:
            window = cls(str(data["client_patch"]), int(data["starts_at"]), int(data["ends_at"]),
                         str(data["source"]), str(data["policy_version"]), int(data["frozen_at"]),
                         str(data["scheduled_date"]), str(data["until_label"]), str(data["until_date"]),
                         str(data["events_sha256"]), int(data["schedule_fetched_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError(f"a frozen window is incomplete ({exc})") from None
        if not window.source or window.starts_at >= window.ends_at:
            raise ScheduleError(f"frozen window {window.client_patch} is invalid")
        return window


def load_frozen(local_dir: Path) -> list[FrozenWindow]:
    """Frozen windows, checked against the fixed registry and each other.
    Raises ScheduleError when the file exists but cannot be trusted (the
    caller then pauses collection and never rewrites the file)."""
    path = Path(local_dir) / FROZEN_NAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScheduleError(f"{FROZEN_NAME} could not be read ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("format") != FROZEN_FORMAT \
            or data.get("format_version") != FROZEN_FORMAT_VERSION or not isinstance(data.get("windows"), list):
        raise ScheduleError(f"{FROZEN_NAME} has an unknown format")
    windows = [FrozenWindow.from_dict(w) for w in data["windows"]]
    try:
        _check_installable(windows)
    except ValueError as exc:
        raise ScheduleError(f"{FROZEN_NAME} is inconsistent: {exc}") from None
    return windows


def _check_installable(windows: list[FrozenWindow]) -> None:
    saved = unreal_patch.installed_windows()
    try:
        install_frozen_windows([w.as_window() for w in windows])  # validates without keeping it
    finally:
        install_frozen_windows(saved)


def freeze(local_dir: Path, window: DerivedWindow, schedule: Schedule, *, at_ms: int) -> FrozenWindow:
    """Append `window` to the frozen sidecar (no-op if that patch is frozen).
    Called only while the collection lock is held."""
    existing = load_frozen(local_dir)
    for frozen in existing:
        if frozen.client_patch == window.client_patch:
            return frozen
    item = FrozenWindow(window.client_patch, window.starts_at, window.ends_at, SOURCE_URL, window.policy_version,
                        at_ms, window.scheduled_date.isoformat(), window.until_label, window.until_date.isoformat(),
                        schedule.events_sha256, schedule.fetched_at)
    windows = [*existing, item]
    _check_installable(windows)
    _write_json_atomic(Path(local_dir) / FROZEN_NAME, {
        "format": FROZEN_FORMAT, "format_version": FROZEN_FORMAT_VERSION,
        "note": "Patch windows TheoryLabs has used for collection. Never edit or delete: stored matches were "
                "classified with them.",
        "windows": [w.as_dict() for w in windows]})
    log.info("froze patch %s window %s .. %s (%s)", item.client_patch, iso_utc(item.starts_at),
             iso_utc(item.ends_at), item.policy_version)
    return item


def install_frozen(local_dir: Path) -> list[FrozenWindow]:
    """Install the frozen windows for this local database into
    `unreal_patch`. An untrustworthy file installs nothing (and raises)."""
    try:
        windows = load_frozen(local_dir)
    except ScheduleError:
        install_frozen_windows(())
        raise
    install_frozen_windows([w.as_window() for w in windows])
    return windows


# ---------------------------------------------------------------- network


def fetch_schedule_page(url: str) -> tuple[int, str, bytes, str]:
    """(status, content type, body, final URL). The only network access."""
    import httpx

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_PAGE_BYTES:
                    raise ScheduleError("the schedule page is unexpectedly large")
            return response.status_code, response.headers.get("content-type", ""), bytes(body), str(response.url)


#: What `refresh_schedule` calls by default (tests replace it).
_http_get = fetch_schedule_page


@dataclass
class RefreshResult:
    attempted: bool
    ok: bool
    changed: bool = False
    message: str = ""


def refresh_schedule(local_dir: Path, *, now_ms: int, force: bool = False,
                     http_get: Callable[[str], tuple[int, str, bytes, str]] | None = None) -> RefreshResult:
    """Fetch, parse and validate Riot's schedule; replace the cache only with
    a schedule that passed every check. Throttled to one attempt per
    `MIN_REFRESH_INTERVAL_MS` unless `force`. Never raises for network or
    page problems: the old cache stays and the reason is recorded in
    `CHECK_NAME` (and logged)."""
    local_dir = Path(local_dir)
    last = load_check(local_dir) or {}
    last_at = last.get("at")
    if not force and isinstance(last_at, int) and 0 <= now_ms - last_at < MIN_REFRESH_INTERVAL_MS:
        return RefreshResult(False, bool(last.get("ok")), message="checked recently")
    get = http_get or _http_get
    try:
        status, content_type, body, final_url = get(SOURCE_URL)
        if status != 200:
            raise ScheduleError(f"Riot Support answered HTTP {status}")
        if not final_url.startswith(f"https://{SOURCE_HOST}/"):
            raise ScheduleError("the schedule page redirected away from Riot Support")
        if "html" not in content_type.lower():
            raise ScheduleError(f"unexpected content type {content_type!r}")
        html = body.decode("utf-8", errors="replace")
        try:
            events = parse_schedule_html(html)
        except ScheduleError:
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / REJECTED_NAME).write_bytes(body)  # a public page; kept for diagnosis
            raise
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ScheduleError) else f"could not reach Riot Support ({type(exc).__name__})"
        kind = "unexpected_structure" if isinstance(exc, ScheduleError) else "network"
        log.warning("patch schedule refresh failed (%s): %s; keeping the previous schedule", kind, reason)
        _write_json_atomic(local_dir / CHECK_NAME, {"at": now_ms, "at_utc": iso_utc(now_ms), "ok": False,
                                                    "kind": kind, "error": reason})
        return RefreshResult(True, False, message=reason)
    try:
        previous = load_cache(local_dir)
    except ScheduleError:
        previous = None
    schedule = Schedule(tuple(events), now_ms, "sha256:" + hashlib.sha256(body).hexdigest(), events_fingerprint(events))
    changed = previous is None or previous.events_sha256 != schedule.events_sha256
    _write_json_atomic(local_dir / CACHE_NAME, schedule.as_cache())
    _write_json_atomic(local_dir / CHECK_NAME, {"at": now_ms, "at_utc": iso_utc(now_ms), "ok": True,
                                                "kind": "ok", "error": None, "changed": changed})
    (local_dir / REJECTED_NAME).unlink(missing_ok=True)
    if changed:
        log.info("patch schedule updated: %d entries, fingerprint %s", len(events), schedule.events_sha256)
    return RefreshResult(True, True, changed, "schedule updated" if changed else "schedule unchanged")


# ---------------------------------------------------------------- the view


@dataclass
class CalendarState:
    """Everything the collector needs to know about patches right now."""

    now_ms: int
    schedule: Schedule | None = None
    schedule_error: str | None = None
    check: dict[str, Any] | None = None
    frozen: list[FrozenWindow] = field(default_factory=list)
    frozen_error: str | None = None
    timezone_error: str | None = None
    planned: list[DerivedWindow] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    frozen_conflicts: set[str] = field(default_factory=set)
    diagnostics: list[str] = field(default_factory=list)
    current: UnrealPatchWindow | None = None
    current_origin: str | None = None  # "built-in" | "frozen" | "planned"
    current_planned: DerivedWindow | None = None
    ready: bool = False
    code: str = ""
    message: str = ""

    # -- derived facts for display

    def next_event(self) -> ScheduleEvent | None:
        """The first schedule entry after the current (or last) patch."""
        if self.schedule is None:
            return None
        today = datetime.fromtimestamp(self.now_ms / 1000, tz=_pacific()).date() if not self.timezone_error else None
        for event in self.schedule.events:
            if today is not None and event.scheduled_date >= today:
                return event
        return None

    def upcoming_balance(self) -> ScheduleEvent | None:
        """The next scheduled balance (suffix) update, if listed."""
        nxt = self.next_event()
        if nxt is None or self.schedule is None:
            return None
        for event in self.schedule.events:
            if event.kind == BALANCE and event.scheduled_date >= nxt.scheduled_date:
                return event
        return None

    def freshness(self) -> dict[str, Any]:
        check = self.check or {}
        fetched = self.schedule.fetched_at if self.schedule else None
        if self.schedule is None:
            state = "failed" if check.get("ok") is False else "none"
            label = "not available — could not read Riot's schedule" if state == "failed" else "not checked yet"
        elif check.get("ok") is False and isinstance(check.get("at"), int) and check["at"] > fetched:
            state = "cached_refresh_failed"
            label = f"cached — refresh failed (Riot checked {_ago(fetched, self.now_ms)})"
        else:
            state, label = "fresh", "Riot — checked " + _ago(fetched, self.now_ms)
        if self.schedule is not None and self.now_ms - fetched > MAX_SCHEDULE_AGE_FOR_NEW_WINDOW_MS:
            state = state if state == "cached_refresh_failed" else "old"
        return {"state": state, "label": label, "fetched_at": fetched, "last_check_at": check.get("at"),
                "last_check_ok": check.get("ok"), "last_check_error": check.get("error")}

    def public(self) -> dict[str, Any]:
        """JSON-safe view for the dashboard and `local-status`. Contains no
        secrets or player data: only public schedule facts and provenance."""
        nxt, balance = self.next_event(), self.upcoming_balance()
        planned_next = next((w for w in self.planned if w.starts_at > self.now_ms), None)
        cur = self.current
        return {
            "ready": self.ready, "code": self.code, "message": self.message,
            "current": None if cur is None else {
                "patch": cur.client_patch, "starts_at": cur.starts_at, "ends_at": cur.ends_at,
                "origin": self.current_origin, "source": cur.source},
            "next": None if nxt is None else {
                "label": nxt.label, "kind": nxt.kind, "base_patch": nxt.base_patch,
                "scheduled_date": nxt.scheduled_date.isoformat(), "timezone": PACIFIC_TZ},
            "next_window": None if planned_next is None else planned_next.as_dict(),
            "upcoming_balance": None if balance is None else {
                "label": balance.label, "base_patch": balance.base_patch,
                "scheduled_date": balance.scheduled_date.isoformat()},
            "freshness": self.freshness(),
            "provenance": {
                "source_id": SOURCE_ID, "source_name": SOURCE_NAME, "source_url": SOURCE_URL,
                "fetched_at": self.schedule.fetched_at if self.schedule else None,
                "content_sha256": self.schedule.content_sha256 if self.schedule else None,
                "events_sha256": self.schedule.events_sha256 if self.schedule else None,
                "parser_version": self.schedule.parser_version if self.schedule else PARSER_VERSION,
                "policy_version": POLICY_VERSION,
                "events": [e.as_dict() for e in self.schedule.events] if self.schedule else [],
                "planned_windows": [w.as_dict() for w in self.planned],
                "frozen_windows": [w.as_dict() for w in self.frozen],
                "built_in_windows": [{"client_patch": w.client_patch, "starts_at": w.starts_at, "ends_at": w.ends_at,
                                      "starts_at_utc": iso_utc(w.starts_at), "ends_at_utc": iso_utc(w.ends_at)}
                                     for w in unreal_patch.UNREAL_PATCH_REGISTRY if w.is_usable],
                "schedule_error": self.schedule_error, "frozen_error": self.frozen_error,
                "conflicts": list(self.conflicts), "diagnostics": list(self.diagnostics),
            },
        }


def _ago(ms: int | None, now_ms: int) -> str:
    if ms is None:
        return "never"
    then = datetime.fromtimestamp(ms / 1000).date()
    today = datetime.fromtimestamp(now_ms / 1000).date()
    days = (today - then).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def _fmt_day(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def _fmt_pacific(ms: int) -> str:
    try:
        local, zone = datetime.fromtimestamp(ms / 1000, tz=_pacific()), "Pacific"
    except Exception:  # no time-zone data: still say something true
        local, zone = datetime.fromtimestamp(ms / 1000, tz=timezone.utc), "UTC"
    return f"{local.strftime('%b')} {local.day}, {local.year} {local.strftime('%H:%M')} {zone}"


PAUSED = "Collection is paused rather than mixing patches. No Riot data was collected."
FOUND_NEXT_BUT_UNSAFE = ("TheoryLabs found the next scheduled patch but cannot create a trusted collection window "
                         "safely yet.")


def calendar_state(local_dir: Path, *, now_ms: int) -> CalendarState:
    """The patch situation at `now_ms`. Read-only and network-free: reads
    the cache, the last check and the frozen sidecar; writes nothing."""
    local_dir = Path(local_dir)
    st = CalendarState(now_ms=now_ms, check=load_check(local_dir))
    try:
        _pacific()
    except Exception as exc:
        st.timezone_error = f"the America/Los_Angeles time zone is unavailable ({type(exc).__name__})"
    try:
        st.schedule = load_cache(local_dir)
    except ScheduleError as exc:
        st.schedule_error = str(exc)
    try:
        st.frozen = load_frozen(local_dir)
    except ScheduleError as exc:
        st.frozen_error = str(exc)

    if st.schedule is not None and st.timezone_error is None:
        _plan(st)
        st.diagnostics.extend(
            f"{e.label}: {e.raw_date!r} names a weekday that date is not; both readings are treated conservatively"
            for e in st.schedule.events if e.ambiguous)
    fixed = [w for w in unreal_patch.UNREAL_PATCH_REGISTRY if w.is_usable]
    origins: dict[int, tuple[str, DerivedWindow | None]] = {}
    registry: list[UnrealPatchWindow] = []
    for w in fixed:
        registry.append(w)
        origins[id(w)] = ("built-in", None)
    if st.frozen_error is None:
        for f in st.frozen:
            w = f.as_window()
            registry.append(w)
            origins[id(w)] = ("frozen", None)
        for d in st.planned:
            w = d.as_window()
            registry.append(w)
            origins[id(w)] = ("planned", d)
    try:
        current = current_trusted_window(now_ms, registry=tuple(registry))
    except NoCurrentTrustedWindow:
        current = None
    if current is not None:
        st.current_origin, st.current_planned = origins[id(current)]
        st.current = current
    _decide(st)
    return st


def _plan(st: CalendarState) -> None:
    """Planned windows: derived windows strictly after all history, never
    overlapping it; plus conflicts between the schedule and frozen windows."""
    assert st.schedule is not None
    derived = derive_windows(list(st.schedule.events))
    fixed = [w for w in unreal_patch.UNREAL_PATCH_REGISTRY if w.is_usable]
    known = [*fixed, *(f.as_window() for f in st.frozen)]
    frozen_by_patch = {f.client_patch: f for f in st.frozen}
    latest_known = max((patch_sort_key(w.client_patch) for w in known), default=(-1, -1, ""))
    last_end = max((w.ends_at for w in known), default=0)
    fixed_patches = {w.client_patch for w in unreal_patch.UNREAL_PATCH_REGISTRY}
    for d in derived:
        frozen = frozen_by_patch.get(d.client_patch)
        if frozen is not None:
            if d.ends_at < frozen.ends_at:
                st.conflicts.append(
                    f"Riot's schedule now has {d.until_label} on {_fmt_day(d.until_date)}, earlier than when patch "
                    f"{frozen.client_patch}'s frozen window was created ({frozen.until_label} on {frozen.until_date}); "
                    f"the frozen window cannot move, so collection in it is paused.")
                st.frozen_conflicts.add(frozen.client_patch)
            elif (d.starts_at, d.ends_at) != (frozen.starts_at, frozen.ends_at):
                st.diagnostics.append(f"patch {frozen.client_patch}: the schedule changed after its window was "
                                      "frozen; the frozen window is kept unchanged")
            continue
        if d.client_patch in fixed_patches or patch_sort_key(d.client_patch) <= latest_known:
            continue  # history: fixed in code or older than it; never regenerated
        if d.starts_at < last_end:
            st.conflicts.append(f"patch {d.client_patch}'s window would start before the previous patch's frozen "
                                "window ends; no window is created for it.")
            continue
        st.planned.append(d)
        last_end = d.ends_at


def _decide(st: CalendarState) -> None:
    cur, nxt = st.current, st.next_event()
    if st.frozen_error is not None:
        st.code, st.current, st.current_origin = "frozen_unreadable", None, None
        st.message = (f"TheoryLabs cannot read its record of patch windows already used ({st.frozen_error}). "
                      f"That record keeps stored data classified consistently, so collection is paused until it is "
                      f"restored (for example from a backup of the data/local folder). {PAUSED}")
        return
    if cur is not None and st.current_origin == "planned" and st.schedule is not None \
            and st.now_ms - st.schedule.fetched_at > MAX_SCHEDULE_AGE_FOR_NEW_WINDOW_MS:
        st.code, st.current, st.current_origin = "schedule_too_old", None, None
        st.message = (f"{FOUND_NEXT_BUT_UNSAFE} Its saved copy of Riot's schedule was last checked "
                      f"{_ago(st.schedule.fetched_at, st.now_ms)} and could not be refreshed; a new patch window "
                      f"is only started from a schedule checked within the last 48 hours. {PAUSED}")
        return
    if cur is not None and cur.client_patch in st.frozen_conflicts:
        st.code, st.current, st.current_origin = "schedule_conflict", None, None
        st.message = f"{FOUND_NEXT_BUT_UNSAFE} {st.conflicts[0]} {PAUSED}"
        return
    if cur is not None:
        st.ready, st.code = True, "ready"
        st.message = f"Patch {cur.client_patch} is trusted until {_fmt_pacific(cur.ends_at)}."
        return
    # Not inside any window: explain why.
    if st.timezone_error:  # only new windows need Pacific Time; fixed and frozen ones are stored instants
        st.code = "no_timezone"
        st.message = (f"TheoryLabs cannot compute Pacific Time patch boundaries for a new patch: "
                      f"{st.timezone_error}. Re-run the one-time install step (pip install -e .) to add the "
                      f"time-zone data. {PAUSED}")
        return
    upcoming = next((w for w in _all_windows(st) if w.starts_at > st.now_ms), None)
    recent_balance = _recent_balance(st)
    if recent_balance is not None:
        st.code = "balance_update"
        resume = f" Collection resumes at {_fmt_pacific(upcoming.starts_at)} (patch {upcoming.client_patch})." \
            if upcoming else " It resumes once Riot's schedule lists the next patch."
        st.message = (f"Riot scheduled a balance update ({recent_balance.label}) on "
                      f"{_fmt_day(recent_balance.scheduled_date)}. It is not a new client patch, and Riot's match "
                      f"data gives TheoryLabs no trusted way to separate matches before and after it, so collection "
                      f"pauses across it.{resume} {PAUSED}")
        return
    if upcoming is not None:
        st.code = "between_patches"
        what = f"Patch {nxt.label} is scheduled for {_fmt_day(nxt.scheduled_date)} (Pacific Time). " \
            if nxt is not None and nxt.kind == ORDINARY and nxt.base_patch == upcoming.client_patch else ""
        st.message = (f"{what}TheoryLabs leaves a gap around every patch change and starts trusting patch "
                      f"{upcoming.client_patch} at {_fmt_pacific(upcoming.starts_at)}, automatically. {PAUSED}")
        return
    if st.schedule is None:
        st.code = "no_schedule"
        why = st.schedule_error or (st.check or {}).get("error") or "it has not been checked yet"
        st.message = ("TheoryLabs could not read Riot's official patch schedule and has no usable saved copy "
                      f"({why}), so it cannot create a window for the new patch. It will try again when the app "
                      f"starts or before the next collection. {PAUSED}")
        return
    balance = st.upcoming_balance()
    if st.conflicts:
        st.code = "schedule_conflict"
        st.message = f"{FOUND_NEXT_BUT_UNSAFE} {st.conflicts[0]} {PAUSED}"
        return
    if nxt is not None or balance is not None:
        st.code = "no_successor"
        target = nxt or balance
        st.message = (f"{FOUND_NEXT_BUT_UNSAFE} Patch {target.label} is scheduled for "
                      f"{_fmt_day(target.scheduled_date)}, but Riot's schedule does not yet list the patch after "
                      f"it, so its window has no trusted end. {PAUSED}")
        return
    last = st.schedule.events[-1]
    st.code = "no_successor"
    st.message = (f"Riot's schedule ends with {last.label} ({_fmt_day(last.scheduled_date)}) and does not yet list "
                  f"the patch after it, so TheoryLabs cannot create a trusted window with an end. {PAUSED}")


def _all_windows(st: CalendarState) -> list[UnrealPatchWindow]:
    fixed = [w for w in unreal_patch.UNREAL_PATCH_REGISTRY if w.is_usable]
    return sorted([*fixed, *(f.as_window() for f in st.frozen), *(d.as_window() for d in st.planned)],
                  key=lambda w: w.starts_at)


def _recent_balance(st: CalendarState) -> ScheduleEvent | None:
    """The balance event that ended the most recent window, if we're in the
    pause it causes (its preceding window ended and the next has not started)."""
    if st.schedule is None:
        return None
    ended = [w for w in _all_windows(st) if w.ends_at <= st.now_ms]
    if not ended:
        return None
    previous = ended[-1]
    for event, successor in zip(st.schedule.events, st.schedule.events[1:]):
        if event.kind == ORDINARY and event.base_patch == previous.client_patch and successor.kind == BALANCE:
            return successor
    return None


def collection_window(local_dir: Path, *, now_ms: int) -> tuple[UnrealPatchWindow, CalendarState]:
    """The window a collection starting now may use. Freezes a planned
    window first (so it can never move afterwards) and installs all frozen
    windows into `unreal_patch` for classification. Raises
    `CalendarNotReady` (a `NoCurrentTrustedWindow`) with a plain message
    otherwise. Call only while holding the collection lock."""
    st = calendar_state(local_dir, now_ms=now_ms)
    if not st.ready or st.current is None:
        try:
            install_frozen(local_dir)
        except ScheduleError:
            pass
        raise CalendarNotReady(st.message, st)
    if st.current_origin == "planned":
        assert st.current_planned is not None and st.schedule is not None
        freeze(local_dir, st.current_planned, st.schedule, at_ms=now_ms)
        st = calendar_state(local_dir, now_ms=now_ms)
        if not st.ready or st.current is None or st.current_origin != "frozen":  # pragma: no cover - defensive
            raise CalendarNotReady(st.message or "the frozen window could not be confirmed", st)
    install_frozen(local_dir)
    return st.current, st
