"""Rate-limit-aware RiotClient: header parsing, proactive pacing, 429 and
transient-failure handling, budgets and telemetry. Fully mocked -- an
`httpx.MockTransport` stands in for Riot and a fake clock for time, so
nothing here touches the network or really sleeps."""

from __future__ import annotations

import json

import httpx
import pytest

from tftlab.riot import (
    MATCH_IDS_METHOD,
    MATCH_METHOD,
    MAX_RATE_LIMIT_RETRIES,
    MAX_TRANSIENT_RETRIES,
    RiotApiError,
    RiotBudgetExhausted,
    RiotClient,
    classify_riot_error,
)
from tftlab.riot_limits import (
    MalformedRateLimitHeader,
    RateLimiter,
    RateWindow,
    parse_rate_counts,
    parse_rate_limits,
    parse_retry_after,
)

from conftest import FakeClock

KEY = "RGAPI-super-secret-fake-key"
NA1 = "na1.api.riotgames.com"
AMERICAS = "americas.api.riotgames.com"


class FakeRiot:
    """Answers every request with `respond(request)` -> (status, headers,
    body) and records when (fake time) each request arrived."""

    def __init__(self, clock: FakeClock, respond=None) -> None:
        self.clock = clock
        self.respond = respond or (lambda request: (200, {}, []))
        self.calls: list[tuple[float, str]] = []
        self.tokens: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((self.clock(), request.url.path))
        self.tokens.add(request.headers.get("X-Riot-Token", ""))
        status, headers, body = self.respond(request)
        return httpx.Response(status, headers=headers, content=json.dumps(body).encode())


def _client(clock: FakeClock, riot: FakeRiot, **kwargs) -> RiotClient:
    return RiotClient(KEY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(riot.handler), **kwargs)


def _limits(app: str | None = None, method: str | None = None, **extra: str) -> dict[str, str]:
    headers = dict(extra)
    if app is not None:
        headers["X-App-Rate-Limit"] = app
        headers["X-App-Rate-Limit-Count"] = ",".join(f"1:{w.split(':')[1]}" for w in app.split(","))
    if method is not None:
        headers["X-Method-Rate-Limit"] = method
        headers["X-Method-Rate-Limit-Count"] = ",".join(f"1:{w.split(':')[1]}" for w in method.split(","))
    return headers


def _times(riot: FakeRiot, path_part: str = "") -> list[float]:
    return [t for t, path in riot.calls if path_part in path]


# ---------------------------------------------------------------- parsing


def test_parses_a_single_app_window() -> None:
    assert parse_rate_limits("100:120") == (RateWindow(seconds=120, limit=100),)


def test_parses_multiple_app_windows_independently() -> None:
    windows = parse_rate_limits("20:1,100:120")
    assert windows == (RateWindow(1, 20), RateWindow(120, 100))
    assert [str(w) for w in windows] == ["20:1", "100:120"]
    # Order in the header does not matter; each window stands alone.
    assert parse_rate_limits("100:120, 20:1") == windows


def test_parses_method_windows_from_responses() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="20:1,100:120", method="250:10"), []))
    with _client(clock, riot) as client:
        client.match_ids("p")
    telemetry = client.telemetry_snapshot()
    assert telemetry["method_limits"] == {f"method {AMERICAS} {MATCH_IDS_METHOD}": "250:10"}
    assert telemetry["app_limits"] == {f"app {AMERICAS}": "20:1,100:120"}


def test_parses_count_headers() -> None:
    assert parse_rate_counts("3:1,50:120") == {1: 3, 120: 50}
    assert parse_rate_counts("0:10") == {10: 0}


@pytest.mark.parametrize(
    "value", ["", "   ", "abc", "20", "20:", ":1", "20:1;100:120", "20:0", "0:1", "-1:1", "20:1,30:1", "1.5:1"]
)
def test_malformed_limit_headers_are_rejected(value: str) -> None:
    with pytest.raises(MalformedRateLimitHeader):
        parse_rate_limits(value, "X-App-Rate-Limit")


