from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .normalize import CostLookup
from .riot import RANKED_TFT_QUEUE_ID, RiotApiError, RiotClient
from .sampling import COHORTS, DIVISION_TIERS, DIVISIONS, LADDER_TIERS, select_cohort_seeds, select_seeds
from .storage import Database

#: Default and maximum pages read per Diamond/Platinum division. Riot
#: documents neither the page size nor a last-page marker, so paging stops
#: at the first empty page or at this cap, whichever comes first.
DEFAULT_MAX_LADDER_PAGES = 3
MAX_LADDER_PAGES = 10


@dataclass(frozen=True)
class CohortIngestReport:
    """One seed cohort's share of a run. Cohort = sampling provenance (how a
    lobby was discovered), not the rank of the match or its players."""

    cohort: str
    requested: int
    selected: int
    #: Distinct players available in the cohort (after cross-cohort dedupe).
    available: int
    never_sampled_selected: int = 0
    previously_sampled_selected: int = 0
    seeds_with_empty_history: int = 0
    failed_history_requests: int = 0
    #: Match-ID references this cohort's seeds returned, before dedupe.
    match_id_references: int = 0
    #: Distinct match IDs this cohort's seeds returned. A lobby found by two
    #: cohorts counts once in each, so these can sum to more than the run's
    #: unique total; it is still fetched and stored once.
    unique_match_ids: int = 0
    #: League requests made to build this cohort's population.
    ladder_requests: int = 0


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
    #: History bounds sent to Riot (epoch seconds), or None for ordinary
    #: recent history. Riot filtering by time narrows what is requested;
    #: each match's own classification still decides its patch/window.
    history_start_time: int | None = None
    history_end_time: int | None = None
    #: Seeds whose (successful) history request returned no match IDs --
    #: with a time bound, players who haven't played in that window.
    seeds_with_empty_history: int = 0
    #: `game_datetime` (epoch ms) range of the matches actually inserted.
    earliest_inserted_game_datetime: int | None = None
    latest_inserted_game_datetime: int | None = None
    #: Per-cohort report, in cohort order (never merged across cohorts).
    cohort_reports: dict[str, CohortIngestReport] = field(default_factory=dict)
    #: Unique match IDs that seeds from more than one cohort returned.
    cross_cohort_match_ids: int = 0
    run_id: str = ""
    #: Seeds recorded in the sampling ledger (successful history requests).
    seed_ledger_rows: int = 0
    #: (match, seed) provenance rows written for stored matches.
    discovery_rows: int = 0
    #: Stored matches (inserted now or already present) with provenance rows.
    matches_with_provenance: int = 0

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


def default_run_id(now_ms: int) -> str:
    """`gh-<run id>-<attempt>` inside GitHub Actions, else `local-<ms>`."""
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        return f"gh-{run}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return f"local-{now_ms}"


def fetch_cohort_entries(client: Any, cohort: str, *, max_pages: int = DEFAULT_MAX_LADDER_PAGES) -> tuple[list[dict], int]:
    """(league entries, requests made) for one seed cohort.

    Apex cohorts: one TFT-LEAGUE-V1 league list (`/tft/league/v1/challenger`
    | `grandmaster` | `master`). Diamond/Platinum: every division I-IV of
    `/tft/league/v1/entries/{TIER}/{DIVISION}?queue=RANKED_TFT&page=N`,
    pages 1..`max_pages`, stopping early at the first empty page -- so the
    population spans the whole tier (all four divisions), not its top."""
    if cohort in LADDER_TIERS:
        return list(getattr(client, cohort)().get("entries") or []), 1
    if cohort not in DIVISION_TIERS:
        raise ValueError(f"Unknown seed cohort {cohort!r}; expected one of {', '.join(COHORTS)}")
    entries: list[dict] = []
    requests = 0
    for division in DIVISIONS:
        for page in range(1, max_pages + 1):
            batch = client.league_entries(DIVISION_TIERS[cohort], division, page=page)
            requests += 1
            if not batch:
                break
            entries.extend(batch)
    return entries, requests


