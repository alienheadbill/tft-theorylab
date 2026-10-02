"""Board-archetype RESEARCH harness -- experimental validation infrastructure.

NOT a production composition system. Nothing here is served to users or
written to any database; it exists to answer one question before any product
work: do real final boards form coherent, recognizable composition families?

What it does, read-only (`Database.open_existing`, and `assert_read_only`
refuses to continue unless a Postgres server reports
`transaction_read_only = on`):

1. Loads every unit-observable Ranked TFT final board of one balance window
   (SELECTs of ids, placements, stars, item ids and traits only; raw payloads
   are read solely by `tftlab.validate`'s source-empty classification and are
   never exported).
2. Normalizes each board deterministically. Structural IDENTITY is the set of
   champions in the committed art manifest's trait-bearing cost 1-5 list
   (`is_identity_unit`); roster summons, camps, anvils and other trait-less
   entities are excluded, duplicate copies count once, and ids missing from
   the roster are kept under their raw id (never guessed) when their stored
   shop cost is 1-5. Champion membership is the fundamental board identity:
   strategies A and B group from champion structure alone, and strategy C
   additionally uses completed-item count, star/cost splash weighting and
   active traits as predeclared secondary structural signals. Placement and
   every other outcome, and the current carry qualification, are post-group
   analytics only and never influence grouping.
3. Groups boards with predeclared strategies (`STRATEGIES`) whose every
   threshold lives in `ArchetypeConfig` and is printed with the results:
   A. structural baseline -- champion-set similarity only (the control);
   B. flex-tolerant -- A plus a structural variant merge for flex slots and
      incomplete boards, each merge kept only if the merged group itself
      stays coherent (core kept, every board >= tau to the merged profile);
   C. structure-aware -- B's idea plus documented secondary structure
      (item counts, splash weighting, active traits);
   plus two EXPERIMENTAL strategies (B_S2 / C_S2, research only): B and C
   on the same variants with the merged-result similarity check replaced by
   the S2 rule (`s2_conditions`); A, B and C stay unchanged controls. The
   experimental strategies also get REPORT-ONLY recursive lock-in
   instrumentation (`recursive_lock_in_summary`: below-tau tails admitted by
   accepted merges, Condition-1 attribution of later rejections, the
   historical-tail removal counterfactual) and tie-order replays
   (`order_replays`); neither changes the real S2 grouping.
4. Reports global grouping statistics and a deterministic, human-reviewable
   sample of real groups with member boards, core/flex, carry/tank/Thief's
   Gloves diagnostics and item sets, using canonical names from committed
   metadata (`UNRESOLVED: <raw id>` when none exists).

The parameters are candidate modeling choices under validation, not facts.
Similarity is Ruzicka (weighted Jaccard, sum(min) / sum(max)) between a board
and a group's mean member vector.
"""

from __future__ import annotations

import csv
import heapq
import io
import json
import math
import os
import shutil
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import carry
from .items import ITEM_STATS_PATH, is_component
from .research_report import assert_read_only
from .riot import RANKED_TFT_QUEUE_ID
from .roster import load_roster
from .storage import Database
from .validate import classify_participants_without_units

DEFAULT_BALANCE_WINDOW = "18.3"


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class ArchetypeConfig:
    """Every modeling choice, declared up front and printed with the report.
    Candidate values carried over from the synthetic v0 prototype; none is
    an established fact about TFT boards."""

    #: Boards with fewer shop units are too incomplete to place.
    min_identity_units: int = 4
    #: Smallest group kept during refinement (singletons are "ungrouped").
    min_group_size: int = 2
    #: Board-to-group similarity needed to join a group (all strategies).
    #: 0.60 ~= two swaps between two 8-unit boards (6 shared / 10 distinct).
    tau: float = 0.60
    #: Refinement passes and the "stable" criterion (moved share of boards).
    max_refine_iterations: int = 10
    convergence_moved_share: float = 0.001
    #: Candidate-group prune (performance heuristic, audited in the report):
    #: a group is scored only if it shares >= this many board units whose
    #: presence in the group is >= prune_presence.
    prune_min_shared: int = 2
    prune_presence: float = 0.30
    #: Boards per strategy re-scored against every group to audit the prune.
    prune_audit_boards: int = 300
    #: Variant merge (strategies B and C): cores are units on >= core_presence
    #: of a variant's boards; cores must share >= max(merge_min_shared,
    #: merge_shared_fraction * larger core) units; at most max_swaps
    #: one-for-one core swaps (pure additions -- bigger boards -- allowed).
    core_presence: float = 0.75
    merge_min_shared: int = 3
    merge_shared_fraction: float = 0.6
    max_swaps: int = 1
    #: Merge result checks (strategies B and C), applied to every tentative
    #: merge: the merged group's core (same core_presence rule) must keep >=
    #: merge_min_result_core units OR >= merge_core_retention of the smaller
    #: pre-merge core; and every board of the merged group must still be >=
    #: tau similar to the merged mean profile (the strategy's own
    #: similarity). Experimental thresholds from validation run #2. The
    #: merged core always contains the shared core, so while
    #: merge_shared_fraction >= merge_core_retention the retention branch is
    #: implied by the pairwise overlap requirement and the similarity check
    #: is the binding one; the core rule binds only if those fractions differ.
    merge_min_result_core: int = 5
    merge_core_retention: float = 0.6
    #: Strategy C only: a differing core unit is "explained" if it is on >=
    #: flex_presence of the other variant's boards, or un-itemized in its own
    #: variant (holds >= 2 completed items on < itemized_share of its boards),
    #: or it is the single addition to a fully shared core of >=
    #: merge_min_result_core units (itemized-splash exception).
    flex_presence: float = 0.15
    itemized_share: float = 0.5
    #: Strategy C weights: units holding >= 2 completed items (any items; the
    #: carry classifier is not used) count double; un-itemized 1-star 4/5-cost
    #: units (typical splash) count half; active traits are 25% of similarity.
    itemized_weight: float = 2.0
    splash_weight: float = 0.5
    trait_share: float = 0.25
    #: Display conventions only (not findings): "core candidates" and "other
    #: common units" in the human-readable report.
    display_core_presence: float = 0.75
    display_other_presence: float = 0.15
    #: Report sizing.
    sample_member_boards: int = 5
    small_sample: int = 10


SIZE_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("2-4", 2, 4), ("5-9", 5, 9), ("10-29", 10, 29), ("30-59", 30, 59), ("60-189", 60, 189), ("190+", 190, None),
)


def size_band(n: int) -> str:
    for label, low, high in SIZE_BANDS:
        if n >= low and (high is None or n <= high):
            return label
    return "1"


# ---------------------------------------------------------------- progress


