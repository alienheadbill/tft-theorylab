from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple, Sequence

from .normalize import CostLookup
from .riot import RANKED_TFT_QUEUE_ID, RiotApiError, RiotClient
from .sampling import COHORTS, DIVISION_TIERS, DIVISIONS, LADDER_TIERS, select_cohort_seeds, select_seeds
from .storage import RUN_COMPLETED, Database

#: Default and maximum pages read per Diamond/Platinum division. Riot
#: documents neither the page size nor a last-page marker, so paging stops
#: at the first empty page or at this cap, whichever comes first.
DEFAULT_MAX_LADDER_PAGES = 3
MAX_LADDER_PAGES = 10

#: PostgreSQL "deadlock_detected". Only this error is retried: the server
#: already aborted the transaction to break a lock cycle, so re-running the
#: same all-or-nothing match insert is safe. Nothing else is retried.
DEADLOCK_SQLSTATE = "40P01"
#: Sleep before each retry of one match: 2 retries after the first attempt.
DEADLOCK_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)


def is_deadlock(exc: BaseException) -> bool:
    """psycopg's `DeadlockDetected` (SQLSTATE 40P01), and nothing else."""
    return getattr(exc, "sqlstate", None) == DEADLOCK_SQLSTATE


@dataclass
class _DeadlockStats:
    retries: int = 0
    matches_retried: int = 0
    recovered: int = 0


def _ingest_with_deadlock_retry(
    db: Database, payload: dict, cost_lookup: CostLookup | None, delays: Sequence[float], stats: _DeadlockStats
) -> bool:
    """`db.ingest_match` for one match, retried only on a deadlock.

    `ingest_match` rolls its match transaction back on any exception, so
    each attempt starts clean and a match is stored completely or not at
    all. After `len(delays)` retries the deadlock is re-raised (with a note)
    and fails the run; any other error is re-raised immediately."""
    attempt = 0
    while True:
        try:
            stored = db.ingest_match(payload, cost_lookup=cost_lookup)
        except Exception as exc:
            if not is_deadlock(exc):
                raise
            if attempt >= len(delays):
                exc.add_note(f"deadlock persisted after {attempt + 1} attempts on one match; retries exhausted")
                raise
            db.conn.rollback()  # already done by ingest_match; harmless and explicit
            stats.retries += 1
            if attempt == 0:
                stats.matches_retried += 1
            time.sleep(delays[attempt])
            attempt += 1
            continue
        if attempt:
            stats.recovered += 1
        return stored


@dataclass(frozen=True)
class CohortIngestReport:
    """One seed cohort's share of a run. Cohort = sampling provenance (how a
    lobby was discovered), not the rank of the match or its players."""

    cohort: str
    requested: int
    selected: int
    #: Distinct candidates fetched for the cohort (after cross-cohort
    #: dedupe). For an apex cohort this is its whole league list; for
    #: Diamond/Platinum it is the whole ladder only when
    #: `pagination_complete` is True.
    fetched_candidates: int
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
    #: League requests made to build this cohort's candidate pool.
    ladder_requests: int = 0
    #: Diamond/Platinum only (None for apex cohorts, which are one
    #: unpaginated league list): True if every division reached an empty
    #: page before the page cap; False if at least one division's last
    #: allowed page still had entries, so more players may exist.
    pagination_complete: bool | None = None
    #: The per-division page cap used (Diamond/Platinum only).
    max_ladder_pages: int | None = None


class LadderFetch(NamedTuple):
    entries: list[dict]
    #: League requests made.
    requests: int
    #: None for apex cohorts; see `CohortIngestReport.pagination_complete`.
    pagination_complete: bool | None


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
    #: Distinct candidates fetched per cohort (see
    #: `CohortIngestReport.fetched_candidates` -- not necessarily the whole
    #: ladder for a capped Diamond/Platinum fetch).
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
    #: `ingest_runs.status` at the end; a returned result is always
    #: "completed" (a run that fails raises instead and stays incomplete).
    run_status: str = ""
    #: Seeds finalized into the sampling ledger (successful history
    #: requests), in the same transaction that completed the run.
    seed_ledger_rows: int = 0
    #: (match, seed) provenance rows finalized for stored matches.
    discovery_rows: int = 0
    #: Stored matches (inserted now or already present) with provenance rows.
    matches_with_provenance: int = 0
    #: Match inserts retried after a PostgreSQL deadlock (40P01), how many
    #: matches needed one, and how many of those then succeeded. A deadlock
    #: that outlasts the retries fails the run instead of being counted.
    deadlock_retries: int = 0
    matches_with_deadlock_retry: int = 0
    deadlocks_recovered: int = 0

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


