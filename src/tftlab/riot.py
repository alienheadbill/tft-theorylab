from __future__ import annotations

import re
import time
from typing import Any, Callable

import httpx

from .riot_limits import DEFAULT_SAFETY_UTILIZATION, RateLimiter, RateWindow, RiotTelemetry, parse_retry_after


class RiotApiError(RuntimeError):
    pass


#: Riot's `queue_id` for standard Ranked Teamfight Tactics, per Riot's public
#: queue metadata (https://static.developer.riotgames.com/docs/lol/queues.json,
#: unreachable from this sandbox's network egress -- this value is taken from
#: well-established, widely-documented TFT tooling convention instead, and
#: matches what this codebase's own test fixtures (`tests/_helpers.py`'s
#: `make_match`) have defaulted to since Milestone 1). Distinct from Normal
#: (unranked) TFT (1090), Hyper Roll (1130), and Double Up (1160): a
#: Challenger-ladder PUUID's recent match history can include any of these,
#: but this project's discovery dataset is scoped to standard ranked only --
#: see `ingest.ingest_ladder`. Verify against the real values seen in a live
#: ingest before trusting this beyond that scope.
RANKED_TFT_QUEUE_ID = 1100

#: TFT-LEAGUE-V1 `queue` query value for standard ranked (league endpoints
#: take the queue name, Match-V1 bodies carry the numeric id above).
RANKED_TFT_QUEUE = "RANKED_TFT"

#: Tiers served by `/tft/league/v1/entries/{tier}/{division}` (every tier
#: below Master; the apex tiers have their own league-list endpoints).
DIVISIONAL_TIERS: tuple[str, ...] = ("DIAMOND", "EMERALD", "PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON")
LEAGUE_DIVISIONS: tuple[str, ...] = ("I", "II", "III", "IV")


_STATUS_RE = re.compile(r"returned (\d+)")


def classify_riot_error(message: str) -> str:
    """Categorize a `RiotApiError` message into a short, actionable label.

    Pulled out as a pure function (rather than inlined where errors are
    handled) so callers -- and tests -- can classify a Riot failure without
    needing a live API key or network access.
    """
    if "Repeated rate limiting" in message:
        return "rate_limited"
    if "Network error contacting Riot API" in message:
        return "network_error"
    match = _STATUS_RE.search(message)
    if not match:
        return "unknown"
    status = int(match.group(1))
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    return f"http_{status}"


#: Riot method ids (`<api service>.<method>`, as the Riot API reference
#: names them). Each is its own method-rate-limit scope; the part before the
#: dot is the API service (service-rate-limit scope).
CHALLENGER_METHOD = "tft-league-v1.getChallengerLeague"
GRANDMASTER_METHOD = "tft-league-v1.getGrandmasterLeague"
MASTER_METHOD = "tft-league-v1.getMasterLeague"
LEAGUE_ENTRIES_METHOD = "tft-league-v1.getLeagueEntries"
MATCH_IDS_METHOD = "tft-match-v1.getMatchIdsByPUUID"
MATCH_METHOD = "tft-match-v1.getMatch"

#: Retries after the first attempt. A 429 is retried after the pause it
#: asks for (Retry-After); a timeout/network error or 5xx after a short
#: exponential backoff. 4xx other than 429 (401, 403, 404, ...) is never
#: retried. Every loop is bounded: when retries run out the request fails
#: with a RiotApiError the caller can count.
MAX_RATE_LIMIT_RETRIES = 3
MAX_TRANSIENT_RETRIES = 2
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
#: Backoff before transient retry n (1 s, 2 s, ...), capped.
TRANSIENT_BACKOFF_S = 1.0
MAX_BACKOFF_S = 8.0
#: Pause after a 429 that carries no usable Retry-After (Riot: an
#: underlying service may 429 without the edge's headers), doubled per
#: repeat.
DEFAULT_429_PAUSE_S = 1.0
#: A Retry-After longer than this is not slept through: the request fails
#: at once (and the scope stays paused for the full duration, so no later
#: request goes out early either).
MAX_RETRY_AFTER_S = 900.0

# Indirection so tests can swap in a fake clock without real sleeping.
_monotonic = time.monotonic
_sleep = time.sleep


class RiotBudgetExhausted(RuntimeError):
    """An operator budget (request count or wall-clock deadline) would be
    exceeded by the next request. Not a RiotApiError: callers stop starting
    new work instead of counting it as one failed request."""