def ingest_ladder(
    client: RiotClient,
    db: Database,
    *,
    player_limit: int = 50,
    matches_per_player: int = 10,
    sampling_mode: str = "challenger",
    seed_allocation: Mapping[str, int] | None = None,
    max_ladder_pages: int = DEFAULT_MAX_LADDER_PAGES,
    cost_lookup: CostLookup | None = None,
    history_start_time: int | None = None,
    history_end_time: int | None = None,
    run_id: str | None = None,
    now_ms: int | None = None,
) -> IngestResult:
    """Seed from TFT ladder PUUIDs and deduplicate overlapping matches.

    Seeds come from `seed_allocation` -- explicit per-cohort counts such as
    ``{"challenger": 20, "diamond": 10}`` (see `tftlab.sampling.COHORTS`) --
    or, when it is None, from the legacy weighted `sampling_mode` with
    `player_limit` seeds. Either way selection is deterministic, rotates
    through the sampling ledger (never-sampled players first, then the
    least recently sampled, spread across each tier), and never looks at
    champions, comps, items, traits, placements or performance. One lobby
    usually shows up in several seeds' histories, so match IDs are
    deduplicated across histories (and cohorts) before any body is fetched,
    and an ID already stored is skipped without fetching its body again.

    After the run, each seed whose history request succeeded is written to
    the ledger (`seed_samples`), and each stored match gets one provenance
    row per seed that surfaced it (`match_discoveries`). A cohort is how a
    lobby was discovered, never a label on the match or its participants.

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

    A ladder PUUID's recent match history isn't exclusively standard ranked
    TFT -- it can include Normal, Hyper Roll, or Double Up games too. Every
    fetched match is checked against `RANKED_TFT_QUEUE_ID` before insertion;
    a non-target-queue match is counted in `non_target_matches_skipped`
    (not `failed_requests` -- Riot answered fine, it's just out of scope for
    this project's dataset) and never stored.

    `history_start_time`/`history_end_time` (epoch seconds) bound each
    seed's match-history request (Riot's `startTime`/`endTime`); a seed
    with no games in that range returns an empty list, which is counted in
    `seeds_with_empty_history` -- not a failure.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    run_id = run_id or default_run_id(now_ms)
    ledger = db.seed_last_sampled()
    ladder_requests: dict[str, int] = {}
    if seed_allocation is not None:
        if not 1 <= max_ladder_pages <= MAX_LADDER_PAGES:
            raise ValueError(f"max_ladder_pages must be 1-{MAX_LADDER_PAGES}")

        def fetch(cohort: str) -> list[dict]:
            entries, ladder_requests[cohort] = fetch_cohort_entries(client, cohort, max_pages=max_ladder_pages)
            return entries

        selection = select_cohort_seeds(fetch, seed_allocation, last_sampled=ledger)
        sampling_mode = "cohorts"
        requested_seeds = selection.requested
    else:
        def fetch_tier(tier: str) -> dict:
            ladder_requests[tier] = 1
            return getattr(client, tier)()

        selection = select_seeds(fetch_tier, total=player_limit, mode=sampling_mode, last_sampled=ledger)
        requested_seeds = player_limit

    ids: list[str] = []
    seen: set[str] = set()
    sources: dict[str, list[tuple[str, str]]] = {}
    sampled: list[tuple[str, str]] = []
    references = 0
    failed_histories = 0
    empty_histories = 0
    per_cohort = {c: {"empty": 0, "failed": 0, "refs": 0, "ids": set()} for c in selection.reports}
    bounds = {}
    if history_start_time is not None:
        bounds["start_time"] = history_start_time
    if history_end_time is not None:
        bounds["end_time"] = history_end_time
    for puuid, cohort in zip(selection.puuids, selection.cohorts):
        stats = per_cohort[cohort]
        try:
            history = client.match_ids(puuid, count=matches_per_player, **bounds)
        except RiotApiError:
            failed_histories += 1
            stats["failed"] += 1
            continue
        sampled.append((puuid, cohort))
        if not history:
            empty_histories += 1
            stats["empty"] += 1
        references += len(history)
        stats["refs"] += len(history)
        for match_id in history:
            stats["ids"].add(match_id)
            if (puuid, cohort) not in sources.setdefault(match_id, []):
                sources[match_id].append((puuid, cohort))
            if match_id not in seen:
                seen.add(match_id)
                ids.append(match_id)
    db.record_seed_samples(run_id, sampled, now_ms)

    fetched = 0
    inserted = 0
    duplicates = 0
    failed = 0
    non_target = 0
    inserted_times: list[int] = []
    stored: list[str] = []
    for match_id in ids:
        if db.has_match(match_id):
            duplicates += 1
            stored.append(match_id)
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
        if db.ingest_match(payload, cost_lookup=cost_lookup):
            inserted += 1
            game_datetime = payload.get("info", {}).get("game_datetime")
            if isinstance(game_datetime, int):
                inserted_times.append(game_datetime)
        stored.append(match_id)
    discoveries = [(m, puuid, cohort) for m in stored for puuid, cohort in sources[m]]
    db.record_match_discoveries(run_id, discoveries, now_ms)

    cohort_reports = {
        c: CohortIngestReport(
            cohort=c,
            requested=r.requested,
            selected=r.selected,
            available=r.available,
            never_sampled_selected=r.never_sampled_selected,
            previously_sampled_selected=r.previously_sampled_selected,
            seeds_with_empty_history=per_cohort[c]["empty"],
            failed_history_requests=per_cohort[c]["failed"],
            match_id_references=per_cohort[c]["refs"],
            unique_match_ids=len(per_cohort[c]["ids"]),
            ladder_requests=ladder_requests.get(c, 0),
        )
        for c, r in selection.reports.items()
    }
    cross_cohort = sum(1 for m in ids if len({cohort for _, cohort in sources[m]}) > 1)

    return IngestResult(
        seed_players=len(selection.puuids),
        match_ids_seen=len(ids),
        matches_fetched=fetched,
        matches_inserted=inserted,
        duplicates_skipped=duplicates,
        failed_requests=failed,
        non_target_matches_skipped=non_target,
        requested_seeds=requested_seeds,
        seeds_by_tier=dict(selection.by_tier),
        ladder_sizes=dict(selection.ladder_sizes),
        sampling_mode=sampling_mode,
        histories_per_seed=matches_per_player,
        match_id_references=references,
        failed_history_requests=failed_histories,
        history_start_time=history_start_time,
        history_end_time=history_end_time,
        seeds_with_empty_history=empty_histories,
        earliest_inserted_game_datetime=min(inserted_times) if inserted_times else None,
        latest_inserted_game_datetime=max(inserted_times) if inserted_times else None,
        cohort_reports=cohort_reports,
        cross_cohort_match_ids=cross_cohort,
        run_id=run_id,
        seed_ledger_rows=len(sampled),
        discovery_rows=len(discoveries),
        matches_with_provenance=len(stored),
    )
