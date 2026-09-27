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
3. Groups boards with three predeclared strategies (`STRATEGIES`) whose every
   threshold lives in `ArchetypeConfig` and is printed with the results:
   A. structural baseline -- champion-set similarity only (the control);
   B. flex-tolerant -- A plus a structural variant merge for flex slots and
      incomplete boards;
   C. structure-aware -- B's idea plus documented secondary structure
      (item counts, splash weighting, active traits).
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
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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
    #: Strategy C only: a differing core unit is "explained" if it is on >=
    #: flex_presence of the other variant's boards, or un-itemized in its own
    #: variant (holds >= 2 completed items on < itemized_share of its boards).
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


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("A_structural_baseline", "A. structural baseline (control)",
             "Champion-set Ruzicka only (every shop unit weight 1). No item, carry, cost or trait information. No merge."),
    Strategy("B_flex_tolerant", "B. flex-tolerant structural",
             "A's similarity, then a structural variant merge: variants whose cores share >= max(3, 60% of the larger core) "
             "and differ by at most one core swap (pure additions allowed) become one group. Champion presence only.",
             merge="structural"),
    Strategy("C_structure_aware", "C. structure-aware",
             "Weighted Ruzicka (units with >= 2 completed items x2, un-itemized 1-star 4/5-cost units x0.5) blended 75/25 "
             "with active-trait Ruzicka, then B's merge restricted so a differing core unit must be flex (>= 15%) in the "
             "other variant or un-itemized in its own (a swapped itemized unit keeps groups apart). Item COUNTS only; "
             "never the carry classifier.",
             weighted=True, trait_share=0.25, merge="structure_aware"),
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


def cluster(boards: Sequence[Board], strategy: Strategy, config: ArchetypeConfig) -> Grouping:
    """Deterministic leader pass + profile refinement (+ merge for B/C).

    Leader pass: boards are visited in a purely STRUCTURAL order
    (`structural_order_key`: more identity units first, then the board's own
    similarity vector); placement and every other outcome play no part, so
    archetype membership is invariant to outcome. Each board joins its most
    similar group (>= tau) or starts one. Refinement: recompute mean profiles, drop groups under
    min_group_size, reassign every board to its most similar profile (>= tau,
    else ungrouped); repeat until at most convergence_moved_share of boards
    move or max_refine_iterations is reached (reported either way)."""
    champions = load_roster().champions
    eligible = [b for b in boards if len(b.identity) >= config.min_identity_units]
    vecs = {b.obs: board_vectors(b, strategy, config, champions) for b in eligible}
    order = sorted(eligible, key=lambda b: structural_order_key(b, vecs[b.obs]))
    log: list[str] = []

    members: list[list[int]] = []
    profiles: list[tuple[dict, dict]] = []
    index: dict[str, set[int]] = defaultdict(set)
    assign: dict[int, int] = {}
    for b in order:
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
        if moved <= threshold:
            converged = True
            break
    # final pass: singleton groups (possible after the last reassignment) are ungrouped
    sizes = Counter(assign.values())
    assign = _renumber({k: g for k, g in assign.items() if sizes[g] >= config.min_group_size}, vecs)

    group, merges = dict(assign), 0
    if strategy.merge:
        to_group, merge_log = merge_variants([b for b in eligible if b.obs in assign], assign, strategy, config)
        group = {k: to_group[v] for k, v in assign.items()}
        merges = len(merge_log)
        log.append(f"variant merge: {merges} merges, {len(set(assign.values()))} variants -> {len(set(group.values()))} groups")
    result = Grouping(variant=assign, group=group, log=log, converged=converged, refine_moves=moves, merges=merges)
    result.prune_audit = prune_audit(eligible, vecs, assign, strategy, config)
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


