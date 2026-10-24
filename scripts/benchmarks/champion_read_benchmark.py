"""Champion Investigation read-path benchmark: live computation vs prepared
runs (local only).

Times `/api/champions/<slug>` through the real web application (FastAPI
TestClient, the database opened read-only as on Render) for every champion
carried in one balance window, twice:

- live:     prepared champion runs removed -- the request-time computation
            (the window-wide "All carries" average is cached after the first
            request, the champion's own evidence is computed on every one);
- prepared: after `prepare_window` published a run -- the steady state.

It reports the first (cold) request, the median and the slowest request of
each path, the preparation time, and checks that every prepared response
equals the live one. Never contacts Riot and never prints the database
target. The target is only written to replace its prepared champion rows.

    python scripts/benchmarks/champion_read_benchmark.py --db /path/to/a-snapshot-copy.sqlite3
    python scripts/benchmarks/champion_read_benchmark.py --db copy.sqlite3 --balance-window 18.5 --limit 20
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402


def _client(target: str) -> TestClient:
    if "://" in target:
        os.environ["DATABASE_URL"] = target
    else:
        os.environ.pop("DATABASE_URL", None)
        os.environ["TFT_DB_PATH"] = target
    import tftlab.webapp as webapp

    webapp._WINDOW_AVERAGE_CACHE.clear()
    return TestClient(webapp.create_app())


def _timed(client: TestClient, slugs: list[str], window: str) -> tuple[list[float], dict[str, dict]]:
    seconds, bodies = [], {}
    for slug in slugs:
        t = time.perf_counter()
        response = client.get(f"/api/champions/{slug}", params={"balance_window": window})
        seconds.append(time.perf_counter() - t)
        response.raise_for_status()
        bodies[slug] = response.json()
    return seconds, bodies


def _line(name: str, seconds: list[float]) -> str:
    return (f"  {name:9s} first {seconds[0]:.3f}s · median {statistics.median(seconds):.3f}s · "
            f"slowest {max(seconds):.3f}s · total {sum(seconds):.1f}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="SQLite path or postgres:// URL (only prepared rows are written)")
    parser.add_argument("--balance-window", help="default: the newest window")
    parser.add_argument("--limit", type=int, default=0, help="champions to time (default: every carried champion)")
    args = parser.parse_args(argv)

    from tftlab.analytics import default_balance_window
    from tftlab.champion_investigation import champion_directory
    from tftlab.prepared_champions import ANALYTICS_VERSION, prepare_window
    from tftlab.storage import Database

    with Database(args.db) as db:
        window = args.balance_window or default_balance_window(db)
        slugs = [c["slug"] for c in sorted(champion_directory(db, window), key=lambda c: -c["carry_games"])
                 if c["carry_games"]]
        slugs = slugs[: args.limit] if args.limit else slugs
        db.execute("DELETE FROM champion_prepared_payloads")
        db.execute("DELETE FROM champion_prepared_runs")
        db.commit()
    print(f"balance window {window}: {len(slugs)} carried champions; champion analytics version {ANALYTICS_VERSION}")

    live_s, live = _timed(_client(args.db), slugs, window)
    with Database(args.db) as db:
        t = time.perf_counter()
        result = prepare_window(db, window)
        prepare_s = time.perf_counter() - t
    prepared_s, prepared = _timed(_client(args.db), slugs, window)

    served = sum(b["prepared"]["status"] == "current" for b in prepared.values())
    strip = lambda b: {k: v for k, v in b.items() if k != "prepared"}  # noqa: E731
    mismatched = [s for s in slugs if strip(prepared[s]) != strip(live[s])]
    print(_line("live", live_s))
    print(_line("prepared", prepared_s))
    print(f"  preparation: {result.champions} champions in {prepare_s:.1f}s")
    print(f"  served prepared: {served}/{len(slugs)}; identical to live: {len(slugs) - len(mismatched)}/{len(slugs)}")
    if mismatched:
        print(f"  MISMATCH: {', '.join(mismatched)}")
    return 1 if mismatched or served != len(slugs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
