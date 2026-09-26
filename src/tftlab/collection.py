"""Maximum collection mode: exhaust what Riot can reach, inside hard budgets.

COLLECTION, NOT ANALYSIS. This module decides only *which Riot requests to
make and in what order*: which ladder players to read, how deep into their
(trusted-window) history to go, and which lobbies to fetch. It never looks
at champions, items, traits, placements or performance, and it changes
nothing about how stored matches are interpreted -- patch windows,
classification, carry eligibility, Discovery and the Opportunity Score
read the same tables exactly as before. More matches in, same rules out.

Compared with the bounded mode (`tftlab.ingest.ingest_ladder`: N seeds per
cohort x M recent matches each), maximum mode:

- enumerates the five seed cohorts completely -- the Challenger,
  Grandmaster and Master league lists, and every page of Diamond I-IV and
  Platinum I-IV until Riot returns an empty page (guarded: see
  `enumerate_division`) -- and dedupes PUUIDs across cohorts (a player
  listed twice seeds once, in the higher cohort);
- orders every candidate breadth-first: never-sampled players first, then
  the least recently sampled, interleaved across cohorts and spread across
  each ladder (so stopping early still covers every cohort and rank band);
- reads each seed's Match-V1 history *to exhaustion* inside the time bounds
  (`startTime`/`endTime`, required), `HISTORY_PAGE_SIZE` IDs per page,
  pages interleaved round-robin across the wave's seeds;
- dedupes every match ID the moment it is seen (in-run) and against the
  store (`has_match`) before any match-detail request, so each lobby is
  fetched and stored at most once;
- works in *waves*: each wave of `wave_size` seeds is its own `ingest_runs`
  row (`<run id>-w001`, `-w002`, ...), finalized atomically with the
  existing `Database.finalize_ingest_run`. An interruption loses at most
  the current wave's bookkeeping; completed waves already advanced the
  rotation, so a rerun continues where this one stopped;
- stops cleanly on the first operator budget reached (wall clock, Riot
  requests, match-detail fetches): no new work is started, the current
  wave is finalized with only the seeds that were fully handled, and the
  stop reason is reported.

Ledger truthfulness (extends PR #20): a seed gets a `seed_samples` row only
when its history reading reached a terminal point (exhausted, a guard, or
the per-seed page cap -- reported separately) *and* every new match ID it
surfaced was handled (stored, already stored, non-ranked, or a counted
failed fetch). A seed interrupted by a budget, or whose history request
failed, is not ledgered and stays at the front of the rotation.
Provenance (`match_discoveries`) is written for every stored match a seed
surfaced, including matches another seed surfaced first. A cohort is
provenance (how a lobby was discovered), never a label on the match.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .ingest import DEADLOCK_RETRY_DELAYS, _DeadlockStats, _ingest_with_deadlock_retry, default_run_id
from .normalize import CostLookup
from .riot import RANKED_TFT_QUEUE_ID, RiotApiError, RiotBudgetExhausted
from .sampling import COHORTS, DIVISION_TIERS, DIVISIONS, LADDER_TIERS, rank_entries
from .storage import Database

COLLECTION_MODES: tuple[str, ...] = ("bounded", "maximum")

#: Match IDs per history page. Riot documents `count` with default 20 and
#: no maximum, so the documented default is used rather than a guessed cap.
HISTORY_PAGE_SIZE = 20
#: Per-seed page cap (x 20 = 1000 matches inside the time bounds). A seed
#: that reaches it is reported as capped.
DEFAULT_MAX_HISTORY_PAGES = 50
#: Per-division page cap for Diamond/Platinum. Riot documents neither the
#: page size nor a last-page marker; enumeration ends at the first empty
#: page, and this cap only guards against a pagination that never ends.
DEFAULT_MAX_DIVISION_PAGES = 500
#: Consecutive pages with no PUUID not already seen in that division.
DUPLICATE_ONLY_PAGE_LIMIT = 2
DEFAULT_WAVE_SIZE = 25

# Division / history stop reasons.
EMPTY_PAGE = "empty_page"
REPEATED_PAGE = "repeated_page"
DUPLICATE_ONLY_PAGES = "duplicate_only_pages"
PAGE_CAP = "page_cap"
EXHAUSTED = "exhausted"
FAILED = "failed"
INTERRUPTED = "interrupted"

# Run stop reasons.
STOP_COMPLETE = "complete"
STOP_REQUESTS = "request_budget"
STOP_DURATION = "duration_budget"
STOP_MATCH_FETCHES = "match_fetch_budget"


@dataclass(frozen=True)
class CollectionBudgets:
    """Hard operator limits for one maximum-mode run. All required: there
    is no silent "unlimited" default."""

    max_duration_s: float
    max_requests: int
    max_match_fetches: int

    def __post_init__(self) -> None:
        if not self.max_duration_s > 0:
            raise ValueError("max_duration_s must be positive")
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if self.max_match_fetches < 0:
            raise ValueError("max_match_fetches must be non-negative")


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- ladders


@dataclass(frozen=True)
class DivisionEnumeration:
    division: str
    pages: int
    entries: int
    stop_reason: str

    @property
    def complete(self) -> bool:
        """Only an empty page is evidence that the division has ended."""
        return self.stop_reason == EMPTY_PAGE


@dataclass(frozen=True)
class CohortEnumeration:
    cohort: str
    entries: list[dict] = field(repr=False)
    requests: int
    #: Diamond/Platinum only.
    divisions: tuple[DivisionEnumeration, ...] = ()

    @property
    def complete(self) -> bool:
        return all(d.complete for d in self.divisions)


def enumerate_division(client: Any, tier: str, division: str, *, max_pages: int) -> tuple[list[dict], DivisionEnumeration]:
    """Every page of `/tft/league/v1/entries/{tier}/{division}` from page 1.

    Stops at, in order of precedence per page:
    - an empty page (`empty_page`) -- the only "complete" outcome;
    - a page whose PUUID set equals the previous page's (`repeated_page`:
      a pagination that stopped advancing); the repeat is not kept;
    - `DUPLICATE_ONLY_PAGE_LIMIT` consecutive pages bringing no PUUID new
      to this division (`duplicate_only_pages`);
    - `max_pages` (`page_cap`), a guard against pagination that never ends.
    Page size is never assumed: a short page is not treated as the last."""
    entries: list[dict] = []
    seen: set[str] = set()
    previous: frozenset[str] | None = None
    duplicate_only = 0
    reason = PAGE_CAP
    pages = 0
    for page in range(1, max_pages + 1):
        batch = client.league_entries(tier, division, page=page)
        pages += 1
        if not batch:
            reason = EMPTY_PAGE
            break
        puuids = frozenset(e.get("puuid") for e in batch if e.get("puuid"))
        if puuids == previous:
            reason = REPEATED_PAGE
            break
        if puuids - seen:
            duplicate_only = 0
        else:
            duplicate_only += 1
        entries.extend(batch)
        seen |= puuids
        previous = puuids
        if duplicate_only >= DUPLICATE_ONLY_PAGE_LIMIT:
            reason = DUPLICATE_ONLY_PAGES
            break
    return entries, DivisionEnumeration(division, pages, len(entries), reason)


def enumerate_cohort(client: Any, cohort: str, *, max_division_pages: int = DEFAULT_MAX_DIVISION_PAGES) -> CohortEnumeration:
    if cohort in LADDER_TIERS:
        return CohortEnumeration(cohort, list(getattr(client, cohort)().get("entries") or []), 1)
    if cohort not in DIVISION_TIERS:
        raise ValueError(f"Unknown seed cohort {cohort!r}; expected one of {', '.join(COHORTS)}")
    entries: list[dict] = []
    divisions = []
    for division in DIVISIONS:
        batch, report = enumerate_division(client, DIVISION_TIERS[cohort], division, max_pages=max_division_pages)
        entries.extend(batch)
        divisions.append(report)
    return CohortEnumeration(cohort, entries, sum(d.pages for d in divisions), tuple(divisions))


# ---------------------------------------------------------------- ordering


def spread_order(n: int) -> list[int]:
    """A permutation of range(n) whose every prefix is spread across the
    whole range (golden-ratio stride), so an early stop still covers every
    rank band of a ladder instead of only its top."""
    if n <= 2:
        return list(range(n))
    stride = max(1, round(n * (math.sqrt(5) - 1) / 2))
    while math.gcd(stride, n) != 1:
        stride += 1
    return [(i * stride) % n for i in range(n)]


def breadth_first_queue(ranked: Mapping[str, Sequence[str]], last_sampled: Mapping[str, int]) -> list[tuple[str, str]]:
    """(puuid, cohort) for every candidate: never-sampled first, then least
    recently sampled; within the same last-sampled time, interleaved across
    cohorts (one from each in turn) and spread across each ladder."""
    keyed = []
    cohorts = list(ranked)
    for c_index, cohort in enumerate(cohorts):
        candidates = ranked[cohort]
        for position, index in enumerate(spread_order(len(candidates))):
            puuid = candidates[index]
            keyed.append(((last_sampled.get(puuid, -1), position, c_index), puuid, cohort))
    keyed.sort(key=lambda item: item[0])
    return [(puuid, cohort) for _, puuid, cohort in keyed]


# ---------------------------------------------------------------- results


@dataclass
class CohortCollectionStats:
    cohort: str
    ladder_entries: int = 0
    #: Distinct PUUIDs seeded from this cohort after cross-cohort dedupe.
    candidates: int = 0
    never_sampled_candidates: int = 0
    ladder_requests: int = 0
    ladder_complete: bool | None = None
    divisions: tuple[DivisionEnumeration, ...] = ()
    seeds_started: int = 0
    seeds_ledgered: int = 0
    history_requests: int = 0
    match_id_references: int = 0
    new_match_ids: int = 0


@dataclass
class MaximumCollectionResult:
    run_id: str
    stop_reason: str = STOP_COMPLETE
    stop_detail: str = ""
    wave_run_ids: list[str] = field(default_factory=list)
    cohorts: dict[str, CohortCollectionStats] = field(default_factory=dict)
    #: PUUIDs listed in more than one cohort's ladder (seeded once, highest cohort).
    cross_listed_puuids: int = 0
    candidates: int = 0
    never_sampled_candidates: int = 0
    seeds_started: int = 0
    seeds_ledgered: int = 0
    #: Terminal history outcomes of started seeds.
    seeds_exhausted: int = 0
    seeds_repeated_page_guard: int = 0
    seeds_page_capped: int = 0
    seeds_failed: int = 0
    seeds_interrupted: int = 0
    #: Terminal but not ledgered: some surfaced match ID was never handled.
    seeds_with_unhandled_matches: int = 0
    seeds_with_empty_history: int = 0
    ladder_requests: int = 0
    history_requests: int = 0
    match_id_references: int = 0
    unique_match_ids: int = 0
    already_stored_skipped: int = 0
    match_fetches: int = 0
    matches_fetched: int = 0
    matches_inserted: int = 0
    non_target_matches_skipped: int = 0
    failed_match_fetches: int = 0
    unhandled_match_ids: int = 0
    seed_ledger_rows: int = 0
    discovery_rows: int = 0
    deadlock_retries: int = 0
    matches_with_deadlock_retry: int = 0
    deadlocks_recovered: int = 0
    history_start_time: int | None = None
    history_end_time: int | None = None
    earliest_inserted_game_datetime: int | None = None
    latest_inserted_game_datetime: int | None = None
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe aggregates only: no PUUIDs, match IDs or names."""
        data = {k: v for k, v in self.__dict__.items() if k != "cohorts"}
        data["elapsed_s"] = round(self.elapsed_s, 3)
        data["cohorts"] = {
            c: {
                **{k: v for k, v in s.__dict__.items() if k != "divisions"},
                "divisions": [
                    {"division": d.division, "pages": d.pages, "entries": d.entries,
                     "stop_reason": d.stop_reason, "complete": d.complete}
                    for d in s.divisions
                ],
            }
            for c, s in self.cohorts.items()
        }
        return data


