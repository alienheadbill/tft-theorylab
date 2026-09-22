from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tftlab.cli import CommunityDragonUnavailable, _resolve_cost_lookup, app
from tftlab.ingest import ingest_ladder
from tftlab.riot import RiotApiError, RiotClient, classify_riot_error
from tftlab.storage import Database

from _helpers import make_match, make_unit


class _FakeResponse:
    """Just enough of an `httpx.Response` for `RiotClient._get`'s success
    path: a 2xx status and a `.json()` body."""

    def __init__(self, json_data: object) -> None:
        self._json_data = json_data
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.text = ""

    def json(self) -> object:
        return self._json_data


class _StubRiotClient:
    """Duck-types the subset of RiotClient's interface `ingest_ladder` uses,
    so ingest logic can be tested without real network access or a key."""

    def __init__(self, *, puuids, match_ids_by_puuid, matches, failing_match_ids=frozenset()):
        self._puuids = puuids
        self._match_ids_by_puuid = match_ids_by_puuid
        self._matches = matches
        self._failing = failing_match_ids

    def ladder_puuids(self, leagues):
        return list(self._puuids)

    def match_ids(self, puuid, *, count=20, start=0):
        return list(self._match_ids_by_puuid.get(puuid, []))[:count]

    def match(self, match_id):
        if match_id in self._failing:
            raise RiotApiError(f"Riot API returned 500 for fake/{match_id}: boom")
        return self._matches[match_id]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Riot API returned 401 for https://x: unauthorized", "unauthorized"),
        ("Riot API returned 403 for https://x: forbidden", "forbidden"),
        ("Riot API returned 429 for https://x: too many requests", "rate_limited"),
        ("Repeated rate limiting for https://x", "rate_limited"),
        ("Riot API returned 500 for https://x: server error", "http_500"),
        ("Network error contacting Riot API (ConnectError) for https://x", "network_error"),
        ("some totally different error", "unknown"),
    ],
)
def test_classify_riot_error(message: str, expected: str) -> None:
    assert classify_riot_error(message) == expected