def merge_variants(boards: Sequence[Board], variant: Mapping[int, int], strategy: Strategy,
                   config: ArchetypeConfig) -> tuple[dict[int, int], list[str]]:
    """Variant -> group. Greedy: the highest core-overlap mergeable pair first
    (ties: lowest ids), every candidate re-checked against the merged group's
    recomputed statistics, so A~B~C chaining cannot pull in a C that the
    merged A+B does not satisfy."""
    counts: dict[int, Counter] = defaultdict(Counter)
    itemized: dict[int, Counter] = defaultdict(Counter)
    sizes: Counter = Counter()
    for b in boards:
        g = variant[b.obs]
        sizes[g] += 1
        counts[g].update(b.identity)
        itemized[g].update({u.cid for u in b.shop if len(u.items) >= 2})
    groups = {g: {g} for g in sizes}
    version = {g: 0 for g in sizes}
    log: list[str] = []

    def core(g: int) -> set[str]:
        return {u for u, c in counts[g].items() if c / sizes[g] >= config.core_presence}

    def score(a: int, b: int) -> float | None:
        ca, cb = core(a), core(b)
        shared = ca & cb
        if len(shared) < max(config.merge_min_shared, math.ceil(config.merge_shared_fraction * max(len(ca), len(cb)))):
            return None
        da, db_ = ca - cb, cb - ca
        if min(len(da), len(db_)) > config.max_swaps:
            return None
        if strategy.merge == "structure_aware":
            def explained(u: str, own: int, other: int) -> bool:
                flex_elsewhere = counts[other][u] / sizes[other] >= config.flex_presence
                support = itemized[own][u] / counts[own][u] < config.itemized_share
                return flex_elsewhere or support
            if not all(explained(u, a, b) for u in da) or not all(explained(u, b, a) for u in db_):
                return None
        return len(shared) / len(ca | cb)

    def core_index() -> dict[str, set[int]]:
        idx: dict[str, set[int]] = defaultdict(set)
        for g in groups:
            for u in core(g):
                idx[u].add(g)
        return idx

    idx = core_index()
    heap: list[tuple[float, int, int, int, int]] = []

    def push_pairs(a: int) -> None:
        near = Counter(h for u in core(a) for h in idx.get(u, ()) if h != a)
        for h, shared in near.items():
            if shared < config.merge_min_shared:
                continue
            s = score(min(a, h), max(a, h))
            if s is not None:
                lo, hi = min(a, h), max(a, h)
                heapq.heappush(heap, (-s, lo, hi, version[lo], version[hi]))

    for g in sorted(groups):
        push_pairs(g)
    while heap:
        neg, a, b, va, vb = heapq.heappop(heap)
        if a not in groups or b not in groups or version[a] != va or version[b] != vb:
            continue
        if score(a, b) is None:  # stats unchanged since push, but stay defensive
            continue
        log.append(f"merge variant-group {b} ({sizes[b]} boards) into {a} ({sizes[a]} boards), core overlap {-neg:.2f}")
        for u in core(a) | core(b):
            idx[u].discard(a)
            idx[u].discard(b)
        counts[a] += counts.pop(b)
        itemized[a] += itemized.pop(b)
        sizes[a] += sizes.pop(b)
        groups[a] |= groups.pop(b)
        del version[b]
        version[a] += 1
        for u in core(a):
            idx[u].add(a)
        push_pairs(a)
    order = sorted(groups, key=lambda g: (-sizes[g], min(groups[g])))
    return {v: rank for rank, g in enumerate(order) for v in sorted(groups[g])}, log


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


def build_report(db: Database, *, balance_window: str = DEFAULT_BALANCE_WINDOW,
                 config: ArchetypeConfig | None = None) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """(JSON report, Markdown lines, anonymized membership rows)."""
    config = config or ArchetypeConfig()
    access = assert_read_only(db)
    population, boards = load_population(db, balance_window)
    return analyze(boards, population, access, config)


def analyze(boards: Sequence[Board], population: dict[str, Any], access: str,
            config: ArchetypeConfig) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
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
    membership: list[dict[str, Any]] = []
    by_obs = {b.obs: b for b in eligible}
    unit_presence_hist: dict[str, Counter] = {}
    for strategy in STRATEGIES:
        grouping = cluster(boards, strategy, config)
        metrics = global_metrics(boards, grouping, strategy, config)
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
        }
        for k, g in sorted(grouping.group.items()):
            membership.append({"strategy": strategy.name, "observation": k, "group": g, "variant": grouping.variant[k],
                               "placement": by_obs[k].placement, "shop_units": len(by_obs[k].identity)})

        md += [f"## Strategy {strategy.label}", strategy.description, ""]
        md += [f"- {k}: {json.dumps(v, default=_json_default)}" for k, v in metrics.items() if k not in ("description", "strategy")]
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
    md += ["## Statistical warnings (no corrections applied)", *(f"- {w}" for w in STATISTICAL_WARNINGS), "",
           "## Unresolved ids (no canonical name in committed metadata)",
           "- " + (", ".join(sorted(names.unresolved)) or "none")]
    report["unresolved_ids"] = sorted(names.unresolved)
    return report, md, membership


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    return str(value)


def write_outputs(report: Mapping[str, Any], markdown: Sequence[str], membership: Sequence[Mapping[str, Any]],
                  out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = str(report["population"]["balance_window"]).replace("/", "_")
    md_path = out_dir / f"archetypes_{tag}_report.md"
    json_path = out_dir / f"archetypes_{tag}_report.json"
    csv_path = out_dir / f"archetypes_{tag}_membership.csv"
    md_path.write_text("\n".join(markdown) + "\n")
    json_path.write_text(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False, default=_json_default))
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["strategy", "observation", "group", "variant", "placement", "shop_units"])
        writer.writeheader()
        writer.writerows(membership)
    return [md_path, json_path, csv_path]