@dataclass
class _Seed:
    puuid: str
    cohort: str
    next_start: int = 0
    pages: int = 0
    outcome: str | None = None
    ids: list[str] = field(default_factory=list)
    id_set: set[str] = field(default_factory=set)


# ---------------------------------------------------------------- collection


def collect_maximum(
    client: Any,
    db: Database,
    *,
    budgets: CollectionBudgets,
    history_start_time: int,
    history_end_time: int | None = None,
    cost_lookup: CostLookup | None = None,
    run_id: str | None = None,
    wave_size: int = DEFAULT_WAVE_SIZE,
    max_history_pages: int = DEFAULT_MAX_HISTORY_PAGES,
    max_division_pages: int = DEFAULT_MAX_DIVISION_PAGES,
    now_ms: Callable[[], int] | None = None,
    clock: Callable[[], float] | None = None,
    retry_delays: Sequence[float] = DEADLOCK_RETRY_DELAYS,
) -> MaximumCollectionResult:
    """Collect as much of the reachable, time-bounded ranked data as the
    budgets allow (see the module docstring for order and guarantees).

    `client` is a RiotClient (or a duck-typed stub). Request and wall-clock
    budgets are enforced by the client itself when it supports them
    (`max_requests` / `deadline` attributes, set here), so no request is
    ever sent past a budget; the match-fetch budget is enforced here.
    Returns normally on completion or a budget stop; raises (after marking
    the current wave failed) on anything else, e.g. a database error or a
    failed ladder request."""
    if history_start_time is None:
        raise ValueError("maximum collection needs a history lower bound (startTime)")
    if wave_size < 1 or max_history_pages < 1 or max_division_pages < 1:
        raise ValueError("wave_size, max_history_pages and max_division_pages must be at least 1")
    now_ms = now_ms or (lambda: int(time.time() * 1000))
    # The client's own clock when it has one, so its deadline and ours agree.
    clock = clock or getattr(client, "_clock", None) or time.monotonic
    started = clock()
    run_id = run_id or default_run_id(now_ms())
    if hasattr(client, "max_requests"):
        client.max_requests = budgets.max_requests
    if hasattr(client, "deadline"):
        client.deadline = started + budgets.max_duration_s
    result = MaximumCollectionResult(
        run_id=run_id, history_start_time=history_start_time, history_end_time=history_end_time
    )
    bounds: dict[str, int] = {"start_time": history_start_time}
    if history_end_time is not None:
        bounds["end_time"] = history_end_time

    def check_time() -> None:
        if clock() - started >= budgets.max_duration_s:
            raise _Stop(STOP_DURATION)

    try:
        _collect(client, db, result, budgets=budgets, bounds=bounds, cost_lookup=cost_lookup, wave_size=wave_size,
                 max_history_pages=max_history_pages, max_division_pages=max_division_pages, now_ms=now_ms,
                 check_time=check_time, retry_delays=retry_delays)
    except _Stop as stop:
        result.stop_reason = stop.reason
    result.elapsed_s = clock() - started
    return result