def fetch_cohort_entries(client: Any, cohort: str, *, max_pages: int = DEFAULT_MAX_LADDER_PAGES) -> LadderFetch:
    """League entries for one seed cohort, with request count and (for
    Diamond/Platinum) whether pagination was complete.

    Apex cohorts: one TFT-LEAGUE-V1 league list (`/tft/league/v1/challenger`
    | `grandmaster` | `master`) -- the whole list, no pages.

    Diamond/Platinum: every division I-IV of
    `/tft/league/v1/entries/{TIER}/{DIVISION}?queue=RANKED_TFT&page=N`,
    pages 1..`max_pages`, a division stopping early at its first empty page.
    This always covers all four divisions, but not necessarily every page
    or player in them: if any division's page `max_pages` still returned
    entries, the fetch is capped (`pagination_complete=False`) and more
    players may exist beyond what was fetched."""
    if cohort in LADDER_TIERS:
        return LadderFetch(list(getattr(client, cohort)().get("entries") or []), 1, None)
    if cohort not in DIVISION_TIERS:
        raise ValueError(f"Unknown seed cohort {cohort!r}; expected one of {', '.join(COHORTS)}")
    entries: list[dict] = []
    requests = 0
    complete = True
    for division in DIVISIONS:
        for page in range(1, max_pages + 1):
            batch = client.league_entries(DIVISION_TIERS[cohort], division, page=page)
            requests += 1
            if not batch:
                break
            entries.extend(batch)
        else:  # never saw an empty page: the cap stopped this division
            complete = False
    return LadderFetch(entries, requests, complete)


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
    retry_delays: Sequence[float] = DEADLOCK_RETRY_DELAYS,
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

    The run is registered in `ingest_runs` as started before anything else.
    Only when every match has been handled does one transaction write the
    ledger (`seed_samples`: each seed whose history request succeeded), the
    provenance (`match_discoveries`: one row per stored match per seed that
    surfaced it) and mark the run completed. Seed rotation counts completed
    runs only, so a run that dies part-way never advances it; matches it
    already stored stay stored (one transaction each) and the next run
    skips them via `has_match` while recording its own provenance for them.
    A cohort is how a lobby was discovered, never a label on the match or
    its participants.

    A PostgreSQL deadlock (SQLSTATE 40P01) while storing one match is
    retried for that match only, after `retry_delays` (default 0.5 s, then
    1 s); if it persists, or any other error occurs, the run fails, is
    marked failed (best effort) and the exception propagates.

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
    db.start_ingest_run(run_id, now_ms)
    try:
        return _ingest_run(
            client, db, run_id=run_id, now_ms=now_ms, player_limit=player_limit,
            matches_per_player=matches_per_player, sampling_mode=sampling_mode,
            seed_allocation=seed_allocation, max_ladder_pages=max_ladder_pages, cost_lookup=cost_lookup,
            history_start_time=history_start_time, history_end_time=history_end_time, retry_delays=retry_delays,
        )
    except BaseException as exc:
        db.mark_ingest_run_failed(run_id, type(exc).__name__)
        raise


def _ingest_run(
    client: RiotClient,
    db: Database,
    *,
    run_id: str,
    now_ms: int,
    player_limit: int,
    matches_per_player: int,
    sampling_mode: str,
    seed_allocation: Mapping[str, int] | None,
    max_ladder_pages: int,
    cost_lookup: CostLookup | None,
    history_start_time: int | None,
    history_end_time: int | None,
    retry_delays: Sequence[float],
) -> IngestResult:
    ledger = db.seed_last_sampled()
    ladder_fetches: dict[str, LadderFetch] = {}
    if seed_allocation is not None:
        if not 1 <= max_ladder_pages <= MAX_LADDER_PAGES:
            raise ValueError(f"max_ladder_pages must be 1-{MAX_LADDER_PAGES}")

        def fetch(cohort: str) -> list[dict]:
            ladder_fetches[cohort] = fetch_cohort_entries(client, cohort, max_pages=max_ladder_pages)
            return ladder_fetches[cohort].entries

        selection = select_cohort_seeds(fetch, seed_allocation, last_sampled=ledger)
        sampling_mode = "cohorts"
        requested_seeds = selection.requested
    else:
        def fetch_tier(tier: str) -> dict:
            payload = getattr(client, tier)()
            ladder_fetches[tier] = LadderFetch([], 1, None)
            return payload

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

    fetched = 0
    inserted = 0
    duplicates = 0
    failed = 0
    non_target = 0
    inserted_times: list[int] = []
    stored: list[str] = []
    deadlocks = _DeadlockStats()
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
        if _ingest_with_deadlock_retry(db, payload, cost_lookup, retry_delays, deadlocks):
            inserted += 1
            game_datetime = payload.get("info", {}).get("game_datetime")
            if isinstance(game_datetime, int):
                inserted_times.append(game_datetime)
        stored.append(match_id)
    discoveries = [(m, puuid, cohort) for m in stored for puuid, cohort in sources[m]]
    db.finalize_ingest_run(
        run_id, sampled, discoveries, sampled_at=now_ms, completed_at=max(now_ms, int(time.time() * 1000))
    )

    cohort_reports = {
        c: CohortIngestReport(
            cohort=c,
            requested=r.requested,
            selected=r.selected,
            fetched_candidates=r.available,
            never_sampled_selected=r.never_sampled_selected,
            previously_sampled_selected=r.previously_sampled_selected,
            seeds_with_empty_history=per_cohort[c]["empty"],
            failed_history_requests=per_cohort[c]["failed"],
            match_id_references=per_cohort[c]["refs"],
            unique_match_ids=len(per_cohort[c]["ids"]),
            ladder_requests=ladder_fetches[c].requests if c in ladder_fetches else 0,
            pagination_complete=ladder_fetches[c].pagination_complete if c in ladder_fetches else None,
            max_ladder_pages=max_ladder_pages if c in DIVISION_TIERS else None,
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
        run_status=RUN_COMPLETED,
        seed_ledger_rows=len(sampled),
        discovery_rows=len(discoveries),
        matches_with_provenance=len(stored),
        deadlock_retries=deadlocks.retries,
        matches_with_deadlock_retry=deadlocks.matches_retried,
        deadlocks_recovered=deadlocks.recovered,
    )