def test_malformed_headers_are_counted_and_the_client_stays_conservative() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, {"X-App-Rate-Limit": "lots", "X-Method-Rate-Limit": "20:1;x"}, []))
    with _client(clock, riot) as client:
        for _ in range(3):
            client.match("M")
    telemetry = client.telemetry_snapshot()
    assert telemetry["malformed_headers"] == {"X-App-Rate-Limit": 3, "X-Method-Rate-Limit": 3}
    assert telemetry["app_limits"] == {}
    # No usable limits ever arrived: the bootstrap pace (1 request/second) holds.
    times = _times(riot)
    assert [b - a for a, b in zip(times, times[1:])] == [1.0, 1.0]


def test_retry_after_parsing() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 2.5 ") == 2.5
    for bad in (None, "", "soon", "-3", "inf", "nan"):
        assert parse_retry_after(bad) is None


# ---------------------------------------------------------------- proactive pacing


def test_bootstrap_paces_conservatively_until_headers_arrive() -> None:
    """Before any limits are known the client sends one request per second;
    once a response advertises limits it paces to those instead."""
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="20:1,100:120", method="100:1"), []))
    with _client(clock, riot) as client:
        for _ in range(3):
            client.match("M")
    times = _times(riot)
    assert times[0] == 1000.0  # the first request goes out immediately
    assert times[1] == times[2] == 1000.0  # limits known after the first response


def test_default_budget_is_90_percent_of_the_advertised_window() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="10:10", method="100:10"), []))
    with _client(clock, riot) as client:
        assert client.limiter.utilization == 0.9
        for _ in range(10):
            client.match("M")
    times = _times(riot)
    assert times[:9] == [1000.0] * 9  # floor(10 * 0.9) = 9 per 10 s
    assert times[9] == 1010.0  # the 10th waits for the first to leave the window


def test_short_window_limits_bursts() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="20:1,100:120", method="1000:10"), []))
    with _client(clock, riot, safety_utilization=1.0) as client:
        for _ in range(25):
            client.match("M")
    times = _times(riot)
    assert times[:20] == [1000.0] * 20
    assert times[20:] == [1001.0] * 5


def test_long_window_limits_sustained_rate() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="20:1,100:120", method="1000:10"), []))
    with _client(clock, riot) as client:  # 90%: 18 per second, 90 per 120 s
        for _ in range(91):
            client.match("M")
    times = _times(riot)
    assert len([t for t in times if t < 1120.0]) == 90
    assert times[90] == 1120.0  # request 91 waits for the 120 s window, not the 1 s one
    assert client.telemetry.pacing_sleep_s > 100


def test_method_limit_tighter_than_app_limit_governs() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="500:10", method="5:10"), []))
    with _client(clock, riot, safety_utilization=1.0) as client:
        for _ in range(6):
            client.match("M")
    assert _times(riot) == [1000.0] * 5 + [1010.0]


def test_app_limit_tighter_than_method_limit_governs() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="5:10", method="500:10"), []))
    with _client(clock, riot, safety_utilization=1.0) as client:
        for _ in range(6):
            client.match("M")
    assert _times(riot) == [1000.0] * 5 + [1010.0]


def test_endpoint_method_budgets_are_independent() -> None:
    """getMatch exhausting its method limit does not slow
    getMatchIdsByPUUID (same host, generous app limit)."""
    clock = FakeClock()

    def respond(request):
        method = "3:10" if "/ids" not in request.url.path else "100:10"
        return 200, _limits(app="500:10", method=method), []

    riot = FakeRiot(clock, respond)
    with _client(clock, riot, safety_utilization=1.0) as client:
        for _ in range(3):
            client.match("M")
        client.match_ids("p")  # getMatch is now full; this is a different method
        assert clock() == 1000.0
        client.match("M")  # this one must wait for getMatch's window
    assert _times(riot, "/ids") == [1000.0]
    assert _times(riot)[-1] == 1010.0