def _budget_reason(exc: RiotBudgetExhausted) -> str:
    return STOP_DURATION if "wall-clock" in str(exc) else STOP_REQUESTS


def _collect(client, db, result: MaximumCollectionResult, **kwargs) -> None:
    seen: set[str] = set()  # every match ID seen this run
    handled: set[str] = set()  # of those, the ones fully dealt with
    try:
        _collect_waves(client, db, result, seen=seen, handled=handled, **kwargs)
    finally:
        result.unhandled_match_ids = len(seen - handled)


def _collect_waves(client, db, result: MaximumCollectionResult, *, seen, handled, budgets, bounds, cost_lookup,
                   wave_size, max_history_pages, max_division_pages, now_ms, check_time, retry_delays) -> None:
    # 1. Ladders: every cohort, highest first; cross-cohort dedupe.
    ranked: dict[str, list[str]] = {}
    claimed: set[str] = set()
    listed: dict[str, int] = {}
    for cohort in COHORTS:
        check_time()
        try:
            enumeration = enumerate_cohort(client, cohort, max_division_pages=max_division_pages)
        except RiotBudgetExhausted as exc:
            result.stop_detail = f"during {cohort} ladder enumeration: {exc}"
            raise _Stop(_budget_reason(exc))
        for puuid in {e.get("puuid") for e in enumeration.entries if e.get("puuid")}:
            listed[puuid] = listed.get(puuid, 0) + 1
        ranked[cohort] = rank_entries(enumeration.entries, claimed)
        claimed.update(ranked[cohort])
        result.ladder_requests += enumeration.requests
        result.cohorts[cohort] = CohortCollectionStats(
            cohort=cohort,
            ladder_entries=len(enumeration.entries),
            candidates=len(ranked[cohort]),
            ladder_requests=enumeration.requests,
            ladder_complete=enumeration.complete if cohort in DIVISION_TIERS else None,
            divisions=enumeration.divisions,
        )
    result.cross_listed_puuids = sum(1 for n in listed.values() if n > 1)

    # 2. Breadth-first queue over every candidate.
    ledger = db.seed_last_sampled()
    queue = breadth_first_queue(ranked, ledger)
    result.candidates = len(queue)
    for puuid, cohort in queue:
        if puuid not in ledger:
            result.never_sampled_candidates += 1
            result.cohorts[cohort].never_sampled_candidates += 1

    # 3. Waves.
    stored: set[str] = set()  # match IDs seen this run that are now in the store
    deadlocks = _DeadlockStats()
    inserted_times: list[int] = []
    for wave_number, offset in enumerate(range(0, len(queue), wave_size), start=1):
        check_time()
        wave = [_Seed(p, c) for p, c in queue[offset : offset + wave_size]]
        wave_id = f"{result.run_id}-w{wave_number:03d}"
        db.start_ingest_run(wave_id, now_ms())
        result.wave_run_ids.append(wave_id)
        stop: _Stop | None = None
        try:
            try:
                _read_histories(client, wave, result, bounds, max_history_pages, seen, check_time)
                _fetch_matches(client, db, wave, result, budgets, seen, stored, handled, cost_lookup,
                               retry_delays, deadlocks, inserted_times, check_time)
            except _Stop as exc:
                stop = exc
            except RiotBudgetExhausted as exc:
                result.stop_detail = str(exc)
                stop = _Stop(_budget_reason(exc))
            _finalize_wave(db, wave_id, wave, result, stored, handled, now_ms)
        except BaseException as exc:
            db.mark_ingest_run_failed(wave_id, type(exc).__name__)
            raise
        finally:
            result.deadlock_retries = deadlocks.retries
            result.matches_with_deadlock_retry = deadlocks.matches_retried
            result.deadlocks_recovered = deadlocks.recovered
            if inserted_times:
                result.earliest_inserted_game_datetime = min(inserted_times)
                result.latest_inserted_game_datetime = max(inserted_times)
        if stop is not None:
            raise stop


