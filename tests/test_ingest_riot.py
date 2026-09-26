from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tftlab.cli import CommunityDragonUnavailable, _resolve_cost_lookup, app
from tftlab.ingest import ingest_ladder
from tftlab.riot import RANKED_TFT_QUEUE_ID, RiotApiError, RiotClient, classify_riot_error
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

    def __init__(
        self,
        *,
        puuids=(),
        match_ids_by_puuid,
        matches,
        failing_match_ids=frozenset(),
        tiers=None,
        failing_histories=frozenset(),
    ):
        # `puuids` is shorthand for a Challenger-only ladder, highest LP first.
        self._tiers = tiers if tiers is not None else {
            "challenger": [{"puuid": p, "leaguePoints": 1000 - i} for i, p in enumerate(puuids)]
        }
        self._match_ids_by_puuid = match_ids_by_puuid
        self._matches = matches
        self._failing = failing_match_ids
        self._failing_histories = failing_histories
        self.ladder_calls: list[str] = []
        self.history_calls: list[str] = []
        self.history_bounds: list[tuple] = []
        self.match_calls: list[str] = []

    def _ladder(self, tier):
        self.ladder_calls.append(tier)
        return {"entries": list(self._tiers.get(tier, []))}

    def challenger(self):
        return self._ladder("challenger")

    def grandmaster(self):
        return self._ladder("grandmaster")

    def master(self):
        return self._ladder("master")

    def match_ids(self, puuid, *, count=20, start=0, start_time=None, end_time=None):
        self.history_calls.append(puuid)
        self.history_bounds.append((start_time, end_time))
        if puuid in self._failing_histories:
            raise RiotApiError(f"Riot API returned 503 for fake/history/{puuid}: busy")
        return list(self._match_ids_by_puuid.get(puuid, []))[:count]

    def match(self, match_id):
        self.match_calls.append(match_id)
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


