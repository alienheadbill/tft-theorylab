"""Current-trusted-window history filtering (Riot startTime/endTime)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tftlab import cli
from tftlab.ingest import ingest_ladder
from tftlab.riot import RiotClient
from tftlab.storage import Database
from tftlab.unreal_patch import (
    UNREAL_PATCH_REGISTRY,
    NoCurrentTrustedWindow,
    UnrealPatchWindow,
    current_trusted_window,
)

from _helpers import make_match, make_unit
from test_ingest_riot import _FakeResponse, _StubRiotClient

START_18_3_MS = 1_790_233_200_000  # 2026-09-24T07:00:00Z
NOW_MS = int(datetime(2026, 9, 25, 12, tzinfo=timezone.utc).timestamp() * 1000)


def _window(patch: str, start: int, end: int, *, verified: bool = True, source: str | None = "fixture") -> UnrealPatchWindow:
    return UnrealPatchWindow(client_patch=patch, starts_at=start, ends_at=end, verified=verified, source=source)


# ---------------------------------------------------------------- Riot client


def _captured_params(monkeypatch: pytest.MonkeyPatch, **kwargs) -> dict:
    captured: dict = {}

    def fake_get(self: httpx.Client, url: str, params: dict | None = None) -> _FakeResponse:
        captured["url"] = url
        captured["params"] = dict(params or {})
        return _FakeResponse([])

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with RiotClient("fake-key") as client:
        client.match_ids("p1", count=5, **kwargs)
    return captured


def test_match_ids_sends_start_and_end_time_in_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _captured_params(monkeypatch, start_time=1_790_233_200, end_time=1_791_244_800)
    assert captured["params"] == {"start": 0, "count": 5, "startTime": 1_790_233_200, "endTime": 1_791_244_800}
    assert captured["url"].endswith("/tft/match/v1/matches/by-puuid/p1/ids")


def test_match_ids_omits_time_bounds_when_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _captured_params(monkeypatch)
    assert captured["params"] == {"start": 0, "count": 5}


# ---------------------------------------------------------------- trusted window


def test_current_18_3_resolves_to_its_verified_start() -> None:
    window = current_trusted_window(NOW_MS)
    assert window.client_patch == "18.3"
    assert window.starts_at == START_18_3_MS
    assert datetime.fromtimestamp(window.starts_at / 1000, tz=timezone.utc).isoformat() == "2026-09-24T07:00:00+00:00"
    assert window.starts_at // 1000 == 1_790_233_200  # the startTime actually sent to Riot (seconds)
    assert window in UNREAL_PATCH_REGISTRY and window.is_usable


def test_latest_usable_window_wins_and_unusable_ones_are_ignored() -> None:
    registry = (
        _window("1.0", 100, 1_000),
        _window("1.1", 200, 1_000),
        _window("1.2", 300, 1_000, verified=False),  # unverified: never a lower bound
        _window("1.3", 400, 1_000, source=None),  # sourceless: never a lower bound
        _window("1.4", 500, 1_000, source=""),
    )
    assert current_trusted_window(600, registry=registry).client_patch == "1.1"


def test_a_window_that_has_not_started_is_not_current() -> None:
    registry = (_window("1.0", 100, 1_000), _window("1.1", 700, 2_000))
    assert current_trusted_window(600, registry=registry).client_patch == "1.0"


@pytest.mark.parametrize(
    "registry, now",
    [
        ((), 500),  # nothing registered
        ((_window("1.0", 100, 1_000, verified=False),), 500),  # nothing usable
        ((_window("1.0", 700, 1_000),), 500),  # nothing started yet
        ((_window("1.0", 100, 400),), 500),  # latest already ended: never open-ended
    ],
)
def test_no_current_window_fails_safely(registry, now) -> None:
    with pytest.raises(NoCurrentTrustedWindow):
        current_trusted_window(now, registry=registry)


def test_registry_and_classification_are_untouched() -> None:
    assert [(w.client_patch, w.starts_at, w.ends_at) for w in UNREAL_PATCH_REGISTRY] == [
        ("18.2", 1_789_084_800_000, 1_790_035_200_000),
        ("18.3", 1_790_233_200_000, 1_791_244_800_000),
    ]


# ---------------------------------------------------------------- ingest


def _m(match_id: str, when_ms: int = START_18_3_MS + 60_000) -> dict:
    return make_match(match_id, game_datetime=when_ms, units=[make_unit("TFT14_Foo", tier=2, items=[])])


def test_ingest_passes_bounds_and_counts_empty_histories(tmp_path: Path) -> None:
    client = _StubRiotClient(
        puuids=["p1", "p2", "p3"],
        match_ids_by_puuid={"p1": ["A", "B"], "p2": ["B"], "p3": []},  # p3 hasn't played this window
        matches={"A": _m("A", START_18_3_MS + 10_000), "B": _m("B", START_18_3_MS + 90_000)},
    )
    with Database(tmp_path / "w.sqlite3") as db:
        result = ingest_ladder(
            client, db, player_limit=3, matches_per_player=5,
            history_start_time=1_790_233_200, history_end_time=1_791_244_800,
        )
    assert client.history_bounds == [(1_790_233_200, 1_791_244_800)] * 3
    assert result.seeds_with_empty_history == 1
    assert result.failed_history_requests == 0  # an empty window is not a failure
    assert (result.match_id_references, result.match_ids_seen, result.matches_inserted) == (3, 2, 2)
    assert client.match_calls == ["A", "B"]  # dedupe still fetches each lobby once
    assert result.earliest_inserted_game_datetime == START_18_3_MS + 10_000
    assert result.latest_inserted_game_datetime == START_18_3_MS + 90_000
    assert (result.history_start_time, result.history_end_time) == (1_790_233_200, 1_791_244_800)


def test_ingest_without_bounds_requests_ordinary_history(tmp_path: Path) -> None:
    client = _StubRiotClient(puuids=["p1"], match_ids_by_puuid={"p1": ["A"]}, matches={"A": _m("A")})
    with Database(tmp_path / "plain.sqlite3") as db:
        result = ingest_ladder(client, db, player_limit=1, matches_per_player=5)
    assert client.history_bounds == [(None, None)]
    assert result.history_start_time is None and result.seeds_with_empty_history == 0


def test_bounded_ingest_keeps_stored_dedupe_ranked_filter_and_stratification(tmp_path: Path) -> None:
    tiers = {
        t: [{"puuid": f"{t[0]}{i}", "leaguePoints": 2000 - i} for i in range(40)]
        for t in ("challenger", "grandmaster", "master")
    }
    normal = make_match("NORMAL", queue_id=1090, game_datetime=START_18_3_MS + 5, units=[make_unit("TFT14_Foo")])
    from tftlab.sampling import select_seeds

    seeds = select_seeds(lambda t: {"entries": tiers[t]}, total=50, mode="high_elo").puuids
    c, g, m = (next(p for p in seeds if p[0] == t) for t in "cgm")
    client = _StubRiotClient(
        tiers=tiers,
        match_ids_by_puuid={c: ["OLD", "NEW"], g: ["NORMAL"], m: ["NEW"]},
        matches={"OLD": _m("OLD"), "NEW": _m("NEW"), "NORMAL": normal},
    )
    with Database(tmp_path / "strat.sqlite3") as db:
        db.ingest_match(_m("OLD"))
        result = ingest_ladder(
            client, db, player_limit=50, matches_per_player=5, sampling_mode="high_elo",
            history_start_time=1_790_233_200, history_end_time=1_791_244_800,
        )
    assert result.seeds_by_tier == {"challenger": 20, "grandmaster": 15, "master": 15}
    assert "OLD" not in client.match_calls  # already stored: not re-fetched
    assert result.duplicates_skipped == 1 and result.matches_inserted == 1 and result.non_target_matches_skipped == 1
    assert result.seeds_with_empty_history == 47


# ---------------------------------------------------------------- CLI


def _run_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client, args: list[str]):
    class _Ctx:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "RiotClient", _Ctx)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", lambda **_: (None, False, 18))
    monkeypatch.setenv("RIOT_API_KEY", "fake-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.chdir(tmp_path)
    return CliRunner().invoke(cli.app, ["ingest-riot", *args])


def _client() -> _StubRiotClient:
    return _StubRiotClient(puuids=["p1", "p2"], match_ids_by_puuid={"p1": ["A"]}, matches={"A": _m("A")})


def test_cli_current_trusted_window_bounds_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "current_trusted_window_for", lambda now_ms: current_trusted_window(NOW_MS))
    client = _client()
    result = _run_cli(monkeypatch, tmp_path, client, ["--players", "2", "--current-trusted-window"])
    assert result.exit_code == 0, result.output
    assert client.history_bounds == [(1_790_233_200, 1_791_244_800)] * 2
    for line in (
        "Trusted window: 18.3",
        "History lower bound (startTime): 1790233200 (2026-09-24T07:00:00+00:00)",
        "History upper bound (endTime): 1791244800 (2026-10-06T00:00:00+00:00)",
        "Seeds with no matches in the requested history: 1",
        "Earliest inserted match:",
    ):
        assert line in result.output


def test_cli_current_trusted_window_fails_safely_without_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def none(now_ms):
        raise NoCurrentTrustedWindow("nothing registered")

    monkeypatch.setattr(cli, "current_trusted_window_for", none)
    client = _client()
    result = _run_cli(monkeypatch, tmp_path, client, ["--current-trusted-window"])
    assert result.exit_code == 1
    assert client.ladder_calls == [] and client.history_calls == []  # no Riot request at all


def test_cli_start_time_is_converted_to_epoch_seconds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _client()
    result = _run_cli(monkeypatch, tmp_path, client, ["--players", "1", "--start-time", "2026-09-24T07:00:00Z"])
    assert result.exit_code == 0, result.output
    assert client.history_bounds == [(1_790_233_200, None)]


@pytest.mark.parametrize(
    "args",
    [
        ["--start-time", "2026-09-24T07:00:00"],  # no timezone
        ["--start-time", "yesterday"],
        ["--start-time", "2026-09-24T07:00:00Z", "--current-trusted-window"],
    ],
)
def test_cli_rejects_bad_time_options_before_any_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args) -> None:
    client = _client()
    result = _run_cli(monkeypatch, tmp_path, client, args)
    assert result.exit_code != 0
    assert client.ladder_calls == []


def test_cli_default_still_requests_ordinary_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _client()
    result = _run_cli(monkeypatch, tmp_path, client, ["--players", "1"])
    assert result.exit_code == 0, result.output
    assert client.history_bounds == [(None, None)]
    assert "Trusted window: none (ordinary recent history)" in result.output
