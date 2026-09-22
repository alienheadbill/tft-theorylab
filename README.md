# TFT Theory Lab

A patch-aware analytics prototype for discovering **low-usage, data-backed TFT carry lines**, with an emphasis on 1/2/3-cost rerolls.

## Milestone 1

The first vertical slice does four things:

1. Seeds recent matches from Riot's high-Elo TFT ladder.
2. Normalizes final boards into SQLite.
3. Defines **carry commitment** as a unit finishing with 2+ non-component items, whether or not the reroll hits 3-star.
4. Produces a hidden-reroll leaderboard with hit/miss results and Bayesian-shrunk Top-4 estimates.

This avoids a major survivorship-bias trap: a reroll is not evaluated only in games where the player successfully reaches 3-star.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"          # add ",postgres" too if you're testing against Postgres locally
```

### Run without a Riot API key

```bash
tftlab demo
```

This creates deterministic fake matches and runs the same normalization + analytics path used by real match data.

### Run on live Riot data

1. Create a Riot development/personal API key.
2. Copy `.env.example` to `.env` and insert the key.
3. Run:

```bash
tftlab ingest-riot --players 25 --matches-per-player 10
tftlab leaderboard --min-samples 20
```

Riot development keys expire periodically, so a 403 after the key worked previously usually means the key needs refreshing.

## Current scoring

`carry_commitment_stats` (`tftlab/analytics/commitment.py`) still exposes a simple, Bayesian-shrunk `opportunity_score` per carry, combining Top4/win strength, rarity, and confidence -- this backs the existing `/api/carries` leaderboard and CLI `leaderboard` command unchanged. It deliberately does **not** reward 3-star hit rate; hit and miss performance are shown separately so a powerful-but-fragile reroll isn't confused with a reliable line.

The richer, discovery-focused **Opportunity Score v2** (partner/item/trait-evidence-aware, floor/ceiling-aware) is a separate, additive score computed only for `DiscoveryCandidate`s -- see "Comp-discovery engine" below.

### Usage vs. presence vs. conversion

`CarryStat`/`DiscoveryCandidate` expose four related-but-distinct rates rather than one ambiguous `usage_rate`:

- `appearance_rate` -- how often this champion is on a board *at all* (`appearances / total_participants`), regardless of build.
- `commitment_rate` -- how often it's on a board *built as a >=2-item carry* (`commitment_games / total_participants`). This is what `usage_rate` has always actually measured.
- `usage_rate` -- kept as a deprecated alias of `commitment_rate` for backward compatibility; new code should read the named rate it actually means.
- `carry_conversion_rate` -- of the games it appeared in at all, how often it became a committed carry (`commitment_games / appearances`).

These separate a champion being rare to see (`appearance_rate`) from a champion being rare to build as a real carry (`commitment_rate`/`carry_conversion_rate`) -- e.g. a common champion that's almost never itemized as a carry (high `appearance_rate`, low `carry_conversion_rate`) is a very different discovery story than a rare champion that's a carry every time it's picked (low `appearance_rate`, high `carry_conversion_rate`).

## Storage backends

`Database` (in `tftlab/storage.py`) is backend-agnostic: pass it a filesystem path for SQLite, or a `postgres://`/`postgresql://` URL (typically from a `DATABASE_URL` environment variable) for Postgres. Every query in the codebase is written once with `?`-style placeholders; the Postgres path translates them internally, so analytics/ingest code never branches on which database it's talking to.

- **Local/demo/tests**: SQLite, as before. No setup needed.
- **Production**: set `DATABASE_URL` to a Postgres connection string. Install the `postgres` extra (`pip install -e ".[postgres]"`, already done for you by `render.yaml`) so `psycopg` is available.
- **Schema/migrations**: the schema is created automatically on first connect (`CREATE TABLE IF NOT EXISTS ...`), for either backend — no manual migration step for a fresh database. The one schema change so far (adding `matches.patch`) is applied automatically to older databases too (`ALTER TABLE ... ADD COLUMN`), so upgrading in place is also automatic.
- Never commit a real `DATABASE_URL` (or any credential) — set it in your host's environment/secret manager. `.env` is gitignored and `.env.example` only has placeholders.

## Balance-window-aware analytics

TFT champions and items get rebalanced every patch -- and sometimes mid-patch, without the client's major.minor version changing -- so mixing two different balance states in one query would blend unrelated data. Every analytics function in `tftlab.analytics` scopes to a single **balance window**, not just a client patch:

- `tftlab.balance_window.resolve_balance_window(client_patch, game_datetime)` derives a window like `18.2a`/`18.2b` from a small registry of known mid-patch cutovers (a client patch with no registered cutover is simply its own window, e.g. `18.3`).
- Pass `balance_window="18.2b"` explicitly to any analytics function, or omit it and the chronologically **latest** window in the store is used (`tftlab.analytics.default_balance_window`) -- never just the most-played one, and ordered numerically (`18.10` sorts after `18.9`), not lexicographically.

