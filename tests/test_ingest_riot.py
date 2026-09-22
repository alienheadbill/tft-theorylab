from pathlib import Path

import pytest

from tftlab.cli import CommunityDragonUnavailable, _resolve_cost_lookup
from tftlab.ingest import ingest_ladder
from tftlab.riot import RiotApiError, classify_riot_error
from tftlab.storage import Database

from _helpers import make_match, make_unit


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
        ("some totally different error", "unknown"),
    ],
)
def test_classify_riot_error(message: str, expected: str) -> None:
    assert classify_riot_error(message) == expected


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
