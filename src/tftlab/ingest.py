from __future__ import annotations

from dataclasses import dataclass

from .normalize import CostLookup
from .riot import RiotApiError, RiotClient
from .storage import Database


@dataclass(frozen=True)
class IngestResult:
    seed_players: int
    match_ids_seen: int
    #: Match bodies successfully fetched from Riot (whether or not they were
    #: already stored -- see `duplicates_skipped` for that).
    matches_fetched: int
    matches_inserted: int
    #: Already in the store, so no network fetch was even attempted for them.
    duplicates_skipped: int
    #: Fetch was attempted and failed (network error, Riot API error, etc.);
    #: these do not abort the rest of the run.
    failed_requests: int


def ingest_ladder(
    client: RiotClient,
    db: Database,
    *,
    player_limit: int = 50,
    matches_per_player: int = 10,
    leagues: tuple[str, ...] = ("challenger",),
    cost_lookup: CostLookup | None = None,
) -> IngestResult:
    """Seed from high-Elo TFT ladder PUUIDs and deduplicate overlapping matches.

    `cost_lookup` should resolve authoritative shop costs (e.g. from
    CommunityDragon static metadata); when omitted, ingested units fall back
    to the Match-V1 `rarity + 1` heuristic.

    A single match's fetch failing (rate limit exhaustion, a Riot API error,
    or a network failure -- `RiotClient._get` wraps `httpx.RequestError`
    into `RiotApiError`, so all three land here the same way) is recorded in
    `failed_requests` and skipped rather than aborting the whole run -- a
    batch of otherwise-good matches shouldn't be lost to one bad request.
    """
    puuids = client.ladder_puuids(leagues)[:player_limit]
    ids: list[str] = []
    seen: set[str] = set()
    for puuid in puuids:
        for match_id in client.match_ids(puuid, count=matches_per_player):
            if match_id not in seen:
                seen.add(match_id)
                ids.append(match_id)

    fetched = 0
    inserted = 0
    duplicates = 0
    failed = 0
    for match_id in ids:
        if db.has_match(match_id):
            duplicates += 1
            continue
        try:
            payload = client.match(match_id)
        except RiotApiError:
            failed += 1
            continue
        fetched += 1
        inserted += int(db.ingest_match(payload, cost_lookup=cost_lookup))

    return IngestResult(
        seed_players=len(puuids),
        match_ids_seen=len(ids),
        matches_fetched=fetched,
        matches_inserted=inserted,
        duplicates_skipped=duplicates,
        failed_requests=failed,
    )