class Progress:
    """Elapsed-time progress lines ("[progress +12.3s] message") for the job
    log, so a long run is never silent and a timeout shows where it was.
    Messages carry counts and phase names only -- never match ids, PUUIDs,
    Riot IDs or secrets. Timings are kept here, outside the deterministic
    report JSON."""

    def __init__(self, emit: Callable[[str], None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.emit = emit
        self.clock = clock
        self.start = clock()
        self.events: list[tuple[float, str]] = []

    def __call__(self, message: str) -> None:
        elapsed = self.clock() - self.start
        self.events.append((elapsed, message))
        if self.emit is not None:
            self.emit(f"[progress +{elapsed:9.1f}s] {message}")


def _noop(message: str) -> None:
    return None


# ---------------------------------------------------------------- canonical names


class Names:
    """Display names from committed metadata only. A missing mapping is
    reported as `UNRESOLVED: <raw id>`, never guessed."""

    def __init__(self) -> None:
        roster = load_roster()
        self.champions = roster.champions
        self.traits = roster.traits
        self.items = json.loads(ITEM_STATS_PATH.read_text()).get("items") or {}
        self.unresolved: set[str] = set()

    def champion(self, cid: str) -> str:
        entry = self.champions.get(cid)
        if entry:
            return entry["name"]
        self.unresolved.add(cid)
        return f"UNRESOLVED: {cid}"

    def item(self, iid: str) -> str:
        entry = self.items.get(iid)
        if entry and entry.get("name"):
            return entry["name"]
        self.unresolved.add(iid)
        return f"UNRESOLVED: {iid}"

    def trait(self, tid: str) -> str:
        name = self.traits.get(tid)
        if name:
            return name
        self.unresolved.add(tid)
        return f"UNRESOLVED: {tid}"


# ---------------------------------------------------------------- boards


@dataclass(frozen=True)
class Unit:
    cid: str
    tier: int
    stored_cost: int | None
    items: tuple[str, ...]  # completed (non-component) items, sorted
    raw_items: tuple[str, ...]
    carry_qualified: bool  # current tftlab rule; descriptive only


@dataclass
class Board:
    obs: int  # anonymous observation id (position in the sorted population)
    key: tuple[str, int]  # (match_id, participant_index); internal, never exported
    placement: int
    level: int
    units: tuple[Unit, ...]
    traits: dict[str, int]  # active trait id -> tier_current
    shop: tuple[Unit, ...] = ()
    excluded: tuple[str, ...] = ()  # non-shop ids left out of identity

    @property
    def identity(self) -> frozenset[str]:
        return frozenset(u.cid for u in self.shop)

    @property
    def trait_keys(self) -> frozenset[str]:
        return frozenset(f"{t}:{tier}" for t, tier in self.traits.items())


def roster_cost(cid: str, stored_cost: int | None, champions: Mapping[str, Mapping[str, Any]]) -> int | None:
    entry = champions.get(cid)
    return int(entry["cost"]) if entry else stored_cost


#: Committed art manifest (`tftlab.game_art_refresh.select_assets`): its
#: champion list is every roster unit with cost 1-5 AND at least one trait in
#: the CommunityDragon feed -- the set's shop champions plus trait-bearing
#: specials such as the Lux forms -- while jungle camps, anvils, the training
#: dummy and other trait-less entities (some of which the roster lists at
#: cost 1) are left out.
MANIFEST_PATH = Path(__file__).parent / "data" / "game_art_manifest.json"


def identity_champions() -> frozenset[str]:
    return frozenset((json.loads(MANIFEST_PATH.read_text()).get("champions") or {}))


def is_identity_unit(cid: str, stored_cost: int | None, champions: Mapping[str, Mapping[str, Any]],
                     identity: frozenset[str]) -> bool:
    """Structural identity membership, decided by committed metadata only:
    - a roster id counts iff it is in the manifest's trait-bearing cost 1-5
      champion list (so roster summons / camps / anvils are excluded);
    - an id the roster does not know cannot be justified as non-shop, so it is
      kept under its raw id (reported UNRESOLVED, never mapped to anything)
      when its stored shop cost is 1-5."""
    if cid in champions:
        return cid in identity
    return stored_cost is not None and 1 <= stored_cost <= 5


def normalize_board(obs: int, key: tuple[str, int], placement: int, level: int, units: Iterable[Unit],
                    traits: Mapping[str, int], champions: Mapping[str, Mapping[str, Any]],
                    identity: frozenset[str] | None = None) -> Board:
    identity = identity_champions() if identity is None else identity
    ordered = tuple(units)
    keep = [is_identity_unit(u.cid, u.stored_cost, champions, identity) for u in ordered]
    shop = tuple(u for u, k in zip(ordered, keep) if k)
    excluded = tuple(sorted(u.cid for u, k in zip(ordered, keep) if not k))
    return Board(obs, key, placement, level, ordered, dict(sorted(traits.items())), shop, excluded)


def _window_scope(alias: str) -> str:
    return f"JOIN matches m ON m.match_id = {alias}.match_id WHERE m.balance_window = ? AND m.queue_id = ?"


def load_population(db: Database, balance_window: str) -> tuple[dict[str, Any], list[Board]]:
    """The exact analysis population and its boards. SELECT-only."""
    params = (balance_window, RANKED_TFT_QUEUE_ID)
    q1 = lambda sql, p=(): db.query_one(sql, p)[0]  # noqa: E731
    all_matches = int(q1("SELECT COUNT(*) FROM matches WHERE balance_window = ?", (balance_window,)))
    ranked_matches = int(q1("SELECT COUNT(*) FROM matches m WHERE m.balance_window = ? AND m.queue_id = ?", params))
    participants = db.query_all(
        f"SELECT p.match_id, p.participant_index, p.placement, p.level FROM participants p {_window_scope('p')}", params
    )
    unit_rows = db.query_all(
        "SELECT u.match_id, u.participant_index, u.unit_index, u.character_id, u.cost, u.tier, u.items_json "
        f"FROM units u {_window_scope('u')}",
        params,
    )
    trait_rows = db.query_all(
        f"SELECT t.match_id, t.participant_index, t.trait_name, t.style, t.tier_current FROM traits t {_window_scope('t')}",
        params,
    )
    # Same Ranked population as the boards: validate's own source-empty
    # semantics, scoped to this window AND the Ranked queue.
    source_empty, unexpected = classify_participants_without_units(
        db, balance_window=balance_window, queue_id=RANKED_TFT_QUEUE_ID
    )

    units: dict[tuple[str, int], list[tuple[int, Unit]]] = defaultdict(list)
    for mid, pidx, uidx, cid, cost, tier, items_json in unit_rows:
        raw = tuple(i for i in json.loads(items_json) if i)
        units[(str(mid), int(pidx))].append((int(uidx), Unit(
            cid=str(cid),
            tier=int(tier or 1),
            stored_cost=int(cost) if cost is not None else None,
            items=tuple(sorted(i for i in raw if not is_component(i))),
            raw_items=raw,
            carry_qualified=carry.is_carry_observation(raw),
        )))
    traits: dict[tuple[str, int], dict[str, int]] = defaultdict(dict)
    for mid, pidx, name, style, tier_current in trait_rows:
        if (tier_current or 0) >= 1 and (style or 0) >= 1:
            traits[(str(mid), int(pidx))][str(name)] = int(tier_current)

    champions = load_roster().champions
    identity = identity_champions()
    observable = sorted((str(m), int(p), int(pl), int(lv or 0)) for m, p, pl, lv in participants if (str(m), int(p)) in units)
    boards = [
        normalize_board(obs, (m, p), pl, lv, (u for _, u in sorted(units[(m, p)], key=lambda x: x[0])), traits.get((m, p), {}), champions, identity)
        for obs, (m, p, pl, lv) in enumerate(observable)
    ]
    without_units = len(participants) - len(boards)
    population = {
        "balance_window": balance_window,
        "queue_id": RANKED_TFT_QUEUE_ID,
        "window_matches_any_queue": all_matches,
        "excluded_non_ranked_matches": all_matches - ranked_matches,
        "ranked_matches": ranked_matches,
        "ranked_participants": len(participants),
        "ranked_unit_observable_participants": len(boards),
        "ranked_participants_without_units": without_units,
        "ranked_source_empty_participants": int(source_empty),
        "ranked_unexpected_participants_without_units": int(unexpected),
        # Every Ranked participant is either unit-observable or classified.
        "ranked_denominators_consistent": without_units == int(source_empty) + int(unexpected),
    }
    return population, boards


# ---------------------------------------------------------------- similarity


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str
    description: str
    weighted: bool = False
    trait_share: float = 0.0
    merge: str | None = None  # None | "structural" | "structure_aware"
    #: Merged-result similarity acceptance: "all_boards" (every member board
    #: >= tau against the merged profile; the A/B/C controls) or "s2" (the
    #: experimental bounded side-specific rule, `s2_conditions`).
    similarity_rule: str = "all_boards"
    #: Experimental strategies re-run only the variant merge, on the exact
    #: variants of this control (same leader pass and refinement inputs).
    variants_from: str | None = None


SIMILARITY_RULES = ("all_boards", "s2")

STRATEGIES: tuple[Strategy, ...] = (
    Strategy("A_structural_baseline", "A. structural baseline (control)",
             "Champion-set Ruzicka only (every shop unit weight 1). No item, carry, cost or trait information. No merge."),
    Strategy("B_flex_tolerant", "B. flex-tolerant structural",
             "A's similarity, then a structural variant merge: variants whose cores share >= max(3, 60% of the larger core) "
             "and differ by at most one core swap (pure additions allowed) become one group, if the merged group keeps a core "
             "of >= 5 units (or >= 60% of the smaller core) and every member board stays >= tau similar to the merged "
             "profile. Champion presence only.",
             merge="structural"),
    Strategy("C_structure_aware", "C. structure-aware",
             "Weighted Ruzicka (units with >= 2 completed items x2, un-itemized 1-star 4/5-cost units x0.5) blended 75/25 "
             "with active-trait Ruzicka, then B's merge restricted so a differing core unit must be flex (>= 15%) in the "
             "other variant or un-itemized in its own (a swapped itemized unit keeps groups apart; a single unit added to a "
             "fully shared core of >= 5 units may merge), with B's merged-result checks. Item COUNTS only; "
             "never the carry classifier.",
             weighted=True, trait_share=0.25, merge="structure_aware"),
    # EXPERIMENTAL (validation run #6): identical to their controls except the
    # merged-result similarity acceptance; not production-approved.
    Strategy("B_S2_experimental", "B-S2. EXPERIMENTAL: B with the S2 merged-result rule",
             "EXPERIMENTAL. B's variants (reused from B_flex_tolerant) and B's structural merge and merged-core check, but "
             "a tentative merge passes the similarity check under S2 instead of 'every board >= tau': no larger-side board "
             "below tau, <= 10% of the smaller side below tau, <= 1% of the merged group below tau, and every board >= "
             "tau - 0.10 (all against the tentative merged profile; thresholds frozen before validation run #5). Accepted "
             "merges really happen, so later candidates see the changed groups.",
             merge="structural", similarity_rule="s2", variants_from="B_flex_tolerant"),
    Strategy("C_S2_experimental", "C-S2. EXPERIMENTAL: C with the S2 merged-result rule",
             "EXPERIMENTAL. C's variants (reused from C_structure_aware), C's weighting, restricted merge, splash exception "
             "and merged-core check, but the similarity check is S2 (see B-S2) instead of 'every board >= tau'.",
             weighted=True, trait_share=0.25, merge="structure_aware", similarity_rule="s2",
             variants_from="C_structure_aware"),
)


def unit_weight(u: Unit, strategy: Strategy, config: ArchetypeConfig, champions) -> float:
    if not strategy.weighted:
        return 1.0
    if len(u.items) >= 2:
        return config.itemized_weight
    if (roster_cost(u.cid, u.stored_cost, champions) or 0) >= 4 and not u.items and u.tier == 1:
        return config.splash_weight
    return 1.0


def board_vectors(board: Board, strategy: Strategy, config: ArchetypeConfig, champions) -> tuple[dict[str, float], dict[str, float]]:
    uv: dict[str, float] = {}
    for u in board.shop:  # duplicate copies: one identity entry, the heavier weight
        uv[u.cid] = max(uv.get(u.cid, 0.0), unit_weight(u, strategy, config, champions))
    tv = {k: 1.0 for k in sorted(board.trait_keys)} if strategy.trait_share else {}
    return uv, tv


def ruzicka(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Weighted Jaccard: sum(min) / sum(max). Keys iterate in sorted order so
    float sums are identical across processes."""
    num = den = 0.0
    for k in sorted(a.keys() | b.keys()):
        x, y = a.get(k, 0.0), b.get(k, 0.0)
        num += min(x, y)
        den += max(x, y)
    return num / den if den else 0.0


def similarity(board_vec, profile, strategy: Strategy) -> float:
    s = ruzicka(board_vec[0], profile[0])
    if strategy.trait_share:
        s = (1 - strategy.trait_share) * s + strategy.trait_share * ruzicka(board_vec[1], profile[1])
    return s


def mean_profile(vectors: Sequence[tuple[dict[str, float], dict[str, float]]]) -> tuple[dict[str, float], dict[str, float]]:
    n = len(vectors)
    units: dict[str, float] = defaultdict(float)
    traits: dict[str, float] = defaultdict(float)
    for uv, tv in vectors:
        for k in sorted(uv):
            units[k] += uv[k]
        for k in sorted(tv):
            traits[k] += tv[k]
    return ({k: units[k] / n for k in sorted(units)}, {k: traits[k] / n for k in sorted(traits)})


# ---------------------------------------------------------------- grouping


@dataclass
class Grouping:
    variant: dict[int, int]  # obs -> variant id (fine clusters)
    group: dict[int, int]  # obs -> group id (after merge; == variant for A)
    log: list[str]
    converged: bool
    refine_moves: list[int]
    merges: int
    prune_audit: dict[str, Any] = field(default_factory=dict)
    merge_checks: dict[str, int] = field(default_factory=dict)
    merge_diagnostics: dict[str, Any] = field(default_factory=dict)
    #: Experimental S2 strategies only (report only): the recursive lock-in
    #: summary of the real merge and the merge-order replays.
    lock_in: dict[str, Any] = field(default_factory=dict)
    order_replays: list[dict[str, Any]] = field(default_factory=list)


#: Leader-pass progress line every this many boards (status only).
LEADER_PROGRESS_EVERY = 2000


def _candidates(uv: Mapping[str, float], index: Mapping[str, set[int]], min_shared: int) -> list[int]:
    hits = Counter(g for u in uv for g in index.get(u, ()))
    return sorted(g for g, shared in hits.items() if shared >= min_shared)


def _best(vec, profiles, candidates: Iterable[int], strategy: Strategy) -> tuple[int | None, float]:
    best, best_s = None, -1.0
    for g in candidates:
        s = similarity(vec, profiles[g], strategy)
        if s > best_s:  # candidates are sorted: ties go to the lowest id
            best, best_s = g, s
    return best, best_s


def _index(profiles: Sequence[tuple[dict, dict]], presence: float) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for g, (units, _) in enumerate(profiles):
        for u, f in units.items():
            if f >= presence:
                index[u].add(g)
    return index


def _moves(old: Mapping[int, int], new: Mapping[int, int]) -> int:
    """Boards that changed group between passes (ids are not comparable, so
    each new group is matched to the old group most of its members came from)."""
    moved = len(set(old) ^ set(new))
    members: dict[int, list[int]] = defaultdict(list)
    for k, g in new.items():
        if k in old:
            members[g].append(k)
    for ks in members.values():
        moved += len(ks) - Counter(old[k] for k in ks).most_common(1)[0][1]
    return moved


def _renumber(assign: Mapping[int, int], vecs: Mapping[int, tuple]) -> dict[int, int]:
    """Stable ids: size descending, then the sorted ids of units on >= half the boards."""
    members: dict[int, list[int]] = defaultdict(list)
    for k, g in assign.items():
        members[g].append(k)

    def order_key(g: int) -> tuple:
        freq = Counter(u for k in members[g] for u in vecs[k][0])
        return (-len(members[g]), tuple(sorted(u for u, c in freq.items() if c * 2 >= len(members[g]))), min(members[g]))

    remap = {g: i for i, g in enumerate(sorted(members, key=order_key))}
    return {k: remap[g] for k, g in sorted(assign.items())}


def structural_order_key(board: Board, vec: tuple[Mapping[str, float], Mapping[str, float]]) -> tuple:
    """Leader-pass order from board STRUCTURE only: more identity units
    first (more complete boards seed groups), then the strategy's own board
    vector (sorted unit weights, then active trait keys). Never placement,
    Top 4, win, carry qualification or item performance. The observation id
    only breaks ties between boards whose vectors are identical, and those
    are interchangeable: every similarity, profile (a mean of vectors) and
    merge statistic depends on the vectors alone, so which of them comes
    first cannot change any board's final group."""
    units, traits = vec
    return (-len(board.identity), tuple(sorted(units.items())), tuple(sorted(traits)), board.obs)


def cluster(boards: Sequence[Board], strategy: Strategy, config: ArchetypeConfig,
            progress: Callable[[str], None] | None = None) -> Grouping:
    """Deterministic leader pass + profile refinement (+ merge for B/C).

    Leader pass: boards are visited in a purely STRUCTURAL order
    (`structural_order_key`: more identity units first, then the board's own
    similarity vector); placement and every other outcome play no part, so
    archetype membership is invariant to outcome. Each board joins its most
    similar group (>= tau) or starts one. Refinement: recompute mean profiles, drop groups under
    min_group_size, reassign every board to its most similar profile (>= tau,
    else ungrouped); repeat until at most convergence_moved_share of boards
    move or max_refine_iterations is reached (reported either way).

    `progress` only receives status lines (counts, phase names); it has no
    influence on the grouping."""
    progress = progress or _noop
    champions = load_roster().champions
    eligible = [b for b in boards if len(b.identity) >= config.min_identity_units]
    vecs = {b.obs: board_vectors(b, strategy, config, champions) for b in eligible}
    order = sorted(eligible, key=lambda b: structural_order_key(b, vecs[b.obs]))
    log: list[str] = []

    members: list[list[int]] = []
    profiles: list[tuple[dict, dict]] = []
    index: dict[str, set[int]] = defaultdict(set)
    assign: dict[int, int] = {}
    progress(f"{strategy.name}: leader pass started over {len(eligible)} eligible boards")
    for i, b in enumerate(order):
        if i and i % LEADER_PROGRESS_EVERY == 0:
            progress(f"{strategy.name}: leader pass {i}/{len(order)} boards, {len(members)} groups so far")
        g, s = _best(vecs[b.obs], profiles, _candidates(vecs[b.obs][0], index, config.prune_min_shared), strategy)
        if g is None or s < config.tau:
            members.append([])
            profiles.append(({}, {}))
            g = len(members) - 1
        members[g].append(b.obs)
        profiles[g] = mean_profile([vecs[k] for k in members[g]])
        for u, f in profiles[g][0].items():
            (index[u].add if f >= config.prune_presence else index[u].discard)(g)
        assign[b.obs] = g
    log.append(f"leader pass: {len(members)} groups over {len(eligible)} eligible boards")
    progress(f"{strategy.name}: leader pass completed, {len(members)} groups")

    converged, moves = False, []
    threshold = max(1, math.floor(config.convergence_moved_share * len(eligible)))
    for it in range(config.max_refine_iterations):
        grouped: dict[int, list[int]] = defaultdict(list)
        for k, g in assign.items():
            grouped[g].append(k)
        kept = sorted(g for g, ks in grouped.items() if len(ks) >= config.min_group_size)
        profiles = [mean_profile([vecs[k] for k in grouped[g]]) for g in kept]
        index = _index(profiles, config.prune_presence)
        new: dict[int, int] = {}
        for b in order:
            g, s = _best(vecs[b.obs], profiles, _candidates(vecs[b.obs][0], index, config.prune_min_shared), strategy)
            if g is not None and s >= config.tau:
                new[b.obs] = g
        moved = _moves(assign, new)
        moves.append(moved)
        assign = _renumber(new, vecs)
        log.append(f"refine {it + 1}: {len(set(assign.values()))} groups, {len(assign)} boards assigned, {moved} moved")
        progress(f"{strategy.name}: refine {it + 1} completed, {len(kept)} candidate groups, "
                 f"{len(set(assign.values()))} groups, {len(assign)} boards assigned, {moved} moved")
        if moved <= threshold:
            converged = True
            break
    # final pass: singleton groups (possible after the last reassignment) are ungrouped
    sizes = Counter(assign.values())
    assign = _renumber({k: g for k, g in assign.items() if sizes[g] >= config.min_group_size}, vecs)
    return _merge_and_audit(eligible, vecs, assign, log, converged, moves, strategy, config, progress)


def cluster_reusing_variants(boards: Sequence[Board], strategy: Strategy, config: ArchetypeConfig, base: Grouping,
                             progress: Callable[[str], None] | None = None) -> Grouping:
    """Experimental strategies: the leader pass and refinement depend only on
    the board vectors (strategy.weighted / trait_share) and the config, so the
    control named by `strategy.variants_from` already produced exactly these
    variants; only the variant merge (and its audit) is run again."""
    progress = progress or _noop
    champions = load_roster().champions
    eligible = [b for b in boards if len(b.identity) >= config.min_identity_units]
    vecs = {b.obs: board_vectors(b, strategy, config, champions) for b in eligible}
    assign = dict(base.variant)
    log = [line for line in base.log if not line.startswith("variant merge")]
    progress(f"{strategy.name}: variants reused from {strategy.variants_from} (identical leader pass and refinement "
             f"inputs), {len(set(assign.values()))} variants")
    return _merge_and_audit(eligible, vecs, assign, log, base.converged, list(base.refine_moves), strategy, config, progress)


def _merge_and_audit(eligible: Sequence[Board], vecs, assign: dict[int, int], log: list[str], converged: bool,
                     moves: list[int], strategy: Strategy, config: ArchetypeConfig,
                     progress: Callable[[str], None]) -> Grouping:
    group, merges, checks, diagnostics = dict(assign), 0, {}, None
    if strategy.merge:
        progress(f"{strategy.name}: variant merge started over {len(set(assign.values()))} variants")
        diagnostics = MergeDiagnostics(config.tau, lock_in=strategy.similarity_rule == "s2")
        to_group, merge_log, checks = merge_variants([b for b in eligible if b.obs in assign], assign, strategy, config,
                                                     vecs=vecs, diagnostics=diagnostics)
        group = {k: to_group[v] for k, v in assign.items()}
        merges = len(merge_log)
        rule = "" if strategy.similarity_rule == "all_boards" else f" ({strategy.similarity_rule.upper()} rule)"
        log.append(f"variant merge: {merges} merges, {len(set(assign.values()))} variants -> {len(set(group.values()))} groups; "
                   f"tentative merges rejected: {checks['rejected_core']} core, {checks['rejected_similarity']} similarity{rule}")
        progress(f"{strategy.name}: variant merge completed, {merges} merges, {len(set(group.values()))} groups, "
                 f"rejected {checks['rejected_core']} core / {checks['rejected_similarity']} similarity{rule}")
    decision_rule = "S2" if strategy.similarity_rule == "s2" else "S0"
    result = Grouping(variant=assign, group=group, log=log, converged=converged, refine_moves=moves, merges=merges,
                      merge_checks=checks,
                      merge_diagnostics=diagnostics.summary(decision_rule) if diagnostics else {})
    if strategy.merge and strategy.similarity_rule == "s2":  # report only; the real grouping above is final
        result.lock_in = recursive_lock_in_summary(diagnostics)
        result.order_replays = order_replays(eligible, vecs, assign, strategy, config, progress)
    progress(f"{strategy.name}: prune audit started")
    result.prune_audit = prune_audit(eligible, vecs, assign, strategy, config)
    progress(f"{strategy.name}: prune audit completed, {result.prune_audit['disagreements']} disagreements "
             f"in {result.prune_audit['boards_audited']} boards")
    return result


def prune_audit(eligible: Sequence[Board], vecs, assign: Mapping[int, int], strategy: Strategy,
                config: ArchetypeConfig) -> dict[str, Any]:
    """Re-score a deterministic sample of boards against EVERY final variant
    profile and count how often the prune chose differently."""
    grouped: dict[int, list[int]] = defaultdict(list)
    for k, g in assign.items():
        grouped[g].append(k)
    gids = sorted(grouped)
    profiles = {g: mean_profile([vecs[k] for k in grouped[g]]) for g in gids}
    index: dict[str, set[int]] = defaultdict(set)
    for g in gids:
        for u, f in profiles[g][0].items():
            if f >= config.prune_presence:
                index[u].add(g)
    step = max(1, len(eligible) // max(1, config.prune_audit_boards))
    sample = [b for b in sorted(eligible, key=lambda b: b.obs)][::step][: config.prune_audit_boards]
    disagreements = 0
    for b in sample:
        pruned = _best(vecs[b.obs], profiles, _candidates(vecs[b.obs][0], index, config.prune_min_shared), strategy)
        brute = _best(vecs[b.obs], profiles, gids, strategy)
        p = pruned[0] if pruned[0] is not None and pruned[1] >= config.tau else None
        q = brute[0] if brute[0] is not None and brute[1] >= config.tau else None
        disagreements += p != q
    return {"boards_audited": len(sample), "disagreements": disagreements}


#: Merge-diagnostic magnitude buckets (research only, fixed before any real
#: run): distances from tau and similarity changes, fine near zero so a
#: 0.5999-vs-0.60 miss is told apart from a 0.45-vs-0.60 one.
DIAGNOSTIC_EDGES: tuple[float, ...] = (0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1)
MAGNITUDE_LABELS: tuple[str, ...] = (
    *(f"<= {hi:g}" if lo == 0.0 else f"({lo:g}, {hi:g}]" for lo, hi in zip((0.0, *DIAGNOSTIC_EDGES), DIAGNOSTIC_EDGES)),
    f"> {DIAGNOSTIC_EDGES[-1]:g}")
BELOW_TAU_LABELS: tuple[str, ...] = ("0", "exactly 1", "2+, <= 1%", "2+, (1%, 5%]", "2+, (5%, 10%]", "2+, > 10%")
OVERLAP_LABELS: tuple[str, ...] = ("identical cores", ">= 0.8", "[0.6, 0.8)", "< 0.6")
PRE_MARGIN_LABELS: tuple[str, ...] = ("already below tau", *MAGNITUDE_LABELS)
DEGRADATION_LABELS: tuple[str, ...] = ("no drop (>= 0)", *(f"drop {m}" for m in MAGNITUDE_LABELS))
#: Deterministic review sample: up to this many new attempts per reason.
DIAGNOSTIC_SAMPLE_PER_REASON = 2
MERGE_DIAGNOSTIC_DEFINITIONS: dict[str, str] = {
    "scope": "Every tentative merge that reached the merged-result similarity check (strategies B and C): "
             "outcome 'accepted' or 'rejected_similarity'. Measurement only; no decision depends on it.",
    "sides": "a = the group with the lower id (ids are variant ids; the lower one absorbs b if accepted), b = the other; "
             "*_root_variant/*_variants: its lowest variant id / number of variants merged into it so far.",
    "cores": "the merge's own core definition (units on >= core_presence of the side's boards); merged_core over a+b; "
             "core_overlap = |shared| / |union| of the two cores (the merge's pair score).",
    "pre_merge_similarity": "each board's similarity to the mean profile of its own side (a or b) as it stood just "
                            "before this attempt, with the strategy's similarity function.",
    "post_merge_similarity": "each board's similarity to the mean profile of a+b (the profile the rule checks).",
    "below_tau": "boards with post-merge similarity < tau (the rule rejects if there is at least one).",
    "weakest": "the board with the lowest post-merge similarity (ties: lowest anonymous observation id); "
               "delta = post - pre; shortfall_below_tau = tau - post (> 0: below tau); pre_margin_above_tau = pre - tau.",
    "below_tau_count_buckets": "'0'; 'exactly 1' (whatever the share); otherwise 2+ boards by share of merged boards: "
                               "<= 1%, (1%, 5%], (5%, 10%], > 10%.",
    "weakest_distance_from_tau_buckets": "rejected: tau - weakest post-merge similarity; accepted: weakest post-merge "
                                         "similarity - tau (edges %s)." % (DIAGNOSTIC_EDGES,),
    "weakest_pre_merge_margin_buckets": "weakest board's pre-merge similarity - tau ('already below tau' if negative).",
    "weakest_degradation_buckets": "weakest board's post - pre similarity: 'no drop' if >= 0, else the drop's size.",
    "core_overlap_buckets": "'identical cores' (equal core sets), else core_overlap >= 0.8, [0.6, 0.8), < 0.6.",
    "merged_size_bands": "tentative merged board count, in the report's size bands.",
    "percentiles": "nearest rank on sorted values, as elsewhere in this report; p01 only when >= 100 boards.",
}


def _magnitude_bucket(x: float) -> str:
    for label, hi in zip(MAGNITUDE_LABELS, DIAGNOSTIC_EDGES):
        if x <= hi:
            return label
    return MAGNITUDE_LABELS[-1]


def _below_tau_bucket(below: int, n: int) -> str:
    if below <= 1:
        return BELOW_TAU_LABELS[below]
    share = below / n
    return next(label for label, hi in zip(BELOW_TAU_LABELS[2:], (0.01, 0.05, 0.10, math.inf)) if share <= hi)


def _overlap_bucket(r: Mapping[str, Any]) -> str:
    if r["identical_cores"]:
        return OVERLAP_LABELS[0]
    o = r["core_overlap"]
    return OVERLAP_LABELS[1] if o >= 0.8 else OVERLAP_LABELS[2] if o >= 0.6 else OVERLAP_LABELS[3]


def _similarity_summary(values: Iterable[float]) -> dict[str, Any]:
    xs = sorted(values)
    q = lambda p: xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]  # noqa: E731
    return {"boards": len(xs), "min": xs[0], "p01": q(0.01) if len(xs) >= 100 else None, "p05": q(0.05),
            "p10": q(0.10), "median": q(0.5)}


class MergeDiagnostics:
    """Research-only measurements of tentative variant merges judged by the
    merged-result similarity rule. `record` is called by `merge_variants`
    after each decision; nothing here is read back by the merge. Rows hold
    unit ids and anonymous observation ids only -- never match or player
    identifiers."""

    def __init__(self, tau: float, *, lock_in: bool = False) -> None:
        self.tau = tau
        self.rows: list[dict[str, Any]] = []
        #: Recursive lock-in instrumentation (report only; experimental S2
        #: strategies only -- `lock_in` stays False for the A/B/C controls,
        #: whose diagnostics then hold no lock-in state at all). Kept OUT of
        #: `rows` so every existing diagnostic and sample is unchanged.
        #: `lock[i]` describes `rows[i]`; `provenance` maps an anonymous
        #: observation id to its FIRST below-tau admission by an accepted
        #: merge; `admissions` lists every such admission event.
        self.lock_in = lock_in
        self.lock: list[dict[str, Any]] = []
        self.provenance: dict[int, dict[str, Any]] = {}
        self.admissions: list[dict[str, Any]] = []

    def record(self, *, accepted: bool, a: int, b: int, variants_a: int, variants_b: int, core_a: set[str],
               core_b: set[str], merged_core: set[str], overlap: float, splash: bool, pre: Mapping[int, float],
               post: Mapping[int, float], side: Mapping[int, str],
               evaluate: Callable[[Sequence[int]], Mapping[int, float]] | None = None) -> None:
        tau = self.tau
        obs = sorted(post)
        n_a = sum(1 for k in obs if side[k] == "a")
        below = [k for k in obs if post[k] < tau]
        shortfalls = sorted(tau - post[k] for k in below)
        weakest = min(obs, key=lambda k: (post[k], k))
        n = {"a": n_a, "b": len(obs) - n_a}
        larger = "a" if n["a"] >= n["b"] else "b"  # tie: a, the lower id (as `_failing_sides`)
        smaller = "b" if larger == "a" else "a"
        side_below = {s: sum(1 for k in below if side[k] == s) for s in ("a", "b")}
        side_min = {s: min(post[k] for k in obs if side[k] == s) for s in ("a", "b")}
        self.rows.append({
            "attempt": len(self.rows), "outcome": "accepted" if accepted else "rejected_similarity",
            "a_root_variant": a, "b_root_variant": b, "a_variants": variants_a, "b_variants": variants_b,
            "a_boards": n_a, "b_boards": len(obs) - n_a, "merged_boards": len(obs),
            "core_a": sorted(core_a), "core_b": sorted(core_b), "merged_core": sorted(merged_core),
            "core_a_size": len(core_a), "core_b_size": len(core_b), "merged_core_size": len(merged_core),
            "shared_core_size": len(core_a & core_b), "core_overlap": overlap, "identical_cores": core_a == core_b,
            "a_only": sorted(core_a - core_b), "b_only": sorted(core_b - core_a),
            "dropped_from_merged_core": sorted((core_a | core_b) - merged_core),
            "new_in_merged_core": sorted(merged_core - (core_a | core_b)),
            "splash_exception_applies": splash,
            "pre_a": _similarity_summary(pre[k] for k in obs if side[k] == "a"),
            "pre_b": _similarity_summary(pre[k] for k in obs if side[k] == "b"),
            "pre_combined": _similarity_summary(pre[k] for k in obs),
            "post": _similarity_summary(post[k] for k in obs),
            "below_tau": len(below), "below_tau_share": len(below) / len(obs),
            "below_tau_from_a": sum(1 for k in below if side[k] == "a"),
            "below_tau_from_b": sum(1 for k in below if side[k] == "b"),
            "shortfall_max": shortfalls[-1] if shortfalls else None,
            "shortfall_mean": statistics.fmean(shortfalls) if shortfalls else None,
            "shortfall_median": shortfalls[(len(shortfalls) - 1) // 2] if shortfalls else None,
            "weakest": {"side": side[weakest], "observation": weakest, "pre_merge_similarity": pre[weakest],
                        "post_merge_similarity": post[weakest], "delta": post[weakest] - pre[weakest], "tau": tau,
                        "shortfall_below_tau": tau - post[weakest], "pre_margin_above_tau": pre[weakest] - tau},
            "larger_side": larger, "larger_side_boards": n[larger], "smaller_side_boards": n[smaller],
            "larger_side_below_tau": side_below[larger], "smaller_side_below_tau": side_below[smaller],
            "larger_side_below_tau_share": side_below[larger] / n[larger],
            "smaller_side_below_tau_share": side_below[smaller] / n[smaller],
            "larger_side_min_post": side_min[larger], "smaller_side_min_post": side_min[smaller],
        })
        if self.lock_in:
            self.lock.append(self._lock_entry(self.rows[-1], obs, post, side, larger, evaluate))

    def _lock_entry(self, r: Mapping[str, Any], obs: Sequence[int], post: Mapping[int, float], side: Mapping[int, str],
                    larger: str, evaluate: Callable[[Sequence[int]], Mapping[int, float]] | None) -> dict[str, Any]:
        """Recursive lock-in measurement of one judged attempt (report only).
        `historical` = boards an EARLIER accepted merge admitted while below
        tau (provenance is updated only after this attempt is described, so
        a board admitted by this very merge is not historical here)."""
        tau = self.tau
        smaller = "b" if larger == "a" else "a"
        hist = self.provenance
        on = {s: [k for k in obs if side[k] == s] for s in ("a", "b")}
        larger_below = [k for k in on[larger] if post[k] < tau]
        failed = [name for name, ok in s2_conditions(
            larger_below=r["larger_side_below_tau"], smaller_below=r["smaller_side_below_tau"],
            smaller_boards=r["smaller_side_boards"], merged_below=r["below_tau"], merged_boards=r["merged_boards"],
            min_post=r["post"]["min"], tau=tau).items() if not ok]
        below_hist = sum(1 for k in larger_below if k in hist)
        if not larger_below:
            attribution = C1_ATTRIBUTION[3]
        elif below_hist == len(larger_below):
            attribution = C1_ATTRIBUTION[0]
        else:
            attribution = C1_ATTRIBUTION[1] if below_hist else C1_ATTRIBUTION[2]
        entry: dict[str, Any] = {
            "attempt": r["attempt"], "outcome": r["outcome"], "larger_side": larger,
            "historical_tails_a": sum(1 for k in on["a"] if k in hist),
            "historical_tails_b": sum(1 for k in on["b"] if k in hist),
            "larger_side_historical_tails": sum(1 for k in on[larger] if k in hist),
            "smaller_side_historical_tails": sum(1 for k in on[smaller] if k in hist),
            "larger_side_below_tau_historical": below_hist,
            "larger_side_below_tau_not_historical": len(larger_below) - below_hist,
            "larger_side_historical_tails_at_or_above_tau": sum(1 for k in on[larger] if k in hist and post[k] >= tau),
            "failed_conditions": failed,
            "condition1_attribution": attribution,
            "admitted_below_tau": 0, "newly_admitted_below_tau": 0,
            "counterfactual": None,
        }
        if r["outcome"] == "accepted":
            admitted = [k for k in obs if post[k] < tau]
            entry["admitted_below_tau"] = len(admitted)
            for k in admitted:
                event = {"observation": k, "attempt": r["attempt"], "side": side[k],
                         "side_role": "larger" if side[k] == larger else "smaller", "similarity": post[k], "tau": tau}
                self.admissions.append(event)
                if k not in hist:
                    hist[k] = event
                    entry["newly_admitted_below_tau"] += 1
        elif entry["larger_side_historical_tails"] and evaluate is not None:
            entry["counterfactual"] = self._tail_removal(r, on, larger, failed, evaluate)
        return entry

    def _tail_removal(self, r: Mapping[str, Any], on: Mapping[str, list[int]], larger: str, failed: Sequence[str],
                      evaluate: Callable[[Sequence[int]], Mapping[int, float]]) -> dict[str, Any]:
        """`historical_tail_removal_counterfactual` for one rejected attempt:
        drop from the CURRENT larger side every board an earlier accepted
        merge admitted below tau -- those boards and no others, however
        weak any other board is -- then recompute the tentative merged
        profile of (remaining larger side + unchanged smaller side) and the
        unchanged S2 conditions (sides re-derived by board count, ties to
        side a, exactly as the real rule derives them). Changes nothing."""
        tau, hist = self.tau, self.provenance
        smaller = "b" if larger == "a" else "a"
        removed = [k for k in on[larger] if k in hist]
        kept = {larger: [k for k in on[larger] if k not in hist], smaller: list(on[smaller])}
        before = {"merged_boards": r["merged_boards"], "larger_side_boards": r["larger_side_boards"],
                  "smaller_side_boards": r["smaller_side_boards"], "larger_side_below_tau": r["larger_side_below_tau"],
                  "smaller_side_below_tau": r["smaller_side_below_tau"], "merged_below_tau": r["below_tau"],
                  "min_similarity": r["post"]["min"], "failed_conditions": list(failed)}
        out: dict[str, Any] = {"removed_boards": len(removed), "removed_share_of_larger_side": len(removed) / len(on[larger]),
                               "before": before, "after": None, "evaluable": bool(kept[larger]), "passes": False}
        if not kept[larger]:  # every larger-side board was a historical tail: nothing left to merge with
            return out
        subset = sorted(kept["a"] + kept["b"])
        post = evaluate(subset)
        new_larger = "a" if len(kept["a"]) >= len(kept["b"]) else "b"
        new_smaller = "b" if new_larger == "a" else "a"
        below = {s: sum(1 for k in kept[s] if post[k] < tau) for s in ("a", "b")}
        conditions = s2_conditions(larger_below=below[new_larger], smaller_below=below[new_smaller],
                                   smaller_boards=len(kept[new_smaller]), merged_below=below["a"] + below["b"],
                                   merged_boards=len(subset), min_post=min(post.values()), tau=tau)
        out["after"] = {"merged_boards": len(subset), "larger_side_boards": len(kept[new_larger]),
                        "smaller_side_boards": len(kept[new_smaller]), "larger_side_below_tau": below[new_larger],
                        "smaller_side_below_tau": below[new_smaller], "merged_below_tau": below["a"] + below["b"],
                        "min_similarity": min(post.values()),
                        "failed_conditions": [name for name, ok in conditions.items() if not ok],
                        "sides_flipped": new_larger != larger}
        out["passes"] = all(conditions.values())
        return out

    def _aggregate(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        def counts(labels: Sequence[str], values: Iterable[str]) -> dict[str, int]:
            c = Counter(values)
            return {label: c[label] for label in labels}

        def distance(r: Mapping[str, Any]) -> str:
            return _magnitude_bucket(abs(r["weakest"]["shortfall_below_tau"]))

        def pre_margin(r: Mapping[str, Any]) -> str:
            m = r["weakest"]["pre_margin_above_tau"]
            return PRE_MARGIN_LABELS[0] if m < 0 else _magnitude_bucket(m)

        def degradation(r: Mapping[str, Any]) -> str:
            d = r["weakest"]["delta"]
            return DEGRADATION_LABELS[0] if d >= 0 else "drop " + _magnitude_bucket(-d)

        overlap = _overlap_bucket

        bands = [label for label, _, _ in SIZE_BANDS]
        below = [_below_tau_bucket(r["below_tau"], r["merged_boards"]) for r in rows]
        return {
            "attempts": len(rows),
            "below_tau_count_buckets": counts(BELOW_TAU_LABELS, below),
            "weakest_distance_from_tau_buckets": counts(MAGNITUDE_LABELS, map(distance, rows)),
            "weakest_pre_merge_margin_buckets": counts(PRE_MARGIN_LABELS, map(pre_margin, rows)),
            "weakest_degradation_buckets": counts(DEGRADATION_LABELS, map(degradation, rows)),
            "core_overlap_buckets": counts(OVERLAP_LABELS, map(overlap, rows)),
            "merged_size_bands": {band: {label: sum(1 for r, x in zip(rows, below) if size_band(r["merged_boards"]) == band
                                                    and x == label) for label in BELOW_TAU_LABELS} for band in bands},
            "below_tau_count_x_distance_from_tau": {
                label: counts(MAGNITUDE_LABELS, (distance(r) for r, x in zip(rows, below) if x == label))
                for label in BELOW_TAU_LABELS},
            "failing_boards_by_side": {
                "larger side only": sum(1 for r in rows if r["below_tau"] and self._failing_sides(r) == {"larger"}),
                "smaller side only": sum(1 for r in rows if r["below_tau"] and self._failing_sides(r) == {"smaller"}),
                "both sides": sum(1 for r in rows if self._failing_sides(r) == {"larger", "smaller"})},
            "splash_exception_attempts": sum(r["splash_exception_applies"] for r in rows),
            "quantiles": {
                "merged_boards": quantiles(r["merged_boards"] for r in rows),
                "larger_side_boards": quantiles(max(r["a_boards"], r["b_boards"]) for r in rows),
                "smaller_side_boards": quantiles(min(r["a_boards"], r["b_boards"]) for r in rows),
                "core_overlap": quantiles(r["core_overlap"] for r in rows),
                "below_tau": quantiles(r["below_tau"] for r in rows),
                "below_tau_share": quantiles(r["below_tau_share"] for r in rows),
                "weakest_post_merge_similarity": quantiles(r["weakest"]["post_merge_similarity"] for r in rows),
                "weakest_pre_merge_similarity": quantiles(r["weakest"]["pre_merge_similarity"] for r in rows),
                "weakest_delta": quantiles(r["weakest"]["delta"] for r in rows),
                "post_merge_p10": quantiles(r["post"]["p10"] for r in rows),
                "post_merge_median": quantiles(r["post"]["median"] for r in rows),
                "pre_merge_p10": quantiles(r["pre_combined"]["p10"] for r in rows),
                "pre_merge_median": quantiles(r["pre_combined"]["median"] for r in rows),
            },
        }

    @staticmethod
    def _failing_sides(r: Mapping[str, Any]) -> set[str]:
        larger = "a" if r["a_boards"] >= r["b_boards"] else "b"
        smaller = "b" if larger == "a" else "a"
        return {name for name, s in (("larger", larger), ("smaller", smaller)) if r[f"below_tau_from_{s}"]}

    def _sample(self, rejected: Sequence[Mapping[str, Any]], accepted: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Deterministic review sample, never by placement: for each reason
        (in this order) up to DIAGNOSTIC_SAMPLE_PER_REASON attempts not yet
        chosen; ties break on the attempt index (the merge's own order)."""
        n = lambda r: r["merged_boards"]  # noqa: E731
        reasons = [
            ("smallest threshold miss", rejected, lambda r: (r["weakest"]["shortfall_below_tau"], -n(r), r["attempt"])),
            ("largest threshold miss", rejected, lambda r: (-r["weakest"]["shortfall_below_tau"], r["attempt"])),
            ("exactly one board below tau", [r for r in rejected if r["below_tau"] == 1], lambda r: (-n(r), r["attempt"])),
            ("highest share below tau", rejected, lambda r: (-r["below_tau_share"], -n(r), r["attempt"])),
            ("largest merged group", rejected, lambda r: (-n(r), r["attempt"])),
            ("highest core overlap", rejected, lambda r: (-r["core_overlap"], -n(r), r["attempt"])),
            ("identical cores", [r for r in rejected if r["identical_cores"]], lambda r: (-n(r), r["attempt"])),
            ("full core + one unit (C splash exception)", [r for r in rejected if r["splash_exception_applies"]],
             lambda r: (-n(r), r["attempt"])),
            ("accepted: closest to tau", accepted, lambda r: (-r["weakest"]["shortfall_below_tau"], r["attempt"])),
            ("accepted: largest merged group", accepted, lambda r: (-n(r), r["attempt"])),
        ]
        chosen: dict[int, dict[str, Any]] = {}
        for reason, pool, key in reasons:
            for r in [r for r in sorted(pool, key=key) if r["attempt"] not in chosen][:DIAGNOSTIC_SAMPLE_PER_REASON]:
                chosen[r["attempt"]] = {"reason": reason, **r}
        return list(chosen.values())

    def summary(self, decision_rule: str = "S0") -> dict[str, Any]:
        """`decision_rule`: the shadow candidate that IS the strategy's real
        rule -- "S0" for the A/B/C controls (which also get the report-only
        shadow evaluation), "S2" for the experimental strategies (which get a
        consistency check and the accepted-merge trajectory instead)."""
        rejected = [r for r in self.rows if r["outcome"] == "rejected_similarity"]
        accepted = [r for r in self.rows if r["outcome"] == "accepted"]
        out = {"definitions": MERGE_DIAGNOSTIC_DEFINITIONS, "tau": self.tau, "attempts_evaluated": len(self.rows),
               "accepted_attempts": len(accepted), "rejected_similarity_attempts": len(rejected),
               "rejected": self._aggregate(rejected), "accepted": self._aggregate(accepted),
               "sample": self._sample(rejected, accepted)}
        if decision_rule == "S0":  # the A/B/C controls: legacy structure, no lock-in provenance
            return {**out, "shadow": shadow_evaluation(self.rows, self.tau)}
        # experimental S2: tail provenance joins the trajectory only when lock-in tracking was on
        lock = {e["attempt"]: e for e in self.lock} if decision_rule == "S2" and self.lock_in else None
        return {**out, "decision_rule": decision_rule,
                "decision_rule_mismatches": sum(
                    all(shadow_conditions(r, self.tau)[decision_rule].values()) != (r["outcome"] == "accepted")
                    for r in self.rows),
                "trajectory": merge_trajectory(accepted, self.tau, lock)}


# ---------------------------------------------------------------- shadow merge rules (report only)

#: Predeclared shadow candidates for validation run #5 (fixed before any real
#: run; never tuned on it). The shadow evaluation is report-only; the S2
#: thresholds below are ALSO the frozen thresholds of the experimental
#: *_S2_experimental strategies (`s2_conditions`), never of A/B/C.
SHADOW_CANDIDATES: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4")
SHADOW_FLOOR = 0.10  # every board >= tau - 0.10 (S1-S4)
SHADOW_MERGED_TAIL = 100  # merged group: at most 1% below tau, i.e. 100 * below <= merged boards
SHADOW_SMALLER_TAIL = 10  # smaller side: at most 10% below tau, i.e. 10 * below <= smaller-side boards
SHADOW_OVERLAP = 0.80  # S3 structural gate
SHADOW_MEDIAN_MARGIN = 0.05  # S4: post-merge median >= tau + 0.05
SMALLER_SIDE_SHARE_LABELS: tuple[str, ...] = ("0%", "(0, 10%]", "(10%, 25%]", "(25%, 50%]", "(50%, 100%)", "100%")
LARGER_SIDE_FAILURE_LABELS: tuple[str, ...] = ("none", "exactly one", "more than one")
DANGER_LABELS: tuple[str, ...] = (
    "A: whole smaller side below tau", "B: > 50% of smaller side below tau", "C: core overlap < 0.6",
    "D: both sides have boards below tau", "E: weakest board < tau - 0.10")
FAMILY_LABELS: tuple[str, ...] = (
    "A: S3-shaped bounded family tail", "B: identical cores", "C: C splash exception", "D: core overlap >= 0.8")
SHADOW_DIFFERENCES: tuple[tuple[str, str], ...] = (("S1", "S2"), ("S2", "S3"), ("S4", "S3"), ("S3", "S4"))
SHADOW_SAMPLE_PER_REASON = 2
SHADOW_DEFINITIONS: dict[str, str] = {
    "scope": "Every tentative B/C merge that reached the merged-result similarity check. Each candidate answers "
             "'would this rule have accepted THIS attempt?'. One-step counterfactual on the ACTUAL merge trajectory: "
             "a shadow acceptance changes no group, so later attempts are the ones the real (S0) rule produced; "
             "what a candidate would build if it had been the real rule is NOT simulated.",
    "sides": "larger_side / smaller_side by board count before the attempt; ties: side a (the lower group id). "
             "Not necessarily the 'incoming' variant: the merge does not define a direction.",
    "S0": "every board's post-merge similarity >= tau (the current rule; must equal the actual decision).",
    "S1": "<= 1% of the merged boards below tau (100 * below <= merged) AND every board >= tau - 0.10.",
    "S2": "no larger-side board below tau AND <= 10% of the smaller side below tau (10 * below <= smaller) AND "
          "<= 1% of the merged boards below tau AND every board >= tau - 0.10.",
    "S3": "S2 AND (core overlap >= 0.80 OR identical cores OR the Strategy C full-core-plus-one splash exception).",
    "S4": "post-merge p10 >= tau AND post-merge median >= tau + 0.05 AND every board >= tau - 0.10 "
          "(nearest-rank percentiles, as in the diagnostics).",
    "recovered": "actual outcome rejected (S0 rejects) but the candidate accepts; lost = actually accepted but the "
                 "candidate rejects.",
    "dangers": "A: every smaller-side board below tau; B: > 50% of the smaller side below tau; C: core overlap < 0.6 "
               "(identical cores have overlap 1.0); D: boards below tau on both sides; E: weakest board < tau - 0.10.",
    "families": "descriptive proxies, not ground truth. A: all S3 conditions; B: identical cores; C: C splash "
                "exception; D: core overlap >= 0.8.",
    "smaller_side_share_buckets": "share of smaller-side boards below tau: 0%, (0, 10%], (10%, 25%], (25%, 50%], "
                                  "(50%, 100%), 100%.",
    "differences": "recovered by X but not by Y, with the count of each Y condition those attempts fail "
                   "(an attempt can fail several).",
}


def s2_conditions(*, larger_below: int, smaller_below: int, smaller_boards: int, merged_below: int,
                  merged_boards: int, min_post: float, tau: float) -> dict[str, bool]:
    """The S2 rule's named conditions -- used by the report-only shadow S2
    AND by the experimental S2 strategies' real merge decision (one
    definition). Every count is against the TENTATIVE MERGED profile; sides
    by pre-merge board count (tie: side a, the lower group id)."""
    return {"larger side: no board below tau": larger_below == 0,
            "smaller side: <= 10% below tau": SHADOW_SMALLER_TAIL * smaller_below <= smaller_boards,
            "merged: <= 1% below tau": SHADOW_MERGED_TAIL * merged_below <= merged_boards,
            "every board >= tau - 0.10": min_post >= tau - SHADOW_FLOOR}


#: The S2 condition names, in `s2_conditions` order (labels only).
S2_CONDITION_NAMES: tuple[str, ...] = tuple(s2_conditions(larger_below=0, smaller_below=0, smaller_boards=1, merged_below=0,
                                                          merged_boards=1, min_post=1.0, tau=0.0))


def shadow_conditions(r: Mapping[str, Any], tau: float) -> dict[str, dict[str, bool]]:
    """Each candidate's named conditions for one diagnostics row; a candidate
    accepts when all of its conditions hold. Pure function of the row."""
    s2 = s2_conditions(larger_below=r["larger_side_below_tau"], smaller_below=r["smaller_side_below_tau"],
                       smaller_boards=r["smaller_side_boards"], merged_below=r["below_tau"],
                       merged_boards=r["merged_boards"], min_post=r["post"]["min"], tau=tau)
    floor, merged_tail = s2["every board >= tau - 0.10"], s2["merged: <= 1% below tau"]
    gate = r["core_overlap"] >= SHADOW_OVERLAP or r["identical_cores"] or r["splash_exception_applies"]
    return {
        "S0": {"every board >= tau": r["below_tau"] == 0},
        "S1": {"merged: <= 1% below tau": merged_tail, "every board >= tau - 0.10": floor},
        "S2": s2,
        "S3": {**s2, "core overlap >= 0.8, identical cores or C splash exception": gate},
        "S4": {"post-merge p10 >= tau": r["post"]["p10"] >= tau,
               "post-merge median >= tau + 0.05": r["post"]["median"] >= tau + SHADOW_MEDIAN_MARGIN,
               "every board >= tau - 0.10": floor},
    }


def _smaller_side_share_bucket(r: Mapping[str, Any]) -> str:
    below, n = r["smaller_side_below_tau"], r["smaller_side_boards"]
    if below == 0:
        return SMALLER_SIDE_SHARE_LABELS[0]
    if below == n:
        return SMALLER_SIDE_SHARE_LABELS[5]
    if 10 * below <= n:
        return SMALLER_SIDE_SHARE_LABELS[1]
    if 4 * below <= n:
        return SMALLER_SIDE_SHARE_LABELS[2]
    return SMALLER_SIDE_SHARE_LABELS[3] if 2 * below <= n else SMALLER_SIDE_SHARE_LABELS[4]


def _dangers(r: Mapping[str, Any], tau: float) -> list[str]:
    small_below, small_n = r["smaller_side_below_tau"], r["smaller_side_boards"]
    flags = [small_below == small_n, 2 * small_below > small_n, r["core_overlap"] < 0.6 and not r["identical_cores"],
             r["larger_side_below_tau"] > 0 and small_below > 0, r["post"]["min"] < tau - SHADOW_FLOOR]
    return [label for label, flag in zip(DANGER_LABELS, flags) if flag]


def _families(r: Mapping[str, Any], tau: float) -> list[str]:
    flags = [all(shadow_conditions(r, tau)["S3"].values()), r["identical_cores"], r["splash_exception_applies"],
             r["core_overlap"] >= 0.8]
    return [label for label, flag in zip(FAMILY_LABELS, flags) if flag]


def _counted(labels: Sequence[str], values: Iterable[str]) -> dict[str, int]:
    c = Counter(values)
    return {label: c[label] for label in labels}


def _shadow_profile(rows: Sequence[Mapping[str, Any]], tau: float) -> dict[str, Any]:
    """Structure of a set of attempts (a recovery set): shares, sides, overlap, size, similarity."""
    return {
        "attempts": len(rows),
        "below_tau_count_buckets": _counted(BELOW_TAU_LABELS, (_below_tau_bucket(r["below_tau"], r["merged_boards"])
                                                               for r in rows)),
        "smaller_side_share_buckets": _counted(SMALLER_SIDE_SHARE_LABELS, map(_smaller_side_share_bucket, rows)),
        "larger_side_failures": _counted(LARGER_SIDE_FAILURE_LABELS, (
            LARGER_SIDE_FAILURE_LABELS[min(2, r["larger_side_below_tau"])] for r in rows)),
        "core_overlap_buckets": _counted(OVERLAP_LABELS, map(_overlap_bucket, rows)),
        "merged_size_bands": _counted([label for label, _, _ in SIZE_BANDS], (size_band(r["merged_boards"]) for r in rows)),
        "dangers": _counted(DANGER_LABELS, (d for r in rows for d in _dangers(r, tau))),
        "families": _counted(FAMILY_LABELS, (f for r in rows for f in _families(r, tau))),
        "quantiles": {
            "weakest_post_merge_similarity": quantiles(r["post"]["min"] for r in rows),
            "post_merge_p10": quantiles(r["post"]["p10"] for r in rows),
            "post_merge_median": quantiles(r["post"]["median"] for r in rows),
            "merged_boards": quantiles(r["merged_boards"] for r in rows),
            "smaller_side_boards": quantiles(r["smaller_side_boards"] for r in rows),
            "smaller_side_below_tau_share": quantiles(r["smaller_side_below_tau_share"] for r in rows),
            "larger_side_below_tau_share": quantiles(r["larger_side_below_tau_share"] for r in rows),
            "core_overlap": quantiles(r["core_overlap"] for r in rows),
        },
    }


def _shadow_case(r: Mapping[str, Any], reason: str) -> dict[str, Any]:
    larger, smaller = r["larger_side"], "b" if r["larger_side"] == "a" else "a"
    return {
        "reason": reason, "attempt": r["attempt"], "outcome": r["outcome"],
        "merged_boards": r["merged_boards"], "larger_side_boards": r["larger_side_boards"],
        "smaller_side_boards": r["smaller_side_boards"], "core_larger": r[f"core_{larger}"],
        "core_smaller": r[f"core_{smaller}"], "larger_only": r[f"{larger}_only"], "smaller_only": r[f"{smaller}_only"],
        "core_overlap": r["core_overlap"], "identical_cores": r["identical_cores"],
        "splash_exception_applies": r["splash_exception_applies"], "below_tau": r["below_tau"],
        "below_tau_share": r["below_tau_share"], "larger_side_below_tau": r["larger_side_below_tau"],
        "smaller_side_below_tau": r["smaller_side_below_tau"],
        "smaller_side_below_tau_share": r["smaller_side_below_tau_share"],
        "larger_side_min_post": r["larger_side_min_post"], "smaller_side_min_post": r["smaller_side_min_post"],
        "post": r["post"],
    }


def _shadow_sample(recovered: Sequence[Mapping[str, Any]], tau: float) -> list[dict[str, Any]]:
    """Deterministic, never by placement: up to SHADOW_SAMPLE_PER_REASON new
    attempts per reason; ties break on the merge's attempt order."""
    n = lambda r: r["merged_boards"]  # noqa: E731
    reasons = [
        ("high core overlap (>= 0.8, not identical)", [r for r in recovered if r["core_overlap"] >= 0.8 and not r["identical_cores"]],
         lambda r: (-r["core_overlap"], -n(r), r["attempt"])),
        ("identical cores", [r for r in recovered if r["identical_cores"]], lambda r: (-n(r), r["attempt"])),
        ("C splash exception", [r for r in recovered if r["splash_exception_applies"]], lambda r: (-n(r), r["attempt"])),
        ("DANGER A: whole smaller side below tau", [r for r in recovered if DANGER_LABELS[0] in _dangers(r, tau)],
         lambda r: (-n(r), r["attempt"])),
        ("DANGER B: > 50% of smaller side below tau", [r for r in recovered if DANGER_LABELS[1] in _dangers(r, tau)],
         lambda r: (-n(r), r["attempt"])),
        ("core overlap < 0.6", [r for r in recovered if DANGER_LABELS[2] in _dangers(r, tau)],
         lambda r: (r["core_overlap"], -n(r), r["attempt"])),
        ("largest recovered merge", list(recovered), lambda r: (-n(r), r["attempt"])),
    ]
    chosen: dict[int, dict[str, Any]] = {}
    for reason, pool, key in reasons:
        for r in [r for r in sorted(pool, key=key) if r["attempt"] not in chosen][:SHADOW_SAMPLE_PER_REASON]:
            chosen[r["attempt"]] = _shadow_case(r, reason)
    return list(chosen.values())


def shadow_evaluation(rows: Sequence[Mapping[str, Any]], tau: float) -> dict[str, Any]:
    """Report-only evaluation of the predeclared candidates S0-S4 over the
    diagnostics rows (see SHADOW_DEFINITIONS). Reads rows, decides nothing."""
    conditions = {r["attempt"]: shadow_conditions(r, tau) for r in rows}
    accepts = {c: {r["attempt"] for r in rows if all(conditions[r["attempt"]][c].values())} for c in SHADOW_CANDIDATES}
    actual = {r["attempt"] for r in rows if r["outcome"] == "accepted"}
    recovered = {c: [r for r in rows if r["attempt"] in accepts[c] and r["attempt"] not in actual] for c in SHADOW_CANDIDATES}
    candidates: dict[str, Any] = {}
    for c in SHADOW_CANDIDATES:
        shadow_accepted = [r for r in rows if r["attempt"] in accepts[c]]
        lost = len(actual - accepts[c])
        candidates[c] = {
            "attempts": len(rows), "shadow_accepted": len(accepts[c]), "shadow_rejected": len(rows) - len(accepts[c]),
            "acceptance_rate": len(accepts[c]) / len(rows) if rows else None,
            "recovered": len(recovered[c]), "lost_vs_actual": lost, "net_vs_actual": len(recovered[c]) - lost,
            "actual_x_shadow": {"actual accepted, shadow accepted": len(actual & accepts[c]),
                                "actual accepted, shadow rejected": lost,
                                "actual rejected, shadow accepted": len(recovered[c]),
                                "actual rejected, shadow rejected": len(rows) - len(actual | accepts[c])},
            "dangers_among_shadow_accepted": _counted(DANGER_LABELS, (d for r in shadow_accepted for d in _dangers(r, tau))),
            "recovery_set": _shadow_profile(recovered[c], tau),
            "sample": _shadow_sample(recovered[c], tau) if c != "S0" else [],
        }
    by_attempt = {r["attempt"]: r for r in rows}
    ids = {c: {r["attempt"] for r in recovered[c]} for c in SHADOW_CANDIDATES}
    differences = {}
    for x, y in SHADOW_DIFFERENCES:
        diff = sorted(ids[x] - ids[y])
        differences[f"{x} recovered, {y} did not"] = {
            "attempts": len(diff),
            "failed_conditions_of_" + y: dict(sorted(Counter(
                name for a in diff for name, ok in conditions[a][y].items() if not ok).items())),
            "core_overlap_buckets": _counted(OVERLAP_LABELS, (_overlap_bucket(by_attempt[a]) for a in diff)),
            "smaller_side_share_buckets": _counted(SMALLER_SIDE_SHARE_LABELS,
                                                   (_smaller_side_share_bucket(by_attempt[a]) for a in diff)),
        }
    shadow = SHADOW_CANDIDATES[1:]
    return {
        "definitions": SHADOW_DEFINITIONS,
        "s0_mismatches_with_actual_decision": len(accepts["S0"] ^ actual),
        "candidates": candidates,
        "recovered_by_S3": len(ids["S3"]),
        "differences": differences,
        "recovery_overlap": {x: {y: len(ids[x] & ids[y]) for y in shadow} for x in shadow},
    }


# ---------------------------------------------------------------- experimental S2 strategies: report-only diagnostics

#: Accepted merges bucketed by how many variants the merged group holds right
#: after the merge (2 = two original variants; more = merging into an
#: already-merged group) -- shows whether repeated merges degrade the result.
TRAJECTORY_VARIANT_BANDS: tuple[tuple[str, int, int | None], ...] = (("2", 2, 2), ("3-4", 3, 4), ("5-9", 5, 9), ("10+", 10, None))


def _variant_band(n: int) -> str:
    return next(label for label, lo, hi in TRAJECTORY_VARIANT_BANDS if n >= lo and (hi is None or n <= hi))


def merge_trajectory(accepted: Sequence[Mapping[str, Any]], tau: float,
                     lock: Mapping[int, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Accepted merges of an experimental strategy, each measured AT THE TIME
    it was accepted (against its tentative merged profile). Later merges can
    change the final group; see the final-group diagnostics for that.
    `lock` (attempt -> recursive lock-in entry) adds each merge's tail
    provenance: boards it admitted below tau and historical tails it held."""
    def provenance(r: Mapping[str, Any]) -> dict[str, Any]:
        entry = (lock or {}).get(r["attempt"])
        if entry is None:
            return {}
        return {"admitted_below_tau": entry["admitted_below_tau"],
                "newly_admitted_below_tau": entry["newly_admitted_below_tau"],
                "historical_tails_before_merge": entry["historical_tails_a"] + entry["historical_tails_b"],
                "larger_side_historical_tails": entry["larger_side_historical_tails"],
                "smaller_side_historical_tails": entry["smaller_side_historical_tails"]}

    def row(r: Mapping[str, Any]) -> dict[str, Any]:
        larger, smaller = r["larger_side"], "b" if r["larger_side"] == "a" else "a"
        return {**provenance(r), "attempt": r["attempt"], "merged_boards": r["merged_boards"],
                "larger_side_boards": r["larger_side_boards"], "smaller_side_boards": r["smaller_side_boards"],
                "variants_after_merge": r["a_variants"] + r["b_variants"], "below_tau": r["below_tau"],
                "below_tau_share": r["below_tau_share"], "smaller_side_below_tau": r["smaller_side_below_tau"],
                "smaller_side_below_tau_share": r["smaller_side_below_tau_share"], "min_post": r["post"]["min"],
                "post_p10": r["post"]["p10"], "post_median": r["post"]["median"], "core_overlap": r["core_overlap"],
                "identical_cores": r["identical_cores"], "splash_exception_applies": r["splash_exception_applies"],
                "core_larger": r[f"core_{larger}"], "core_smaller": r[f"core_{smaller}"]}

    rows = [row(r) for r in accepted]
    by_band: dict[str, Any] = {}
    for label, _, _ in TRAJECTORY_VARIANT_BANDS:
        band = [r for r in rows if _variant_band(r["variants_after_merge"]) == label]
        by_band[label] = {"accepted_merges": len(band), "with_any_board_below_tau": sum(r["below_tau"] > 0 for r in band),
                          "min_post": quantiles(r["min_post"] for r in band),
                          "post_p10": quantiles(r["post_p10"] for r in band),
                          "below_tau_share": quantiles(r["below_tau_share"] for r in band)}
    return {
        "definition": "each accepted merge measured when it was accepted, against its tentative merged profile "
                      "(one-step); the final-group diagnostics show what later merges did to the group",
        "accepted_merges": len(rows),
        "with_any_board_below_tau": sum(r["below_tau"] > 0 for r in rows),
        "splash_exception": sum(r["splash_exception_applies"] for r in rows),
        "identical_cores": sum(r["identical_cores"] for r in rows),
        "core_overlap_buckets": _counted(OVERLAP_LABELS, (_overlap_bucket(r) for r in accepted)),
        "merged_size_bands": _counted([label for label, _, _ in SIZE_BANDS], (size_band(r["merged_boards"]) for r in rows)),
        "by_variants_after_merge": by_band,
        "quantiles": {key: quantiles(r[key] for r in rows) for key in (
            "merged_boards", "larger_side_boards", "smaller_side_boards", "below_tau", "below_tau_share",
            "smaller_side_below_tau_share", "min_post", "post_p10", "post_median", "core_overlap")},
        "merges": rows,
    }


# ---------------------------------------------------------------- experimental S2 strategies: recursive lock-in (report only)

#: Mutually exclusive Condition-1 attribution of an S2 similarity rejection.
C1_ATTRIBUTION: tuple[str, ...] = ("all historical-tail", "partial historical-tail", "no historical-tail",
                                   "condition 1 did not fail")
#: Condition-failure categories of an S2 similarity rejection (an attempt can
#: be in several; the first three split the Condition-1 failures).
REJECTION_CATEGORIES: tuple[str, ...] = (
    "condition 1 failed: all failing larger-side boards are historical tails",
    "condition 1 failed: some failing larger-side boards are historical tails",
    "condition 1 failed: no failing larger-side board is a historical tail",
    "only condition 1 failed",
    "only the smaller-side tail condition failed",
    "only the merged-tail condition failed",
    "floor condition failed (alone or with others)",
    "multiple conditions failed",
)
LOCK_SAMPLE_PER_REASON = 2
LOCK_IN_DEFINITIONS: dict[str, str] = {
    "scope": "Experimental S2 strategies only. REPORT ONLY: every number here is measured on the real S2 merge "
             "trajectory (or on a report-only order replay); no grouping decision reads any of it, and S2's conditions, "
             "tau, tail percentages and every other threshold are unchanged.",
    "historically admitted tail": "a board that was below tau against the tentative merged profile of an ACCEPTED S2 "
                                  "merge at the moment that merge was accepted (anonymous observation id, attempt, "
                                  "pre-merge side a/b, larger/smaller role, similarity and tau are kept). It stays "
                                  "historical through every later merge whatever its later similarity; a board that "
                                  "falls below tau only later, without an accepted merge admitting it below tau, is "
                                  "never historical. Pre-merge similarity (a board's similarity to its own side's or "
                                  "original variant's profile) is a separate diagnostic: it neither qualifies nor "
                                  "disqualifies a board -- only the accepted merge's tentative merged profile decides.",
    "historical (at an attempt)": "admitted by an accepted merge judged EARLIER than this attempt.",
    "similarity_rejection_attribution": "denominator: every S2 similarity rejection, i.e. every tentative merge that "
                                        "passed the pairwise core checks (overlap, swaps, C's restriction) and the "
                                        "merged-core check and was then rejected by the S2 conditions. Merged-core "
                                        "rejections never reach the similarity check and are excluded.",
    "condition 1": "S2's 'larger side: no board below tau'. Failing larger-side boards = larger-side boards below "
                   "tau against this attempt's tentative merged profile. A historical tail now at or above tau is "
                   "not a current blocker.",
    "condition1_attribution": "mutually exclusive over the denominator: 'all historical-tail' (Condition 1 failed and "
                              "every failing larger-side board is historical), 'partial historical-tail' (some are), "
                              "'no historical-tail' (none are), 'condition 1 did not fail'.",
    "categories": "multi-label condition-failure categories over the same denominator (an attempt can be in "
                  "several); 'exact_failed_condition_sets' is the mutually exclusive version.",
    "historical_tail_removal_counterfactual": "for an S2 similarity rejection whose CURRENT larger side holds >= 1 "
                                              "historical tail: remove exactly those historical-tail boards from the "
                                              "larger side (no other board, however weak; no search for a passing "
                                              "subset), recompute the tentative merged profile of the remaining larger "
                                              "side + the unchanged smaller side, and evaluate the unchanged S2 "
                                              "conditions (sides re-derived by board count, ties to side a, as the real "
                                              "rule does). It changes no real grouping decision and is a diagnostic "
                                              "of recursive lock-in, not evidence that the merge should happen.",
    "tail_trajectory": "attempts judged after an accepted merge admitted below-tau boards, where either side "
                       "already holds historical tails; outcomes as the real S2 decided them.",
    "order_sensitivity": "report-only replays of the same S2 merge on the same variants. They change only which of "
                         "several candidates tied at the SAME highest core overlap is judged first (baseline = the real "
                         "order, lowest ids first; reversed = highest ids first; seeded = fixed deterministic "
                         "pseudo-random permutations of the tied candidates). A lower-overlap candidate is never judged "
                         "while a valid higher-overlap one exists. The real S2 grouping stays the canonical baseline.",
    "evidence": "Patch-window results on a population that has already shaped this work (e.g. 18.3) are "
                "DEVELOPMENT evidence, not independent validation. No result here proves that any composition-family "
                "rule is correct, and placement plays no part in any of it.",
}


def _counts(entries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Lock-in headline counts of one merge run (real or replay)."""
    rejected = [e for e in entries if e["outcome"] == "rejected_similarity"]
    c1 = [e for e in rejected if e["condition1_attribution"] != C1_ATTRIBUTION[3]]
    cf = [e for e in rejected if e["counterfactual"] is not None]
    return {
        "accepted_merges": sum(e["outcome"] == "accepted" for e in entries),
        "accepted_merges_with_any_board_below_tau": sum(e["admitted_below_tau"] > 0 for e in entries),
        "boards_admitted_below_tau": sum(e["newly_admitted_below_tau"] for e in entries),
        "similarity_rejections": len(rejected),
        "condition1_rejections": len(c1),
        "condition1_rejections_all_historical_tail": sum(e["condition1_attribution"] == C1_ATTRIBUTION[0] for e in c1),
        "condition1_rejections_partial_historical_tail": sum(e["condition1_attribution"] == C1_ATTRIBUTION[1] for e in c1),
        "historical_tail_attributed_condition1_rejections": sum(e["condition1_attribution"] in C1_ATTRIBUTION[:2] for e in c1),
        "counterfactual_eligible": len(cf),
        "counterfactual_recoveries": sum(e["counterfactual"]["passes"] for e in cf),
    }


def _categories(e: Mapping[str, Any]) -> list[str]:
    c1, small, merged, floor = S2_CONDITION_NAMES
    failed = set(e["failed_conditions"])
    out = []
    if c1 in failed:
        out.append(REJECTION_CATEGORIES[C1_ATTRIBUTION.index(e["condition1_attribution"])])
    for single, label in ((c1, REJECTION_CATEGORIES[3]), (small, REJECTION_CATEGORIES[4]), (merged, REJECTION_CATEGORIES[5])):
        if failed == {single}:
            out.append(label)
    if floor in failed:
        out.append(REJECTION_CATEGORIES[6])
    if len(failed) > 1:
        out.append(REJECTION_CATEGORIES[7])
    return out


def _lock_case(reason: str, r: Mapping[str, Any], e: Mapping[str, Any]) -> dict[str, Any]:
    larger, smaller = r["larger_side"], "b" if r["larger_side"] == "a" else "a"
    cf = e["counterfactual"]
    return {
        "reason": reason, "attempt": r["attempt"], "outcome": r["outcome"], "merged_boards": r["merged_boards"],
        "larger_side_boards": r["larger_side_boards"], "smaller_side_boards": r["smaller_side_boards"],
        "core_larger": r[f"core_{larger}"], "core_smaller": r[f"core_{smaller}"],
        "larger_only": r[f"{larger}_only"], "smaller_only": r[f"{smaller}_only"], "core_overlap": r["core_overlap"],
        "larger_side_historical_tails": e["larger_side_historical_tails"],
        "smaller_side_historical_tails": e["smaller_side_historical_tails"],
        "larger_side_below_tau": r["larger_side_below_tau"],
        "larger_side_below_tau_historical": e["larger_side_below_tau_historical"],
        "larger_side_below_tau_not_historical": e["larger_side_below_tau_not_historical"],
        "smaller_side_below_tau": r["smaller_side_below_tau"], "merged_below_tau": r["below_tau"],
        "condition1_attribution": e["condition1_attribution"],
        "failed_conditions_before": e["failed_conditions"],
        "failed_conditions_after": cf["after"]["failed_conditions"] if cf and cf["after"] else None,
        "counterfactual_passes": cf["passes"] if cf else None,
        "removed_boards": cf["removed_boards"] if cf else None,
        "larger_side_min_similarity": r["larger_side_min_post"], "smaller_side_min_similarity": r["smaller_side_min_post"],
        "post_min": r["post"]["min"], "post_p10": r["post"]["p10"],
        "counterfactual_after": cf["after"] if cf else None,
    }


def recursive_lock_in_summary(diag: MergeDiagnostics) -> dict[str, Any]:
    """Report-only recursive lock-in section of one S2 merge run (see
    LOCK_IN_DEFINITIONS). Reads `diag`, decides nothing."""
    if not diag.lock_in:
        raise ValueError("recursive lock-in tracking was not enabled for these diagnostics")
    tau, rows, lock = diag.tau, {r["attempt"]: r for r in diag.rows}, diag.lock
    rejected = [e for e in lock if e["outcome"] == "rejected_similarity"]
    accepted = [e for e in lock if e["outcome"] == "accepted"]
    adm = diag.admissions
    provenance = {
        "accepted_merges": len(accepted),
        "accepted_merges_with_any_board_below_tau": sum(e["admitted_below_tau"] > 0 for e in accepted),
        "tail_admission_events": len(adm),
        "boards_admitted_below_tau": len(diag.provenance),
        "boards_admitted_below_tau_more_than_once": sum(c > 1 for c in Counter(a["observation"] for a in adm).values()),
        "admission_events_by_side_role": _counted(("larger", "smaller"), (a["side_role"] for a in adm)),
        "admission_events_by_pre_merge_side": _counted(("a", "b"), (a["side"] for a in adm)),
        "similarity_at_admission": quantiles(a["similarity"] for a in adm),
        "shortfall_below_tau_at_admission": quantiles(tau - a["similarity"] for a in adm),
    }
    c1 = [e for e in rejected if e["condition1_attribution"] != C1_ATTRIBUTION[3]]
    combos = Counter(" + ".join(e["failed_conditions"]) for e in rejected)
    attribution = {
        "denominator": "S2 similarity rejections (passed the pairwise core and merged-core checks)",
        "s2_similarity_rejections": len(rejected),
        "condition_failures": _counted(S2_CONDITION_NAMES, (c for e in rejected for c in e["failed_conditions"])),
        "exact_failed_condition_sets": dict(sorted(combos.items(), key=lambda kv: (-kv[1], kv[0]))),
        "categories": _counted(REJECTION_CATEGORIES, (c for e in rejected for c in _categories(e))),
        "condition1_attribution": _counted(C1_ATTRIBUTION, (e["condition1_attribution"] for e in rejected)),
        "condition1_attribution_when_condition1_is_the_only_failure": _counted(
            C1_ATTRIBUTION[:3], (e["condition1_attribution"] for e in c1 if len(e["failed_conditions"]) == 1)),
        "failing_larger_side_boards": sum(e["larger_side_below_tau_historical"] + e["larger_side_below_tau_not_historical"]
                                          for e in rejected),
        "failing_larger_side_boards_historical_tails": sum(e["larger_side_below_tau_historical"] for e in rejected),
        "failing_larger_side_boards_not_historical": sum(e["larger_side_below_tau_not_historical"] for e in rejected),
        "historical_tails_on_larger_side_at_or_above_tau": sum(e["larger_side_historical_tails_at_or_above_tau"]
                                                               for e in rejected),
    }
    eligible = [e for e in rejected if e["counterfactual"] is not None]
    evaluable = [e for e in eligible if e["counterfactual"]["evaluable"]]
    cfs = [e["counterfactual"] for e in evaluable]

    def side(key: str) -> dict[str, Any]:
        return {"before": quantiles(c["before"][key] for c in cfs), "after": quantiles(c["after"][key] for c in cfs)}

    counterfactual = {
        "eligible_rejections": len(eligible),
        "not_evaluable_every_larger_side_board_removed": len(eligible) - len(evaluable),
        "would_pass_all_unchanged_s2_conditions": sum(c["passes"] for c in cfs),
        "would_still_fail": sum(not c["passes"] for c in cfs),
        "by_condition1_attribution": {label: {"eligible": sum(e["condition1_attribution"] == label for e in eligible),
                                              "would_pass": sum(e["condition1_attribution"] == label
                                                                and e["counterfactual"]["passes"] for e in evaluable)}
                                      for label in C1_ATTRIBUTION},
        "failed_conditions_before": _counted(S2_CONDITION_NAMES, (c for x in cfs for c in x["before"]["failed_conditions"])),
        "failed_conditions_after": _counted(S2_CONDITION_NAMES, (c for x in cfs for c in x["after"]["failed_conditions"])),
        "failure_transitions": dict(sorted(Counter(
            f"{' + '.join(x['before']['failed_conditions'])} -> {' + '.join(x['after']['failed_conditions']) or 'passes'}"
            for x in cfs).items(), key=lambda kv: (-kv[1], kv[0]))),
        "sides_flipped": sum(c["after"]["sides_flipped"] for c in cfs),
        "removed_boards_total": sum(e["counterfactual"]["removed_boards"] for e in eligible),
        "removed_boards": quantiles(e["counterfactual"]["removed_boards"] for e in eligible),
        "removed_share_of_larger_side": quantiles(e["counterfactual"]["removed_share_of_larger_side"] for e in eligible),
        **{key: side(key) for key in ("merged_boards", "min_similarity", "larger_side_below_tau", "smaller_side_below_tau",
                                      "merged_below_tau")},
    }
    involving = [e for e in lock if e["historical_tails_a"] + e["historical_tails_b"]]
    on_larger = [e for e in involving if e["larger_side_historical_tails"]]
    trajectory = {
        "accepted_merges": len(accepted),
        "accepted_merges_with_any_board_below_tau": provenance["accepted_merges_with_any_board_below_tau"],
        "boards_admitted_below_tau": len(diag.provenance),
        "admission_events_by_side_role": provenance["admission_events_by_side_role"],
        "later_attempts_involving_historical_tails": len(involving),
        "later_attempts_involving_historical_tails_accepted": sum(e["outcome"] == "accepted" for e in involving),
        "later_s2_rejections_involving_historical_tails": sum(e["outcome"] != "accepted" for e in involving),
        "later_attempts_with_historical_tails_on_larger_side": len(on_larger),
        "later_s2_rejections_with_historical_tails_on_larger_side": sum(e["outcome"] != "accepted" for e in on_larger),
        "later_condition1_rejections_with_historical_tails_on_larger_side": sum(
            e["outcome"] != "accepted" and e["condition1_attribution"] != C1_ATTRIBUTION[3] for e in on_larger),
        "later_condition1_rejections_attributed_to_historical_tails": sum(
            e["condition1_attribution"] in C1_ATTRIBUTION[:2] for e in rejected),
        "counterfactual_recoveries": counterfactual["would_pass_all_unchanged_s2_conditions"],
    }
    n = lambda e: rows[e["attempt"]]["merged_boards"]  # noqa: E731
    reasons = [
        ("largest later rejection recovered by historical-tail removal",
         [e for e in evaluable if e["counterfactual"]["passes"]]),
        ("condition 1 failed: ALL failing larger-side boards are historical tails",
         [e for e in rejected if e["condition1_attribution"] == C1_ATTRIBUTION[0]]),
        ("condition 1 failed: PARTIAL historical-tail attribution",
         [e for e in rejected if e["condition1_attribution"] == C1_ATTRIBUTION[1]]),
        ("condition 1 failed with NO historical tail involved (none on either side)",
         [e for e in rejected if e["condition1_attribution"] == C1_ATTRIBUTION[2]
          and not e["historical_tails_a"] + e["historical_tails_b"]]),
    ]
    chosen: dict[int, dict[str, Any]] = {}
    for reason, pool in reasons:
        for e in [e for e in sorted(pool, key=lambda e: (-n(e), e["attempt"])) if e["attempt"] not in chosen][:LOCK_SAMPLE_PER_REASON]:
            chosen[e["attempt"]] = _lock_case(reason, rows[e["attempt"]], e)
    return {"definitions": LOCK_IN_DEFINITIONS, "tau": tau, "counts": _counts(lock),
            "accepted_tail_provenance_summary": provenance, "similarity_rejection_attribution": attribution,
            "historical_tail_removal_counterfactual": counterfactual, "tail_trajectory": trajectory,
            "deterministic_review_samples": list(chosen.values())}


# ---------------------------------------------------------------- experimental S2 strategies: merge-order sensitivity (report only)

_MASK64 = (1 << 64) - 1


def _splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & _MASK64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK64
    return x ^ (x >> 31)


def _tie_hash(seed: int, *values: int) -> int:
    """Deterministic across processes and platforms (no Python `hash`)."""
    h = _splitmix64(seed & _MASK64)
    for v in values:
        h = _splitmix64(h ^ (v & _MASK64))
    return h


@dataclass(frozen=True)
class TieOrder:
    """A REPORT-ONLY tie order for `merge_variants`: which of several valid
    candidates tied at the same highest core overlap is judged first.
    "baseline" = the real order (lowest group ids first); "reversed" =
    highest ids first; "seeded" = a fixed pseudo-random permutation of the
    tied candidates (the key hashes the seed, both group ids and their
    current versions, so a pair whose groups changed is a new draw)."""

    mode: str
    seed: int = 0

    @property
    def label(self) -> str:
        return f"seeded tie permutation {self.seed}" if self.mode == "seeded" else f"{self.mode} tie order"

    def key(self, lo: int, hi: int, va: int, vb: int) -> tuple[int, ...]:
        if self.mode == "baseline":
            return (lo, hi)
        if self.mode == "reversed":
            return (-lo, -hi)
        if self.mode == "seeded":
            return (_tie_hash(self.seed, lo, hi, va, vb), lo, hi)
        raise ValueError(f"unknown tie order {self.mode!r}")


#: Fixed before any real run: the baseline (a check that the replay path
#: reproduces the real grouping), the reversed order, and three seeds -- the
#: smallest set that still shows whether different tie choices diverge.
ORDER_REPLAYS: tuple[TieOrder, ...] = (TieOrder("baseline"), TieOrder("reversed"), TieOrder("seeded", 1),
                                       TieOrder("seeded", 2), TieOrder("seeded", 3))


def order_replays(boards: Sequence[Board], vecs, assign: Mapping[int, int], strategy: Strategy, config: ArchetypeConfig,
                  progress: Callable[[str], None] | None = None,
                  orders: Sequence[TieOrder] = ORDER_REPLAYS) -> list[dict[str, Any]]:
    """Re-run the strategy's variant merge in memory once per tie order, on
    the same variants and vectors (no database, no new data). Report only:
    the real grouping is never replaced."""
    progress = progress or _noop
    members = [b for b in boards if b.obs in assign]
    out = []
    for order in orders:
        progress(f"{strategy.name}: order replay '{order.label}' started")
        diag = MergeDiagnostics(config.tau, lock_in=True)
        stats = {"judged_steps": 0, "steps_with_tied_candidates": 0}
        to_group, log, checks = merge_variants(members, assign, strategy, config, vecs=vecs, diagnostics=diag,
                                               tie_order=order, tie_stats=stats)
        out.append({"replay": order.label, "mode": order.mode, "seed": order.seed if order.mode == "seeded" else None,
                    "variant_to_group": to_group, "merges": len(log), "merge_checks": checks, "tie_stats": stats,
                    "decision_rule_mismatches": sum(
                        all(shadow_conditions(r, config.tau)["S2"].values()) != (r["outcome"] == "accepted")
                        for r in diag.rows),
                    "counts": _counts(diag.lock)})
        progress(f"{strategy.name}: order replay '{order.label}' completed, {len(log)} merges, "
                 f"{len(set(to_group.values()))} groups, {stats['steps_with_tied_candidates']} of "
                 f"{stats['judged_steps']} judged steps had tied candidates")
    return out


def partition_disagreement(universe: Iterable[int], x: Mapping[int, int], y: Mapping[int, int]) -> dict[str, Any]:
    """Pairwise co-membership disagreement between two partitions of the same
    boards (a board absent from a mapping is a singleton). Pair counts come
    from group sizes and group intersections (sum of C(n, 2)), never from
    enumerating board pairs."""
    obs = sorted(set(universe))

    def pairs(c: int) -> int:
        return c * (c - 1) // 2

    tx = sum(pairs(c) for c in Counter(x[k] for k in obs if k in x).values())
    ty = sum(pairs(c) for c in Counter(y[k] for k in obs if k in y).values())
    inter = Counter((x[k], y[k]) for k in obs if k in x and k in y)
    both = sum(pairs(c) for c in inter.values())
    size_x, size_y = Counter(x[k] for k in obs if k in x), Counter(y[k] for k in obs if k in y)
    unchanged = sum(c for (gx, gy), c in inter.items() if c == size_x[gx] == size_y[gy])
    unchanged += sum(1 for k in obs if k not in x and k not in y)
    either = tx + ty - both
    total = pairs(len(obs))
    disagree = tx + ty - 2 * both
    return {"boards": len(obs), "board_pairs": total, "pairs_together_in_baseline": tx, "pairs_together_in_replay": ty,
            "pairs_together_in_both": both, "together_in_baseline_apart_in_replay": tx - both,
            "together_in_replay_apart_in_baseline": ty - both, "disagreeing_pairs": disagree,
            "disagreement_share_of_all_pairs": disagree / total if total else 0.0,
            "disagreement_share_of_pairs_together_in_either": disagree / either if either else 0.0,
            "boards_with_identical_group_membership": unchanged,
            "boards_with_changed_group_membership": len(obs) - unchanged}


def merge_variants(boards: Sequence[Board], variant: Mapping[int, int], strategy: Strategy,
                   config: ArchetypeConfig, *, vecs: Mapping[int, tuple[dict[str, float], dict[str, float]]] | None = None,
                   diagnostics: MergeDiagnostics | None = None, tie_order: TieOrder | None = None,
                   tie_stats: dict[str, int] | None = None,
                   trace: list[tuple[float, float | None, int]] | None = None) -> tuple[dict[int, int], list[str], dict[str, int]]:
    """Variant -> group. Greedy: the highest core-overlap mergeable pair first
    (ties: lowest ids), every candidate re-checked against the merged group's
    recomputed statistics. Pairwise checks alone let a merged core shrink
    below both input cores, which lowers the bar for the next merge (A~B,
    then the eroded A+B ~ C, ...), so every tentative merge is also checked
    on its RESULT (`result_check`) and kept only if the merged core survives
    and every member board is still >= tau similar to the merged profile.
    Returns (variant -> group, merge log, merge-check counts).

    `diagnostics` (research only) receives a measurement of every tentative
    merge that reaches the similarity check; it only reads the merge state
    and never influences a decision.

    `tie_order` (REPORT-ONLY order replays; None for every real grouping)
    changes only which of several candidates tied at the SAME highest core
    overlap is judged first: queue entries are ranked by score and then by
    `tie_order.key`, so the next judged candidate is the lowest-key valid one
    at the current top score. A lower-overlap candidate is never judged while
    a valid higher-overlap one exists, and every check and threshold is
    unchanged. `TieOrder("baseline")` reproduces the real order exactly.
    `tie_stats` receives tie counts on that path; `trace` (tests only)
    receives, per judged step, (chosen score, brute-force best valid score,
    whether another valid candidate shared the top score)."""
    if vecs is None:
        champions = load_roster().champions
        vecs = {b.obs: board_vectors(b, strategy, config, champions) for b in boards}
    counts: dict[int, Counter] = defaultdict(Counter)
    itemized: dict[int, Counter] = defaultdict(Counter)
    sizes: Counter = Counter()
    members: dict[int, list[int]] = defaultdict(list)
    for b in boards:
        g = variant[b.obs]
        sizes[g] += 1
        counts[g].update(b.identity)
        itemized[g].update({u.cid for u in b.shop if len(u.items) >= 2})
        members[g].append(b.obs)
    groups = {g: {g} for g in sizes}
    version = {g: 0 for g in sizes}
    log: list[str] = []
    checks = {"rejected_core": 0, "rejected_similarity": 0, "variants_with_a_board_below_tau_before_merge": 0}

    def core_of(c: Counter, n: int) -> set[str]:
        return {u for u, k in c.items() if k / n >= config.core_presence}

    def core(g: int) -> set[str]:
        return core_of(counts[g], sizes[g])

    def below_tau(obs: Sequence[int]) -> bool:
        profile = mean_profile([vecs[k] for k in obs])
        return any(similarity(vecs[k], profile, strategy) < config.tau for k in obs)

    checks["variants_with_a_board_below_tau_before_merge"] = sum(below_tau(sorted(members[g])) for g in sorted(groups))

    def score(a: int, b: int) -> float | None:
        ca, cb = core(a), core(b)
        shared = ca & cb
        if len(shared) < max(config.merge_min_shared, math.ceil(config.merge_shared_fraction * max(len(ca), len(cb)))):
            return None
        da, db_ = ca - cb, cb - ca
        if min(len(da), len(db_)) > config.max_swaps:
            return None
        if strategy.merge == "structure_aware":
            # Itemized-splash exception: one variant's core is the other's
            # full core (>= merge_min_result_core units) plus exactly one
            # unit; that unit needs no flex/un-itemized explanation, so an
            # itemized splash on an unchanged core may merge. Any swap, or a
            # smaller shared core, gets no exception.
            splash = ((not da and len(db_) == 1) or (not db_ and len(da) == 1)) and len(shared) >= config.merge_min_result_core

            def explained(u: str, own: int, other: int) -> bool:
                flex_elsewhere = counts[other][u] / sizes[other] >= config.flex_presence
                support = itemized[own][u] / counts[own][u] < config.itemized_share
                return flex_elsewhere or support or splash
            if not all(explained(u, a, b) for u in da) or not all(explained(u, b, a) for u in db_):
                return None
        return len(shared) / len(ca | cb)

    def result_check(a: int, b: int) -> str | None:
        """None when the merged a+b stays coherent, else the failed rule:
        "core" -- the merged core has < merge_min_result_core units AND keeps
        < merge_core_retention of the smaller pre-merge core (on equal core
        sizes both must be kept); "similarity" -- a member board is < tau
        similar to the merged mean profile."""
        ca, cb = core(a), core(b)
        merged = core_of(counts[a] + counts[b], sizes[a] + sizes[b])
        smaller = [c for c in (ca, cb) if len(c) == min(len(ca), len(cb))]
        kept = all(len(merged & c) >= math.ceil(config.merge_core_retention * len(c)) for c in smaller)
        if len(merged) < config.merge_min_result_core and not kept:
            return "core"
        if strategy.similarity_rule == "all_boards":  # the A/B/C controls
            if below_tau(sorted(members[a] + members[b])):
                return "similarity"
            return None
        if strategy.similarity_rule == "s2":  # experimental strategies only
            return None if all(s2_merge_conditions(a, b).values()) else "similarity"
        raise ValueError(f"unknown similarity rule {strategy.similarity_rule!r}")

    def s2_merge_conditions(a: int, b: int) -> dict[str, bool]:
        """S2 against the tentative merged profile of a+b (sides by board count; tie: a, the lower id)."""
        obs = sorted(members[a] + members[b])
        profile = mean_profile([vecs[k] for k in obs])
        post = {k: similarity(vecs[k], profile, strategy) for k in obs}
        larger, smaller = (a, b) if sizes[a] >= sizes[b] else (b, a)
        below = {g: sum(1 for k in members[g] if post[k] < config.tau) for g in (a, b)}
        return s2_conditions(larger_below=below[larger], smaller_below=below[smaller], smaller_boards=sizes[smaller],
                             merged_below=below[a] + below[b], merged_boards=len(obs), min_post=min(post.values()),
                             tau=config.tau)

    pre_cache: dict[tuple[int, int], dict[int, float]] = {}

    def pre_similarity(g: int) -> dict[int, float]:
        """obs -> similarity to group g's CURRENT (pre-merge) mean profile;
        cached per group version (a group only changes when it merges)."""
        key = (g, version[g])
        if key not in pre_cache:
            obs = sorted(members[g])
            profile = mean_profile([vecs[k] for k in obs])
            pre_cache[key] = {k: similarity(vecs[k], profile, strategy) for k in obs}
        return pre_cache[key]

    def measure(a: int, b: int, overlap: float, accepted: bool) -> None:
        """Diagnostics only: describe the tentative merge a+b just judged by
        the similarity check. Reads the merge state, changes nothing."""
        ca, cb = core(a), core(b)
        da, db_ = ca - cb, cb - ca
        merged_core = core_of(counts[a] + counts[b], sizes[a] + sizes[b])
        obs = sorted(members[a] + members[b])
        profile = mean_profile([vecs[k] for k in obs])
        post = {k: similarity(vecs[k], profile, strategy) for k in obs}
        pre = {**pre_similarity(a), **pre_similarity(b)}
        side = {**{k: "a" for k in members[a]}, **{k: "b" for k in members[b]}}
        splash = (strategy.merge == "structure_aware"
                  and ((not da and len(db_) == 1) or (not db_ and len(da) == 1))
                  and len(ca & cb) >= config.merge_min_result_core)

        def evaluate(subset: Sequence[int]) -> dict[int, float]:
            """Similarity of each board to the mean profile of `subset` (the
            lock-in counterfactual); reads vectors only."""
            sub = mean_profile([vecs[k] for k in subset])
            return {k: similarity(vecs[k], sub, strategy) for k in subset}

        diagnostics.record(accepted=accepted, a=a, b=b, variants_a=len(groups[a]), variants_b=len(groups[b]),
                           core_a=ca, core_b=cb, merged_core=merged_core, overlap=overlap, splash=splash,
                           pre=pre, post=post, side=side, evaluate=evaluate)

    def core_index() -> dict[str, set[int]]:
        idx: dict[str, set[int]] = defaultdict(set)
        for g in groups:
            for u in core(g):
                idx[u].add(g)
        return idx

    idx = core_index()
    heap: list[tuple] = []

    def push_pairs(a: int) -> None:
        near = Counter(h for u in core(a) for h in idx.get(u, ()) if h != a)
        for h, shared in near.items():
            if shared < config.merge_min_shared:
                continue
            s = score(min(a, h), max(a, h))
            if s is not None:
                lo, hi = min(a, h), max(a, h)
                if tie_order is None:  # every real grouping
                    heapq.heappush(heap, (-s, lo, hi, version[lo], version[hi]))
                else:  # replay: the (static) tie key ranks entries only within an equal score
                    heapq.heappush(heap, (-s, tie_order.key(lo, hi, version[lo], version[hi]), lo, hi, version[lo], version[hi]))

    rejected: set[tuple[int, int, int, int]] = set()

    def junk(entry: tuple) -> bool:
        """A queued entry the real loop would skip: a stale version or a pair already rejected at these versions."""
        _, _, a, b, va, vb = entry
        return a not in groups or b not in groups or version[a] != va or version[b] != vb or entry[2:] in rejected

    def pop_replay() -> tuple[float, int, int, int, int] | None:
        """Replay path only. Entries carry the tie key right after the score,
        so the heap yields, among the valid candidates at the current top
        score, the one with the lowest key; stale and rejected entries are
        skipped exactly as the real loop skips them (the key never ranks
        across different scores)."""
        while heap:
            entry = heapq.heappop(heap)
            if junk(entry):
                continue
            neg, chosen = entry[0], entry[2:]
            # tie statistic: drop junk (and queued duplicates of the chosen pair, which the real loop would skip
            # once it is judged) from the top, then see whether another valid candidate shares the top score
            while heap and (junk(heap[0]) or heap[0][2:] == chosen):
                heapq.heappop(heap)
            tied = bool(heap) and heap[0][0] == neg
            if tie_stats is not None:
                tie_stats["judged_steps"] = tie_stats.get("judged_steps", 0) + 1
                tie_stats["steps_with_tied_candidates"] = tie_stats.get("steps_with_tied_candidates", 0) + tied
            if trace is not None:  # tests only: the best valid score over ALL current pairs, by brute force
                ids = sorted(groups)
                scores = [score(g, h) for i, g in enumerate(ids) for h in ids[i + 1:]
                          if (g, h, version[g], version[h]) not in rejected]
                trace.append((-neg, max((x for x in scores if x is not None), default=None), tied))
            return (neg, *chosen)
        return None

    for g in sorted(groups):
        push_pairs(g)
    while heap:
        if tie_order is None:  # every real grouping
            neg, a, b, va, vb = heapq.heappop(heap)
            if a not in groups or b not in groups or version[a] != va or version[b] != vb:
                continue
            if score(a, b) is None:  # stats unchanged since push, but stay defensive
                continue
            if (a, b, va, vb) in rejected:  # the same pair can be queued from both sides
                continue
        else:  # report-only order replay
            picked = pop_replay()
            if picked is None:
                break
            neg, a, b, va, vb = picked
            if score(a, b) is None:
                continue
        failed = result_check(a, b)
        if diagnostics is not None and failed != "core":  # after the decision; before any state changes
            measure(a, b, -neg, failed is None)
        if failed:  # re-examined only if a or b later changes (push_pairs after a merge)
            rejected.add((a, b, va, vb))
            checks[f"rejected_{failed}"] += 1
            continue
        log.append(f"merge variant-group {b} ({sizes[b]} boards) into {a} ({sizes[a]} boards), core overlap {-neg:.2f}")
        for u in core(a) | core(b):
            idx[u].discard(a)
            idx[u].discard(b)
        counts[a] += counts.pop(b)
        itemized[a] += itemized.pop(b)
        sizes[a] += sizes.pop(b)
        groups[a] |= groups.pop(b)
        members[a] += members.pop(b)
        del version[b]
        version[a] += 1
        for u in core(a):
            idx[u].add(a)
        push_pairs(a)
    order = sorted(groups, key=lambda g: (-sizes[g], min(groups[g])))
    return {v: rank for rank, g in enumerate(order) for v in sorted(groups[g])}, log, checks


# ---------------------------------------------------------------- statistics


def quantiles(values: Iterable[float]) -> dict[str, float] | None:
    xs = sorted(values)
    if not xs:
        return None
    q = lambda p: xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]  # noqa: E731
    return {"min": xs[0], "p10": q(0.10), "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "p90": q(0.9),
            "p99": q(0.99), "max": xs[-1], "mean": statistics.fmean(xs)}


def outcome(boards: Sequence[Board]) -> dict[str, Any]:
    n = len(boards)
    top4 = sum(b.placement <= 4 for b in boards)
    return {
        "boards": n,
        "distinct_matches": len({b.key[0] for b in boards}),
        "avg_placement": sum(b.placement for b in boards) / n,
        "top4_rate": top4 / n,
        "top4_wilson95": wilson(top4, n),
        "win_rate": sum(b.placement == 1 for b in boards) / n,
    }


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _copy(board: Board, cid: str) -> Unit | None:
    """The most-itemized copy of a champion on a board (then highest star)."""
    copies = [u for u in board.shop if u.cid == cid]
    return max(copies, key=lambda u: (len(u.items), u.tier)) if copies else None


def group_summary(gid: int, members: Sequence[Board], config: ArchetypeConfig, *, eligible: int, observable: int) -> dict[str, Any]:
    n = len(members)
    intents = carry.load_item_intent()
    adaptive = set(carry.adaptive_helm_item_ids())
    champions = load_roster().champions
    presence = Counter(cid for b in members for cid in b.identity)
    units: dict[str, dict[str, Any]] = {}
    for cid, c in sorted(presence.items(), key=lambda kv: (-kv[1], kv[0])):
        copies = [u for u in (_copy(b, cid) for b in members) if u is not None]
        stars = Counter(u.tier for u in copies)
        units[cid] = {
            "presence": c / n,
            "cost": roster_cost(cid, copies[0].stored_cost, champions),
            "two_plus_items": sum(len(u.items) >= 2 for u in copies) / n,
            "carry_qualified": sum(u.carry_qualified for u in copies) / n,
            "defensive_item_holder": sum(any(carry.item_intent(i) == carry.TANK for i in u.items) for u in copies) / n,
            "thiefs_gloves": sum(any(i in THIEFS_GLOVES for i in u.raw_items) for u in copies) / n,
            "adaptive_helm": sum(any(i in adaptive for i in u.raw_items) for u in copies) / n,
            "unknown_intent_snapshot": sum(any(i in intents and carry.item_intent(i) == carry.UNKNOWN for i in u.items) for u in copies) / n,
            "unmapped_item": sum(any(i not in intents for i in u.items) for u in copies) / n,
            "stars": {str(k): v / len(copies) for k, v in sorted(stars.items())},
        }
    ordered = list(units)
    core = [u for u in ordered if units[u]["presence"] >= config.display_core_presence]
    other = [u for u in ordered if config.display_other_presence <= units[u]["presence"] < config.display_core_presence]
    trait_freq = Counter(k for b in members for k in b.trait_keys)
    primary = max(ordered, key=lambda u: (units[u]["two_plus_items"], units[u]["presence"], u)) if ordered else None
    shop_sizes = [len(b.identity) for b in members]
    mean_size = statistics.fmean(shop_sizes)
    presences = sorted((units[u]["presence"] for u in ordered), reverse=True)
    gaps = [(presences[i] - presences[i + 1], i) for i in range(len(presences) - 1)]
    largest_gap = max(gaps) if gaps else (0.0, -1)
    return {
        "group": gid,
        "boards": n,
        "size_band": size_band(n),
        "share_of_eligible_boards": n / eligible if eligible else None,
        "share_of_unit_observable_boards": n / observable if observable else None,
        "outcome": outcome(members),
        "placement_histogram": {str(k): v for k, v in sorted(Counter(b.placement for b in members).items())},
        "low_placement_share_7_8": sum(b.placement >= 7 for b in members) / n,
        "shop_units_per_board": quantiles(shop_sizes),
        "units": units,
        "core_candidates": core,
        "other_common_units": other,
        "presence_distribution": presences,
        "largest_presence_gap": {"gap": largest_gap[0], "after_rank": largest_gap[1] + 1},
        "stable_slot_share": sum(p >= config.display_core_presence for p in presences) / mean_size if mean_size else 0.0,
        "primary_itemized_unit": primary,
        "primary_itemized_cost": units[primary]["cost"] if primary else None,
        "core_mean_cost": statistics.fmean([units[u]["cost"] or 0 for u in core]) if core else None,
        "traits": [(k, c / n) for k, c in sorted(trait_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
        "same_lobby_repeats": n - len({b.key[0] for b in members}),
    }


THIEFS_GLOVES = frozenset(
    i for i, m in (json.loads(ITEM_STATS_PATH.read_text()).get("items") or {}).items() if m.get("name") == "Thief's Gloves"
)


def item_set_summary(members: Sequence[Board], cid: str, config: ArchetypeConfig, names: Names) -> dict[str, Any]:
    """Exact completed-item sets and 2-item cores for one unit, over the
    group's boards where that unit holds >= 2 completed items (the stated
    denominator; label-free, i.e. not the carry classifier)."""
    intents = carry.load_item_intent()
    rows: dict[tuple[str, ...], list[tuple[Board, Unit]]] = defaultdict(list)
    pairs: dict[tuple[str, str], list[tuple[Board, Unit]]] = defaultdict(list)
    for b in members:
        u = _copy(b, cid)
        if u is None or len(u.items) < 2:
            continue
        rows[u.items].append((b, u))
        for pair in sorted({(u.items[i], u.items[j]) for i in range(len(u.items)) for j in range(i + 1, len(u.items))}):
            pairs[pair].append((b, u))
    population = sum(len(v) for v in rows.values())

    def describe(items: tuple[str, ...], obs: list[tuple[Board, Unit]]) -> dict[str, Any]:
        o = outcome([b for b, _ in obs])
        flags = []
        if any(i in THIEFS_GLOVES for i in items):
            flags.append("THIEFS_GLOVES_AMBIGUOUS")
        if any(i not in intents for i in items):
            flags.append("UNMAPPED_INTENT_ID")
        if any(i not in names.items for i in items):
            flags.append("NO_CANONICAL_NAME")
        if len(obs) < config.small_sample:
            flags.append("SMALL_SAMPLE")
        return {"items": list(items), "labels": [names.item(i) for i in items], "observations": len(obs),
                "share_of_population": len(obs) / population, "avg_placement": o["avg_placement"],
                "top4_rate": o["top4_rate"], "win_rate": o["win_rate"],
                "three_star_share": sum(u.tier >= 3 for _, u in obs) / len(obs),
                "carry_qualified_share": sum(u.carry_qualified for _, u in obs) / len(obs), "flags": flags}

    by_count = lambda kv: (-len(kv[1]), kv[0])  # noqa: E731
    return {
        "unit": cid,
        "population": population,
        "population_definition": "boards in this group where the unit holds >= 2 completed items",
        "exact_sets": [describe(k, v) for k, v in sorted(rows.items(), key=by_count)[:5]],
        "two_item_cores": [describe(k, v) for k, v in sorted(pairs.items(), key=by_count)[:5]],
        "distinct_exact_sets": len(rows),
    }


def core_jaccard(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    ca, cb = set(a["core_candidates"]), set(b["core_candidates"])
    return len(ca & cb) / len(ca | cb) if ca | cb else 0.0


def global_metrics(boards: Sequence[Board], grouping: Grouping, strategy: Strategy, config: ArchetypeConfig) -> dict[str, Any]:
    champions = load_roster().champions
    eligible = [b for b in boards if len(b.identity) >= config.min_identity_units]
    by_obs = {b.obs: b for b in eligible}
    members: dict[int, list[Board]] = defaultdict(list)
    for k, g in grouping.group.items():
        members[g].append(by_obs[k])
    sizes = sorted((len(v) for v in members.values()), reverse=True)
    vecs = {b.obs: board_vectors(b, strategy, config, champions) for b in eligible}
    profiles = {g: mean_profile([vecs[b.obs] for b in bs]) for g, bs in sorted(members.items())}
    within = [similarity(vecs[b.obs], profiles[g], strategy) for g, bs in sorted(members.items()) for b in bs]
    index: dict[str, set[int]] = defaultdict(set)
    for g, (units, _) in profiles.items():
        for u, f in units.items():
            if f >= config.prune_presence:
                index[u].add(g)
    nearest = []
    for g, prof in profiles.items():
        near = [h for h in _candidates(prof[0], index, config.prune_min_shared) if h != g]
        nearest.append(max((similarity(prof, profiles[h], strategy) for h in near), default=0.0))
    return {
        "strategy": strategy.name,
        "description": strategy.description,
        "boards_considered": len(eligible),
        "boards_in_multi_board_groups": len(grouping.group),
        "boards_ungrouped": len(eligible) - len(grouping.group),
        "groups": len(sizes),
        "variants_before_merge": len(set(grouping.variant.values())),
        "group_size_quantiles": quantiles(sizes),
        "largest_groups": sizes[:10],
        "size_bands": {label: sum(1 for s in sizes if size_band(s) == label) for label, _, _ in SIZE_BANDS},
        "boards_by_size_band": {label: sum(s for s in sizes if size_band(s) == label) for label, _, _ in SIZE_BANDS},
        "within_group_similarity": quantiles(within),
        "nearest_other_group_similarity": quantiles(nearest),
        "nearest_other_group_note": "among groups sharing >= prune_min_shared units present on >= prune_presence of boards",
        "groups_with_nearest_other_at_or_above_tau": sum(x >= config.tau for x in nearest),
        "converged": grouping.converged,
        "refine_moves": grouping.refine_moves,
        "merges": grouping.merges,
        "merge_checks": grouping.merge_checks,
        "prune_audit": grouping.prune_audit,
        "log": grouping.log,
    }


# ---------------------------------------------------------------- sampling


def validation_sample(summaries: Sequence[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """Deterministic review sample: (reason, group id), each group at most once."""
    chosen: list[tuple[str, int]] = []
    seen: set[int] = set()
    by_size = sorted(summaries, key=lambda s: (-s["boards"], s["group"]))

    def take(reason: str, pool: Iterable[Mapping[str, Any]], k: int = 1) -> None:
        for s in pool:
            if k == 0:
                break
            if s["group"] not in seen:
                chosen.append((reason, s["group"]))
                seen.add(s["group"])
                k -= 1

    take("largest/common", by_size, 3)
    band = lambda lo, hi: [s for s in by_size if lo <= s["boards"] <= hi]  # noqa: E731
    medium = band(30, 189)
    take("medium (30-189 boards)", medium[len(medium) // 3:] + medium[: len(medium) // 3], 2)
    low = band(5, 29)
    take("low-frequency repeated (5-29 boards)", low[len(low) // 3:] + low[: len(low) // 3], 2)
    reviewable = [s for s in by_size if s["boards"] >= 10]
    for cost in (1, 2, 3):
        take(f"{cost}-cost-heavy / reroll-looking (primary itemized unit costs {cost}, 3-star on >= 30% of boards)",
             [s for s in reviewable if s["primary_itemized_cost"] == cost
              and s["units"][s["primary_itemized_unit"]]["stars"].get("3", 0) >= 0.3])
    take("expensive / high-cost (core mean cost >= 3.5)",
         sorted([s for s in reviewable if (s["core_mean_cost"] or 0) >= 3.5], key=lambda s: (-(s["core_mean_cost"] or 0), s["group"])))
    take("highly flexible (lowest stable-slot share)", sorted(reviewable, key=lambda s: (s["stable_slot_share"], s["group"])))
    take("substantial low-placement / incomplete boards (highest 7th-8th share)",
         sorted(reviewable, key=lambda s: (-s["low_placement_share_7_8"], s["group"])))
    return chosen


def same_unit_different_shells(summaries: Sequence[Mapping[str, Any]], *, min_boards: int = 10,
                               min_itemized: float = 0.5, max_core_jaccard: float = 0.5, limit: int = 3) -> list[dict[str, Any]]:
    """Champions heavily itemized (>= 2 completed items on >= min_itemized of
    boards) in >= 2 groups whose core candidates overlap <= max_core_jaccard."""
    by_unit: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for s in summaries:
        if s["boards"] < min_boards:
            continue
        for cid, u in s["units"].items():
            if u["two_plus_items"] >= min_itemized:
                by_unit[cid].append(s)
    cases = []
    for cid, groups in sorted(by_unit.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        groups = sorted(groups, key=lambda s: (-s["boards"], s["group"]))
        distinct: list[Mapping[str, Any]] = []
        for s in groups:
            if all(core_jaccard(s, d) <= max_core_jaccard for d in distinct):
                distinct.append(s)
        if len(distinct) >= 2:
            cases.append({"unit": cid, "groups": [s["group"] for s in distinct[:4]],
                          "pairwise_core_jaccard": [[round(core_jaccard(a, b), 2) for b in distinct[:4]] for a in distinct[:4]]})
        if len(cases) >= limit:
            break
    return cases


def similar_shell_different_unit(summaries: Sequence[Mapping[str, Any]], *, min_boards: int = 10,
                                 min_core_jaccard: float = 0.6, limit: int = 3) -> list[dict[str, Any]]:
    pool = sorted([s for s in summaries if s["boards"] >= min_boards and s["primary_itemized_unit"]],
                  key=lambda s: (-s["boards"], s["group"]))
    cases = []
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if a["primary_itemized_unit"] != b["primary_itemized_unit"] and core_jaccard(a, b) >= min_core_jaccard:
                cases.append({"groups": [a["group"], b["group"]], "core_jaccard": round(core_jaccard(a, b), 2),
                              "primary_itemized_units": [a["primary_itemized_unit"], b["primary_itemized_unit"]]})
                if len(cases) >= limit:
                    return cases
    return cases


# ---------------------------------------------------------------- report


STATISTICAL_WARNINGS = (
    "Final-board survivorship: only the board at elimination exists; winners field bigger, more expensive boards.",
    "Low placements are early, incomplete boards; groups with many 7th-8th boards partly describe board size, not a line.",
    "3-star / high-roll conditioning: hitting 3 stars and placing well are entangled; hit/miss is not controlled.",
    "Same-lobby non-independence: the 8 boards of a match share a lobby; same-lobby repeats are counted per group.",
    "Population: boards come from a selected NA ladder collection (seeded players and their lobbies), not all of ranked TFT.",
    "Provenance / rank imbalance: cohorts are sampled unevenly and nothing here is weighted by rank.",
    "Small samples: groups under ~30 boards have wide intervals (see the Wilson 95% interval on Top 4).",
    "Multiple comparisons: many groups x units x item sets are examined; the most extreme rows are biased upward.",
    "Item-set selection bias: observed sets reflect what players built, including Thief's Gloves random items.",
    "No causal interpretation: associations between structure, items and placement are descriptive only.",
)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def _special(d: Mapping[str, float]) -> float:
    return max(d["thiefs_gloves"], d["adaptive_helm"], d["unknown_intent_snapshot"], d["unmapped_item"])


def render_group(s: Mapping[str, Any], members: Sequence[Board], names: Names, config: ArchetypeConfig,
                 item_sets: Sequence[Mapping[str, Any]], reason: str | None = None) -> list[str]:
    o = s["outcome"]
    units = s["units"]
    lines = [f"#### Group {s['group']}" + (f" -- {reason}" if reason else ""),
             f"OBSERVATIONS: {s['boards']} boards ({o['distinct_matches']} matches, {s['same_lobby_repeats']} same-lobby repeats; "
             f"{_pct(s['share_of_eligible_boards'])} of eligible boards). Shop units per board: median "
             f"{s['shop_units_per_board']['median']}, range {s['shop_units_per_board']['min']}-{s['shop_units_per_board']['max']}. "
             f"7th-8th share {_pct(s['low_placement_share_7_8'])}.",
             "CORE CANDIDATES (presence >= %d%%, display convention): " % round(100 * config.display_core_presence)
             + (", ".join(f"{names.champion(u)} {_pct(units[u]['presence'])}" for u in s["core_candidates"]) or "-"),
             "OTHER COMMON UNITS: " + (", ".join(f"{names.champion(u)} {_pct(units[u]['presence'])}" for u in s["other_common_units"]) or "-"),
             "PRESENCE DISTRIBUTION: " + " ".join(f"{p:.2f}" for p in s["presence_distribution"][:14])
             + f"  (largest gap {s['largest_presence_gap']['gap']:.2f} after rank {s['largest_presence_gap']['after_rank']})",
             "TRAITS: " + ", ".join(f"{names.trait(k.rsplit(':', 1)[0])} (tier {k.rsplit(':', 1)[1]}) {_pct(f)}" for k, f in s["traits"][:6]),
             "ITEMIZED UNITS (>= 2 completed items): " + (", ".join(
                 f"{names.champion(u)} {_pct(d['two_plus_items'])}" for u, d in sorted(units.items(), key=lambda kv: (-kv[1]['two_plus_items'], kv[0]))[:4]
                 if d["two_plus_items"] >= 0.1) or "-"),
             "CARRY-QUALIFIED (current rule, descriptive): " + (", ".join(
                 f"{names.champion(u)} {_pct(d['carry_qualified'])}" for u, d in sorted(units.items(), key=lambda kv: (-kv[1]['carry_qualified'], kv[0]))[:4]
                 if d["carry_qualified"] >= 0.1) or "-"),
             "DEFENSIVE-ITEM HOLDERS (holds a TANK-intent item): " + (", ".join(
                 f"{names.champion(u)} {_pct(d['defensive_item_holder'])}" for u, d in sorted(units.items(), key=lambda kv: (-kv[1]['defensive_item_holder'], kv[0]))[:3]
                 if d["defensive_item_holder"] >= 0.1) or "-"),
             "SPECIAL ITEMS: " + (", ".join(
                 f"{names.champion(u)} TG {_pct(d['thiefs_gloves'])} / AH {_pct(d['adaptive_helm'])} / snapshot-UNKNOWN {_pct(d['unknown_intent_snapshot'])} / unmapped {_pct(d['unmapped_item'])}"
                 for u, d in sorted(units.items(), key=lambda kv: (-_special(kv[1]), kv[0]))[:4] if _special(d) >= 0.05) or "-"),
             "STAR LEVELS: " + ", ".join(
                 f"{names.champion(u)} " + "/".join(f"{k}*{_pct(v)}" for k, v in units[u]["stars"].items())
                 for u in (s["core_candidates"] or list(units))[:6]),
             f"PERFORMANCE (denominator: these {o['boards']} boards): avg placement {o['avg_placement']:.2f}, "
             f"top 4 {_pct(o['top4_rate'])} (95% CI {_pct(o['top4_wilson95'][0])}-{_pct(o['top4_wilson95'][1])}), win {_pct(o['win_rate'])}"]
    for sets in item_sets:
        lines.append(f"ITEM SETS on {names.champion(sets['unit'])} (population: {sets['population']} = {sets['population_definition']}; "
                     f"{sets['distinct_exact_sets']} distinct exact sets):")
        for kind in ("exact_sets", "two_item_cores"):
            for r in sets[kind][:3]:
                lines.append(f"  {'exact' if kind == 'exact_sets' else '2-core'}: {' + '.join(r['labels'])} -- n={r['observations']} "
                             f"({_pct(r['share_of_population'])}), avg {r['avg_placement']:.2f}, top4 {_pct(r['top4_rate'])}, "
                             f"win {_pct(r['win_rate'])}, 3* {_pct(r['three_star_share'])} {' '.join(r['flags'])}".rstrip())
    lines.append("MEMBER BOARDS (deterministic: best placement first, then spread across placements):")
    ordered = sorted(members, key=lambda b: (b.placement, b.obs))
    k = config.sample_member_boards
    picks = [ordered[round(i * (len(ordered) - 1) / max(1, k - 1))] for i in range(min(k, len(ordered)))]
    for b in {b.obs: b for b in picks}.values():
        shown = ", ".join(
            f"{names.champion(u.cid)} {u.tier}*" + (f" [{' / '.join(names.item(i) for i in u.items)}]" if u.items else "")
            for u in sorted(b.shop, key=lambda u: (-(roster_cost(u.cid, u.stored_cost, names.champions) or 0), u.cid)))
        extra = f"  (+{len(b.excluded)} non-shop)" if b.excluded else ""
        lines.append(f"  #{b.placement}: {shown}{extra}")
    return lines


def render_merge_diagnostics(d: Mapping[str, Any], names: Names) -> list[str]:
    """Concise Markdown for `MergeDiagnostics.summary()` (full values are in the JSON report)."""
    def f(x: Any) -> str:
        return "n/a" if x is None else f"{x:.4f}" if isinstance(x, float) else str(x)

    def dist(s: Mapping[str, Any]) -> str:
        return f"min {f(s['min'])} / p01 {f(s['p01'])} / p05 {f(s['p05'])} / p10 {f(s['p10'])} / median {f(s['median'])}"

    def buckets(b: Mapping[str, int]) -> str:
        return ", ".join(f"{k}: {v}" for k, v in b.items())

    def units(ids: Sequence[str]) -> str:
        return ", ".join(names.champion(u) for u in ids) or "-"

    rej, acc = d["rejected"], d["accepted"]
    lines = ["", "### Merge-result similarity diagnostics (research measurement only; no decision depends on it)",
             f"- attempts reaching the similarity check: {d['attempts_evaluated']} (accepted {d['accepted_attempts']}, "
             f"rejected {d['rejected_similarity_attempts']}); tau {d['tau']}. Definitions are in the JSON report "
             "(`merge_diagnostics.definitions`)."]
    for label, agg in (("rejected", rej), ("accepted", acc)):
        if not agg["attempts"]:
            lines.append(f"- {label}: none")
            continue
        q = agg["quantiles"]
        lines += [
            f"- {label}: boards below tau per attempt: {buckets(agg['below_tau_count_buckets'])}",
            f"- {label}: weakest board's distance from tau ({'below' if label == 'rejected' else 'above'}): "
            + buckets(agg["weakest_distance_from_tau_buckets"]),
            f"- {label}: weakest board's pre-merge margin above tau: {buckets(agg['weakest_pre_merge_margin_buckets'])}",
            f"- {label}: weakest board's change (post - pre): {buckets(agg['weakest_degradation_buckets'])}",
            f"- {label}: core overlap: {buckets(agg['core_overlap_buckets'])}; splash-exception pairs "
            f"{agg['splash_exception_attempts']}",
            f"- {label}: merged size band -> attempts: "
            + ", ".join(f"{band}: {sum(v.values())}" for band, v in agg["merged_size_bands"].items()),
            f"- {label}: medians -- merged boards {f(q['merged_boards']['median'])}, core overlap "
            f"{f(q['core_overlap']['median'])}, weakest pre {f(q['weakest_pre_merge_similarity']['median'])} -> post "
            f"{f(q['weakest_post_merge_similarity']['median'])}, post-merge p10 {f(q['post_merge_p10']['median'])}, "
            f"post-merge median {f(q['post_merge_median']['median'])}",
        ]
    if rej["attempts"]:
        lines.append("- rejected: exactly-1-below-tau attempts by merged size band: "
                     + ", ".join(f"{band}: {v['exactly 1']}/{sum(v.values())}" for band, v in rej["merged_size_bands"].items()))
        lines.append("- rejected: boards below tau x weakest distance below tau: " + "; ".join(
            f"{k} -> {buckets({b: n for b, n in v.items() if n})}" for k, v in rej["below_tau_count_x_distance_from_tau"].items()
            if sum(v.values())))
        lines.append(f"- rejected: failing boards come from {buckets(rej['failing_boards_by_side'])}")
    lines += ["", "#### Deterministic sample of judged merges (selection by structure/threshold only, never placement)"]
    for r in d["sample"]:
        w = r["weakest"]
        lines += [
            f"- attempt {r['attempt']} [{r['reason']}] {r['outcome']}: a {r['a_boards']} boards ({r['a_variants']} variants), "
            f"b {r['b_boards']} boards ({r['b_variants']} variants), merged {r['merged_boards']}; core overlap "
            f"{f(r['core_overlap'])}, shared {r['shared_core_size']}, identical cores {r['identical_cores']}, "
            f"splash exception {r['splash_exception_applies']}",
            f"  - core a ({r['core_a_size']}): {units(r['core_a'])} | core b ({r['core_b_size']}): {units(r['core_b'])} | "
            f"merged core ({r['merged_core_size']}): {units(r['merged_core'])}",
            f"  - a only: {units(r['a_only'])}; b only: {units(r['b_only'])}; dropped from merged core: "
            f"{units(r['dropped_from_merged_core'])}; new in merged core: {units(r['new_in_merged_core'])}",
            f"  - pre a: {dist(r['pre_a'])}; pre b: {dist(r['pre_b'])}",
            f"  - post: {dist(r['post'])}",
            f"  - below tau: {r['below_tau']} ({100 * r['below_tau_share']:.2f}%; from a {r['below_tau_from_a']}, "
            f"from b {r['below_tau_from_b']}); shortfall max {f(r['shortfall_max'])}, mean {f(r['shortfall_mean'])}, "
            f"median {f(r['shortfall_median'])}",
            f"  - weakest board (side {w['side']}): pre {f(w['pre_merge_similarity'])} -> post {f(w['post_merge_similarity'])} "
            f"(delta {f(w['delta'])}); tau {w['tau']}; shortfall below tau {f(w['shortfall_below_tau'])}; "
            f"pre-merge margin {f(w['pre_margin_above_tau'])}",
        ]
    return lines + [""]


def render_shadow_evaluation(sh: Mapping[str, Any], names: Names) -> list[str]:
    """Markdown for `shadow_evaluation` -- every decision-relevant count is
    printed here, not only in the JSON artifact."""
    def pct(k: int, n: int) -> str:
        return f"{k} ({100 * k / n:.1f}%)" if n else f"{k}"

    def counts(b: Mapping[str, int], n: int) -> str:
        return ", ".join(f"{k}: {pct(v, n)}" for k, v in b.items())

    def med(q: Mapping[str, Any] | None, key: str = "median") -> str:
        return "n/a" if not q else f"{q[key]:.4f}" if isinstance(q[key], float) else str(q[key])

    def units(ids: Sequence[str]) -> str:
        return ", ".join(names.champion(u) for u in ids) or "-"

    cands = sh["candidates"]
    lines = ["", "### Shadow merge-rule evaluation (REPORT ONLY -- no grouping decision depends on it)",
             "One-step counterfactual on the actual merge trajectory: each candidate is asked whether it would have "
             "accepted each attempt the real rule judged; a shadow acceptance changes no group, so later attempts are "
             "unchanged and what a candidate would build as the real rule is NOT simulated. Candidates were declared "
             "before any real run. Definitions: `merge_diagnostics.shadow.definitions` in the JSON report.",
             *(f"- {c}: {SHADOW_DEFINITIONS[c]}" for c in SHADOW_CANDIDATES),
             f"- S0 sanity check: {sh['s0_mismatches_with_actual_decision']} attempts where S0 differs from the actual "
             "decision (must be 0).", "",
             "| candidate | attempts | shadow accepted | acceptance | recovered (actual rejected) | lost (actual accepted) | net |",
             "|---|---|---|---|---|---|---|"]
    for c in SHADOW_CANDIDATES:
        d = cands[c]
        rate = "n/a" if d["acceptance_rate"] is None else f"{100 * d['acceptance_rate']:.1f}%"
        lines.append(f"| {c} | {d['attempts']} | {d['shadow_accepted']} | {rate} | {d['recovered']} | "
                     f"{d['lost_vs_actual']} | {d['net_vs_actual']:+d} |")
    for c in SHADOW_CANDIDATES[1:]:
        d = cands[c]
        rs, n = d["recovery_set"], d["recovery_set"]["attempts"]
        q = rs["quantiles"]
        lines += [
            "", f"#### {c} recovery set ({n} attempts the real rule rejected)",
            f"- boards below tau (merged): {counts(rs['below_tau_count_buckets'], n)}",
            f"- smaller-side share below tau: {counts(rs['smaller_side_share_buckets'], n)}",
            f"- larger-side boards below tau: {counts(rs['larger_side_failures'], n)}",
            f"- core overlap: {counts(rs['core_overlap_buckets'], n)}",
            f"- merged size band: {counts(rs['merged_size_bands'], n)}",
            f"- DANGER (recovered): {counts(rs['dangers'], n)}",
            f"- DANGER (all shadow-accepted, {d['shadow_accepted']}): {counts(d['dangers_among_shadow_accepted'], d['shadow_accepted'])}",
            f"- family proxies (recovered): {counts(rs['families'], n)}",
            f"- medians: weakest post {med(q['weakest_post_merge_similarity'])} (min {med(q['weakest_post_merge_similarity'], 'min')}), "
            f"post p10 {med(q['post_merge_p10'])}, post median {med(q['post_merge_median'])}, merged boards "
            f"{med(q['merged_boards'])} (max {med(q['merged_boards'], 'max')}), smaller side {med(q['smaller_side_boards'])} boards, "
            f"smaller-side share below tau {med(q['smaller_side_below_tau_share'])} (p90 {med(q['smaller_side_below_tau_share'], 'p90')}), "
            f"core overlap {med(q['core_overlap'])}",
        ]
    lines += ["", "#### Candidate difference sets (what each added constraint removes)"]
    for label, d in sh["differences"].items():
        failed = next(v for k, v in d.items() if k.startswith("failed_conditions_of_"))
        lines.append(f"- {label}: {d['attempts']}; failing conditions: "
                     + (", ".join(f"{k}: {v}" for k, v in failed.items()) or "-")
                     + f"; core overlap: {', '.join(f'{k}: {v}' for k, v in d['core_overlap_buckets'].items())}"
                     + f"; smaller-side share: {', '.join(f'{k}: {v}' for k, v in d['smaller_side_share_buckets'].items())}")
    lines.append(f"- recovered by S3: {sh['recovered_by_S3']}")
    lines.append("- recovery-set overlaps |X and Y|: " + "; ".join(
        f"{x}&{y}: {v}" for x, row in sh["recovery_overlap"].items() for y, v in row.items() if x < y))
    lines += ["", "#### Deterministic review samples of recovered attempts (structure only, never placement)"]
    for c in SHADOW_CANDIDATES[1:]:
        for r in cands[c]["sample"]:
            post = r["post"]
            lines += [
                f"- {c} attempt {r['attempt']} [{r['reason']}]: merged {r['merged_boards']} = larger {r['larger_side_boards']} + "
                f"smaller {r['smaller_side_boards']}; overlap {r['core_overlap']:.3f}, identical {r['identical_cores']}, "
                f"splash {r['splash_exception_applies']}; below tau {r['below_tau']} ({100 * r['below_tau_share']:.2f}%): "
                f"larger {r['larger_side_below_tau']}, smaller {r['smaller_side_below_tau']} "
                f"({100 * r['smaller_side_below_tau_share']:.0f}% of smaller); min post larger {r['larger_side_min_post']:.4f}, "
                f"smaller {r['smaller_side_min_post']:.4f}; post p10 {post['p10']:.4f}, median {post['median']:.4f}",
                f"  - larger core: {units(r['core_larger'])} | smaller core: {units(r['core_smaller'])} | "
                f"larger only: {units(r['larger_only'])}; smaller only: {units(r['smaller_only'])}",
            ]
    return lines + [""]


# ---------------------------------------------------------------- experimental S2 strategies: final groups, comparison, review

#: Structural lookups for human review of the experimental strategies (report
#: only; never used by grouping). A group matches when every anchor unit is in
#: its core (presence >= the display core threshold). Labels are descriptive,
#: not claims that a group IS that comp.
FAMILY_ANCHORS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Aphelios/Nidalee", frozenset({"DA_18_Aphelios", "DA_Nidalee18_AP"})),
    ("Summoner-like (Zyra/Soraka/Malphite)", frozenset({"DA_18_Zyra", "DA_18_Soraka", "DA_18_Malphite"})),
    ("Veigar reroll-like (Veigar/Ornn/Rek'Sai)", frozenset({"DA_18_Veigar", "DA_18_Ornn", "DA_18_RekSai"})),
    ("Caitlyn reroll-like (Caitlyn/Scuttlecrab)", frozenset({"DA_18_Caitlyn", "DA_Scuttlecrab18"})),
    ("Kha'Zix reroll-like (Kha'Zix/Hecarim/Diana)", frozenset({"DA_18_KhaZix", "DA_18_Hecarim", "DA_18_Diana"})),
)
#: Shells of the mixed groups seen under the pre-PR-#29 merge (run #2's B35,
#: B36, B49); matched structurally, never by historical group id.
REGRESSION_ANCHORS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Elder Dragon / Sentinel / Draven", frozenset({"DA_18_ElderDragon", "DA_Sentinel18", "DA_Draven18"})),
    ("Diana / Sentinel / Taric", frozenset({"DA_18_Diana", "DA_Sentinel18", "DA_Taric18"})),
    ("Alune / Diana / Fiddlesticks", frozenset({"DA_18_Alune", "DA_18_Diana", "DA_Fiddlesticks18"})),
)
#: The run #2 smearing shape: a group of >= this many boards whose core has
#: <= TINY_CORE_UNITS units.
TINY_CORE_UNITS = 4
TINY_CORE_MIN_BOARDS = 30
REVIEW_PER_REASON = 3


def final_group_diagnostics(boards: Sequence[Board], grouping: Grouping, strategy: Strategy,
                            config: ArchetypeConfig) -> dict[str, Any]:
    """Every grouped board against its FINAL group's mean profile (after all
    merges), with the strategy's similarity: below-tau membership, weakest
    member, how many variants were merged in, and core drift (1 - Jaccard of
    the final core and the core of the group's largest original variant)."""
    champions = load_roster().champions
    by_obs = {b.obs: b for b in boards}
    members: dict[int, list[int]] = defaultdict(list)
    for k, g in sorted(grouping.group.items()):
        members[g].append(k)
    groups = []
    for g, obs in sorted(members.items()):
        vecs = [board_vectors(by_obs[k], strategy, config, champions) for k in obs]
        profile = mean_profile(vecs)
        sims = sorted(similarity(v, profile, strategy) for v in vecs)
        below = sum(x < config.tau for x in sims)

        def core(ks: Sequence[int]) -> set[str]:
            c = Counter(u for k in ks for u in by_obs[k].identity)
            return {u for u, n in c.items() if n / len(ks) >= config.core_presence}

        by_variant: dict[int, list[int]] = defaultdict(list)
        for k in obs:
            by_variant[grouping.variant[k]].append(k)
        largest = min(by_variant, key=lambda v: (-len(by_variant[v]), v))
        final_core, base_core = core(obs), core(by_variant[largest])
        union = final_core | base_core
        groups.append({"group": g, "boards": len(obs), "variants": len(by_variant), "below_tau": below,
                       "below_tau_share": below / len(obs), "min_similarity": sims[0],
                       "median_similarity": sims[(len(sims) - 1) // 2],
                       "core_drift": 1 - len(final_core & base_core) / len(union) if union else 0.0,
                       "final_core_size": len(final_core)})
    grouped = sum(r["boards"] for r in groups)
    below = sum(r["below_tau"] for r in groups)
    return {
        "definition": "every grouped board vs its final group's mean profile after all merges (strategy similarity)",
        "groups": len(groups), "grouped_boards": grouped, "boards_below_tau": below,
        "boards_below_tau_share": below / grouped if grouped else None,
        "groups_with_any_board_below_tau": sum(r["below_tau"] > 0 for r in groups),
        "groups_over_1pct_below_tau": sum(100 * r["below_tau"] > r["boards"] for r in groups),
        "groups_over_5pct_below_tau": sum(20 * r["below_tau"] > r["boards"] for r in groups),
        "groups_over_10pct_below_tau": sum(10 * r["below_tau"] > r["boards"] for r in groups),
        "groups_merged_from_2plus_variants": sum(r["variants"] > 1 for r in groups),
        "tiny_core_large_groups": sum(r["final_core_size"] <= TINY_CORE_UNITS and r["boards"] >= TINY_CORE_MIN_BOARDS
                                      for r in groups),
        "quantiles": {"group_min_similarity": quantiles(r["min_similarity"] for r in groups),
                      "group_below_tau_share": quantiles(r["below_tau_share"] for r in groups),
                      "variants_per_group": quantiles(r["variants"] for r in groups),
                      "core_drift": quantiles(r["core_drift"] for r in groups)},
        "per_group": groups,
    }


def control_comparison(control: Mapping[str, Any], experimental: Mapping[str, Any], control_final: Mapping[str, Any],
                       experimental_final: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows (metric, control, experimental, delta) -- structural outcomes, not quality judgments."""
    def q(m: Mapping[str, Any], key: str, stat: str) -> Any:
        return (m.get(key) or {}).get(stat)

    rows = [("groups", control["groups"], experimental["groups"]),
            ("boards assigned", control["boards_in_multi_board_groups"], experimental["boards_in_multi_board_groups"]),
            ("boards ungrouped", control["boards_ungrouped"], experimental["boards_ungrouped"]),
            ("accepted merges", control["merges"], experimental["merges"]),
            ("rejected: core", control["merge_checks"].get("rejected_core"), experimental["merge_checks"].get("rejected_core")),
            ("rejected: similarity (control: every board >= tau; experimental: S2)",
             control["merge_checks"].get("rejected_similarity"), experimental["merge_checks"].get("rejected_similarity")),
            ("near-duplicate groups (nearest other >= tau)", control["groups_with_nearest_other_at_or_above_tau"],
             experimental["groups_with_nearest_other_at_or_above_tau"]),
            ("within-group similarity median", q(control, "within_group_similarity", "median"),
             q(experimental, "within_group_similarity", "median")),
            ("within-group similarity p10", q(control, "within_group_similarity", "p10"),
             q(experimental, "within_group_similarity", "p10")),
            ("within-group similarity min", q(control, "within_group_similarity", "min"),
             q(experimental, "within_group_similarity", "min")),
            ("group size median", q(control, "group_size_quantiles", "median"), q(experimental, "group_size_quantiles", "median")),
            ("group size p90", q(control, "group_size_quantiles", "p90"), q(experimental, "group_size_quantiles", "p90")),
            ("group size max", q(control, "group_size_quantiles", "max"), q(experimental, "group_size_quantiles", "max")),
            ("grouped boards below tau (final profile)", control_final["boards_below_tau"], experimental_final["boards_below_tau"]),
            ("share of grouped boards below tau", control_final["boards_below_tau_share"],
             experimental_final["boards_below_tau_share"]),
            ("groups with >= 1 board below tau", control_final["groups_with_any_board_below_tau"],
             experimental_final["groups_with_any_board_below_tau"]),
            ("groups with > 1% below tau", control_final["groups_over_1pct_below_tau"], experimental_final["groups_over_1pct_below_tau"]),
            ("groups with > 5% below tau", control_final["groups_over_5pct_below_tau"], experimental_final["groups_over_5pct_below_tau"]),
            ("groups with > 10% below tau", control_final["groups_over_10pct_below_tau"],
             experimental_final["groups_over_10pct_below_tau"]),
            (f"groups >= {TINY_CORE_MIN_BOARDS} boards with core <= {TINY_CORE_UNITS} units", control_final["tiny_core_large_groups"],
             experimental_final["tiny_core_large_groups"])]
    rows += [(f"groups in size band {band}", control["size_bands"][band], experimental["size_bands"][band])
             for band in control["size_bands"]]
    rows += [(f"boards in size band {band}", control["boards_by_size_band"][band], experimental["boards_by_size_band"][band])
             for band in control["boards_by_size_band"]]
    out = []
    for metric, a, b in rows:
        delta = b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        out.append({"metric": metric, "control": a, "experimental": b, "delta": delta})
    return out


def anchored_groups(summaries: Mapping[int, Mapping[str, Any]], anchors: frozenset[str]) -> list[Mapping[str, Any]]:
    """Groups whose core contains every anchor unit, largest first (ties: group id)."""
    return sorted((s for s in summaries.values() if anchors <= set(s["core_candidates"])),
                  key=lambda s: (-s["boards"], s["group"]))


def chaining_review(final: Mapping[str, Any]) -> list[tuple[str, int]]:
    """Deterministic, bounded: (reason, group id), each group once; never by placement."""
    groups = final["per_group"]
    reasons = [
        ("highest share of members below tau (>= 10 boards)", [r for r in groups if r["boards"] >= 10],
         lambda r: (-r["below_tau_share"], -r["boards"], r["group"])),
        ("weakest member similarity", groups, lambda r: (r["min_similarity"], -r["boards"], r["group"])),
        ("largest groups", groups, lambda r: (-r["boards"], r["group"])),
        ("most variants merged in", [r for r in groups if r["variants"] > 1], lambda r: (-r["variants"], -r["boards"], r["group"])),
        ("largest core drift from its largest original variant", [r for r in groups if r["variants"] > 1],
         lambda r: (-r["core_drift"], -r["boards"], r["group"])),
    ]
    chosen: dict[int, str] = {}
    for reason, pool, key in reasons:
        for r in [r for r in sorted(pool, key=key) if r["group"] not in chosen][:REVIEW_PER_REASON]:
            chosen[r["group"]] = reason
    return [(reason, g) for g, reason in chosen.items()]


def light_group_cores(group: Mapping[int, int], by_obs: Mapping[int, Board],
                      config: ArchetypeConfig) -> dict[int, dict[str, Any]]:
    """Per group: size, member observations and `core_candidates` exactly as
    `group_summary` defines them (presence >= display_core_presence, ordered
    by presence then id) -- without the item/outcome work, for replays."""
    members: dict[int, list[int]] = defaultdict(list)
    for k, g in sorted(group.items()):
        members[g].append(k)
    out = {}
    for g, obs in sorted(members.items()):
        presence = Counter(u for k in obs for u in by_obs[k].identity)
        core = [u for u, c in sorted(presence.items(), key=lambda kv: (-kv[1], kv[0])) if c / len(obs) >= config.display_core_presence]
        out[g] = {"group": g, "boards": len(obs), "core_candidates": core, "members": frozenset(obs)}
    return out


FAMILY_CHANGE_LABELS: tuple[str, ...] = ("identical", "more merged", "more fragmented",
                                         "same group count, different membership")
REGRESSION_CHANGE_LABELS: tuple[str, ...] = (
    "absent in both", "appears (absent in baseline)", "disappears (absent in replay)", "present in both: identical",
    "present in both: more groups", "present in both: fewer groups", "present in both: same group count, different membership")
#: Numeric per-run metrics compared between each replay and the baseline.
ORDER_METRICS: tuple[str, ...] = (
    "groups", "grouped_boards", "ungrouped_boards", "accepted_merges", "core_rejections", "similarity_rejections",
    "condition1_rejections", "historical_tail_attributed_condition1_rejections", "counterfactual_eligible",
    "counterfactual_recoveries", "boards_admitted_below_tau", "final_boards_below_tau",
    "final_groups_with_any_board_below_tau", "tiny_core_large_groups")
ORDER_SAMPLE_LIMIT = 5
ORDER_SENSITIVITY_DEFINITIONS: dict[str, str] = {
    "baseline": "the REAL S2 grouping (canonical); the 'baseline tie order' replay re-runs it through the replay path "
                "and must be identical (checked, not assumed).",
    "tie_stats": "judged steps on the replay path; a step 'had tied candidates' when >= 2 valid candidates shared the "
                 "top core overlap (only those steps can differ between tie orders).",
    "partition_vs_baseline": "pairwise co-membership over the strategy's eligible boards (>= min_identity_units; "
                             "ungrouped boards are singletons): pairs together in the baseline but apart in the replay, "
                             "and vice versa. Shares use two stated denominators: all eligible board pairs, and pairs "
                             "together in either partition. Pair counts come from group-intersection sizes.",
    "family_anchors": "groups whose core (presence >= display core threshold) contains every anchor unit, as in the "
                      "family review; 'identical' = the same matching groups with the same members; otherwise more "
                      "merged (fewer matching groups), more fragmented (more), or same count with different membership. "
                      "Descriptive structural proxies, not ground truth; fewer groups is not better by itself.",
    "regression_anchors": "the same lookup for the shells of earlier mixed groups (report only; never used by "
                          "grouping), compared with the baseline.",
}


def _anchor_lookup(light: Mapping[int, Mapping[str, Any]], anchors: frozenset[str]) -> dict[str, Any]:
    m = anchored_groups(light, anchors)
    return {"groups": len(m), "boards": sum(s["boards"] for s in m), "largest_sizes": [s["boards"] for s in m[:8]],
            "largest_core_sizes": [len(s["core_candidates"]) for s in m[:8]],
            "largest_cores": [list(s["core_candidates"]) for s in m[:3]],
            "members": frozenset(s["members"] for s in m)}


def _family_change(base: Mapping[str, Any], rep: Mapping[str, Any]) -> str:
    if base["members"] == rep["members"]:
        return FAMILY_CHANGE_LABELS[0]
    if rep["groups"] != base["groups"]:
        return FAMILY_CHANGE_LABELS[1] if rep["groups"] < base["groups"] else FAMILY_CHANGE_LABELS[2]
    return FAMILY_CHANGE_LABELS[3]


def _regression_change(base: Mapping[str, Any], rep: Mapping[str, Any]) -> str:
    if not base["groups"] or not rep["groups"]:
        return REGRESSION_CHANGE_LABELS[0 if not base["groups"] and not rep["groups"] else 1 if not base["groups"] else 2]
    if base["members"] == rep["members"]:
        return REGRESSION_CHANGE_LABELS[3]
    if rep["groups"] != base["groups"]:
        return REGRESSION_CHANGE_LABELS[4] if rep["groups"] > base["groups"] else REGRESSION_CHANGE_LABELS[5]
    return REGRESSION_CHANGE_LABELS[6]


def order_sensitivity(boards: Sequence[Board], strategy: Strategy, grouping: Grouping, config: ArchetypeConfig,
                      final: Mapping[str, Any]) -> dict[str, Any]:
    """Compare every report-only order replay with the real S2 grouping:
    run metrics, pairwise partition disagreement, family and regression
    anchors. Structural only; placement is never read."""
    by_obs = {b.obs: b for b in boards}
    universe = [b.obs for b in boards if len(b.identity) >= config.min_identity_units]

    def run(group: Mapping[int, int], merges: int, checks: Mapping[str, int], counts: Mapping[str, int],
            fin: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if fin is None:
            fin = final_group_diagnostics(boards, Grouping(variant=grouping.variant, group=dict(group), log=[],
                                                           converged=grouping.converged, refine_moves=[], merges=merges,
                                                           merge_checks=dict(checks)), strategy, config)
        light = light_group_cores(group, by_obs, config)
        row = {"groups": fin["groups"], "grouped_boards": fin["grouped_boards"],
               "ungrouped_boards": len(universe) - fin["grouped_boards"], "accepted_merges": merges,
               "core_rejections": checks.get("rejected_core"), "similarity_rejections": counts["similarity_rejections"],
               "condition1_rejections": counts["condition1_rejections"],
               "historical_tail_attributed_condition1_rejections": counts["historical_tail_attributed_condition1_rejections"],
               "counterfactual_eligible": counts["counterfactual_eligible"],
               "counterfactual_recoveries": counts["counterfactual_recoveries"],
               "boards_admitted_below_tau": counts["boards_admitted_below_tau"],
               "final_boards_below_tau": fin["boards_below_tau"],
               "final_groups_with_any_board_below_tau": fin["groups_with_any_board_below_tau"],
               "tiny_core_large_groups": fin["tiny_core_large_groups"]}
        families = {label: _anchor_lookup(light, anchors) for label, anchors in FAMILY_ANCHORS}
        regressions = {label: _anchor_lookup(light, anchors) for label, anchors in REGRESSION_ANCHORS}
        return row, families, regressions

    def public(lookup: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in lookup.items() if k != "members"}

    base_row, base_fam, base_reg = run(grouping.group, grouping.merges, grouping.merge_checks, grouping.lock_in["counts"], final)
    baseline = {"replay": "real S2 grouping (canonical baseline)", **base_row,
                "decision_rule_mismatches": grouping.merge_diagnostics["decision_rule_mismatches"],
                "family_anchors": [{"label": label, **public(base_fam[label])} for label, _ in FAMILY_ANCHORS],
                "regression_anchors": [{"label": label, **public(base_reg[label])} for label, _ in REGRESSION_ANCHORS]}
    replays, family_changes, appearances = [], [], []
    for i, rep in enumerate(grouping.order_replays):
        group = {k: rep["variant_to_group"][v] for k, v in grouping.variant.items()}
        identical = group == grouping.group and rep["merges"] == grouping.merges and rep["merge_checks"] == grouping.merge_checks
        row, fam, reg = run(group, rep["merges"], rep["merge_checks"], rep["counts"], final if identical else None)
        fam_rows = []
        for j, (label, anchors) in enumerate(FAMILY_ANCHORS):
            change = _family_change(base_fam[label], fam[label])
            fam_rows.append({"label": label, **public(fam[label]), "change_vs_baseline": change,
                             "groups_delta": fam[label]["groups"] - base_fam[label]["groups"],
                             "boards_delta": fam[label]["boards"] - base_fam[label]["boards"]})
            if change != FAMILY_CHANGE_LABELS[0]:
                b0 = (base_fam[label]["largest_sizes"] or [0])[0]
                r0 = (fam[label]["largest_sizes"] or [0])[0]
                bc = set((base_fam[label]["largest_cores"] or [[]])[0])
                rc = set((fam[label]["largest_cores"] or [[]])[0])
                family_changes.append(((-abs(r0 - b0), -abs(fam[label]["groups"] - base_fam[label]["groups"]), j, i), {
                    "reason": "largest order-sensitive family change", "replay": rep["replay"], "family": label,
                    "change_vs_baseline": change, "baseline_groups": base_fam[label]["groups"],
                    "baseline_sizes": base_fam[label]["largest_sizes"], "baseline_cores": base_fam[label]["largest_cores"],
                    "replay_groups": fam[label]["groups"], "replay_sizes": fam[label]["largest_sizes"],
                    "replay_cores": fam[label]["largest_cores"],
                    "largest_core_differing_units": sorted(bc ^ rc)}))
        reg_rows = []
        for label, _ in REGRESSION_ANCHORS:
            change = _regression_change(base_reg[label], reg[label])
            reg_rows.append({"label": label, **public(reg[label]), "change_vs_baseline": change})
            if change == REGRESSION_CHANGE_LABELS[1]:
                appearances.append({"reason": "replay creates a regression-anchor pattern", "replay": rep["replay"],
                                    "pattern": label, "replay_groups": reg[label]["groups"],
                                    "replay_sizes": reg[label]["largest_sizes"],
                                    "replay_core_sizes": reg[label]["largest_core_sizes"],
                                    "replay_cores": reg[label]["largest_cores"]})
        replays.append({"replay": rep["replay"], "mode": rep["mode"], "seed": rep["seed"], **row,
                        "decision_rule_mismatches": rep["decision_rule_mismatches"], "tie_stats": rep["tie_stats"],
                        "identical_to_real_grouping": identical,
                        "partition_vs_baseline": partition_disagreement(universe, grouping.group, group),
                        "deltas_vs_baseline": {k: (row[k] - base_row[k]) if isinstance(row[k], int) and isinstance(base_row[k], int)
                                               else None for k in ORDER_METRICS},
                        "family_anchors": fam_rows, "regression_anchors": reg_rows})
    baseline_replay = next((r for r in replays if r["mode"] == "baseline"), None)
    others = [r for r in replays if r["mode"] != "baseline"]
    samples = [case for _, case in sorted(family_changes, key=lambda kv: kv[0])[:2]] + appearances[:ORDER_SAMPLE_LIMIT]
    return {
        "definitions": ORDER_SENSITIVITY_DEFINITIONS,
        "replay_modes": [r["replay"] for r in replays],
        "baseline": baseline,
        "baseline_replay_identical_to_real_grouping": baseline_replay["identical_to_real_grouping"] if baseline_replay else None,
        "replays_with_any_partition_change": sum(r["partition_vs_baseline"]["disagreeing_pairs"] > 0 for r in others),
        "max_disagreement_share_of_all_pairs": max((r["partition_vs_baseline"]["disagreement_share_of_all_pairs"]
                                                    for r in others), default=None),
        "max_disagreement_share_of_pairs_together_in_either": max(
            (r["partition_vs_baseline"]["disagreement_share_of_pairs_together_in_either"] for r in others), default=None),
        "replays": replays,
        "deterministic_review_samples": samples,
    }


def experimental_review(boards: Sequence[Board], strategy: Strategy, grouping: Grouping, metrics: Mapping[str, Any],
                        summaries: Mapping[int, Mapping[str, Any]], members: Mapping[int, Sequence[Board]],
                        control: Mapping[str, Any], names: Names, config: ArchetypeConfig) -> dict[str, Any]:
    """JSON for an experimental strategy (report-only): control comparison,
    accepted-merge trajectory, final-group membership, review samples."""
    final = final_group_diagnostics(boards, grouping, strategy, config)
    control_final = final_group_diagnostics(boards, control["grouping"], control["strategy"], config)
    ctrl_summaries = control["summaries"]
    family = []
    for label, anchors in FAMILY_ANCHORS:
        ctrl, mine = anchored_groups(ctrl_summaries, anchors), anchored_groups(summaries, anchors)
        detail = mine[0]["group"] if mine else None
        item_sets = []
        if detail is not None:
            s = summaries[detail]
            top = sorted(s["units"], key=lambda u: (-max(s["units"][u]["two_plus_items"], s["units"][u]["carry_qualified"]), u))[:2]
            item_sets = [item_set_summary(members[detail], u, config, names) for u in top if s["units"][u]["two_plus_items"] >= 0.15]
        family.append({"label": label, "anchors": sorted(anchors), "control_groups": len(ctrl),
                       "control_sizes": [s["boards"] for s in ctrl[:8]], "control_boards": sum(s["boards"] for s in ctrl),
                       "experimental_groups": len(mine), "experimental_sizes": [s["boards"] for s in mine[:8]],
                       "experimental_boards": sum(s["boards"] for s in mine), "detail_group": detail, "item_sets": item_sets})
    regression = []
    for label, anchors in REGRESSION_ANCHORS:
        ctrl, mine = anchored_groups(ctrl_summaries, anchors), anchored_groups(summaries, anchors)
        regression.append({"label": label, "anchors": sorted(anchors), "control_groups": len(ctrl),
                           "control_sizes": [s["boards"] for s in ctrl[:8]],
                           "control_core_sizes": [len(s["core_candidates"]) for s in ctrl[:8]],
                           "experimental_groups": len(mine), "experimental_sizes": [s["boards"] for s in mine[:8]],
                           "experimental_core_sizes": [len(s["core_candidates"]) for s in mine[:8]],
                           "experimental_detail": [s["group"] for s in mine[:REVIEW_PER_REASON]]})
    tiny = sorted((r for r in final["per_group"] if r["final_core_size"] <= TINY_CORE_UNITS
                   and r["boards"] >= TINY_CORE_MIN_BOARDS), key=lambda r: (-r["boards"], r["group"]))
    lock_in = None
    if grouping.lock_in:
        lock_in = {**grouping.lock_in, "order_sensitivity": order_sensitivity(boards, strategy, grouping, config, final)}
    return {
        "definition": "EXPERIMENTAL strategy -- report-only review; one Patch-window research population; S2 thresholds "
                      "frozen before validation run #5; same-population results are model-development evidence, not "
                      "independent validation",
        "control": strategy.variants_from,
        "decision_rule_mismatches": grouping.merge_diagnostics["decision_rule_mismatches"],
        "comparison": control_comparison(control["metrics"], metrics, control_final, final),
        "trajectory": grouping.merge_diagnostics["trajectory"],
        "final": final, "control_final": {k: v for k, v in control_final.items() if k != "per_group"},
        "family_review": family, "regression_review": regression,
        "tiny_core_groups": [r["group"] for r in tiny[:5]],
        "chaining_review": chaining_review(final),
        "recursive_lock_in": lock_in,
    }


def render_experimental_review(strategy: Strategy, exp: Mapping[str, Any], summaries: Mapping[int, Mapping[str, Any]],
                               members: Mapping[int, Sequence[Board]], names: Names, config: ArchetypeConfig) -> list[str]:
    """Markdown for an experimental strategy: comparison with its control,
    accepted-merge trajectory, final-group membership, and human-review
    samples. Every decision-relevant number is printed here."""
    def f(x: Any) -> str:
        return "n/a" if x is None else f"{x:.4f}" if isinstance(x, float) else str(x)

    def core_line(s: Mapping[str, Any]) -> str:
        return ", ".join(f"{names.champion(u)} {round(100 * s['units'][u]['presence'])}%" for u in s["core_candidates"])

    def flex_line(s: Mapping[str, Any]) -> str:
        return ", ".join(f"{names.champion(u)} {round(100 * s['units'][u]['presence'])}%" for u in s["other_common_units"][:8]) or "-"

    per_group = {r["group"]: r for r in exp["final"]["per_group"]}

    def compact(s: Mapping[str, Any], label: str = "") -> list[str]:
        r = per_group[s["group"]]
        return [f"- {label}group {s['group']}: {s['boards']} boards, {r['variants']} variants merged, core drift "
                f"{f(r['core_drift'])}; below tau (final profile) {r['below_tau']} ({100 * r['below_tau_share']:.1f}%), "
                f"min member similarity {f(r['min_similarity'])}, median {f(r['median_similarity'])}",
                f"  - core ({len(s['core_candidates'])}): {core_line(s)} | other common: {flex_line(s)}"]

    t, final = exp["trajectory"], exp["final"]
    lines = ["", f"### EXPERIMENTAL {strategy.label}: comparison with control {exp['control']} (structural outcomes, "
             "NOT quality judgments -- fewer groups or more merges are not better by themselves)",
             f"- S2 decision consistency: {exp['decision_rule_mismatches']} attempts where the real decision differs from "
             "the S2 conditions evaluated on the same measurements (must be 0).",
             "| metric | control | experimental | delta |", "|---|---|---|---|"]
    lines += [f"| {r['metric']} | {f(r['control'])} | {f(r['experimental'])} | {f(r['delta'])} |" for r in exp["comparison"]]
    q = t["quantiles"]
    lines += ["", "#### Accepted S2 merges, each measured WHEN ACCEPTED (tentative merged profile; one step)",
              f"- accepted merges: {t['accepted_merges']}; with >= 1 board below tau at acceptance: "
              f"{t['with_any_board_below_tau']}; identical cores: {t['identical_cores']}; C splash exception: "
              f"{t['splash_exception']}",
              "- core overlap: " + ", ".join(f"{k}: {v}" for k, v in t["core_overlap_buckets"].items()),
              "- merged size band: " + ", ".join(f"{k}: {v}" for k, v in t["merged_size_bands"].items()),
              *(f"- {k}: median {f((q[k] or {}).get('median'))}, p10 {f((q[k] or {}).get('p10'))}, min "
                f"{f((q[k] or {}).get('min'))}, max {f((q[k] or {}).get('max'))}" for k in q),
              "- by variants in the merged group right after the merge (later bands = merging into already-merged groups):"]
    for band, d in t["by_variants_after_merge"].items():
        lines.append(f"  - {band} variants: {d['accepted_merges']} merges, {d['with_any_board_below_tau']} with a board "
                     f"below tau; median min post {f((d['min_post'] or {}).get('median'))}, median post p10 "
                     f"{f((d['post_p10'] or {}).get('median'))}, max below-tau share {f((d['below_tau_share'] or {}).get('max'))}")
    fq = final["quantiles"]
    lines += ["", "#### Final groups after ALL recursive merges (every grouped board vs its final group profile)",
              f"- grouped boards {final['grouped_boards']}; below tau {final['boards_below_tau']} "
              f"({f(final['boards_below_tau_share'])}); groups with >= 1 below tau {final['groups_with_any_board_below_tau']}, "
              f"> 1% {final['groups_over_1pct_below_tau']}, > 5% {final['groups_over_5pct_below_tau']}, "
              f"> 10% {final['groups_over_10pct_below_tau']}; groups merged from 2+ variants "
              f"{final['groups_merged_from_2plus_variants']}; groups >= {TINY_CORE_MIN_BOARDS} boards with core <= "
              f"{TINY_CORE_UNITS} units: {final['tiny_core_large_groups']}",
              *(f"- {k}: min {f((v or {}).get('min'))}, p10 {f((v or {}).get('p10'))}, median {f((v or {}).get('median'))}, "
                f"p90 {f((v or {}).get('p90'))}, max {f((v or {}).get('max'))}" for k, v in fq.items())]
    lines += ["", "#### Composition-family review (structural anchor lookup; descriptive labels, not ground truth)"]
    for fam in exp["family_review"]:
        lines.append(f"- **{fam['label']}**: control {fam['control_groups']} matching groups (largest sizes "
                     f"{fam['control_sizes']}); experimental {fam['experimental_groups']} (largest sizes {fam['experimental_sizes']})")
        if fam["detail_group"] is not None:
            s = summaries[fam["detail_group"]]
            lines += compact(s, "largest experimental match: ")
            lines += ["  " + line for line in render_group(s, members[s["group"]], names, config, fam["item_sets"])]
    lines += ["", "#### Regression-pattern review (shells of earlier mixed groups; matched by structure only)"]
    for reg in exp["regression_review"]:
        lines.append(f"- **{reg['label']}**: control {reg['control_groups']} matching groups (sizes {reg['control_sizes']}, "
                     f"core sizes {reg['control_core_sizes']}); experimental {reg['experimental_groups']} (sizes "
                     f"{reg['experimental_sizes']}, core sizes {reg['experimental_core_sizes']})")
        for g in reg["experimental_detail"]:
            lines += ["  " + line for line in compact(summaries[g])]
    lines.append(f"- experimental groups >= {TINY_CORE_MIN_BOARDS} boards with core <= {TINY_CORE_UNITS} units (the run #2 "
                 f"smearing shape): {len(exp['tiny_core_groups'])}")
    for g in exp["tiny_core_groups"]:
        lines += ["  " + line for line in compact(summaries[g])]
    lines += ["", "#### Chaining review (bounded, deterministic, structure only; for human review, no score)"]
    for reason, g in exp["chaining_review"]:
        lines += compact(summaries[g], f"[{reason}] ")
        lines += [f"  - member board: {line}" for line in _member_lines(members[g], names)]
    if exp.get("recursive_lock_in"):
        lines += render_recursive_lock_in(exp["recursive_lock_in"], names)
    return lines + [""]


def render_recursive_lock_in(lock: Mapping[str, Any], names: Names) -> list[str]:
    """Markdown for the recursive lock-in section; every decision-relevant
    count is printed (full values in `experimental_s2.recursive_lock_in`)."""
    def f(x: Any) -> str:
        return "n/a" if x is None else f"{x:.4f}" if isinstance(x, float) else str(x)

    def units(ids: Sequence[str] | None) -> str:
        return ", ".join(names.champion(u) for u in ids or ()) or "-"

    def counts(d: Mapping[str, Any]) -> str:
        return "; ".join(f"{k}: {v}" for k, v in d.items()) or "-"

    def q(d: Mapping[str, Any] | None, stat: str = "median") -> str:
        return f(d.get(stat)) if d else "n/a"

    p, a = lock["accepted_tail_provenance_summary"], lock["similarity_rejection_attribution"]
    c, t, o = lock["historical_tail_removal_counterfactual"], lock["tail_trajectory"], lock["order_sensitivity"]
    lines = ["", "#### Recursive lock-in diagnostics (REPORT ONLY -- S2 itself is unchanged; no grouping decision reads this)",
             *(f"- {k}: {v}" for k, v in LOCK_IN_DEFINITIONS.items()),
             "", "##### Accepted-merge tail provenance",
             f"- accepted S2 merges: {p['accepted_merges']}; with >= 1 board below tau at acceptance: "
             f"{p['accepted_merges_with_any_board_below_tau']}",
             f"- boards admitted below tau (historical tails): {p['boards_admitted_below_tau']} (admission events "
             f"{p['tail_admission_events']}; admitted more than once {p['boards_admitted_below_tau_more_than_once']})",
             f"- admission events by side role: {counts(p['admission_events_by_side_role'])}; by pre-merge side: "
             f"{counts(p['admission_events_by_pre_merge_side'])}",
             f"- similarity at admission: min {q(p['similarity_at_admission'], 'min')}, median "
             f"{q(p['similarity_at_admission'])}, max {q(p['similarity_at_admission'], 'max')}",
             "", f"##### S2 similarity-rejection attribution (denominator: {a['s2_similarity_rejections']} {a['denominator']})",
             f"- S2 conditions failed (multi-label): {counts(a['condition_failures'])}",
             f"- exact failed-condition sets: {counts(a['exact_failed_condition_sets'])}",
             f"- categories (multi-label): {counts(a['categories'])}",
             f"- Condition-1 attribution (mutually exclusive): {counts(a['condition1_attribution'])}",
             f"- ... when Condition 1 was the ONLY failed condition: "
             f"{counts(a['condition1_attribution_when_condition1_is_the_only_failure'])}",
             f"- failing larger-side boards (below tau): {a['failing_larger_side_boards']}, of which historical tails "
             f"{a['failing_larger_side_boards_historical_tails']} and not historical {a['failing_larger_side_boards_not_historical']}; "
             f"historical tails on the larger side but at/above tau (not blockers): "
             f"{a['historical_tails_on_larger_side_at_or_above_tau']}",
             "", "##### historical_tail_removal_counterfactual (diagnostic of recursive lock-in; NOT evidence a merge should happen)",
             f"- eligible rejections (larger side holds >= 1 historical tail): {c['eligible_rejections']}; not evaluable "
             f"(every larger-side board removed): {c['not_evaluable_every_larger_side_board_removed']}",
             f"- would pass ALL unchanged S2 conditions after removal: {c['would_pass_all_unchanged_s2_conditions']}; "
             f"would still fail: {c['would_still_fail']}; sides flipped by removal: {c['sides_flipped']}",
             "- by Condition-1 attribution: " + "; ".join(f"{k}: {v['would_pass']}/{v['eligible']} pass"
                                                          for k, v in c["by_condition1_attribution"].items()),
             f"- failed conditions before: {counts(c['failed_conditions_before'])}",
             f"- failed conditions after: {counts(c['failed_conditions_after'])}",
             f"- transitions: {counts(c['failure_transitions'])}",
             f"- removed boards: total {c['removed_boards_total']}, median {q(c['removed_boards'])}, max "
             f"{q(c['removed_boards'], 'max')}; share of the larger side: median {q(c['removed_share_of_larger_side'])}, "
             f"max {q(c['removed_share_of_larger_side'], 'max')}",
             *(f"- {key} before -> after: median {q(c[key]['before'])} -> {q(c[key]['after'])}, min "
               f"{q(c[key]['before'], 'min')} -> {q(c[key]['after'], 'min')}, max {q(c[key]['before'], 'max')} -> "
               f"{q(c[key]['after'], 'max')}" for key in ("merged_boards", "min_similarity", "larger_side_below_tau",
                                                           "smaller_side_below_tau", "merged_below_tau")),
             "", "##### Tail trajectory (attempts after a below-tau admission, as the real S2 decided them)",
             *(f"- {k}: {counts(v) if isinstance(v, dict) else v}" for k, v in t.items()),
             "", "##### Merge-order sensitivity (REPORT-ONLY replays; the real S2 grouping stays the canonical baseline)",
             *(f"- {k}: {v}" for k, v in o["definitions"].items()),
             f"- baseline-order replay identical to the real grouping: {o['baseline_replay_identical_to_real_grouping']}; "
             f"other replays with any partition change: {o['replays_with_any_partition_change']}; max disagreement share: "
             f"{f(o['max_disagreement_share_of_all_pairs'])} of all eligible pairs, "
             f"{f(o['max_disagreement_share_of_pairs_together_in_either'])} of pairs together in either", ""]
    rows = [o["baseline"], *o["replays"]]
    lines += ["| metric | " + " | ".join(r["replay"] for r in rows) + " |", "|---|" + "---|" * len(rows)]
    lines += [f"| {k} | " + " | ".join(f(r[k]) for r in rows) + " |" for k in (*ORDER_METRICS, "decision_rule_mismatches")]
    lines.append("| tied steps / judged steps | - | " + " | ".join(
        f"{r['tie_stats']['steps_with_tied_candidates']}/{r['tie_stats']['judged_steps']}" for r in o["replays"]) + " |")
    lines.append("| pairs together in baseline -> apart / apart -> together | - | " + " | ".join(
        f"{r['partition_vs_baseline']['together_in_baseline_apart_in_replay']} / "
        f"{r['partition_vs_baseline']['together_in_replay_apart_in_baseline']}" for r in o["replays"]) + " |")
    lines.append("| boards with changed group membership | - | " + " | ".join(
        str(r["partition_vs_baseline"]["boards_with_changed_group_membership"]) for r in o["replays"]) + " |")
    lines += ["", "- family anchors (matching groups / boards / largest sizes; change vs baseline):"]
    for i, (label, _) in enumerate(FAMILY_ANCHORS):
        base = o["baseline"]["family_anchors"][i]
        lines.append(f"  - **{label}**: baseline {base['groups']} / {base['boards']} / {base['largest_sizes']}; largest core: "
                     f"{units((base['largest_cores'] or [[]])[0])}")
        for r in o["replays"]:
            fam = r["family_anchors"][i]
            lines.append(f"    - {r['replay']}: {fam['groups']} / {fam['boards']} / {fam['largest_sizes']} -- "
                         f"{fam['change_vs_baseline']}")
    lines += ["- regression anchors (matching groups / largest sizes / core sizes; change vs baseline):"]
    for i, (label, _) in enumerate(REGRESSION_ANCHORS):
        base = o["baseline"]["regression_anchors"][i]
        lines.append(f"  - **{label}**: baseline {base['groups']} / {base['largest_sizes']} / {base['largest_core_sizes']}; "
                     + "; ".join(f"{r['replay']}: {r['regression_anchors'][i]['groups']} -- "
                                 f"{r['regression_anchors'][i]['change_vs_baseline']}" for r in o["replays"]))
    lines += ["", "##### Deterministic review samples (structure only, never placement)"]
    for s in lock["deterministic_review_samples"]:
        lines += [f"- attempt {s['attempt']} [{s['reason']}]: merged {s['merged_boards']} = larger {s['larger_side_boards']} + "
                  f"smaller {s['smaller_side_boards']}; core overlap {f(s['core_overlap'])}; historical tails larger "
                  f"{s['larger_side_historical_tails']} / smaller {s['smaller_side_historical_tails']}; larger-side below tau "
                  f"{s['larger_side_below_tau']} (historical {s['larger_side_below_tau_historical']}, not "
                  f"{s['larger_side_below_tau_not_historical']}); smaller-side below tau {s['smaller_side_below_tau']}",
                  f"  - larger core: {units(s['core_larger'])} | smaller core: {units(s['core_smaller'])} | larger only: "
                  f"{units(s['larger_only'])}; smaller only: {units(s['smaller_only'])}",
                  f"  - failed before: {', '.join(s['failed_conditions_before']) or '-'}; after historical-tail removal: "
                  + ("not eligible" if s["failed_conditions_after"] is None else (", ".join(s["failed_conditions_after"]) or "passes"))
                  + f"; min similarity larger {f(s['larger_side_min_similarity'])}, smaller {f(s['smaller_side_min_similarity'])}"
                  + (f", after removal {f(s['counterfactual_after']['min_similarity'])}" if s["counterfactual_after"] else "")]
    for s in o["deterministic_review_samples"]:
        if s["reason"].startswith("largest"):
            lines += [f"- [{s['reason']}] {s['family']} under {s['replay']}: {s['change_vs_baseline']}; baseline "
                      f"{s['baseline_groups']} groups {s['baseline_sizes']}, replay {s['replay_groups']} groups {s['replay_sizes']}",
                      f"  - baseline largest core: {units((s['baseline_cores'] or [[]])[0])} | replay largest core: "
                      f"{units((s['replay_cores'] or [[]])[0])} | differing units: {units(s['largest_core_differing_units'])}"]
        else:
            lines += [f"- [{s['reason']}] {s['pattern']} under {s['replay']}: {s['replay_groups']} groups, sizes "
                      f"{s['replay_sizes']}, core sizes {s['replay_core_sizes']}",
                      *(f"  - core: {units(core)}" for core in s["replay_cores"])]
    if not lock["deterministic_review_samples"] and not o["deterministic_review_samples"]:
        lines.append("- no case in any sampled category")
    return lines


def _member_lines(boards: Sequence[Board], names: Names, n: int = 3) -> list[str]:
    """A few member boards (structure only), deterministically the first by anonymous observation id."""
    out = []
    for b in sorted(boards, key=lambda b: b.obs)[:n]:
        out.append(", ".join(f"{names.champion(u.cid)} {u.tier}*" + (f" [{len(u.items)} items]" if u.items else "")
                             for u in sorted(b.shop, key=lambda u: (-len(u.items), u.cid))))
    return out


@dataclass(frozen=True)
class LoadedInputs:
    """Everything the analysis needs, fully materialized in memory (plain
    lists and dataclasses; no cursor, connection or lazy query)."""

    access: str
    population: dict[str, Any]
    boards: list[Board]


def load_inputs(db: Database, balance_window: str = DEFAULT_BALANCE_WINDOW,
                progress: Callable[[str], None] | None = None) -> LoadedInputs:
    """The only phase that touches the database: verify read-only, then read
    the window's population into memory. Callers close the connection right
    after this returns and run `analyze` without it."""
    progress = progress or _noop
    access = assert_read_only(db)
    progress(f"read-only verified: {access}")
    progress(f"population loading started (balance window {balance_window}, queue {RANKED_TFT_QUEUE_ID})")
    population, boards = load_population(db, balance_window)
    progress(f"population loading completed: {population['ranked_matches']} ranked matches, "
             f"{population['ranked_participants']} ranked participants, {len(boards)} unit-observable boards loaded")
    return LoadedInputs(access=access, population=population, boards=list(boards))


def build_report(db: Database, *, balance_window: str = DEFAULT_BALANCE_WINDOW,
                 config: ArchetypeConfig | None = None) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """(JSON report, Markdown lines, anonymized membership rows)."""
    inputs = load_inputs(db, balance_window)
    return analyze(inputs.boards, inputs.population, inputs.access, config or ArchetypeConfig())


#: `on_section(phase, data, markdown_lines)` receives each finished part of
#: the report as soon as it exists: "population" first, then every strategy
#: name, then "closing". Used to stream and persist partial results.
SectionCallback = Callable[[str, Mapping[str, Any], Sequence[str]], None]


def analyze(boards: Sequence[Board], population: dict[str, Any], access: str,
            config: ArchetypeConfig, *, progress: Callable[[str], None] | None = None,
            on_section: SectionCallback | None = None) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Pure in-memory analysis (no database). Returns the same (report,
    markdown, membership) whether or not `progress`/`on_section` are given."""
    progress = progress or _noop
    emit = on_section or (lambda phase, data, lines: None)
    names = Names()
    eligible = [b for b in boards if len(b.identity) >= config.min_identity_units]
    population = {**population,
                  "ranked_boards_eligible_for_grouping": len(eligible),
                  "boards_below_min_identity_units": len(boards) - len(eligible),
                  "boards_with_excluded_non_shop_units": sum(1 for b in boards if b.excluded)}
    report: dict[str, Any] = {"kind": "RESEARCH / VALIDATION -- experimental board-archetype analysis, not served to users",
                              "read_only_connection": access, "population": population, "config": asdict(config),
                              "strategies": {}, "statistical_warnings": list(STATISTICAL_WARNINGS)}
    md: list[str] = [
        "# Board archetype research report (EXPERIMENTAL -- research/validation only)",
        f"Read-only connection: {access}",
        "",
        "## Population (all percentages below state their denominator)",
        *(f"- {k}: {v}" for k, v in population.items()),
        "",
        "## Declared configuration (candidate modeling choices, not facts)",
        *(f"- {k}: {v}" for k, v in asdict(config).items()),
        "",
    ]
    progress(f"normalization completed: {len(boards)} boards, {len(eligible)} eligible for grouping")
    emit("population", {"read_only_connection": access, "population": population, "config": asdict(config)}, list(md))
    membership: list[dict[str, Any]] = []
    by_obs = {b.obs: b for b in eligible}
    unit_presence_hist: dict[str, Counter] = {}
    # kept for the experimental strategies (variant reuse, comparison with their controls)
    done: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        progress(f"{strategy.name}: started")
        chunk_start = len(md)
        if strategy.variants_from:
            grouping = cluster_reusing_variants(boards, strategy, config, done[strategy.variants_from]["grouping"], progress)
        else:
            grouping = cluster(boards, strategy, config, progress)
        progress(f"{strategy.name}: global metrics started")
        metrics = global_metrics(boards, grouping, strategy, config)
        progress(f"{strategy.name}: global metrics completed")
        progress(f"{strategy.name}: summaries/diagnostics started")
        members: dict[int, list[Board]] = defaultdict(list)
        for k, g in sorted(grouping.group.items()):
            members[g].append(by_obs[k])
        summaries = {g: group_summary(g, bs, config, eligible=len(eligible), observable=len(boards)) for g, bs in sorted(members.items())}
        sample = validation_sample(list(summaries.values()))
        shells = same_unit_different_shells(list(summaries.values()))
        swaps = similar_shell_different_unit(list(summaries.values()))
        detail_ids = sorted({g for _, g in sample} | {g for c in shells for g in c["groups"]} | {g for c in swaps for g in c["groups"]})
        item_sets = {}
        for g in detail_ids:
            s = summaries[g]
            top = sorted(s["units"], key=lambda u: (-max(s["units"][u]["two_plus_items"], s["units"][u]["carry_qualified"]), u))[:2]
            item_sets[g] = [item_set_summary(members[g], u, config, names) for u in top if s["units"][u]["two_plus_items"] >= 0.15]
        hist = Counter()
        for s in summaries.values():
            if s["boards"] >= 30:
                for p in s["presence_distribution"]:
                    hist[f"{min(9, int(p * 10)) / 10:.1f}"] += 1
        unit_presence_hist[strategy.name] = hist
        niche = {}
        for label, _, _ in SIZE_BANDS:
            band = [s for s in summaries.values() if s["size_band"] == label]
            niche[label] = {"groups": len(band), "boards": sum(s["boards"] for s in band),
                            "avg_placement": quantiles(s["outcome"]["avg_placement"] for s in band),
                            "top4_rate": quantiles(s["outcome"]["top4_rate"] for s in band)}
        review = sorted([s for s in summaries.values() if 10 <= s["boards"] <= 59], key=lambda s: (-s["boards"], s["group"]))[:12]
        report["strategies"][strategy.name] = {
            **metrics,
            "unit_presence_histogram_groups_30plus": dict(sorted(hist.items())),
            "niche_by_size_band": niche,
            "validation_sample": [{"reason": r, "group": g} for r, g in sample],
            "same_unit_different_shells": shells,
            "similar_shell_different_primary_unit": swaps,
            "manual_review_candidates_10_59": [s["group"] for s in review],
            "groups": [summaries[g] for g in sorted(summaries)],
            "item_sets": {str(g): v for g, v in item_sets.items()},
            "merge_diagnostics": grouping.merge_diagnostics,
        }
        for k, g in sorted(grouping.group.items()):
            membership.append({"strategy": strategy.name, "observation": k, "group": g, "variant": grouping.variant[k],
                               "placement": by_obs[k].placement, "shop_units": len(by_obs[k].identity)})

        md += [f"## Strategy {strategy.label}", strategy.description, ""]
        md += [f"- {k}: {json.dumps(v, default=_json_default)}" for k, v in metrics.items() if k not in ("description", "strategy")]
        if grouping.merge_diagnostics:
            md += render_merge_diagnostics(grouping.merge_diagnostics, names)
            if "shadow" in grouping.merge_diagnostics:
                md += render_shadow_evaluation(grouping.merge_diagnostics["shadow"], names)
        md += ["", "### Unit-presence histogram (all groups with >= 30 boards; one count per unit per group)",
               "- " + ", ".join(f"{k}-: {v}" for k, v in sorted(hist.items())) if hist else "- no group has >= 30 boards", "",
               "### Size bands (niche test; performance quantiles are per group)"]
        for label, row in niche.items():
            ap, t4 = row["avg_placement"], row["top4_rate"]
            md.append(f"- {label}: {row['groups']} groups, {row['boards']} boards" + (
                f"; avg placement median {ap['median']:.2f} (p10 {ap['p10']:.2f} / p90 {ap['p90']:.2f}); top4 median {_pct(t4['median'])}" if ap else ""))
        md += ["", "### Candidates for manual review (10-59 boards; listed by size, NOT by performance; small samples)"]
        md += [f"- group {s['group']}: n={s['boards']}, avg {s['outcome']['avg_placement']:.2f}, top4 {_pct(s['outcome']['top4_rate'])} "
               f"(95% CI {_pct(s['outcome']['top4_wilson95'][0])}-{_pct(s['outcome']['top4_wilson95'][1])}); core: "
               + ", ".join(names.champion(u) for u in s["core_candidates"]) for s in review] or ["- none"]
        md += ["", "### Validation sample"]
        for reason, g in sample:
            md += render_group(summaries[g], members[g], names, config, item_sets.get(g, []), reason) + [""]
        md += ["### Same itemized champion, structurally different groups (side by side)"]
        if not shells:
            md.append("- no case found at the declared thresholds")
        for case in shells:
            md.append(f"**{names.champion(case['unit'])}** in groups {case['groups']}; pairwise core Jaccard {case['pairwise_core_jaccard']}")
            for g in case["groups"]:
                md += render_group(summaries[g], members[g], names, config, item_sets.get(g, [])) + [""]
        md += ["### Similar shells, different primary itemized unit"]
        if not swaps:
            md.append("- no case found at the declared thresholds")
        for case in swaps:
            md.append(f"groups {case['groups']} (core Jaccard {case['core_jaccard']}): primary itemized "
                      + " vs ".join(names.champion(u) for u in case["primary_itemized_units"]))
            for g in case["groups"]:
                md += render_group(summaries[g], members[g], names, config, item_sets.get(g, [])) + [""]
        if strategy.variants_from:
            progress(f"{strategy.name}: experimental comparison with {strategy.variants_from} started")
            exp = experimental_review(boards, strategy, grouping, metrics, summaries, members, done[strategy.variants_from],
                                      names, config)
            report["strategies"][strategy.name]["experimental_s2"] = exp
            md += render_experimental_review(strategy, exp, summaries, members, names, config)
            progress(f"{strategy.name}: experimental comparison completed")
        done[strategy.name] = {"grouping": grouping, "metrics": metrics, "summaries": summaries, "strategy": strategy}
        progress(f"{strategy.name}: summaries/diagnostics completed")
        progress(f"{strategy.name}: completed, {metrics['groups']} groups, {metrics['boards_in_multi_board_groups']} boards "
                 f"assigned, {metrics['boards_ungrouped']} ungrouped")
        emit(strategy.name, report["strategies"][strategy.name], md[chunk_start:])
    closing_start = len(md)
    md += ["## Statistical warnings (no corrections applied)", *(f"- {w}" for w in STATISTICAL_WARNINGS), "",
           "## Unresolved ids (no canonical name in committed metadata)",
           "- " + (", ".join(sorted(names.unresolved)) or "none")]
    report["unresolved_ids"] = sorted(names.unresolved)
    emit("closing", {"statistical_warnings": list(STATISTICAL_WARNINGS), "unresolved_ids": report["unresolved_ids"]},
         md[closing_start:])
    progress("report generation completed")
    return report, md, membership


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    return str(value)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temporary file and os.replace, so a reader (or an artifact
    upload after a timeout) sees the old file or the new one, never half."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _output_paths(out_dir: Path, balance_window: str) -> dict[str, Path]:
    tag = str(balance_window).replace("/", "_")
    return {
        "markdown": out_dir / f"archetypes_{tag}_report.md",
        "json": out_dir / f"archetypes_{tag}_report.json",
        "membership": out_dir / f"archetypes_{tag}_membership.csv",
        "progress": out_dir / f"archetypes_{tag}_progress.log",
        "partial": out_dir / "partial",
    }


def write_outputs(report: Mapping[str, Any], markdown: Sequence[str], membership: Sequence[Mapping[str, Any]],
                  out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = _output_paths(out_dir, report["population"]["balance_window"])
    _atomic_write_text(paths["markdown"], "\n".join(markdown) + "\n")
    _atomic_write_text(paths["json"], json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False, default=_json_default))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["strategy", "observation", "group", "variant", "placement", "shop_units"])
    writer.writeheader()
    writer.writerows(membership)
    _atomic_write_text(paths["membership"], buffer.getvalue())
    return [paths["markdown"], paths["json"], paths["membership"]]


PARTIAL_LABEL = "PARTIAL / INCOMPLETE RESEARCH RESULT"
PHASES = ("population", *(s.name for s in STRATEGIES), "closing")


class ProgressiveWriter:
    """Persists results as each phase finishes, so a timeout keeps what was
    already computed.

    - `<out>/archetypes_<w>_progress.log`: every progress line, appended and
      flushed as it happens (elapsed-time phase log; no identifiers).
    - `<out>/partial/NN_<phase>.md|.json`: one file pair per finished phase
      (population, each strategy, closing), each written once, atomically,
      and labelled PARTIAL / INCOMPLETE RESEARCH RESULT.
    - `<out>/partial/00_STATUS.md|.json`: completed and pending phases,
      atomically replaced after every phase.
    - `complete()`: writes the final report files (`write_outputs`) and only
      then removes `partial/`. The final report therefore exists only for a
      run that finished; a run that did not finish leaves only labelled
      partial files.
    Stale outputs of an earlier run in `out_dir` are removed first, so an
    old complete report can never sit next to a new run's partial one."""

    def __init__(self, out_dir: Path, balance_window: str) -> None:
        self.out_dir = out_dir
        self.balance_window = balance_window
        self.paths = _output_paths(out_dir, balance_window)
        self.completed: list[str] = []
        self._prepared = False

    def _prepare(self) -> None:
        if self._prepared:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for key in ("markdown", "json", "membership", "progress"):
            self.paths[key].unlink(missing_ok=True)
        shutil.rmtree(self.paths["partial"], ignore_errors=True)
        self.paths["partial"].mkdir()
        self._prepared = True

    def log(self, line: str) -> None:
        self._prepare()
        with self.paths["progress"].open("a") as fh:
            fh.write(line + "\n")
            fh.flush()

    def section(self, phase: str, data: Mapping[str, Any], lines: Sequence[str]) -> None:
        self._prepare()
        stem = self.paths["partial"] / f"{len(self.completed) + 1:02d}_{phase}"
        header = [f"# {PARTIAL_LABEL}", f"Phase: {phase} (balance window {self.balance_window}). "
                  "Not a complete report: see 00_STATUS.md for completed and pending phases.", ""]
        _atomic_write_text(stem.with_suffix(".md"), "\n".join([*header, *lines]) + "\n")
        _atomic_write_text(stem.with_suffix(".json"), json.dumps(
            {"status": PARTIAL_LABEL, "phase": phase, "balance_window": self.balance_window, "data": data},
            indent=1, sort_keys=True, ensure_ascii=False, default=_json_default))
        self.completed.append(phase)
        self._write_status()

    def _write_status(self) -> None:
        pending = [p for p in PHASES if p not in self.completed]
        status = {"status": PARTIAL_LABEL, "balance_window": self.balance_window,
                  "completed_phases": list(self.completed), "pending_phases": pending,
                  "note": f"The complete report is {self.paths['markdown'].name}; if it is absent this run did not finish."}
        _atomic_write_text(self.paths["partial"] / "00_STATUS.json", json.dumps(status, indent=1, sort_keys=True))
        _atomic_write_text(self.paths["partial"] / "00_STATUS.md", "\n".join([
            f"# {PARTIAL_LABEL}", f"Balance window: {self.balance_window}",
            "Completed phases: " + (", ".join(self.completed) or "none"),
            "Pending phases: " + (", ".join(pending) or "none"), status["note"], ""]))

    def complete(self, report: Mapping[str, Any], markdown: Sequence[str],
                 membership: Sequence[Mapping[str, Any]]) -> list[Path]:
        self._prepare()
        paths = write_outputs(report, markdown, membership, self.out_dir)
        shutil.rmtree(self.paths["partial"], ignore_errors=True)
        return paths
