"""Discovery read-path benchmark: live computation vs prepared runs (local only).

Times the production Discovery request through the real web application
(FastAPI TestClient, DATABASE_URL set, so the database is opened read-only
exactly as on Render) twice:

- live:     prepared runs ignored -- the request-time computation the site
            used before prepared analytics (population cache warm after the
            first request, per-carry evidence computed on every request);
- prepared: after `prepare_window` published a run -- the steady state.

For each it reports cold/warm timings plus the work behind one request: SQL
statements and rows fetched (both scale with data size, which is the part a
local run cannot reproduce for production). It also checks that both paths
return identical candidates. Never contacts Riot and never prints the
database target.

    # synthetic, live-shaped data in a fresh scratch SQLite file
    python scripts/benchmarks/discovery_read_benchmark.py --db /tmp/bench.sqlite3 --generate 6000
    # a larger single window: 20,000 matches 30 s apart
    python scripts/benchmarks/discovery_read_benchmark.py --db /tmp/big.sqlite3 --generate 20000 --spacing-seconds 30
    # an existing database (e.g. a local copy) -- read-only except for prepared rows
    python scripts/benchmarks/discovery_read_benchmark.py --db postgresql://...

`--generate` builds SYNTHETIC matches (real Set 18 ids from the committed
art manifest and item snapshot, fictional boards and outcomes) and refuses
a target that already has matches.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

PRODUCTION_REQUEST = {"costs": "1,2,3", "min_samples": 10, "top_n": 5, "limit": 200}
DATA = Path(__file__).resolve().parents[2] / "src" / "tftlab" / "data"


def generate(target: str, matches: int, *, spacing_s: int = 150, seed: int = 1830) -> None:
    from tftlab.storage import Database

    rng = random.Random(seed)
    manifest = json.loads((DATA / "game_art_manifest.json").read_text())
    intent = json.loads((DATA / "item_intent.json").read_text())["items"]
    champs = [(k, v["cost"]) for k, v in manifest["champions"].items()]
    traits = list(manifest["traits"])
    damage = sorted(k for k, v in intent.items() if v.get("intent") == "damage" and k.startswith("DA_") and "Emblem" not in k)
    tank = sorted(k for k, v in intent.items() if v.get("intent") == "tank" and k.startswith("DA_"))
    carries = rng.sample(champs, min(50, len(champs)))
    weights = [rng.random() ** 2 + 0.02 for _ in carries]

    def board():
        cid, cost = rng.choices(carries, weights)[0]
        committed = rng.random() < 0.72
        items = rng.sample(damage, 3) if committed else [rng.choice(damage)]
        hit = committed and rng.random() < (0.45 if cost <= 3 else 0.1)
        partners = [p for p, _ in rng.sample(champs, 8) if p != cid][:7]
        units = [{"character_id": cid, "rarity": cost - 1, "tier": 3 if hit else 2, "itemNames": items}]
        units += [{"character_id": p, "rarity": 1, "tier": 2, "itemNames": rng.sample(tank, 2) if i == 0 else []}
                  for i, p in enumerate(partners)]
        tr = []
        for t in rng.sample(traits, rng.randint(3, 6)):
            n = rng.choice([2, 2, 3, 4, 4, 5, 6])
            tr.append({"name": t, "num_units": n, "style": 1 + (n >= 4), "tier_current": 1 + (n >= 4), "tier_total": 3})
        return rng.random() + (0.15 if hit else 0.0), units, tr

    start = 1_790_233_200_000
    with Database(target) as db:
        if db.query_one("SELECT COUNT(*) FROM matches")[0]:
            raise SystemExit("--generate refuses a database that already has matches")
        for i in range(matches):
            parts = sorted((board() for _ in range(8)), key=lambda b: -b[0])
            db.ingest_match({"metadata": {"match_id": f"BENCH_{i:06d}"}, "info": {
                "game_version": "TFT Unreal Version ?.?.?.?", "tft_game_type": "standard", "queue_id": 1100,
                "tft_set_number": 18, "tft_set_core_name": "TFTSet18",
                "game_datetime": start + 3_600_000 + i * spacing_s * 1000,
                "participants": [{"placement": r + 1, "level": 8, "augments": [], "units": u, "traits": t}
                                 for r, (_, u, t) in enumerate(parts)]}})


class Work:
    """Counts SQL statements and fetched rows through Database."""

    def __init__(self) -> None:
        from tftlab.storage import Database

        self.statements = self.rows = 0
        original_all, original_one = Database.query_all, Database.query_one
        work = self

        def query_all(db, sql, params=()):
            out = original_all(db, sql, params)
            if "WHERE 1 = 0" not in sql:  # open_existing's schema probes read no rows
                work.statements += 1
                work.rows += len(out)
            return out

        def query_one(db, sql, params=()):
            out = original_one(db, sql, params)
            if "WHERE 1 = 0" not in sql:
                work.statements += 1
                work.rows += out is not None
            return out

        Database.query_all, Database.query_one = query_all, query_one

    def reset(self) -> None:
        self.statements = self.rows = 0


def measure(client: TestClient, work: Work, params: dict, repeats: int, *, cold: bool) -> dict:
    import tftlab.webapp as webapp

    times, body = [], None
    for i in range(repeats + 1):
        if i == 0 and cold:
            webapp._POPULATION_CACHE.clear()
        work.reset()
        t0 = time.perf_counter()
        response = client.get("/api/discovery", params=params)
        elapsed = time.perf_counter() - t0
        assert response.status_code == 200, response.text[:300]
        body = response.json()
        times.append(elapsed)
    return {
        "first_s": round(times[0], 4),
        "warm_median_s": round(statistics.median(times[1:]), 4),
        "warm_min_s": round(min(times[1:]), 4),
        "sql_statements": work.statements,
        "rows_fetched": work.rows,
        "candidates": len(body["candidates"]),
        "window_carries": body["window_carries"],
        "prepared_status": body["prepared"]["status"],
        "_body": body,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="SQLite path or postgres:// URL (never printed)")
    parser.add_argument("--generate", type=int, default=0, help="first fill an EMPTY target with N synthetic matches")
    parser.add_argument("--spacing-seconds", type=int, default=150,
                        help="time between generated matches; smaller packs more of them into one balance window")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    if args.generate:
        generate(args.db, args.generate, spacing_s=args.spacing_seconds)

    os.environ["DATABASE_URL"] = args.db
    import tftlab.webapp as webapp
    from tftlab.analytics import default_balance_window
    from tftlab.prepared_discovery import PreparedLookup, prepare_window
    from tftlab.storage import Database

    with Database(args.db) as db:
        window = default_balance_window(db)
        matches = db.query_one("SELECT COUNT(*) FROM matches WHERE balance_window = ?", (window,))[0]
        # Start from "no current run" so the live path is the old behaviour.
        db.execute("DELETE FROM discovery_prepared_candidates")
        db.execute("DELETE FROM discovery_prepared_runs")
        db.commit()

    work = Work()
    client = TestClient(webapp.create_app())
    params = {**PRODUCTION_REQUEST, "balance_window": window}

    original_lookup = webapp.lookup_prepared
    webapp.lookup_prepared = lambda db, w: PreparedLookup("missing")
    live = measure(client, work, params, args.repeats, cold=True)
    webapp.lookup_prepared = original_lookup

    t0 = time.perf_counter()
    with Database(args.db) as db:
        result = prepare_window(db, window)
    prepare_s = time.perf_counter() - t0

    pre = measure(client, work, params, args.repeats, cold=True)
    live_body, pre_body = live.pop("_body"), pre.pop("_body")
    live_body.pop("prepared"), pre_body.pop("prepared")
    print(json.dumps({
        "dataset": {"balance_window": window, "matches_in_window": matches,
                    "synthetic": bool(args.generate), "backend": "postgres" if args.db.startswith("postgres") else "sqlite"},
        "request": PRODUCTION_REQUEST,
        "live": live,
        "prepared": pre,
        "prepare_window": {"seconds": round(prepare_s, 2), "carries": result.window_carries},
        "identical_responses": live_body == pre_body,
        "warm_reduction": round(1 - pre["warm_median_s"] / live["warm_median_s"], 3),
    }, indent=1))


if __name__ == "__main__":
    main()