class RiotClient:
    """TFT API client that paces itself to Riot's advertised rate limits.

    Ladder endpoints use a platform route (e.g. na1), while Match-V1 uses a
    regional route (e.g. americas); each routing host is its own
    application-limit scope, each endpoint on it its own method scope (see
    `tftlab.riot_limits`). Every request first waits until all of its
    scopes have room at `safety_utilization` of the limits Riot's response
    headers advertise (conservatively paced until the first headers
    arrive), plus any operator `rate_ceilings` (e.g. a policy cap such as
    ``10:10``) applied at 100% per host. A 429 is a fallback, not the
    pacing mechanism: it pauses the scope it names for Retry-After seconds
    and is retried a bounded number of times.

    `max_requests` / `deadline` (a `clock()` value) are hard operator
    budgets: a request that would exceed one raises RiotBudgetExhausted
    instead of being sent (or waiting past the deadline). The API key is
    only ever sent as a header; it never appears in errors or telemetry.
    """

    def __init__(
        self,
        api_key: str,
        *,
        platform: str = "na1",
        region: str = "americas",
        timeout: float = 20.0,
        safety_utilization: float = DEFAULT_SAFETY_UTILIZATION,
        rate_ceilings: tuple[RateWindow, ...] = (),
        max_requests: int | None = None,
        deadline: float | None = None,
        max_rate_limit_retries: int = MAX_RATE_LIMIT_RETRIES,
        max_transient_retries: int = MAX_TRANSIENT_RETRIES,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A Riot API key is required")
        if max_requests is not None and max_requests < 0:
            raise ValueError("max_requests must be non-negative")
        self.api_key = api_key
        self.platform = platform
        self.region = region
        self.max_requests = max_requests
        self.deadline = deadline
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_transient_retries = max_transient_retries
        self._clock = clock or _monotonic
        self._sleep = sleep or _sleep
        self.telemetry = RiotTelemetry()
        self.limiter = RateLimiter(utilization=safety_utilization, ceilings=rate_ceilings, telemetry=self.telemetry)
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Riot-Token": api_key, "User-Agent": "tft-theory-lab/0.1"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RiotClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def telemetry_snapshot(self) -> dict[str, Any]:
        """Aggregate telemetry (counts, sleeps, limits, high-water marks)
        as a JSON-safe dict. Holds no key, ids or bodies."""
        if self.telemetry.started_monotonic is not None:
            self.telemetry.elapsed_s = self._clock() - self.telemetry.started_monotonic
        return self.telemetry.as_dict()

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[redacted]") if self.api_key else text

    def _error(self, message: str) -> RiotApiError:
        return RiotApiError(self._redact(message))

    def _wait_for_capacity(self, host: str, method: str, service: str) -> None:
        """Sleep until every scope of this request has room. Each sleep
        lasts exactly until the earliest moment a slot opens, so this never
        spins; the deadline is checked before sleeping, never after."""
        while True:
            now = self._clock()
            wait, reason = self.limiter.wait_time(host, method, service, now)
            if wait <= 1e-9:
                return
            if self.deadline is not None and now + wait > self.deadline:
                raise RiotBudgetExhausted(
                    f"wall-clock budget reached: the next {method} request would have to wait {wait:.1f}s"
                )
            self._sleep(wait)
            if reason == "blocked":
                self.telemetry.rate_limit_sleep_s += wait
            else:
                self.telemetry.pacing_sleep_s += wait

    def _backoff(self, seconds: float) -> None:
        if self.deadline is not None and self._clock() + seconds > self.deadline:
            raise RiotBudgetExhausted("wall-clock budget reached during retry backoff")
        self._sleep(seconds)
        self.telemetry.backoff_sleep_s += seconds

    def _get(self, url: str, params: dict[str, Any] | None = None, *, method: str = "unknown.unknown") -> Any:
        host = httpx.URL(url).host
        service = method.split(".", 1)[0]
        telemetry = self.telemetry
        rate_limited = 0
        transient = 0
        while True:
            if self.max_requests is not None and telemetry.requests >= self.max_requests:
                raise RiotBudgetExhausted(f"request budget reached ({self.max_requests} Riot requests)")
            self._wait_for_capacity(host, method, service)
            now = self._clock()
            if telemetry.started_monotonic is None:
                telemetry.started_monotonic = now
            self.limiter.record(host, method, service, now)
            telemetry.requests += 1
            telemetry.by_method[method] += 1
            telemetry.by_host[host] += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                # httpx.RequestError's own message is just the underlying
                # network failure (DNS, timeout, connection refused, ...);
                # the API key is sent only via a header and never appears in
                # it or in `url`. Timeouts and connection failures are
                # retried a bounded number of times, then surfaced.
                telemetry.by_status["network_error"] += 1
                if transient < self.max_transient_retries:
                    telemetry.network_retries += 1
                    self._backoff(min(MAX_BACKOFF_S, TRANSIENT_BACKOFF_S * 2**transient))
                    transient += 1
                    continue
                raise self._error(
                    f"Network error contacting Riot API ({type(exc).__name__}) for {url}"
                ) from exc
            headers = httpx.Headers(response.headers)
            status = response.status_code
            telemetry.by_status[status] += 1
            self.limiter.observe(host, method, headers, self._clock())
            if 200 <= status < 300:
                telemetry.successes += 1
                return response.json()
            if status == 429:
                telemetry.rate_limited += 1
                retry_after = parse_retry_after(headers.get("Retry-After"))
                pause = retry_after if retry_after is not None else DEFAULT_429_PAUSE_S * 2**rate_limited
                kind = self.limiter.penalize(
                    host, method, service, headers.get("X-Rate-Limit-Type"), pause, self._clock()
                )
                telemetry.rate_limited_by_type[kind] += 1
                if pause > MAX_RETRY_AFTER_S:
                    raise self._error(
                        f"Repeated rate limiting for {url}: Riot asked to halt {kind} requests for "
                        f"{pause:.0f}s (over the {MAX_RETRY_AFTER_S:.0f}s the client will wait)"
                    )
                if rate_limited >= self.max_rate_limit_retries:
                    raise self._error(
                        f"Repeated rate limiting for {url} ({rate_limited + 1} consecutive 429s, last type: {kind})"
                    )
                rate_limited += 1
                continue
            if status in TRANSIENT_STATUSES and transient < self.max_transient_retries:
                telemetry.transient_retries += 1
                self._backoff(min(MAX_BACKOFF_S, TRANSIENT_BACKOFF_S * 2**transient))
                transient += 1
                continue
            request = getattr(response, "request", None)
            shown = request.url if request is not None else url
            raise self._error(f"Riot API returned {status} for {shown}: {response.text[:300]}")

    def challenger(self) -> dict[str, Any]:
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/challenger", method=CHALLENGER_METHOD
        )

    def grandmaster(self) -> dict[str, Any]:
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/grandmaster", method=GRANDMASTER_METHOD
        )

    def master(self) -> dict[str, Any]:
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/master", method=MASTER_METHOD
        )

    def league_entries(
        self, tier: str, division: str, *, page: int = 1, queue: str = RANKED_TFT_QUEUE
    ) -> list[dict[str, Any]]:
        """One page of a divisional tier's league entries.

        TFT-LEAGUE-V1 `GET /tft/league/v1/entries/{tier}/{division}` on the
        platform route: `tier` is e.g. `DIAMOND` or `PLATINUM`, `division`
        is `I`-`IV`, and the optional query parameters are `queue` (default
        `RANKED_TFT`) and `page` (default 1, starting at 1). Returns a list
        of LeagueEntryDTO (`puuid`, `tier`, `rank` = division,
        `leaguePoints`, ...). Riot does not document the page size or a
        last-page marker, so callers stop on an empty page or their own
        page cap. The apex tiers are not served here -- they have their own
        league-list endpoints (`challenger()` etc.).
        """
        if tier not in DIVISIONAL_TIERS:
            raise ValueError(f"tier must be one of {', '.join(DIVISIONAL_TIERS)}")
        if division not in LEAGUE_DIVISIONS:
            raise ValueError(f"division must be one of {', '.join(LEAGUE_DIVISIONS)}")
        if page < 1:
            raise ValueError("page starts at 1")
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/entries/{tier}/{division}",
            params={"queue": queue, "page": page},
            method=LEAGUE_ENTRIES_METHOD,
        )

    def match_ids(
        self,
        puuid: str,
        *,
        count: int = 20,
        start: int = 0,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[str]:
        """Recent match IDs for `puuid`, newest first.

        `start_time`/`end_time` optionally bound the history by match time.
        They are epoch **seconds**, as Riot documents for TFT-MATCH-V1
        `GET /tft/match/v1/matches/by-puuid/{puuid}/ids` (`startTime` /
        `endTime`: "Epoch timestamp in seconds"; `game_datetime` in match
        bodies is milliseconds, so callers convert). Omitted when `None`, so
        existing callers still get ordinary recent history.
        """
        params: dict[str, Any] = {"start": start, "count": count}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self._get(
            f"https://{self.region}.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids",
            params=params,
            method=MATCH_IDS_METHOD,
        )

    def match(self, match_id: str) -> dict[str, Any]:
        return self._get(
            f"https://{self.region}.api.riotgames.com/tft/match/v1/matches/{match_id}", method=MATCH_METHOD
        )
