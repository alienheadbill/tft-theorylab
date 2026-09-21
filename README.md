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
pip install -e ".[dev]"
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

## Next milestones

- Join CommunityDragon static metadata instead of relying on `rarity + 1` for shop cost.
- Patch/set filters and patch-window weighting.
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

The website automatically uses `TFT_DB_PATH` when that database contains matches. If no live database is available, it creates a deterministic synthetic demo dataset and clearly labels the UI as **Demo dataset**. Use demo data only to test the product flow; it is not live TFT performance data.

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

If no live TFT database is present at `TFT_DB_PATH`, the app automatically falls back to a generated demo dataset, so it boots and serves data even with zero configuration. Set `RIOT_API_KEY` (and optionally `TFT_PLATFORM`/`TFT_REGION`) in the Render dashboard to enable live ingestion later; never commit real keys to `.env` or the repo.