The web API exposes this as an optional `?balance_window=` query param on every carry/discovery endpoint; the current frontend doesn't send it, so behavior is unchanged there, but a window selector can be added later without any backend work.

## Static metadata (CommunityDragon)

`tftlab.cdragon.CommunityDragonClient` fetches and disk-caches TFT static metadata (champion shop costs, champion/item/trait names and art) from CommunityDragon. `tftlab ingest-riot` uses it by default to resolve authoritative champion costs instead of the `rarity + 1` heuristic (pass `--no-use-static-costs` to disable). If CommunityDragon is unreachable, ingestion logs a warning and falls back to `rarity + 1` rather than failing.

A separate GitHub Actions workflow (`.github/workflows/cdragon-live.yml`) smoke-tests the real feed on a manual trigger or nightly schedule. It's intentionally isolated from any PR/unit-test CI (it has no `push`/`pull_request` trigger), so a CommunityDragon outage never blocks a merge.

## Comp-discovery engine (carry + partners + items + traits)

Given a committed carry, three analytics modules find the statistical building blocks around it, all balance-window-scoped and all sharing one shrinkage-adjusted engine (`tftlab.analytics.association.compute_associations`):

- **Partners** (`carry_partner_associations`) -- for every teammate seen in the carry's commitment games: games together, inclusion rate, avg placement/Top4/win rate together, and a **with-vs-without** comparison against the carry's *other* commitment games (not global averages).
- **Items** (`item_package_stats`) -- the same with-vs-without comparison for individual completed items, item pairs, and exact 3-item packages (packages need `min_package_games`, default 2, before being surfaced at all).
- **Traits** (`trait_breakpoint_associations`) -- the same for active trait breakpoints (e.g. `"Juggernaut:2"`).