def test_routing_hosts_are_independent_app_scopes() -> None:
    """na1 (league) and americas (match) are different regions' app limits."""
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="2:10", method="100:10"), {"entries": []}))
    with _client(clock, riot, safety_utilization=1.0) as client:
        client.challenger()
        client.challenger()  # na1 app budget now full
        client.match("M")  # americas: unaffected
        assert clock() == 1000.0
        client.challenger()  # na1 must wait
    assert _times(riot)[-1] == 1010.0
    snapshot = client.telemetry_snapshot()
    assert snapshot["by_host"] == {AMERICAS: 1, NA1: 3}


def test_operator_ceiling_caps_every_host_at_100_percent() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="500:10,30000:600", method="1000:10"), []))
    with _client(clock, riot, rate_ceilings=(RateWindow(10, 10),)) as client:
        for _ in range(11):
            client.match("M")
    assert _times(riot) == [1000.0] * 10 + [1010.0]


def test_server_counts_only_ever_raise_the_local_count() -> None:
    """If Riot has counted more requests than this client sent (another
    process, a restart), pacing catches up to Riot's count."""
    clock = FakeClock()
    riot = FakeRiot(
        clock,
        lambda r: (200, {"X-App-Rate-Limit": "10:10", "X-App-Rate-Limit-Count": "9:10",
                         "X-Method-Rate-Limit": "100:10", "X-Method-Rate-Limit-Count": "1:10"}, []),
    )
    with _client(clock, riot, safety_utilization=1.0) as client:
        client.match("M")
        client.match("M")  # Riot says 9 of 10 used: one more fits
        client.match("M")  # now full
    times = _times(riot)
    assert times[:2] == [1000.0, 1000.0] and times[2] == 1010.0
    assert client.telemetry_snapshot()["high_water"][f"app {AMERICAS} 10s"] == 0.9


# ---------------------------------------------------------------- 429 handling


def _sequence(*responses):
    queue = list(responses)

    def respond(request):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return respond


def test_429_honours_retry_after() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, _sequence(
        (429, {**_limits(app="100:10", method="100:10"), "Retry-After": "7", "X-Rate-Limit-Type": "method"}, {}),
        (200, _limits(app="100:10", method="100:10"), ["ok"]),
    ))
    with _client(clock, riot) as client:
        assert client.match("M") == ["ok"]
    assert _times(riot) == [1000.0, 1007.0]
    assert client.telemetry.rate_limit_sleep_s == 7.0


def test_application_429_pauses_the_whole_host() -> None:
    clock = FakeClock()
    first = True

    def respond(request):
        nonlocal first
        if first:
            first = False
            return 429, {**_limits(app="100:10", method="100:10"), "Retry-After": "5", "X-Rate-Limit-Type": "application"}, {}
        return 200, _limits(app="100:10", method="100:10"), []

    riot = FakeRiot(clock, respond)
    with _client(clock, riot, max_rate_limit_retries=0) as client:
        with pytest.raises(RiotApiError):
            client.match("M")
        client.match_ids("p")  # different method, same host: also paused
        client.challenger()  # different host: not paused
    assert _times(riot, "/ids") == [1005.0]
    assert client.telemetry_snapshot()["rate_limited_by_type"] == {"application": 1}


def test_method_429_pauses_only_that_method() -> None:
    clock = FakeClock()
    first = True

    def respond(request):
        nonlocal first
        if first:
            first = False
            return 429, {**_limits(app="100:10", method="100:10"), "Retry-After": "5", "X-Rate-Limit-Type": "method"}, {}
        return 200, _limits(app="100:10", method="100:10"), []

    riot = FakeRiot(clock, respond)
    with _client(clock, riot, max_rate_limit_retries=0) as client:
        with pytest.raises(RiotApiError):
            client.match("M")
        client.match_ids("p")  # other method: not paused
        assert clock() == 1000.0
        client.match("M")  # same method: waits out the pause
    assert _times(riot)[-1] == 1005.0
    assert client.telemetry_snapshot()["rate_limited_by_type"] == {"method": 1}


