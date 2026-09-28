"""Runtime benchmark for the board-archetype research harness (SYNTHETIC; local only).

Purpose: find out whether higher structural diversity reproduces the runtime
blow-up seen on the first real 18.3 run (run 36354518525 timed out after 60
minutes). It measures speed only -- it says nothing about clustering quality,
and the boards and comps are fictional (real Set 18 identity ids so the
normal code paths run; fictional trait memberships and items).

No database, no network: boards are built in memory and passed straight to
`tftlab.archetype_research.analyze`, whose progress lines give per-phase
timings. The model and its parameters are used exactly as shipped.

Usage:
    python scripts/benchmarks/archetype_runtime_benchmark.py --boards 4000 --diversity high
    python scripts/benchmarks/archetype_runtime_benchmark.py --boards 4000 --diversity high --strategies A_structural_baseline
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tftlab import archetype_research as ar  # noqa: E402
from tftlab.roster import load_roster  # noqa: E402

DIVERSITY = {
    # share of boards from templates, number of templates, per-slot swap
    # probability on template boards, random boards make up the rest.
    "low": {"template_share": 0.95, "templates": 13, "swap": 0.05},
    "medium": {"template_share": 0.75, "templates": 40, "swap": 0.15},
    "high": {"template_share": 0.50, "templates": 120, "swap": 0.25},
    "extreme": {"template_share": 0.20, "templates": 200, "swap": 0.35},
}
DAMAGE = ["DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_LastWhisper", "DA_JeweledGauntlet", "DA_RabadonsDeathcap",
          "DA_BlueBuff", "DA_SteraksGage", "DA_TitansResolve", "DA_Bloodthirster", "DA_Deathblade"]
TANK = ["DA_WarmogsArmor", "DA_GargoyleStoneplate", "DA_BrambleVest", "DA_DragonsClaw", "DA_SunfireCape"]


def build_boards(n: int, level: str, seed: int = 18) -> list[ar.Board]:
    rng = random.Random(seed)
    params = DIVERSITY[level]
    champions = load_roster().champions
    identity = sorted(ar.identity_champions())
    traits = sorted(load_roster().traits)
    trait_of = {cid: traits[i % len(traits)] for i, cid in enumerate(identity)}
    # meta skew: a Zipf-like popularity over champions
    weights = [1.0 / (rank + 1) ** 0.8 for rank in range(len(identity))]
    popularity = identity[:]
    rng.shuffle(popularity)

    def pick(k: int, exclude: set[str]) -> list[str]:
        out: list[str] = []
        while len(out) < k:
            c = rng.choices(popularity, weights=weights, k=1)[0]
            if c not in exclude and c not in out:
                out.append(c)
        return out

    templates = [pick(rng.randint(7, 9), set()) for _ in range(params["templates"])]
    tpl_weights = [1.0 / (i + 1) for i in range(len(templates))]
    boards = []
    for obs in range(n):
        size = max(4, min(10, round(rng.gauss(8, 1))))
        if rng.random() < params["template_share"]:
            tpl = rng.choices(templates, weights=tpl_weights, k=1)[0]
            units = [c if rng.random() >= params["swap"] else None for c in tpl][:size]
            kept = [c for c in units if c]
            units = kept + pick(size - len(kept), set(kept))
        else:
            units = pick(size, set())
        board_units = []
        for i, cid in enumerate(units):
            items: tuple[str, ...] = ()
            if i == 0:
                items = tuple(sorted(rng.sample(DAMAGE, rng.choice([2, 3]))))
            elif i == 1 and rng.random() < 0.7:
                items = tuple(sorted(rng.sample(TANK, rng.choice([1, 2]))))
            cost = champions[cid]["cost"]
            board_units.append(ar.Unit(cid, rng.choice([1, 2, 2, 3]) if cost <= 3 else rng.choice([1, 2]), cost,
                                       items, items, len(items) >= 2))
        counts = Counter(trait_of[c] for c in units)
        active = {t: sum(k >= bp for bp in (2, 4, 6)) for t, k in counts.items() if k >= 2}
        boards.append(ar.normalize_board(obs, (f"BENCH{obs // 8:05d}", obs % 8), rng.randint(1, 8), size,
                                         board_units, active, champions))
    return boards


def diversity_stats(boards: list[ar.Board], seed: int = 7) -> dict:
    rng = random.Random(seed)
    ids = [b.identity for b in boards]
    counts = Counter(ids)
    pairs = [rng.sample(ids, 2) for _ in range(3000)]
    jaccard = [len(a & b) / len(a | b) for a, b in pairs if a | b]
    return {
        "boards": len(boards),
        "distinct_identity_sets": len(counts),
        "distinct_share": len(counts) / len(boards),
        "boards_whose_identity_repeats": sum(c for c in counts.values() if c > 1),
        "mean_random_pair_jaccard": sum(jaccard) / len(jaccard),
        "distinct_champions_used": len({c for i in ids for c in i}),
    }


def run(n: int, level: str, strategies: list[str] | None) -> dict:
    boards = build_boards(n, level)
    stats = diversity_stats(boards)
    selected = [s for s in ar.STRATEGIES if not strategies or s.name in strategies]
    original = ar.STRATEGIES
    ar.STRATEGIES = tuple(selected)  # benchmark-only: time a subset of the unchanged strategies
    progress = ar.Progress(lambda line: print(line, flush=True))
    started = time.monotonic()
    try:
        report, _, _ = ar.analyze(boards, {"balance_window": "BENCH"}, "benchmark (no database)", ar.ArchetypeConfig(),
                                  progress=progress)
    finally:
        ar.STRATEGIES = original
    total = time.monotonic() - started
    phases: dict[str, dict[str, float]] = {}
    marks: dict[str, float] = {}
    for elapsed, msg in progress.events:
        name, _, what = msg.partition(": ")
        if name in {s.name for s in selected}:
            marks.setdefault(f"{name}:start", elapsed) if what == "started" else None
            if what.startswith("leader pass completed"):
                phases.setdefault(name, {})["leader_pass_s"] = elapsed - marks[f"{name}:start"]
                marks[f"{name}:leader"] = elapsed
            if what.startswith("completed,"):
                phases.setdefault(name, {})["total_s"] = elapsed - marks[f"{name}:start"]
    result = {"diversity_level": level, "params": DIVERSITY[level], "diversity": stats, "analysis_total_s": total,
              "strategies": {}}
    for s in selected:
        m = report["strategies"][s.name]
        result["strategies"][s.name] = {**phases.get(s.name, {}), "leader_pass_groups": int(m["log"][0].split()[2]),
                                        "variants": m["variants_before_merge"], "groups": m["groups"],
                                        "assigned": m["boards_in_multi_board_groups"], "ungrouped": m["boards_ungrouped"],
                                        "refine_iterations": len(m["refine_moves"]), "converged": m["converged"]}
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", type=int, default=4000)
    ap.add_argument("--diversity", choices=sorted(DIVERSITY), default="high")
    ap.add_argument("--strategies", nargs="*")
    ap.add_argument("--stats-only", action="store_true", help="print diversity statistics without clustering")
    args = ap.parse_args()
    if args.stats_only:
        print(json.dumps(diversity_stats(build_boards(args.boards, args.diversity)), indent=1))
        return
    print(json.dumps(run(args.boards, args.diversity, args.strategies), indent=1))


if __name__ == "__main__":
    main()