def test_ingest_ladder_skips_non_ranked_queue_matches(tmp_path: Path) -> None:
    """A Challenger PUUID's recent match history isn't exclusively standard
    ranked TFT -- Normal/Hyper Roll/Double Up games show up too. A fetched
    match from one of those queues must be skipped (never inserted) and
    counted in `non_target_matches_skipped`, not `failed_requests` -- Riot
    answered fine, the match is just out of this project's scope."""
    ranked = make_match("RANKED_1", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    assert ranked["info"]["queue_id"] == RANKED_TFT_QUEUE_ID
    normal = make_match("NORMAL_1", queue_id=1090, units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _StubRiotClient(
        puuids=["p1"],
        match_ids_by_puuid={"p1": ["RANKED_1", "NORMAL_1"]},
        matches={"RANKED_1": ranked, "NORMAL_1": normal},
    )

    with Database(tmp_path / "queue_filter.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=10, matches_per_player=10)
        stored = db.has_match("RANKED_1"), db.has_match("NORMAL_1")

    assert result.match_ids_seen == 2
    assert result.matches_fetched == 2
    assert result.matches_inserted == 1
    assert result.failed_requests == 0
    assert result.non_target_matches_skipped == 1
    assert stored == (True, False)


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


# ---------------------------------------------------------------- stratified seeds


def _tier(prefix: str, n: int, top_lp: int) -> list[dict]:
    return [{"puuid": f"{prefix}{i}", "leaguePoints": top_lp - i} for i in range(n)]


def _stratified_client(**kwargs) -> _StubRiotClient:
    tiers = {"challenger": _tier("c", 30, 2000), "grandmaster": _tier("g", 30, 900), "master": _tier("m", 30, 400)}
    return _StubRiotClient(tiers=tiers, **kwargs)


def test_shared_lobby_across_stratified_seeds_is_fetched_once(tmp_path: Path) -> None:
    """A lobby with a Challenger, a Grandmaster and a Master seed in it shows
    up in all three histories; it must be fetched and stored exactly once."""
    probe = _stratified_client(match_ids_by_puuid={}, matches={})
    from tftlab.sampling import select_seeds

    seeds = select_seeds(lambda t: getattr(probe, t)(), total=10, mode="high_elo").puuids
    shared = make_match("LOBBY", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    own = {p: make_match(f"OWN_{p}", units=[make_unit("TFT14_Foo", tier=2, items=[])]) for p in seeds}
    client = _stratified_client(
        match_ids_by_puuid={p: ["LOBBY", f"OWN_{p}"] for p in seeds},
        matches={"LOBBY": shared, **{f"OWN_{p}": m for p, m in own.items()}},
    )

    with Database(tmp_path / "strat.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=10, matches_per_player=5, sampling_mode="high_elo")
        stored = db.query_one("SELECT COUNT(*) FROM matches")[0]

    assert client.ladder_calls == ["challenger", "grandmaster", "master"]
    assert result.seeds_by_tier == {"challenger": 4, "grandmaster": 3, "master": 3}
    assert client.match_calls.count("LOBBY") == 1
    assert result.match_id_references == 20  # 10 seeds x 2 references
    assert result.match_ids_seen == 11  # LOBBY once + 10 own games
    assert result.matches_fetched == result.matches_inserted == stored == 11
    assert result.in_run_overlap_rate == pytest.approx(9 / 20)


def test_already_stored_ids_are_not_fetched_again_with_stratified_seeds(tmp_path: Path) -> None:
    old = make_match("OLD", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    new = make_match("NEW", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _stratified_client(
        match_ids_by_puuid={"c0": ["OLD", "NEW"], "g0": ["OLD"], "m0": ["NEW"]},
        matches={"OLD": old, "NEW": new},
    )
    with Database(tmp_path / "known.sqlite3") as db:
        db.ingest_match(old)
        result = ingest_ladder(client, db, player_limit=90, matches_per_player=5, sampling_mode="high_elo")

    assert "OLD" not in client.match_calls
    assert client.match_calls == ["NEW"]
    assert result.duplicates_skipped == 1 and result.matches_inserted == 1
    assert result.known_duplicate_rate == pytest.approx(0.5)


def test_non_ranked_queues_stay_excluded_with_stratified_seeds(tmp_path: Path) -> None:
    ranked = make_match("R", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    hyper = make_match("H", queue_id=1130, units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _stratified_client(match_ids_by_puuid={"c0": ["R"], "m0": ["H"]}, matches={"R": ranked, "H": hyper})
    with Database(tmp_path / "queues.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=90, matches_per_player=5, sampling_mode="high_elo")
        assert (db.has_match("R"), db.has_match("H")) == (True, False)
    assert result.non_target_matches_skipped == 1 and result.matches_inserted == 1


def test_one_failed_history_skips_that_seed_only(tmp_path: Path) -> None:
    a = make_match("A", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _StubRiotClient(
        puuids=["p1", "p2"],
        match_ids_by_puuid={"p1": ["A"], "p2": ["B"]},
        matches={"A": a},
        failing_histories={"p2"},
    )
    with Database(tmp_path / "history.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=2, matches_per_player=5)
    assert result.failed_history_requests == 1
    assert result.matches_inserted == 1
    assert result.match_id_references == 1


def test_report_fields_are_raw_counts_with_derived_rates(tmp_path: Path) -> None:
    ms = {m: make_match(m, units=[make_unit("TFT14_Foo", tier=2, items=[])]) for m in ("A", "B", "C")}
    client = _StubRiotClient(puuids=["p1", "p2"], match_ids_by_puuid={"p1": ["A", "B"], "p2": ["B", "C"]}, matches=ms)
    with Database(tmp_path / "report.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=5, matches_per_player=2)
    assert (result.requested_seeds, result.seed_players, result.histories_per_seed) == (5, 2, 2)
    assert result.seeds_by_tier == {"challenger": 2} and result.ladder_sizes == {"challenger": 2}
    assert (result.match_id_references, result.match_ids_seen, result.matches_inserted) == (4, 3, 3)
    assert result.in_run_overlap_rate == pytest.approx(0.25)
    assert result.inserted_per_seed == pytest.approx(1.5)
    assert result.new_match_yield == pytest.approx(0.75)


def _run_ingest_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client, args: list[str]):
    import tftlab.cli as cli

    class _Ctx:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "RiotClient", _Ctx)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", lambda **_: (None, False, 18))
    monkeypatch.setenv("RIOT_API_KEY", "super-secret-fake-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.chdir(tmp_path)  # no stray .env
    return CliRunner().invoke(app, ["ingest-riot", *args])


def test_cli_reports_seed_tiers_and_sampling_metrics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _stratified_client(
        match_ids_by_puuid={"c0": ["X"]}, matches={"X": make_match("X", units=[make_unit("TFT14_Foo", tier=2, items=[])])}
    )
    result = _run_ingest_cli(monkeypatch, tmp_path, client, ["--players", "50", "--matches-per-player", "5", "--sampling", "high_elo"])
    assert result.exit_code == 0, result.output
    for line in (
        "Sampling mode: high_elo",
        "Requested seed players: 50",
        "challenger: 20 selected (requested 20)",
        "grandmaster: 15 selected (requested 15)",
        "master: 15 selected (requested 15)",
        "available ladder entries: 30",
        "Requested histories per seed: 5",
        "Match-ID references (before dedupe): 1",
        "Unique match IDs discovered: 1",
        "Matches inserted: 1",
        "In-run overlap rate: 0.0%",
    ):
        assert line in result.output
    assert "super-secret-fake-key" not in result.output


def test_cli_include_master_still_means_high_elo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _stratified_client(match_ids_by_puuid={}, matches={})
    result = _run_ingest_cli(monkeypatch, tmp_path, client, ["--players", "10", "--include-master"])
    assert result.exit_code == 0, result.output
    assert client.ladder_calls == ["challenger", "grandmaster", "master"]


def test_cli_rejects_unknown_sampling_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _stratified_client(match_ids_by_puuid={}, matches={})
    result = _run_ingest_cli(monkeypatch, tmp_path, client, ["--sampling", "everyone"])
    assert result.exit_code != 0
    assert client.ladder_calls == []


def test_cli_fails_when_every_history_request_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _StubRiotClient(puuids=["p1", "p2"], match_ids_by_puuid={}, matches={}, failing_histories={"p1", "p2"})
    result = _run_ingest_cli(monkeypatch, tmp_path, client, ["--players", "2"])
    assert result.exit_code == 1
    assert "Failed history requests: 2" in result.output