@pytest.mark.parametrize("limit_type, expected", [("service", "service"), (None, "unknown"), ("weird", "unknown")])
def test_service_or_unknown_429_pauses_that_api_service(limit_type, expected) -> None:
    """Riot: an underlying service may 429 without X-Rate-Limit-Type (and
    without the app/method headers). The client pauses every endpoint of
    that API service on that host -- not other services."""
    clock = FakeClock()
    first = True

    def respond(request):
        nonlocal first
        if first:
            first = False
            headers = {"Retry-After": "4"}
            if limit_type:
                headers["X-Rate-Limit-Type"] = limit_type
            return 429, headers, {}
        return 200, _limits(app="100:10", method="100:10"), {"entries": []}

    riot = FakeRiot(clock, respond)
    with _client(clock, riot, max_rate_limit_retries=0) as client:
        with pytest.raises(RiotApiError):
            client.match("M")
        client.challenger()  # tft-league-v1 on na1: unaffected
        assert clock() == 1000.0
        client.match_ids("p")  # tft-match-v1 on americas: paused with getMatch
    assert _times(riot, "/ids") == [1004.0]
    assert client.telemetry_snapshot()["rate_limited_by_type"] == {expected: 1}


def test_429_without_retry_after_backs_off_exponentially() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, _sequence(
        (429, {}, {}), (429, {}, {}), (200, _limits(app="100:10", method="100:10"), [])
    ))
    with _client(clock, riot) as client:
        client.match("M")
    assert _times(riot) == [1000.0, 1001.0, 1003.0]


def test_repeated_429_is_bounded_and_fails_clearly() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (429, {"Retry-After": "2", "X-Rate-Limit-Type": "application"}, {}))
    with _client(clock, riot) as client:
        with pytest.raises(RiotApiError) as exc_info:
            client.match("M")
    assert len(riot.calls) == MAX_RATE_LIMIT_RETRIES + 1
    assert classify_riot_error(str(exc_info.value)) == "rate_limited"
    assert "application" in str(exc_info.value)
    # Waited out every Retry-After; never hammered.
    assert [b - a for a, b in zip(_times(riot), _times(riot)[1:])] == [2.0] * MAX_RATE_LIMIT_RETRIES


def test_very_long_retry_after_fails_instead_of_sleeping_and_keeps_the_pause() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (429, {"Retry-After": "3600", "X-Rate-Limit-Type": "application"}, {}))
    with _client(clock, riot, deadline=clock() + 600) as client:
        with pytest.raises(RiotApiError, match="halt application requests for 3600s"):
            client.match("M")
        assert len(riot.calls) == 1 and clock() == 1000.0
        with pytest.raises(RiotBudgetExhausted):  # the host stays paused; the deadline comes first
            client.match("M")
    assert len(riot.calls) == 1


# ---------------------------------------------------------------- other failures


@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_client_errors_are_never_retried(status: int) -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (status, {}, {"status": {"message": "no"}}))
    with _client(clock, riot) as client:
        with pytest.raises(RiotApiError, match=f"Riot API returned {status}"):
            client.match("M")
    assert len(riot.calls) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_get_bounded_retries_with_backoff(status: int) -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (status, {}, {}))
    with _client(clock, riot) as client:
        with pytest.raises(RiotApiError, match=f"Riot API returned {status}"):
            client.match("M")
    assert len(riot.calls) == MAX_TRANSIENT_RETRIES + 1
    assert client.telemetry.transient_retries == MAX_TRANSIENT_RETRIES
    assert client.telemetry.backoff_sleep_s == 1.0 + 2.0


def test_server_error_then_success_recovers() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, _sequence((503, {}, {}), (200, _limits(app="100:10", method="100:10"), ["ok"])))
    with _client(clock, riot) as client:
        assert client.match("M") == ["ok"]