def _read_histories(client, wave: list[_Seed], result, bounds, max_history_pages, seen, check_time) -> None:
    """Round-robin: one page per still-active seed per round, until every
    seed reaches a terminal outcome. New IDs are deduped as they arrive."""
    for seed in wave:
        result.seeds_started += 1
        result.cohorts[seed.cohort].seeds_started += 1
    active = list(wave)
    try:
        while active:
            still = []
            for seed in active:
                check_time()
                try:
                    page = client.match_ids(seed.puuid, count=HISTORY_PAGE_SIZE, start=seed.next_start, **bounds)
                except RiotApiError:
                    seed.outcome = FAILED
                    result.seeds_failed += 1
                    result.history_requests += 1
                    result.cohorts[seed.cohort].history_requests += 1
                    continue
                seed.pages += 1
                result.history_requests += 1
                stats = result.cohorts[seed.cohort]
                stats.history_requests += 1
                fresh = [m for m in page if m not in seed.id_set]
                result.match_id_references += len(page)
                stats.match_id_references += len(page)
                for match_id in fresh:
                    seed.id_set.add(match_id)
                    seed.ids.append(match_id)
                    if match_id not in seen:
                        seen.add(match_id)
                        result.unique_match_ids += 1
                        stats.new_match_ids += 1
                if page and not fresh:
                    seed.outcome = REPEATED_PAGE
                    result.seeds_repeated_page_guard += 1
                elif len(page) < HISTORY_PAGE_SIZE:
                    seed.outcome = EXHAUSTED
                    result.seeds_exhausted += 1
                    if not seed.ids:
                        result.seeds_with_empty_history += 1
                elif seed.pages >= max_history_pages:
                    seed.outcome = PAGE_CAP
                    result.seeds_page_capped += 1
                else:
                    seed.next_start += len(page)
                    still.append(seed)
            active = still
    finally:
        for seed in wave:
            if seed.outcome is None:
                seed.outcome = INTERRUPTED
                result.seeds_interrupted += 1


