"""Riot API rate limits: header parsing, proactive pacing and telemetry.

Riot's rules (Developer Portal, "Rate Limiting", checked 2026-09-26 --
https://developer.riotgames.com/docs/portal#web-apis_rate-limiting):

- Three kinds of limit, all enforced **per region**: *application* (per API
  key, every call to any endpoint in the region counts), *method* (per
  endpoint per key) and *service* (per service, shared by all applications).
- A 429 means "halt future API calls for the duration, in seconds, indicated
  by the Retry-After header". `X-Rate-Limit-Type` is only present when the
  API edge enforced the limit; an underlying service may also 429 without it.
- Riot does not reveal how its buckets work; "you can assume ... the bucket
  starts when you make your first API call".

The limits themselves are not hard-coded here: every response's
`X-App-Rate-Limit` / `X-Method-Rate-Limit` headers (and their `-Count`
companions) tell the limiter what applies, as comma-separated
`<requests>:<seconds>` windows (e.g. `20:1,100:120`) -- the format Riot's
edge returns (the portal page names the limits but does not spell the header
format out; anything else is treated as malformed and counted). Until a
scope has reported its limits, it is paced conservatively.

Pacing is local and sliding-window: before each request, for every window of
the request's application scope (routing host) and method scope (host +
endpoint), the limiter waits until fewer than `floor(limit * utilization)`
requests fall inside the window. Server-reported counts only ever raise the
local count (never lower it), so drift is always toward caution. Operator
`ceilings` (e.g. a policy cap such as `10:10`) are applied to every host at
100% on top of whatever Riot reports.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Mapping

#: Default share of each server-advertised window the client plans to use.
DEFAULT_SAFETY_UTILIZATION = 0.9
#: Pacing for a scope that has not reported its limits yet: one request per
#: second (the first request goes out immediately; its response carries the
#: real limits).
BOOTSTRAP_WINDOW_SECONDS = 1.0
#: Scope keys. A scope is where a Riot limit is counted.
APP, METHOD, SERVICE = "application", "method", "service"

_WINDOW = re.compile(r"^(\d+):(\d+)$")


class MalformedRateLimitHeader(ValueError):
    """A rate-limit header whose value is not `<n>:<seconds>[,...]`."""


@dataclass(frozen=True, order=True)
class RateWindow:
    """At most `limit` requests in any `seconds`-long window."""

    seconds: int
    limit: int

    def __str__(self) -> str:
        return f"{self.limit}:{self.seconds}"


def _pairs(value: str | None, header: str) -> list[tuple[int, int]]:
    if value is None or not value.strip():
        raise MalformedRateLimitHeader(f"{header} is empty")
    pairs = []
    for part in value.split(","):
        match = _WINDOW.match(part.strip())
        if not match:
            raise MalformedRateLimitHeader(f"{header} has an unexpected window {part.strip()!r}")
        first, seconds = int(match.group(1)), int(match.group(2))
        if seconds <= 0:
            raise MalformedRateLimitHeader(f"{header} has a non-positive window length")
        pairs.append((first, seconds))
    if len({s for _, s in pairs}) != len(pairs):
        raise MalformedRateLimitHeader(f"{header} repeats a window length")
    return pairs


def parse_rate_limits(value: str | None, header: str = "rate limit header") -> tuple[RateWindow, ...]:
    """`"20:1,100:120"` -> (RateWindow(1, 20), RateWindow(120, 100)): each
    window independent. Raises MalformedRateLimitHeader for anything else,
    including a zero limit."""
    pairs = _pairs(value, header)
    if any(limit <= 0 for limit, _ in pairs):
        raise MalformedRateLimitHeader(f"{header} has a non-positive limit")
    return tuple(sorted(RateWindow(seconds, limit) for limit, seconds in pairs))


def parse_rate_counts(value: str | None, header: str = "rate limit count header") -> dict[int, int]:
    """`"3:1,50:120"` -> {1: 3, 120: 50} (requests counted per window length)."""
    return {seconds: count for count, seconds in _pairs(value, header)}


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After in seconds (Riot sends delta-seconds); None if absent or
    not a non-negative number."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 and math.isfinite(seconds) else None


@dataclass
class _Scope:
    label: str
    windows: tuple[RateWindow, ...] = ()
    #: False until a response has reported (or failed to report) limits.
    observed: bool = False
    history: deque = field(default_factory=deque)
    blocked_until: float = 0.0


@dataclass
class RiotTelemetry:
    """Aggregate, low-cardinality counters for one client. Never holds the
    API key, URLs with ids, or response bodies."""

    requests: int = 0
    successes: int = 0
    by_method: Counter = field(default_factory=Counter)
    by_host: Counter = field(default_factory=Counter)
    by_status: Counter = field(default_factory=Counter)
    rate_limited: int = 0
    rate_limited_by_type: Counter = field(default_factory=Counter)
    transient_retries: int = 0
    network_retries: int = 0
    pacing_sleep_s: float = 0.0
    rate_limit_sleep_s: float = 0.0
    backoff_sleep_s: float = 0.0
    #: Latest limits each scope reported, e.g. {"app na1.api.riotgames.com": "20:1,100:120"}.
    app_limits: dict[str, str] = field(default_factory=dict)
    method_limits: dict[str, str] = field(default_factory=dict)
    #: Highest server-reported count/limit per scope window, e.g.
    #: {"app na1.api.riotgames.com 120s": 0.92}.
    high_water: dict[str, float] = field(default_factory=dict)
    malformed_headers: Counter = field(default_factory=Counter)
    missing_headers: Counter = field(default_factory=Counter)
    started_monotonic: float | None = None
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "by_method": dict(sorted(self.by_method.items())),
            "by_host": dict(sorted(self.by_host.items())),
            "by_status": {str(k): v for k, v in sorted(self.by_status.items(), key=lambda kv: str(kv[0]))},
            "rate_limited": self.rate_limited,
            "rate_limited_by_type": dict(sorted(self.rate_limited_by_type.items())),
            "transient_retries": self.transient_retries,
            "network_retries": self.network_retries,
            "pacing_sleep_s": round(self.pacing_sleep_s, 3),
            "rate_limit_sleep_s": round(self.rate_limit_sleep_s, 3),
            "backoff_sleep_s": round(self.backoff_sleep_s, 3),
            "app_limits": dict(sorted(self.app_limits.items())),
            "method_limits": dict(sorted(self.method_limits.items())),
            "high_water": {k: round(v, 4) for k, v in sorted(self.high_water.items())},
            "malformed_headers": dict(sorted(self.malformed_headers.items())),
            "missing_headers": dict(sorted(self.missing_headers.items())),
            "elapsed_s": round(self.elapsed_s, 3),
        }


class RateLimiter:
    """Local sliding-window budget per Riot scope.

    Scopes: application = routing host (`na1.api.riotgames.com`,
    `americas.api.riotgames.com` -- Riot enforces limits per region), method
    = host + endpoint method id, service = host + API service (used only to
    pause after a service/unknown 429). `acquire` returns how long to wait
    before the next request may go out; `record` counts a request that did.
    """

    def __init__(
        self,
        *,
        utilization: float = DEFAULT_SAFETY_UTILIZATION,
        ceilings: tuple[RateWindow, ...] = (),
        telemetry: RiotTelemetry | None = None,
    ) -> None:
        if not 0 < utilization <= 1:
            raise ValueError("utilization must be in (0, 1]")
        self.utilization = utilization
        self.ceilings = tuple(ceilings)
        self.telemetry = telemetry or RiotTelemetry()
        self._scopes: dict[tuple[str, ...], _Scope] = {}

    def _scope(self, kind: str, host: str, name: str = "") -> _Scope:
        key = (kind, host, name)
        if key not in self._scopes:
            label = f"{'app' if kind == APP else kind} {host}" + (f" {name}" if name else "")
            self._scopes[key] = _Scope(label)
        return self._scopes[key]

    # ---------------------------------------------------------------- pacing

    @staticmethod
    def _window_wait(history: deque, seconds: float, allowed: int, now: float) -> float:
        inside = [t for t in history if t > now - seconds]
        if len(inside) < allowed:
            return 0.0
        # The oldest requests that must leave the window before one more fits.
        return max(0.0, inside[len(inside) - allowed] + seconds - now)

    def _scope_wait(self, scope: _Scope, now: float, extra: tuple[RateWindow, ...] = ()) -> float:
        waits = [scope.blocked_until - now]
        if not scope.observed:
            waits.append(self._window_wait(scope.history, BOOTSTRAP_WINDOW_SECONDS, 1, now))
        for window in scope.windows:
            allowed = max(1, math.floor(window.limit * self.utilization))
            waits.append(self._window_wait(scope.history, window.seconds, allowed, now))
        for ceiling in extra:  # operator policy caps: exact, not scaled
            waits.append(self._window_wait(scope.history, ceiling.seconds, ceiling.limit, now))
        return max(0.0, *waits)

    def wait_time(self, host: str, method: str, service: str, now: float) -> tuple[float, str]:
        """(seconds to wait, "blocked" | "pacing") before a request."""
        app, meth, serv = self._scope(APP, host), self._scope(METHOD, host, method), self._scope(SERVICE, host, service)
        blocked = max(app.blocked_until, meth.blocked_until, serv.blocked_until) - now
        pacing = max(self._scope_wait(app, now, self.ceilings), self._scope_wait(meth, now))
        if blocked > 0 and blocked >= pacing:
            return blocked, "blocked"
        return max(0.0, pacing), "pacing"

    def record(self, host: str, method: str, service: str, now: float) -> None:
        for scope in (self._scope(APP, host), self._scope(METHOD, host, method)):
            scope.history.append(now)
            horizon = max([w.seconds for w in scope.windows] + [c.seconds for c in self.ceilings] + [BOOTSTRAP_WINDOW_SECONDS])
            while scope.history and scope.history[0] <= now - horizon:
                scope.history.popleft()

    # ---------------------------------------------------------------- telemetry from Riot

    def _apply(self, scope: _Scope, limits_header: str, count_header: str, headers: Mapping[str, str],
               now: float, report: dict[str, str]) -> None:
        raw_limits = headers.get(limits_header)
        if raw_limits is None:
            self.telemetry.missing_headers[limits_header] += 1
            if not scope.windows:
                scope.observed = scope.observed or scope.label.startswith("method")
            return
        try:
            windows = parse_rate_limits(raw_limits, limits_header)
        except MalformedRateLimitHeader:
            self.telemetry.malformed_headers[limits_header] += 1
            return  # keep what we had (or the conservative bootstrap)
        scope.windows = windows
        scope.observed = True
        report[scope.label] = ",".join(str(w) for w in windows)
        raw_counts = headers.get(count_header)
        if raw_counts is None:
            self.telemetry.missing_headers[count_header] += 1
            return
        try:
            counts = parse_rate_counts(raw_counts, count_header)
        except MalformedRateLimitHeader:
            self.telemetry.malformed_headers[count_header] += 1
            return
        for window in windows:
            count = counts.get(window.seconds)
            if count is None:
                continue
            key = f"{scope.label} {window.seconds}s"
            self.telemetry.high_water[key] = max(self.telemetry.high_water.get(key, 0.0), count / window.limit)
            inside = sum(1 for t in scope.history if t > now - window.seconds)
            for _ in range(count - inside):  # Riot has counted more than we did: catch up (never down)
                scope.history.append(now)
        scope.history = deque(sorted(scope.history))

    def observe(self, host: str, method: str, headers: Mapping[str, str], now: float) -> None:
        """Update limits and counts from one response's headers."""
        self._apply(self._scope(APP, host), "X-App-Rate-Limit", "X-App-Rate-Limit-Count", headers, now,
                    self.telemetry.app_limits)
        self._apply(self._scope(METHOD, host, method), "X-Method-Rate-Limit", "X-Method-Rate-Limit-Count",
                    headers, now, self.telemetry.method_limits)

    def penalize(self, host: str, method: str, service: str, limit_type: str | None, pause: float, now: float) -> str:
        """Pause the scope a 429 names for `pause` seconds; returns the scope
        kind paused. Application -> every request to this host; method ->
        this endpoint; service or no/unknown type -> every endpoint of this
        API service on this host (Riot: an underlying service can limit
        without sending X-Rate-Limit-Type)."""
        kind = (limit_type or "").strip().lower()
        if kind == APP:
            scope = self._scope(APP, host)
        elif kind == METHOD:
            scope = self._scope(METHOD, host, method)
        else:
            kind = SERVICE if kind == SERVICE else "unknown"
            scope = self._scope(SERVICE, host, service)
        scope.blocked_until = max(scope.blocked_until, now + pause)
        return kind