def test_network_errors_and_timeouts_get_bounded_retries() -> None:
    clock = FakeClock()
    attempts = []

    def handler(request):
        attempts.append(clock())
        raise httpx.ReadTimeout("timed out", request=request)

    with RiotClient(KEY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RiotApiError) as exc_info:
            client.match("M")
    assert len(attempts) == MAX_TRANSIENT_RETRIES + 1
    assert classify_riot_error(str(exc_info.value)) == "network_error"
    assert client.telemetry.network_retries == MAX_TRANSIENT_RETRIES
    assert [b - a for a, b in zip(attempts, attempts[1:])] == [1.0, 2.0]  # backoff, never a busy loop


def test_telemetry_mixes_http_and_network_outcomes() -> None:
    clock = FakeClock()
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, headers=_limits(app="100:10", method="100:10"), content=b"[]")

    with RiotClient(KEY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handler)) as client:
        client.match("M")
        assert client.telemetry_snapshot()["by_status"] == {"200": 1, "network_error": 1}


# ---------------------------------------------------------------- budgets


def test_request_budget_is_never_exceeded() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="100:10", method="100:10"), []))
    with _client(clock, riot, max_requests=3) as client:
        for _ in range(3):
            client.match("M")
        with pytest.raises(RiotBudgetExhausted):
            client.match("M")
    assert len(riot.calls) == 3


def test_deadline_stops_before_a_wait_that_would_cross_it() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, lambda r: (200, _limits(app="2:60", method="100:10"), []))
    with _client(clock, riot, safety_utilization=1.0, deadline=clock() + 30) as client:
        client.match("M")
        client.match("M")
        with pytest.raises(RiotBudgetExhausted):
            client.match("M")  # would have to wait 60 s; only 30 s allowed
    assert clock() == 1000.0 and len(riot.calls) == 2


# ---------------------------------------------------------------- secrets and telemetry


def test_no_secret_in_diagnostics_or_errors() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, _sequence(
        (429, {"Retry-After": "1", "X-App-Rate-Limit": KEY}, {}),  # a hostile echo, parsed as malformed
        (403, {}, {"message": f"forbidden for {KEY}"}),
    ))
    with _client(clock, riot) as client:
        with pytest.raises(RiotApiError) as exc_info:
            client.match("M")
        snapshot = json.dumps(client.telemetry_snapshot())
    assert riot.tokens == {KEY}  # sent to Riot, as a header only
    assert KEY not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)
    assert KEY not in snapshot
    assert KEY not in repr(client.limiter.telemetry)


def test_telemetry_counts_requests_outcomes_and_sleep() -> None:
    clock = FakeClock()
    riot = FakeRiot(clock, _sequence(
        (200, _limits(app="10:10", method="100:10"), {"entries": []}),
        (429, {**_limits(app="10:10", method="100:10"), "Retry-After": "3", "X-Rate-Limit-Type": "method"}, {}),
        (200, _limits(app="10:10", method="100:10"), []),
    ))
    with _client(clock, riot) as client:
        client.challenger()
        client.match("M")
        client.match_ids("p")
        snapshot = client.telemetry_snapshot()
    assert snapshot["requests"] == 4 and snapshot["successes"] == 3
    assert snapshot["by_method"] == {
        "tft-league-v1.getChallengerLeague": 1, MATCH_METHOD: 2, MATCH_IDS_METHOD: 1,
    }
    assert snapshot["by_status"] == {"200": 3, "429": 1}
    assert snapshot["rate_limited"] == 1 and snapshot["rate_limited_by_type"] == {"method": 1}
    assert snapshot["rate_limit_sleep_s"] == 3.0
    assert snapshot["elapsed_s"] == 3.0
    assert set(snapshot["high_water"]) >= {f"app {NA1} 10s", f"app {AMERICAS} 10s"}


def test_limiter_rejects_invalid_utilization() -> None:
    for bad in (0, -0.1, 1.5):
        with pytest.raises(ValueError):
            RateLimiter(utilization=bad)