None of these rank by raw performance. Each factor gets a shrinkage-adjusted `top4_delta` (posterior Top4-with minus posterior Top4-without, both pulled toward the carry's own baseline) and a `confidence` from `min(games_with, games_without)`; the two multiply into `association_score`, which is what results are sorted by. This is deliberate: a partner seen in 2 games with a 100% Top4 rate must not outrank one seen in 15 games at 80%, and it doesn't (`tests/test_association.py` asserts this with hand-derived numbers). The same protection applies to a 2-game "BIS" item package.

### DiscoveryCandidate and Opportunity Score v2

`tftlab.analytics.discover_candidates` (backing `/api/discovery`) assembles each qualifying carry into a `DiscoveryCandidate`: its commitment stats, best-evidenced partners/item-packages/trait-breakpoints, and a v2 Opportunity Score. The score (`compute_opportunity_score`, in `tftlab/analytics/discovery.py`) is a weighted blend of mostly-orthogonal 0..1 signals, every one of which is returned alongside the score (`opportunity_components`) rather than hidden inside a single number:

| Component | Weight | What it measures |
|---|---|---|
| `relative_performance` | 0.25 | Posterior Top4 vs. this balance window's own carry population, weighted by each carry's own sample size (see "Population baseline" below) -- not a hardcoded 50% |
| `carry_rarity` | 0.12 | Low carry-**commitment** rate -- how rare it is for this unit to be built as a real carry at all |
| `champion_rarity` | 0.08 | Low champion **presence** rate -- how rare the unit is on a board in the first place, independent of build |
| `confidence` | 0.10 | Sample-size trust |
| `floor_ceiling` | 0.20 | Shrinkage-adjusted blend of hit-game Top4 and miss-game Top4 (see "Floor/ceiling shrinkage" below) -- a carry that's only good when it 3-stars scores low here even if its blended rate looks fine |
| `partner_shell` | 0.10 | Strength of the single best partner association |
| `item_flexibility` | 0.05 | How many independently-supported, at-least-neutral completed items exist, not just whether the single best one is positive (see "Item flexibility" below) |
| `cost_bias` | 0.10 | Favors 1-3 cost for discovery purposes; 4-5 cost still scores, just not boosted |

Weights sum to 1.0 (enforced by a test). `carry_rarity`/`champion_rarity` split what a single "rarity" component used to conflate, at the same combined weight (0.20 total) -- not a second full-weight rarity signal. Having both, side by side and never collapsed into one number, is what lets you tell apart "rare champion + rare carry" (both high), "common champion + unusual/off-meta carry" (`champion_rarity` low, `carry_rarity` high), and "rare champion that's commonly itemized whenever played" (both track together; check `carry_conversion_rate` on the candidate itself to see it's the *pick* that's rare, not the build choice). `relative_performance` and `floor_ceiling` both ultimately derive from placement outcomes, but they're deliberately kept at moderate (not dominant) weights and measure different things -- overall strength vs. hit/miss consistency -- specifically so one placement-derived signal doesn't get counted twice under two names. `tests/test_discovery.py` includes a deterministic case where a carry that's spectacular only in its (rare) hit games and bad everywhere else scores below a carry that's consistently solid, despite the same handful-of-hits looking flawless in isolation, and a case where a low-usage carry with similar raw performance to a very common one still surfaces above it.

#### Population baseline

`relative_performance` (and the floor/ceiling shrinkage prior below) is computed by `_population_baseline_top4`: a **commitment-observation-weighted** average of every qualifying carry's posterior Top4 rate in the balance window -- each carry contributes in proportion to its own `commitment_games`, not one vote per carry. A carry with 2 fluky games can't swing the "typical" performance for the window the way an unweighted per-carry average would; `tests/test_discovery.py::test_population_baseline_is_commitment_weighted` adds a 2-game 100%-Top4 outlier on top of several hundred normal games and asserts the baseline barely moves.

#### Floor/ceiling shrinkage

`hit_top4_rate`/`miss_top4_rate` stay exposed on the API exactly as raw rates -- this shrinkage only affects the `floor_ceiling` *scoring component*. Before averaging them, each is pulled toward the population baseline in proportion to how few hit/miss games back it (a small Bayesian shrink, prior strength 10), so e.g. a single lucky hit-game at 100% Top4 can't single-handedly max out the "ceiling" half of the score the way the raw rate would.

#### Item flexibility

`compute_item_flexibility` counts completed items that are both well-evidenced (>= 3 games and >= 0.15 confidence) and at-least-neutral (`top4_delta >= 0`, or no "without" comparison at all -- an item present in every commitment game has no evidence against it). The viable count is capped at 3 alternatives for full credit and mapped linearly to 0..1. This replaces an earlier `positive-associations / all-observed-items` formula that could hand a carry with exactly one lightly-sampled positive item a "perfect" 1.0; `tests/test_discovery.py` asserts a carry with one viable item scores below a carry with several independently-supported ones.

## API endpoints

All of these are balance-window scoped (`?balance_window=`, defaulting to the latest window) and cost-neutral to call (frontend changes are a separate milestone):

- `GET /api/discovery` -- ranked `DiscoveryCandidate` list (`max_cost`, `min_samples`, `top_n`, `limit`)
- `GET /api/discovery/{character_id}` -- one carry's full `DiscoveryCandidate`
- `GET /api/carries/{character_id}/partners` -- partner associations
- `GET /api/carries/{character_id}/items` -- `{"items": [...], "pairs": [...], "packages": [...]}`
- `GET /api/carries/{character_id}/traits` -- trait-breakpoint associations
- `GET /api/carries`, `GET /api/carries/{character_id}` -- unchanged from before this milestone (still back the current frontend)

## Next milestones

- Candidate-board generation with beam search (explicitly out of scope for this milestone).
- Known-comp similarity detection against external public sources.
- TFT Academy-style comp pages with an evidence panel showing observed vs inferred recommendations, and a frontend that surfaces partner/item/trait evidence and the Opportunity Score breakdown.

## Riot policy boundary

This project is designed around aggregate/post-game analysis and static recommendations. It should not become a live tool that reads opponents' boards or dynamically dictates decisions during a match.

## Website

Milestone 1.5 includes a functional web UI for the discovery engine.

```bash
pip install -e .
tftlab web
```

Then open `http://127.0.0.1:8000`.

The website automatically uses `DATABASE_URL` (Postgres) or `TFT_DB_PATH` (SQLite) when that database contains matches. If neither is available, it creates a deterministic synthetic demo dataset and clearly labels the UI as **Demo dataset**. Use demo data only to test the product flow; it is not live TFT performance data.

Current web pages/features:
- TFT Theory Lab landing/discovery page
- 1/2/3/4/5-cost filter
- Minimum-sample filter
- Hidden reroll candidate cards
- Opportunity Score
- Hit-vs-miss 3-star performance
- Recurring partner analysis
- Observed item packages
- Responsive desktop/mobile layout

The next web milestone is a dedicated comp page with a hex board, champion/item assets, traits, patch selector, known-vs-theorycrafted labels, and live Riot/CommunityDragon data.

## Deploying on Render

The repo includes a `render.yaml` Blueprint that runs the FastAPI app with:

```
uvicorn tftlab.webapp:app --host 0.0.0.0 --port $PORT
```

If no live database is reachable, the app automatically falls back to a generated demo dataset, so it boots and serves data even with zero configuration. `/api/health` and `/api/carries` report `"demo"` (true/false) and `"backend"` (`"sqlite"`/`"postgres"`) so it's always clear which one is live.

To move off ephemeral SQLite in production: create a Postgres database (a Render Postgres instance's "Internal Connection String" works well) and set `DATABASE_URL` in the Render dashboard — `render.yaml` already declares it (and `RIOT_API_KEY`) as `sync: false`, meaning Render will prompt for a value but never store one in the repo.