def _fetch_matches(client, db, wave, result, budgets, seen, stored, handled, cost_lookup, retry_delays, deadlocks,
                   inserted_times, check_time) -> None:
    for seed in wave:
        for match_id in seed.ids:
            if match_id in handled:
                continue
            check_time()
            if db.has_match(match_id):
                result.already_stored_skipped += 1
                stored.add(match_id)
                handled.add(match_id)
                continue
            if result.match_fetches >= budgets.max_match_fetches:
                raise _Stop(STOP_MATCH_FETCHES)
            try:
                payload = client.match(match_id)
            except RiotApiError:
                result.match_fetches += 1
                result.failed_match_fetches += 1
                handled.add(match_id)
                continue
            result.match_fetches += 1
            result.matches_fetched += 1
            if payload.get("info", {}).get("queue_id") != RANKED_TFT_QUEUE_ID:
                result.non_target_matches_skipped += 1
                handled.add(match_id)
                continue
            if _ingest_with_deadlock_retry(db, payload, cost_lookup, retry_delays, deadlocks):
                result.matches_inserted += 1
                game_datetime = payload.get("info", {}).get("game_datetime")
                if isinstance(game_datetime, int):
                    inserted_times.append(game_datetime)
            stored.add(match_id)
            handled.add(match_id)


