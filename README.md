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

The initial Opportunity Score combines:

- Bayesian-shrunk Top-4 strength
- Bayesian-shrunk win signal
- low carry usage (rarity)
- sample-size confidence

It deliberately does **not** reward 3-star hit rate. Instead we display hit and miss performance separately so a powerful-but-fragile reroll is not confused with a reliable line.

## Storage backends

`Database` (in `tftlab/storage.py`) is backend-agnostic: pass it a filesystem path for SQLite, or a `postgres://`/`postgresql://` URL (typically from a `DATABASE_URL` environment variable) for Postgres. Every query in the codebase is written once with `?`-style placeholders; the Postgres path translates them internally, so analytics/ingest code never branches on which database it's talking to.

- **Local/demo/tests**: SQLite, as before. No setup needed.
- **Production**: set `DATABASE_URL` to a Postgres connection string. Install the `postgres` extra (`pip install -e ".[postgres]"`, already done for you by `render.yaml`) so `psycopg` is available.
- **Schema/migrations**: the schema is created automatically on first connect (`CREATE TABLE IF NOT EXISTS ...`), for either backend — no manual migration step for a fresh database. The one schema change so far (adding `matches.patch`) is applied automatically to older databases too (`ALTER TABLE ... ADD COLUMN`), so upgrading in place is also automatic.
- Never commit a real `DATABASE_URL` (or any credential) — set it in your host's environment/secret manager. `.env` is gitignored and `.env.example` only has placeholders.

## Patch-aware analytics

TFT champions and items get rebalanced every patch, so mixing patches in one query would blend unrelated data. `carry_commitment_stats` always scopes to a single patch:

- Pass `patch="14.6"` explicitly, or
- Omit it and the store's most-played patch is used automatically (`tftlab.analytics.default_patch`).

The web API exposes this as an optional `?patch=` query param on `/api/carries` and `/api/carries/{id}`; the current frontend doesn't send it, so behavior is unchanged there, but a patch selector can be added later without any backend work.

## Static metadata (CommunityDragon)

`tftlab.cdragon.CommunityDragonClient` fetches and disk-caches TFT static metadata (champion shop costs, champion/item/trait names and art) from CommunityDragon. `tftlab ingest-riot` uses it by default to resolve authoritative champion costs instead of the `rarity + 1` heuristic (pass `--no-use-static-costs` to disable). If CommunityDragon is unreachable, ingestion logs a warning and falls back to `rarity + 1` rather than failing.

## Next milestones

- Unit-pair / support-shell association statistics.
- Item-package analysis.
- Trait-breakpoint analysis.
- Candidate-board generation with beam search.
- Known-comp similarity detection against external public sources.
- TFT Academy-style comp pages with an evidence panel showing observed vs inferred recommendations.

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