def test_get_wraps_network_failure_into_riot_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`httpx.RequestError` (timeout, DNS failure, connection refused, ...)
    must never escape `_get` uncaught -- it needs to become a `RiotApiError`
    so `verify-riot` and `ingest_ladder` can handle it like any other Riot
    failure. The wrapped message must never leak the API key (it's only
    ever sent as a header, never part of the URL)."""

    def fake_get(self: httpx.Client, url: str, params: dict | None = None) -> _FakeResponse:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    with RiotClient("super-secret-fake-key") as client:
        with pytest.raises(RiotApiError) as exc_info:
            client.challenger()

    assert "super-secret-fake-key" not in str(exc_info.value)
    assert classify_riot_error(str(exc_info.value)) == "network_error"


def test_verify_riot_reports_network_error_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(self: httpx.Client, url: str, params: dict | None = None) -> _FakeResponse:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setenv("RIOT_API_KEY", "super-secret-fake-key")

    result = CliRunner().invoke(app, ["verify-riot"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Network error" in result.output
    assert "super-secret-fake-key" not in result.output


def test_resolve_cost_lookup_uses_metadata_when_available() -> None:
    sentinel_lookup = object()

    class _Meta:
        set_number = 14
        cost_for_champion = sentinel_lookup

    cost_lookup, degraded, set_number = _resolve_cost_lookup(
        use_static_costs=True, allow_degraded_costs=False, fetch_metadata=lambda: _Meta()
    )
    assert cost_lookup is sentinel_lookup
    assert degraded is False
    assert set_number == 14


def test_resolve_cost_lookup_aborts_by_default_when_communitydragon_fails() -> None:
    def _boom():
        raise RuntimeError("network down")

    with pytest.raises(CommunityDragonUnavailable):
        _resolve_cost_lookup(use_static_costs=True, allow_degraded_costs=False, fetch_metadata=_boom)


def test_resolve_cost_lookup_degrades_when_explicitly_allowed() -> None:
    def _boom():
        raise RuntimeError("network down")

    cost_lookup, degraded, set_number = _resolve_cost_lookup(
        use_static_costs=True, allow_degraded_costs=True, fetch_metadata=_boom
    )
    assert cost_lookup is None
    assert degraded is True
    assert set_number is None


def test_resolve_cost_lookup_skips_metadata_entirely_when_disabled() -> None:
    def _should_not_be_called():
        raise AssertionError("fetch_metadata should not be called when use_static_costs=False")

    cost_lookup, degraded, set_number = _resolve_cost_lookup(
        use_static_costs=False, allow_degraded_costs=False, fetch_metadata=_should_not_be_called
    )
    assert cost_lookup is None
    assert degraded is False
    assert set_number is None


def test_ingest_ladder_deduplicates_match_ids_across_seed_players(tmp_path: Path) -> None:
    """The same match can be returned for multiple seed players (they were
    in the same lobby); it must only be fetched/inserted once."""
    payload = make_match("SHARED_MATCH", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _StubRiotClient(
        puuids=["p1", "p2"],
        match_ids_by_puuid={"p1": ["SHARED_MATCH"], "p2": ["SHARED_MATCH"]},
        matches={"SHARED_MATCH": payload},
    )

    with Database(tmp_path / "dedupe.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=10, matches_per_player=10)

    assert result.match_ids_seen == 1  # deduplicated before any fetch
    assert result.matches_fetched == 1
    assert result.matches_inserted == 1
    assert result.duplicates_skipped == 0
    assert result.failed_requests == 0


def test_ingest_ladder_is_idempotent_across_runs(tmp_path: Path) -> None:
    """Running ingestion twice against matches already in the store must
    skip them as duplicates, not re-fetch or double-insert."""
    payload = make_match("ALREADY_STORED", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _StubRiotClient(
        puuids=["p1"],
        match_ids_by_puuid={"p1": ["ALREADY_STORED"]},
        matches={"ALREADY_STORED": payload},
    )

    with Database(tmp_path / "idempotent.sqlite3") as db:
        first = ingest_ladder(client, db, player_limit=10, matches_per_player=10)
        second = ingest_ladder(client, db, player_limit=10, matches_per_player=10)

    assert first.matches_inserted == 1
    assert first.duplicates_skipped == 0

    assert second.matches_inserted == 0
    assert second.duplicates_skipped == 1
    assert second.matches_fetched == 0  # skipped before any network fetch


def test_ingest_ladder_counts_failed_requests_without_aborting_the_batch(tmp_path: Path) -> None:
    good = make_match("GOOD", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _StubRiotClient(
        puuids=["p1"],
        match_ids_by_puuid={"p1": ["GOOD", "BAD"]},
        matches={"GOOD": good},
        failing_match_ids={"BAD"},
    )

    with Database(tmp_path / "partial_failure.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=10, matches_per_player=10)

    assert result.match_ids_seen == 2
    assert result.matches_fetched == 1
    assert result.matches_inserted == 1
    assert result.failed_requests == 1


def test_ingest_ladder_counts_network_failure_without_aborting_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end version of the partial-failure test above, using a real
    `RiotClient` (not the duck-typed stub) so it actually exercises
    `RiotClient._get`'s `httpx.RequestError` -> `RiotApiError` wrapping."""
    good_payload = make_match("GOOD", units=[make_unit("TFT14_Foo", tier=2, items=[])])

    def fake_get(self: httpx.Client, url: str, params: dict | None = None) -> _FakeResponse:
        if "league/v1/challenger" in url:
            return _FakeResponse({"entries": [{"puuid": "p1"}]})
        if "by-puuid" in url:
            return _FakeResponse(["GOOD", "BAD_NETWORK"])
        if url.endswith("/BAD_NETWORK"):
            raise httpx.ConnectError("Connection refused")
        if url.endswith("/GOOD"):
            return _FakeResponse(good_payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    with Database(tmp_path / "network_failure.sqlite3") as db:
        with RiotClient("fake-key") as client:
            result = ingest_ladder(client, db, player_limit=10, matches_per_player=10)

    assert result.match_ids_seen == 2
    assert result.matches_fetched == 1
    assert result.matches_inserted == 1
    assert result.failed_requests == 1