def _finalize_wave(db, wave_id, wave, result, stored, handled, now_ms) -> None:
    sampled = []
    for seed in wave:
        if seed.outcome in (EXHAUSTED, REPEATED_PAGE, PAGE_CAP):
            if all(m in handled for m in seed.ids):
                sampled.append((seed.puuid, seed.cohort))
                result.cohorts[seed.cohort].seeds_ledgered += 1
            else:
                result.seeds_with_unhandled_matches += 1
    discoveries = [(m, seed.puuid, seed.cohort) for seed in wave for m in seed.ids if m in stored]
    at = now_ms()
    db.finalize_ingest_run(wave_id, sampled, discoveries, sampled_at=at, completed_at=at)
    result.seeds_ledgered += len(sampled)
    result.seed_ledger_rows += len(sampled)
    result.discovery_rows += len(discoveries)


# ---------------------------------------------------------------- planning


def collection_plan(
    *,
    budgets: CollectionBudgets,
    rate_ceilings: Sequence[Any] = (),
    store: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Network-free plan for a maximum run: the budgets, the pacing policy
    and an upper bound on request throughput from the operator ceilings
    alone. It makes no Riot, CommunityDragon or database call itself;
    `store` is whatever read-only counts the caller already has."""
    ceiling_rps = min((c.limit / c.seconds for c in rate_ceilings), default=None)
    by_time = None if ceiling_rps is None else int(budgets.max_duration_s * ceiling_rps)
    return {
        "network": "none (planning only: no Riot, CommunityDragon or write access)",
        "budgets": {
            "max_duration_s": budgets.max_duration_s,
            "max_requests": budgets.max_requests,
            "max_match_fetches": budgets.max_match_fetches,
        },
        "rate_ceilings": [str(c) for c in rate_ceilings],
        "ceiling_requests_per_second": ceiling_rps,
        "max_requests_by_time_at_ceiling": by_time,
        "request_upper_bound": budgets.max_requests if by_time is None else min(budgets.max_requests, by_time),
        "request_cost_model": (
            "ladder: 3 apex requests + one per Diamond/Platinum division page (page size undocumented, so the "
            "page count is only known after enumeration); history: at least 1 request per seed, plus 1 per "
            f"further {HISTORY_PAGE_SIZE} match IDs in the time bounds; match detail: 1 per new, not-yet-stored "
            "lobby. Real pacing also follows the limits Riot advertises per key, which are not known offline."
        ),
        "store": dict(store or {}),
    }
