from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import CostLookup
from .riot import RANKED_TFT_QUEUE_ID, RiotApiError, RiotClient
from .sampling import select_seeds
from .storage import Database


@dataclass(frozen=True)
class IngestResult:
    seed_players: int
    #: Unique match IDs across all seed histories (after in-run dedupe).
    match_ids_seen: int
    #: Match bodies successfully fetched from Riot (whether or not they were
    #: already stored -- see `duplicates_skipped` for that -- or from a
    #: non-ranked queue -- see `non_target_matches_skipped`).
    matches_fetched: int
    matches_inserted: int
    #: Already in the store, so no network fetch was even attempted for them.
    duplicates_skipped: int
    #: Fetch was attempted and failed (network error, Riot API error, etc.);
    #: these do not abort the rest of the run.
    failed_requests: int
    #: Fetched successfully but not `RANKED_TFT_QUEUE_ID` (a Challenger PUUID
    #: can play Normal/Hyper Roll/Double Up too), so never inserted. Not a
    #: failure -- Riot answered fine, the match just isn't in this project's
    #: target dataset.
    non_target_matches_skipped: int
    requested_seeds: int = 0
    #: Seeds taken from each ladder tier (see `tftlab.sampling`).
    seeds_by_tier: dict[str, int] = field(default_factory=dict)
    #: Distinct players each fetched tier had available.
    ladder_sizes: dict[str, int] = field(default_factory=dict)
    sampling_mode: str = "challenger"
    histories_per_seed: int = 0
    #: Match-ID references returned across all seed histories, before dedupe.
    match_id_references: int = 0
    #: Seed history requests that failed; that seed is skipped, the run goes on.
    failed_history_requests: int = 0

    @property
    def in_run_overlap_rate(self) -> float | None:
        """Share of history references that repeated a match another seed
        (or the same seed) already returned in this run."""
        if not self.match_id_references:
            return None
        return 1 - self.match_ids_seen / self.match_id_references

    @property
    def known_duplicate_rate(self) -> float | None:
        """Share of this run's unique match IDs that were already stored."""
        return self.duplicates_skipped / self.match_ids_seen if self.match_ids_seen else None

    @property
    def inserted_per_seed(self) -> float | None:
        return self.matches_inserted / self.seed_players if self.seed_players else None

    @property
    def new_match_yield(self) -> float | None:
        """Matches inserted per match-ID reference requested."""
        return self.matches_inserted / self.match_id_references if self.match_id_references else None


def ingest_ladder(
    client: RiotClient,
    db: Database,
    *,
    player_limit: int = 50,
    matches_per_player: int = 10,
    sampling_mode: str = "challenger",
    cost_lookup: CostLookup | None = None,
) -> IngestResult:
    """Seed from high-Elo TFT ladder PUUIDs and deduplicate overlapping matches.

    Seeds are chosen by `tftlab.sampling.select_seeds` (deterministic and
    stratified across the tiers of `sampling_mode`; never by champion or
    comp). One lobby usually shows up in several seeds' histories, so match
    IDs are deduplicated across histories before any body is fetched, and an
    ID already stored is skipped without fetching its body again.

    `cost_lookup` should resolve authoritative shop costs (e.g. from
    CommunityDragon static metadata); when omitted, ingested units fall back
    to the Match-V1 `rarity + 1` heuristic.

    A single request failing (rate limit exhaustion, a Riot API error, or a
    network failure -- `RiotClient._get` wraps `httpx.RequestError` into
    `RiotApiError`, so all three land here the same way) is counted and
    skipped rather than aborting the whole run: a failed match fetch in
    `failed_requests`, a failed seed history in `failed_history_requests`.
    Ladder (league) requests are not tolerated this way: without them there
    are no seeds.

    A Challenger/Grandmaster/Master PUUID's recent match history isn't
    exclusively standard ranked TFT -- it can include Normal, Hyper Roll, or
    Double Up games too. Every fetched match is checked against
    `RANKED_TFT_QUEUE_ID` before insertion; a non-target-queue match is
    counted in `non_target_matches_skipped` (not `failed_requests` -- Riot
    answered fine, it's just out of scope for this project's dataset) and
    never stored.
    """
    selection = select_seeds(lambda tier: getattr(client, tier)(), total=player_limit, mode=sampling_mode)
    ids: list[str] = []
    seen: set[str] = set()
    references = 0
    failed_histories = 0
    for puuid in selection.puuids:
        try:
            history = client.match_ids(puuid, count=matches_per_player)
        except RiotApiError:
            failed_histories += 1
            continue
        references += len(history)
        for match_id in history:
            if match_id not in seen:
                seen.add(match_id)
                ids.append(match_id)

    fetched = 0
    inserted = 0
    duplicates = 0
    failed = 0
    non_target = 0
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
        if payload.get("info", {}).get("queue_id") != RANKED_TFT_QUEUE_ID:
            non_target += 1
            continue
        inserted += int(db.ingest_match(payload, cost_lookup=cost_lookup))

    return IngestResult(
        seed_players=len(selection.puuids),
        match_ids_seen=len(ids),
        matches_fetched=fetched,
        matches_inserted=inserted,
        duplicates_skipped=duplicates,
        failed_requests=failed,
        non_target_matches_skipped=non_target,
        requested_seeds=player_limit,
        seeds_by_tier=dict(selection.by_tier),
        ladder_sizes=dict(selection.ladder_sizes),
        sampling_mode=sampling_mode,
        histories_per_seed=matches_per_player,
        match_id_references=references,
        failed_history_requests=failed_histories,
    )
