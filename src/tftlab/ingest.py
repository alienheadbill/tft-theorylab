from __future__ import annotations

from dataclasses import dataclass

from .riot import RiotClient
from .storage import Database


@dataclass(frozen=True)
class IngestResult:
    seed_players: int
    match_ids_seen: int
    matches_inserted: int


def ingest_ladder(
    client: RiotClient,
    db: Database,
    *,
    player_limit: int = 50,
    matches_per_player: int = 10,
    leagues: tuple[str, ...] = ("challenger",),
) -> IngestResult:
    """Seed from high-Elo TFT ladder PUUIDs and deduplicate overlapping matches."""
    puuids = client.ladder_puuids(leagues)[:player_limit]
    ids: list[str] = []
    seen: set[str] = set()
    for puuid in puuids:
        for match_id in client.match_ids(puuid, count=matches_per_player):
            if match_id not in seen:
                seen.add(match_id)
                ids.append(match_id)

    inserted = 0
    for match_id in ids:
        if db.has_match(match_id):
            continue
        inserted += int(db.ingest_match(client.match(match_id)))

    return IngestResult(
        seed_players=len(puuids),
        match_ids_seen=len(ids),
        matches_inserted=inserted,
    )
