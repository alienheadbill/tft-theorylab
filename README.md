# TFT Theory Lab

A patch-aware analytics prototype for discovering **low-usage, data-backed TFT carry lines**, with an emphasis on 1/2/3-cost rerolls.

## Milestone 1

The first vertical slice does four things:

1. Seeds recent ranked matches from selected Riot TFT ladder players (Challenger, Grandmaster, Master, Diamond, Platinum -- see "Data population and seed cohorts").
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
3. Verify the key works before pulling any real data:

   ```bash
   tftlab verify-riot
   ```

   This makes one minimal authenticated request (no ingestion) and reports success/failure. It never prints the key itself, and distinguishes 401 (invalid/expired key), 403 (key lacks permission), and 429 (rate limited -- back off and retry) rather than a generic failure.

4. Run a small, safe first ingest:

   ```bash
   tftlab ingest-riot --challenger-seeds 10 --matches-per-player 5
   ```

   Seeds are requested per cohort: `--challenger-seeds`, `--grandmaster-seeds`, `--master-seeds`, `--diamond-seeds`, `--platinum-seeds` (any may be 0; one left out is 0) -- see "Data population and seed cohorts" below. The older weighted modes still work when no `--<cohort>-seeds` is given (`--players N`, Challenger-only by default, `--sampling high_elo` or `--include-master` for Challenger / Grandmaster / Master 4:3:3), and still report each tier separately. Prints a report: sampling mode, run id, requested and actual seed players, then for each cohort separately: requested, selected, the candidate count (`available ladder entries` for the apex cohorts; `fetched candidate pool` plus `pagination: complete` or `pagination: capped at N pages/division; additional players may exist` for Diamond/Platinum), ladder requests, never-sampled vs previously sampled seeds, seeds with no games in the requested history, failed history requests, match-ID references and unique match IDs it contributed; then overall: requested histories per seed, match-ID references returned before dedupe, unique match IDs after dedupe, unique match IDs found by more than one cohort, matches skipped as already stored, fetched, inserted, failed requests, non-ranked-queue matches skipped, derived rates (in-run overlap, already-stored duplicates, inserted per seed, new-match yield), seeds recorded in the sampling ledger, discovery-provenance rows written, balance windows found, and total participants now stored. A single match's fetch failing doesn't abort the batch -- it's counted in "failed requests" and the run continues; a single seed's history request failing is likewise counted and skipped (the command exits non-zero only if every seed's history failed).

   A ladder PUUID's recent match history isn't exclusively ranked TFT -- it can include Normal, Hyper Roll, or Double Up games too. `ingest_ladder` checks every fetched match's `queue_id` against `tftlab.riot.RANKED_TFT_QUEUE_ID` (`1100`, standard Ranked TFT) before inserting it; a non-target-queue match is reported separately (not as a failure -- Riot answered fine) and never stored, so this project's discovery dataset stays scoped to ranked TFT only.

5. Check the result:

   ```bash
   tftlab validate-live-data
   tftlab discovery-smoke
   tftlab leaderboard --min-samples 20
   ```

Riot development keys expire periodically, so a 401/403 after the key worked previously usually means the key needs refreshing -- `tftlab verify-riot` will say so directly.

By default, `ingest-riot` resolves champion shop costs from CommunityDragon and **aborts** (not silently falls back to `rarity + 1`) if that fetch fails, printing exactly why. Pass `--allow-degraded-costs` to proceed anyway; the run and its printed report are then clearly marked `DEGRADED INGEST`.

### Carry eligibility (item-only)

**Appearance** = the champion was on the board. **Carry commitment** = it finished with **>= 2 completed items** (2-star misses included) **and** that itemization was not entirely defensive. That is the only change: one Discovery system, no tank/hybrid/support statistics, no role table, no champion allowlist.

`tftlab.carry` classifies each completed item from CommunityDragon stat metadata (never from its display name):

- **Offensive** -- any of the holder's own damage stats: effects `AD`, `AP`, `AS`, `CritChance` and their Set 18 variants (`AD_NotStatBar`, `AP_NotStatBar`, `ADIncrease`, `APIncrease`, `StackingAD`, `StackingSP`, `ADOnAttack`, `ADPerBonus`, `APPerBonus`, `ASPerStack`, `AttackSpeedPerStack`, `ADAPPerTakedown`, `CritDamageToGive`, `CritDamageBonusPercent`, `DamageAmp`), or tags `AttackDamage`, `AbilityPower`, `AttackSpeed`, `CritChance`. Offensive + defensive stats (Titan's Resolve, Sterak's Gage, Crownguard) is offensive. Ally buffs (Zephyr `AllyBonusAS`, Aegis `ASBuff`, Banshee's `BuffAttackSpeed`, Chalice `ChaliceAP`, Zeke's `AttackSpeed`) are not.
- **Defensive** -- effects `Health`, `Armor`, `MagicResist` or tag `Health`, and nothing offensive (Warmog's, Gargoyle, Dragon's Claw, Spirit Visage, Bramble, Sunfire, ...: 39 completed Set 18 items).
- **Unknown** -- anything else: not in the snapshot, only unresolved `{hash}` names, or only passive parameters. Set 18 trait emblems (`DA_18_Emblem*`; Ravager Emblem = `DA_18_EmblemSlayer`) have no readable stats, so they are unknown.

A board is excluded **only** when every completed item is defensive. Any offensive **or unknown** completed item keeps it a carry commitment -- uncertain means include, because hiding an unusual build is worse than a false positive. Star level plays no part. So Elise with Warmog's + Gargoyle is not a carry observation, while Elise with Ravager Emblem + Guinsoo's (or Gargoyle + Guinsoo's + Titan's) is -- per board, never per champion.

**Item ids are the exact ones Match-V1 stores.** Set 18 boards store the `DA_*` namespace (`DA_GargoyleStoneplate`, `DA_GuinsoosRageblade`, `DA_TitansResolve`, ...). CommunityDragon has an entry for each, but with **no named `effects`** and only some readable tags (e.g. `DA_GuinsoosRageblade` = `AttackSpeed`, `DA_WarmogsArmor` = `Health`; `DA_GargoyleStoneplate` and `DA_DragonsClaw` have only hashed tags). So the snapshot keeps each exact `DA_*` id and adds the stats of its `TFT_Item_*` counterpart **only when the alias is verified unambiguous**: identical display name, component names not contradicting, and every remaining candidate sharing one stat signature (a Corrupted copy with the same stats is fine; two different "Blue Buff" entries are not). The display name, not the id, is the bridge -- a prefix rewrite would be wrong: `DA_RedBuff` ("Red Buff") is `TFT_Item_RapidFireCannon`, while `TFT_Item_RedBuff` is "Sunfire Cape". Items with no verified alias keep only their own metadata (often none, i.e. unknown). The snapshot holds 256 items: 185 `TFT_Item_*`/`TFT18_Item_*` plus 71 `DA_*` (craftables, components, `DA_18_Emblem*`, `DA_Item_*`), 41 of them aliased; `DA_*` augments are deliberately left out.

Metadata reaches analytics without the network: `src/tftlab/data/item_stats.json` is a committed snapshot of CommunityDragon's readable item stats (from `tftlab.cdragon.item_stats_snapshot`), loaded once per process; the opt-in live test compares it with the feed and prints a fresh copy when it drifts. The rule runs inside SQL (`carry_commitment_sql`: defensive items counted exactly in `items_json`, duplicates included), identically in SQLite and Postgres, in every query that selects carry boards -- commitment stats, partners, item packages, traits, the carry detail endpoints and Comp Scout. Appearance counts, Opportunity Score weights, 3-star logic and the association formulas are unchanged; only commitment games, commitment/carry-conversion rates, carry outcomes and the evidence built on them move.

**Champion roles were investigated and are not used.** CommunityDragon's champion `role` field (`APTank`, `ADTank`, `APCaster`) was null for 72 of 74 Set 18 shop champions on 2026-09-26 (only Kobuko and Alune had one). `ChampionMeta.role` preserves it as served, and the live inventory test shows if that changes.

## Current scoring

`carry_commitment_stats` (`tftlab/analytics/commitment.py`) still exposes a simple, Bayesian-shrunk `opportunity_score` per carry, combining Top4/win strength, rarity, and confidence -- this backs the existing `/api/carries` leaderboard and CLI `leaderboard` command unchanged. It deliberately does **not** reward 3-star hit rate; hit and miss performance are shown separately so a powerful-but-fragile reroll isn't confused with a reliable line.

The richer, discovery-focused **Opportunity Score v2** (partner/item/trait-evidence-aware, floor/ceiling-aware) is a separate, additive score computed only for `DiscoveryCandidate`s -- see "Comp-discovery engine" below.

### Usage vs. presence vs. conversion

`CarryStat`/`DiscoveryCandidate` expose four related-but-distinct rates rather than one ambiguous `usage_rate`:

- `appearance_rate` -- how often this champion is on a board *at all* (`appearances / unit-observable participants`), regardless of build.
- `commitment_rate` -- how often it's on a board *built as a >=2-item carry* (`commitment_games / unit-observable participants`). This is what `usage_rate` has always actually measured.
- `usage_rate` -- kept as a deprecated alias of `commitment_rate` for backward compatibility; new code should read the named rate it actually means.
- `carry_conversion_rate` -- of the games it appeared in at all, how often it became a committed carry (`commitment_games / appearances`).

**Unit-observable participants** are the participants in the balance window with at least one stored unit. A participant Riot itself sent with an empty (or missing) `units` list -- a *source-empty* board, see `validate-live-data` below -- has no observable board, so it could never contribute an appearance; counting it would deflate every champion's rate. It is left out of these two denominators only. Its match, placement and row stay stored and count everywhere else.

These separate a champion being rare to see (`appearance_rate`) from a champion being rare to build as a real carry (`commitment_rate`/`carry_conversion_rate`) -- e.g. a common champion that's almost never itemized as a carry (high `appearance_rate`, low `carry_conversion_rate`) is a very different discovery story than a rare champion that's a carry every time it's picked (low `appearance_rate`, high `carry_conversion_rate`).

## Storage backends

`Database` (in `tftlab/storage.py`) is backend-agnostic: pass it a filesystem path for SQLite, or a `postgres://`/`postgresql://` URL (typically from a `DATABASE_URL` environment variable) for Postgres. Every query in the codebase is written once with `?`-style placeholders; the Postgres path translates them internally, so analytics/ingest code never branches on which database it's talking to.

- **Local/demo/tests**: SQLite, as before. No setup needed.
- **Production**: set `DATABASE_URL` to a Postgres connection string. Install the `postgres` extra (`pip install -e ".[postgres]"`, already done for you by `render.yaml`) so `psycopg` is available.
- **Schema/migrations**: the schema is created automatically on first connect (`CREATE TABLE IF NOT EXISTS ...`), for either backend — no manual migration step for a fresh database. Schema changes since (adding `matches.patch`/`matches.balance_window`, and `units.unit_index` — see below) are applied automatically to older databases too on every connect, idempotently, without wiping or recreating anything, so upgrading a database that already has real match data in place is also automatic.
- **`units.unit_index`**: a real TFT board can field more than one instance of the same champion in one game (e.g. via clone/duplication effects), so `units`' primary key is `(match_id, participant_index, unit_index)`, not `character_id` — `character_id` remains a normal, indexed column. A database created before this existed is migrated in place the next time `Database` connects to it (backfilling `unit_index` from each row's original insertion order), and every analytics query that reads `units` collapses a participant's duplicate champion instances back down to one observation per game rather than double-counting them (see `analytics/commitment.py`, `analytics/item_packages.py`, and `tests/test_duplicate_units.py`).
- **Ingest writes**: each match is stored in one transaction, with its participants, units and traits written as one batch per table (`Database.executemany`; psycopg pipelines these). Over a remote connection (GitHub Actions to Render Postgres) this is what keeps a larger ingest within the workflow timeout -- one round trip per row made a 34-match run take about ten minutes.
- Never commit a real `DATABASE_URL` (or any credential) — set it in your host's environment/secret manager. `.env` is gitignored and `.env.example` only has placeholders.

## Balance-window-aware analytics

TFT champions and items get rebalanced every patch -- and sometimes mid-patch, without the client's major.minor version changing -- so mixing two different balance states in one query would blend unrelated data. Every analytics function in `tftlab.analytics` scopes to a single **balance window**, not just a client patch:

- `tftlab.balance_window.resolve_balance_window(client_patch, game_datetime)` derives a window like `18.2a`/`18.2b` from a small registry of known mid-patch cutovers (a client patch with no registered cutover is simply its own window, e.g. `18.3`).
- Pass `balance_window="18.2b"` explicitly to any analytics function, or omit it and the chronologically **latest** window in the store is used (`tftlab.analytics.default_balance_window`) -- never just the most-played one, and ordered numerically (`18.10` sorts after `18.9`), not lexicographically.

The web API exposes this as an optional `?balance_window=` query param on every carry/discovery endpoint. `GET /api/balance-windows` lists every window actually present in the store (never the Unreal-unresolved sentinel, which always has a `NULL` balance_window); the dashboard's window selector is built directly from this endpoint, so it can only ever offer a real, resolvable window.

### Unreal-era client patch resolution

Riot Match-V1 has started returning a masked, unparseable `game_version` for some matches (the literal placeholder `"TFT Unreal Version ?.?.?.?"`), with no major.minor version left in the string at all. `patch_from_game_version` always tries a normal numeric `Version X.Y` parse **first** -- a hypothetical future `"TFT Unreal Version 18.3.1234"` resolves to `"18.3"` via that normal parse, never routed into timestamp resolution just for mentioning "Unreal" -- and only falls back to `tftlab.unreal_patch.resolve_unreal_patch` when the string matches the actual `"?.?.?.?"` placeholder shape.

`resolve_unreal_patch` resolves the real client patch purely from `game_datetime`, via `UNREAL_PATCH_REGISTRY` -- a registry of **bounded, conservative classification windows** (`UnrealPatchWindow(client_patch, starts_at, ends_at, verified, source)`, `ends_at` exclusive), kept entirely separate from `balance_window`'s mid-patch registry above (one answers "which client patch"; the other then answers "which half of that patch"). A timestamp before the earliest window, in a gap between two windows, or at/after the latest window's `ends_at` all resolve the same way: unresolved. The newest registered patch never implicitly "extends forever" just because nothing later is registered yet -- every window's end must be explicit.

**A window is a classification window, not a deployment window.** `starts_at`/`ends_at` are not a claim about the exact second Riot flipped a patch live, or when every region received it -- real rollouts are messy (regional staggering, early/late deploys). Each window is deliberately drawn with a safety margin so it's entirely inside one patch's real lifetime, with the actual rollout-transition period around a patch boundary left as an unresolved gap on purpose. Losing a handful of matches to "unresolved" during a patch-day transition is the accepted, correct cost -- far better than silently mixing two different balance states into one bucket.

**Only verified, sourced windows are ever used to classify a match.** `UnrealPatchWindow.is_usable` requires both `verified=True` and a non-empty `source`; an unverified or sourceless entry is silently skipped during resolution (as if it weren't in the registry at all) rather than affecting production classification, no matter how plausible its timestamp looks. `tftlab validate-live-data` and `tftlab patch-diagnostics` both report `unreal_registry_usable_windows`/`unreal_registry_total_windows`, so a registered-but-not-yet-verified window is visible, not silently inert.

**Currently populated with two conservative windows**, sourced from Riot's official TFT patch schedule (<https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/> -- 18.2 scheduled 2026-09-10, 18.3 scheduled 2026-09-23 Pacific Time, 18.4 scheduled 2026-10-07):

| Patch | Window (UTC) | Notes |
| --- | --- | --- |
| `18.2` | `2026-09-11T00:00:00Z` .. `2026-09-22T00:00:00Z` (exclusive) | Starts a full day after the scheduled release; ends before the earliest reported NA 18.3 sighting. |
| `18.3` | `2026-09-24T07:00:00Z` .. `2026-10-06T00:00:00Z` (exclusive) | Starts after the full scheduled Pacific-Time release day has elapsed everywhere in that timezone; ends before the next scheduled patch (18.4). |

The gap between them (`2026-09-22T00:00:00Z` through `2026-09-24T07:00:00Z`) deliberately covers the reported early-NA-18.3 rollout ambiguity and stays `UNRESOLVED_UNREAL_PATCH` -- see `src/tftlab/unreal_patch.py`'s module comment for the full reasoning. When a match's `game_datetime` doesn't fall in any usable window:

- It resolves to the explicit `UNRESOLVED_UNREAL_PATCH` sentinel (`"unreal-unresolved"`), never a fake shared patch bucket and never the raw masked string.
- Its `balance_window` is left `None`, so it's automatically excluded from every balance-window-scoped analytics query (never silently blended into a real window's stats) and counted in `IntegrityReport.unresolved_unreal_matches` / flagged by `tftlab validate-live-data` as a severe issue (a missing balance window always is) until resolved.
- Run `tftlab patch-diagnostics` (read-only, no Riot/CommunityDragon calls -- see below) or `tftlab validate-live-data` and read the diagnostics (raw `game_version` distribution, resolved patch distribution, balance-window distribution, earliest/latest `game_datetime`, and specifically the masked-Unreal matches' own earliest/latest `game_datetime` -- store-wide, not scoped to one window) to see the actual timestamp range needing a wider or additional window, without running another ingest.

To extend this as new patches ship: read off the actual masked-Unreal timestamp range via `tftlab patch-diagnostics`, cross-reference Riot's published patch schedule, and add another conservative, verified+sourced `UnrealPatchWindow` to `UNREAL_PATCH_REGISTRY` in `src/tftlab/unreal_patch.py`, following the same margin-in-from-both-ends pattern. Every affected row -- already-ingested matches included -- picks up the correct patch automatically on the next `Database` connect; no separate backfill command or data wipe is needed.

## Static metadata (CommunityDragon)

`tftlab.cdragon.CommunityDragonClient` fetches and disk-caches TFT static metadata (champion shop costs, champion/item/trait names and art) from CommunityDragon. `tftlab ingest-riot` uses it by default to resolve authoritative champion costs instead of the `rarity + 1` heuristic (pass `--no-use-static-costs` to disable). If CommunityDragon is unreachable, ingestion **aborts** rather than silently falling back to `rarity + 1` -- see "Production safety" below; pass `--allow-degraded-costs` to proceed anyway with a prominently marked `DEGRADED INGEST`.

A separate GitHub Actions workflow (`.github/workflows/cdragon-live.yml`) smoke-tests the real feed on a manual trigger or nightly schedule. It's intentionally isolated from any PR/unit-test CI (it has no `push`/`pull_request` trigger), so a CommunityDragon outage never blocks a merge.

## Data population and seed cohorts

**Data population:** recent NA standard Ranked TFT matches discovered through selected ladder players. This is not "all TFT games" and is not globally representative -- it is whatever the seed players played recently (inside the current trusted patch window in production), restricted to the ranked queue (`RANKED_TFT_QUEUE_ID`).

**Seed cohorts:** seeds come from five separate cohorts -- `challenger`, `grandmaster`, `master`, `diamond`, `platinum`. There is no combined "elite" cohort: Challenger, Grandmaster and Master are always requested, selected, counted and reported on their own. Diamond I-IV collapse into one `diamond` cohort and Platinum I-IV into one `platinum` cohort; the division is only used to spread seeds across the tier and is never exposed analytically. Emerald (between Platinum and Diamond) is not a cohort.

**A seed cohort describes how a lobby was discovered, not every player's rank.** It is sampling provenance: the ladder a seed player was on when their history was read. The other seven players in that lobby can be any rank, and matches and participants are never labelled with a seed's rank. A lobby reached by seeds from two cohorts is one match with two provenance rows.

**Analytical search space:** everything observed inside that population, all cohorts combined -- every champion, every qualifying carry, every partner, item package and trait breakpoint on every sampled board. Discovery analyzes the combined population (there are no rank-specific performance stats), is not limited to the comps saved in My Experiments or to any named champions; the examples there (Kha'Zix, Cassiopeia / Fiddlesticks, Caitlyn, ...) are test cases, not a search list.

### Ladder endpoints (TFT-LEAGUE-V1, platform route, e.g. `na1`)

| Cohort | Request(s) |
|---|---|
| `challenger` | `GET /tft/league/v1/challenger` |
| `grandmaster` | `GET /tft/league/v1/grandmaster` |
| `master` | `GET /tft/league/v1/master` |
| `diamond` | `GET /tft/league/v1/entries/DIAMOND/{I,II,III,IV}?queue=RANKED_TFT&page=N` |
| `platinum` | `GET /tft/league/v1/entries/PLATINUM/{I,II,III,IV}?queue=RANKED_TFT&page=N` |

The entries endpoint returns one page of `LeagueEntryDTO`s (`puuid`, `tier`, `rank` = division, `leaguePoints`, ...); `page` starts at 1 and `queue` defaults to `RANKED_TFT`. Riot documents neither the page size nor a last-page marker, so for every one of the four divisions pages 1..N are read, stopping at the first empty page or at `--max-ladder-pages` (default 3, at most 10). This always covers **all four divisions**, so candidates are not just the top of the tier -- but it may **not** cover every page or player in those divisions. If every division reaches an empty page before the cap, the report says `pagination: complete` and the candidate pool is the whole Diamond/Platinum ladder; if any division's last allowed page still had entries, it says `pagination: capped at N pages/division; additional players may exist`, and the count is only the fetched candidate pool, never the ladder size. The apex endpoints return their whole league list, so their count is reported as available ladder entries. A cohort is only fetched when it has a non-zero request.

### Seed selection and rotation

Seed selection (`tftlab.sampling`) knows nothing about champions, items, traits, comps, placements or performance -- it only uses ladder standing and the sampling ledger, deterministically:

1. Fetch each requested cohort once. A player listed in two fetched cohorts (e.g. promoted between requests) counts once, in the higher cohort, so no one is seeded twice.
2. Rank each cohort by division (I before IV), then League Points, then PUUID, so Riot's response order never matters.
3. **Rotate:** prefer players never sampled before, then the least recently sampled. Players are grouped by when the ledger last sampled them (never-sampled is the oldest group); whole groups are taken oldest-first while they fit, and the group that doesn't fit is thinned to evenly spaced ranks across the cohort's fetched candidates -- not its top LP. With an empty ledger this is exactly "evenly spaced across the candidates" (for Diamond/Platinum: across all four divisions, within the pages fetched). Same ladder + same ledger always gives the same seeds.
4. Explicit per-cohort counts are honoured as given; a cohort short of players gives what it has and the shortfall shows in the report (it is not moved to another cohort). The legacy weighted modes split `--players` by weight (largest remainder) and re-split a short tier's seats.

Match IDs are then deduplicated across all seed histories and cohorts before any body is fetched (a lobby is usually in several seeds' histories and becomes one match), and IDs already stored are skipped without being fetched again.

### Sampling ledger and discovery provenance (additive tables)

- `seed_samples(run_id, puuid, cohort, sampled_at)` -- one row per seed whose history request succeeded in a run (an empty in-window history counts; a failed request does not, so that player stays eligible). Only the PUUID is stored: no Riot ID, name or summoner id. Rotation reads only rows whose run is **completed** in `ingest_runs` (below).
- `match_discoveries(match_id, run_id, puuid, cohort, discovered_at)` -- many-to-many provenance: one row per stored match per seed that surfaced it in a run. The match itself stays one canonical row in `matches`. Rows are written only for matches that are stored (inserted this run or already present), not for failed fetches or non-ranked queues.

Run ids are `gh-<GITHUB_RUN_ID>-<attempt>` in Actions and `local-<epoch ms>` otherwise.

### Ingest runs: completion, resume and deadlock retry

- `ingest_runs(run_id, started_at, completed_at, status, failure)` -- every ingest registers its run as `started` before touching Riot. **A run is completed only when `status = 'completed'` and `completed_at` is set**, which happens in one final transaction together with that run's `seed_samples` and `match_discoveries` rows. If anything fails before or during that transaction, none of the three is visible as completed; the run is marked `failed` (best effort, exception type only) or simply stays `started` if the process died.
- `seed_last_sampled()` (what rotation reads) joins `seed_samples` to `ingest_runs` and counts only completed runs. Ledger rows with no completed run -- an interrupted run, or rows written by the pre-`ingest_runs` build (e.g. the first five-cohort run that failed on a deadlock) -- are ignored automatically and never deleted; they remain audit evidence.
- **Resume:** matches are still stored one transaction each, so a run that dies after storing N matches keeps those N. Because it never completed, the next run selects the same eligible seeds, their histories return the same IDs, the N stored matches are skipped by `has_match` (never duplicated), the missing ones are fetched, and the new run records provenance for both before completing -- only then do those seeds advance rotation.
- **Deadlocks:** a PostgreSQL deadlock (SQLSTATE `40P01`, psycopg `DeadlockDetected`) while storing one match rolls that match's transaction back and retries the same match after 0.5 s, then 1 s (3 attempts in all). If it still deadlocks, the run fails. No other database error is retried. The report shows `Database deadlock retries` (and how many matches needed one / recovered); a failed run prints its run id and that seed rotation will ignore it.

### Database access: the web app never runs schema setup

`Database(target)` (CLI, ingest, admin, local SQLite, demo) creates and migrates the schema: CREATE TABLE, ALTER TABLE, backfills, CREATE INDEX. Web requests against a configured `DATABASE_URL` use `Database.open_existing(target)` instead, which runs none of that: Postgres sessions are opened with `default_transaction_read_only=on` (SQLite in `mode=ro`), so any accidental write fails loudly, and the tables/columns the app reads are checked first. A missing or incompatible schema returns the existing 503 "production database unavailable" response -- it is never created from a request. The web app is read-only; no API request writes anything.

## Comp-discovery engine (carry + partners + items + traits)

**Current limit: Discovery v1 is carry-centric.** It finds unusual carries and, around each, its partner shells, item packages and trait breakpoints. It does not yet treat arbitrary complete boards as entities of their own, so a niche comp built around a *common* carry isn't surfaced separately. That is a later milestone (likely normalized final-board signatures plus clustering / similarity), once there is enough data.

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

## Production safety

`webapp._resolve_database` (used by every API endpoint) recognizes three states, and never silently blurs them together:

1. **`DATABASE_URL` unset** -- local/dev only. Falls back to a populated local SQLite file, then the deterministic demo dataset. This is the *only* case where demo data is ever served.
2. **`DATABASE_URL` set and reachable** -- always treated as live (`"demo": false`), even with zero matches so far (a fresh production database before first ingest). It is never swapped for demo data just because it's empty.
3. **`DATABASE_URL` set but unreachable** -- every endpoint returns **HTTP 503** with `{"ok": false, "demo": false, "status": "error", "error": "..."}` instead of falling through to demo data. The error message never includes the DSN or credentials. This is intentional: if you've pointed the app at a real database, a connection failure is a production incident to see immediately (including via Render's own health check, since `render.yaml` points `healthCheckPath` at `/api/health`), not something to paper over.

`/api/health` (and every other endpoint) also reports `backend` (`"sqlite"`/`"postgres"`), the active `balance_window`, and `matches`/`participants` counts, so "is this real data, and how much of it" is always answerable from one request.

## Operational CLI commands

- `tftlab verify-riot` -- one minimal authenticated Riot request to confirm `RIOT_API_KEY` works, without ingesting anything.
- `tftlab ingest-riot [--challenger-seeds N] [--grandmaster-seeds N] [--master-seeds N] [--diamond-seeds N] [--platinum-seeds N] [--max-ladder-pages N] | [--players N] [--sampling challenger|high_elo]; [--matches-per-player N] [--current-trusted-window | --start-time ISO8601] [--allow-degraded-costs]` -- see "Run on live Riot data" above.
- `tftlab validate-live-data [--db ...] [--balance-window ...] [--no-check-metadata]` -- data-integrity checks against ingested data for one balance window: total matches/participants, how many of those matches are ranked-TFT vs. a non-target queue (visibility only -- never deletes non-target rows itself), % of units with a present shop cost, CommunityDragon champion coverage and unknown champion/item/trait IDs (cross-checked against a live CommunityDragon fetch unless `--no-check-metadata`; reported as "skipped" rather than a possibly-wrong empty list/0% when metadata isn't available), matches missing a `balance_window`, malformed placements, duplicate match IDs, participants with no units at all (split in two, see below), and matches with an unresolved Unreal-era patch (see "Unreal-era client patch resolution" above). **A missing `balance_window` is split into two counts**: `matches_missing_balance_window` (every such match) and `unexpected_missing_balance_window` (everything except matches intentionally left unresolved because their masked-Unreal `game_version` doesn't fall in any usable `UNREAL_PATCH_REGISTRY` window -- a `NULL` patch, e.g. a totally missing `game_version`, always counts as unexpected). **Participants without units are split in two as well**: `source_empty_participants` -- the participant's own raw Riot entry (same position in `info.participants`, same placement, same participant count) has a `units` field that is missing or `[]`, so storage is faithful to the source (first seen in production on 18.3: a 2nd-place, level-9 board Riot sent with no units) -- and `unexpected_participants_without_units` -- the raw entry lists units that aren't stored, or the raw/stored mapping can't be trusted. Source-empty boards are printed as a warning and kept exactly as stored; only the unexpected kind is severe. **Exits non-zero** on the structural checks -- `unexpected_missing_balance_window`, malformed placement, duplicate ID, `unexpected_participants_without_units`. Intentionally-unresolved Unreal rollout-gap matches are still printed prominently as a warning (with their own earliest/latest `game_datetime`), but do **not** by themselves fail validation: a production database sitting entirely inside a documented Unreal rollout gap ends "No severe integrity issues detected." Unknown IDs, a low cost-presence/metadata-coverage rate, and non-target-queue matches are also printed as warnings, not failures, since those can legitimately happen right after a patch before CommunityDragon updates, or before this milestone's queue filtering existed. Also prints store-wide (not window-scoped) diagnostics -- raw `game_version`/resolved-patch/balance-window distributions and the earliest/latest `game_datetime` -- specifically to help identify real Unreal-era patch cutover timestamps; never prints full match payloads.
- `tftlab discovery-smoke [--db ...] [--max-cost N] [--limit N]` -- runs the discovery engine against the latest balance window and prints the top candidates (carry, cost, commitment games, appearance/commitment/conversion rates, avg placement, Top4, win rate, 3-star hit rate, opportunity score, and the single best-evidenced partner/item-package/trait-breakpoint). Candidates under 30 commitment games are labeled `LOW SAMPLE`.
- `tftlab patch-diagnostics [--db ...]` -- store-wide, read-only game_version/patch/balance-window diagnostics: total matches, earliest/latest `game_datetime`, the three distributions from `validate-live-data` above, the masked-Unreal (unresolved) match count and its own earliest/latest `game_datetime`, and the Unreal patch registry's usable/total window count. Makes **no Riot API or CommunityDragon calls** and runs no discovery/analytics queries or deletions -- purpose-built to read off the real timestamp range needed to fill in `UNREAL_PATCH_REGISTRY` without running another ingest. Connecting still runs the normal, safe, idempotent Unreal-patch backfill migration (see above), which may move a still-unresolved row to the explicit sentinel; that's expected.

`validate-live-data`/`discovery-smoke`'s `--db` accepts either a SQLite path or a `postgres://` URL (defaulting to `DATABASE_URL`, then `TFT_DB_PATH`), and is deliberately typed as a plain string rather than a filesystem path -- `pathlib.Path` collapses a URL's `//` after the scheme, which would otherwise silently break it.

## API endpoints

Every carry/discovery endpoint below is balance-window scoped (`?balance_window=`, defaulting to the latest window):

- `GET /api/balance-windows` -- every balance window actually present in the store (`balance_window`, `matches`, `latest_game_datetime`), plus `default_balance_window` and a store-wide `unresolved_unreal_matches` count. Backs the dashboard's window selector; never lists the Unreal-unresolved sentinel as a selectable window.
- `GET /api/discovery` -- ranked `DiscoveryCandidate` list (`max_cost`, `min_samples`, `top_n`, `limit`)
- `GET /api/discovery/{character_id}` -- one carry's full `DiscoveryCandidate`
- `GET /api/carries/{character_id}/partners` -- partner associations
- `GET /api/carries/{character_id}/items` -- `{"items": [...], "pairs": [...], "packages": [...]}`
- `GET /api/carries/{character_id}/traits` -- trait-breakpoint associations
- `GET /api/carries`, `GET /api/carries/{character_id}` -- unchanged; predate `/api/discovery` and are kept for their own existing tests/consumers
- `GET /api/experiments`, `GET /api/experiments/{slug-or-id}` -- the read-only theorycraft notebook (see below); not balance-window scoped

## Frontend: Discovery Dashboard

The web frontend (`src/tftlab/web/`) is a single-page discovery dashboard built entirely on the API endpoints above -- no backend logic lives in the frontend. It defaults to 1-3 cost, sorted by Opportunity Score, and fetches the full candidate pool for the selected balance window once (`min_samples=1`, `max_cost=5`) so cost/sample/sort controls re-filter instantly client-side rather than round-tripping on every change. Each candidate card previews its single best-evidenced partner/item-package/trait-breakpoint (already returned by `/api/discovery`); clicking a card opens a full evidence panel backed by `/api/discovery/{character_id}` at a higher `top_n`. Candidates under 30 commitment games are labeled `LOW SAMPLE`, matching the CLI's `discovery-smoke` threshold.

Visually it's a "strategist's notebook": system serif/monospace type (no web fonts or other external assets), a faint graph-paper surface, index-card entries with cost-colored tabs, a hand-circled Opportunity Score, rubber-stamp evidence labels, and margin annotations in the working-notes panel. All decoration is CSS or small inline SVG marked `aria-hidden`, and every imperfection (rotations, uneven corners, alternating rules, which circle shape a score gets) is fixed by CSS `nth-child` or list position, never randomized, so the page renders identically on every load. Reduced-motion preferences are respected.

Champion/item/trait art isn't fetched from CommunityDragon here (that would add a live external dependency to every page load). Instead, champions get a taped `figure.portrait` frame and partners/items/traits get a small clipped `.ref-icon` slot, both showing initials for now; both frames already style a child `<img>` (`object-fit: cover`), so real art can drop into the same frames later without a layout change.

Evidence is currently always labeled `OBSERVED` (a real statistical result); `VARIANT` and `THEORYCRAFTED` styles exist in the CSS for later use but are never applied yet, since nothing in the backend synthesizes either kind of result today.

## Theorycraft notebook ("My Experiments")

A place to keep personal comp ideas ("6 Ravager Kha'Zix", "Cassiopeia/Fiddlesticks reroll") and let them grow. An idea can start as nothing but a title.

**Model** (`src/tftlab/experiments.py`), created in the same database as the match data:

- `experiments`: one row per idea. Anything we filter or sort on is a real column: `slug` (unique, URL name), `title`, `carry_character_id`, `carry_name`, `evidence_status`, `lifecycle`, `summary` (the thesis), `author_notes`, `origin` (`manual` or `demo`), and `created_at`/`updated_at` (ISO-8601 UTC). The id is a generated text key (`exp_…`), which works identically on SQLite and Postgres without auto-increment differences.
- `comp_json`: the structured comp as one validated JSON document. Fields: `core_units`, `optional_units` (each `{name, character_id?, star?, note?}`), `target_traits` (`{name, breakpoint?, note?}`; `"6 Ravager"` shorthand works), `carry_items`, `tank_items`, `secondary_carry` (`{unit, items}`), `target_level`, `reroll_level`, `roll_timing`, `positioning_notes`, `augment_notes`. Every field is optional; unknown keys are rejected so typos fail loudly. JSON rather than child tables because the comp is always read and written whole, never queried field by field, and it has to grow without schema changes. It's stored as plain `TEXT`, so there are no backend-specific JSON operators.
- `experiment_tags`: normalized, so `?tag=` filtering is a portable join.
- `experiment_field_notes`: the dated research log (see "Comp Scout" below). Columns: `noted_at`, `kind`, `source_key`, `source_name`, `source_url`, `evidence_status`, `research_label`, `body`, and a `data_json` blob for structured extras (Riot evidence snapshots, similarity scores, first-seen/last-checked, match ids). The detail API returns them oldest first.

**Evidence vs. lifecycle** are separate columns. Evidence is `THEORYCRAFTED` (the default), `VARIANT` or `OBSERVED`. `OBSERVED` can't be set by hand, because it's reserved for ideas with attached statistical evidence, which isn't supported yet; nothing is ever promoted automatically. Lifecycle is the owner's workflow: `idea`, `testing`, `watching` or `archived`. Both are also enforced with database `CHECK` constraints.

**Migration**: the tables are `CREATE TABLE IF NOT EXISTS` and run on every connect, like the rest of the schema. An existing production database gains them on its next connect, with no separate command, no downtime and no change to match data. Running it repeatedly is a no-op.

**Writing (owner only, via CLI)**: the website is read-only, since there are no accounts yet. Entries are written with these commands, against `--db`, else `DATABASE_URL` (production), else the local SQLite file:

```bash
tftlab experiment-add --title "Kha'Zix + 6 Ravager. Try rerolling him."
tftlab experiment-add --title "6 Ravager Kha'Zix" --carry "Kha'Zix" --core "Kha'Zix" \
  --trait "6 Ravager" --carry-item "Infinity Edge" --reroll-level 7 --tag reroll
tftlab experiment-add --from-json idea.json          # richer entries; flags override the file
tftlab experiment-list [--status ...] [--lifecycle ...] [--carry ...] [--tag ...] [--json]
tftlab experiment-show 6-ravager-khazix [--json]
tftlab experiment-update 6-ravager-khazix --lifecycle testing --add-tag ravager
tftlab experiment-update 6-ravager-khazix --from-json edited.json
```

`experiment-show --json` prints exactly the shape `--from-json` accepts. So the workflow for "tell an assistant a comp, get an entry" is: have it write that JSON, then `experiment-add --from-json`. To edit, export with `show --json`, change it, and apply it with `update --from-json`. In `update`, list flags (`--core`, `--carry-item`, …) replace that list, and `comp` in JSON is merged key by key.

**Reading (web)**: `GET /api/experiments` (filters `status`, `lifecycle`, `carry`, `tag`) and `GET /api/experiments/{slug-or-id}` (adds `field_notes`, 404 if unknown). There are no POST/PUT/PATCH/DELETE routes, and a test asserts that every route in the app is GET/HEAD only. Pages: `/experiments` and `/experiments/{slug}`.

**Examples**: three clearly-labeled example entries (`is_example: true`, tagged `example`, `THEORYCRAFTED`, no stats) are seeded only into the local demo database, and only when it has no experiments. They're never written to a real database.

## Comp Scout (research log)

The loop is **Discover → Save → Scout → Gather evidence → Add field notes → Reassess**. Scouting v1 makes no web requests: our own Riot data is checked automatically, and outside research is recorded by hand (or by an assistant working through the CLI) after someone actually looks.

**Fingerprint** (`tftlab.scout.comp_fingerprint`). A deterministic, normalized description of what an idea actually specifies:
- It covers the primary and secondary carry, core units, optional units (kept separate, never duplicating core), trait targets with breakpoints, carry items, target and reroll level, and roll timing.
- Names are matched to current-set ids through the shipped roster (`src/tftlab/data/set_roster.json`, from CommunityDragon). "Ravager" becomes `DA_18_Slayer` and "Kha'Zix" becomes `DA_18_KhaZix`.
- Matching ignores case and punctuation. Item names and ids normalize to the same key (`Infinity Edge` = `TFT_Item_InfinityEdge`), and every list is de-duplicated and sorted.
- The output includes a `signature` such as `carry=khazix;traits=ravager@6;reroll_level=7`.
- Nothing is inferred, and a full board is never required. "6 Ravager Kha'Zix" with only a carry and a trait target still fingerprints; a title-only idea fingerprints as empty rather than guessed.

**Our Riot data** (`tftlab.scout.riot_evidence`), for one balance window (the latest by default, or `--balance-window`):
- **Carry numbers:** commitment games, appearance, commitment and conversion rates, average placement, top 4, win, 3★ hit, our Opportunity Score, and the strongest partners, item packages and trait breakpoints. These come straight from the existing discovery analytics (`discovery_candidate_for`), so they match the Discoveries page exactly; nothing is re-derived.
- **The idea's own pieces:** how often each other core unit, all core units together, and each trait target appeared alongside the committed carry. "6 Ravager" means six Ravager units on the board, not trait tier 6.
- **Small samples:** fewer than 30 committed games is labeled `LOW SAMPLE`.

**Field notes** (`tftlab.experiments.add_field_note`):

| Kind | Meaning |
|---|---|
| `riot_evidence` | A snapshot of our data. Written only by `experiment-scout --save`, so "our data" can't be hand-typed. |
| `scout_report` | What a checked source showed (or didn't). |
| `mechanic_note` | Odds, shop, loot and encounter mechanics. |
| `community_sighting`, `tournament_sighting` | Sightings from players or competitive play. |
| `my_note` | A personal hypothesis or observation. |
| `status_change` | Logged automatically whenever an experiment's evidence status changes. |

- Every note is dated (`--noted-at`, default now; no future dates). Notes list oldest first.
- Source URLs must be http(s) links to a real host, with no spaces and no embedded credentials.
- A note may carry an evidence stamp (THEORYCRAFTED or VARIANT; never OBSERVED by hand) and a research label.
- A note never changes the experiment's own evidence status.

**Source vocabulary** (`tftlab.sources`, or `tftlab scout-sources`):

| Source | Role |
|---|---|
| Our data / Riot | Statistical source of truth for Theory Lab metrics. The only input to the Opportunity Score. |
| CommunityDragon | Game metadata (and, later, cached art). |
| TFT Academy | Curated / established-comp scout signal. |
| MetaTFT | External comp, meta and tournament corroboration. |
| tactics.tools | External statistical relationship corroboration. |
| Little Buddy Bot | Mechanics, odds, loot/shop/encounter/system notes. |
| Community / Reddit | Emerging player sightings. |
| Tournament / high-Elo | Competitive sightings. |
| User / My note | Personal hypothesis or observation. |

External sites' statistics are stored as separate evidence (in a note's body or `data`) and are **never** blended into our numbers or the Opportunity Score. A test checks that adding notes leaves every Opportunity Score unchanged. `--source` is matched by name ("tft academy", "MetaTFT", "Reddit"); a URL is never used to guess a source, and unknown names are kept as written (`source_key: other`).

**Research labels** are a prepared vocabulary that a person attaches to a note after checking a source; nothing assigns them automatically.
- `KNOWN`: a sufficiently similar established comp exists.
- `VARIANT`: a recognizable established shell, but this version materially differs.
- `EMERGING`: outside evidence exists, but it isn't an established listed comp.
- `NO_PUBLIC_MATCH_FOUND`: nothing sufficiently similar in the sources actually checked. It must name its source and is dated by the note. We never say "nobody has played this."
- `THEORYCRAFTED`: primarily our own hypothesis.

**Scout checklist**. The experiment page (and `experiment-show`) lists:
- Our Riot data
- TFT Academy
- MetaTFT
- tactics.tools
- Mechanics check
- Community sightings
- High-Elo / tournament sightings

A line is ticked only when a field note from that source, or of that kind, exists. An unticked line means "not recorded as checked", nothing more.

```bash
tftlab experiment-scout 6-ravager-khazix              # fingerprint + our data + what's still unchecked
tftlab experiment-scout 6-ravager-khazix --save       # also append a dated riot_evidence note
tftlab experiment-note 6-ravager-khazix --kind scout_report --source "TFT Academy" \
  --url "https://..." --body "No sufficiently similar listed comp found." --label "no public match found"
tftlab experiment-note 6-ravager-khazix --kind mechanic_note --source "Little Buddy Bot" \
  --url "https://..." --body "Relevant shop mechanic affects practical reroll odds."
tftlab experiment-note 6-ravager-khazix --from-json note.json   # kind/body/source/url/status/label/noted_at/data
```

All of these write only through the CLI. The website remains read-only.

## Next milestones

- Candidate-board generation with beam search (explicitly out of scope for this milestone).
- Selected automated scout/watch runs that write dated field notes, using the fingerprint for similarity (no novelty scoring yet).
- Champion/item/trait art via a cached CommunityDragon-backed endpoint.
- TFT Academy-style comp pages with an evidence panel showing observed vs inferred recommendations (`VARIANT`/`THEORYCRAFTED`), once board synthesis exists.
- Augments (not yet investigated): the diagnosed Set 18 match (`NA1_5648101147`) had no `augments` field for any of its eight participants. Unrelated to the source-empty board; augment normalization and analytics are unchanged until this is looked into.

## Riot policy boundary

This project is designed around aggregate/post-game analysis and static recommendations. It should not become a live tool that reads opponents' boards or dynamically dictates decisions during a match.

## Website

Milestone 1.5 includes a functional web UI for the discovery engine.

```bash
pip install -e .
tftlab web
```

Then open `http://127.0.0.1:8000`.

The website automatically uses `DATABASE_URL` (Postgres) when configured -- reachable is enough, even with zero matches so far -- or `TFT_DB_PATH` (SQLite) when that file contains matches. Only when neither is configured/populated does it create a deterministic synthetic demo dataset and clearly label the UI as **Demo dataset**. See "Production safety" above for exactly what happens if a configured `DATABASE_URL` is unreachable (a loud 503, not a quiet fallback to demo). Use demo data only to test the product flow; it is not live TFT performance data.

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

With no `DATABASE_URL` configured, the app automatically falls back to a generated demo dataset, so it boots and serves data even with zero configuration. `/api/health` and `/api/carries` report `"demo"` (true/false) and `"backend"` (`"sqlite"`/`"postgres"`) so it's always clear which one is live. Once `DATABASE_URL` **is** set, that changes: see "Production safety" above -- an unreachable configured database now fails loudly (HTTP 503 from every endpoint, including `/api/health`) instead of quietly serving demo data. Since `render.yaml`'s `healthCheckPath` points at `/api/health`, this means Render will correctly flag the service as unhealthy if the configured database goes down -- that's the intended behavior, not a bug to work around.

To move off ephemeral SQLite in production: create a Postgres database (a Render Postgres instance's "Internal Connection String" works well) and set `DATABASE_URL` in the Render dashboard — `render.yaml` already declares it (and `RIOT_API_KEY`) as `sync: false`, meaning Render will prompt for a value but never store one in the repo. After setting it, confirm `/api/health` reports `"backend": "postgres"` and `"demo": false` before relying on it.

## Live ingestion via GitHub Actions

`.github/workflows/live-ingest.yml` runs a manually-triggered pull of real Riot data into the production database: `tftlab verify-riot`, then `tftlab ingest-riot` sized by the run's inputs, then `tftlab validate-live-data`, then `tftlab discovery-smoke`.

**Current trusted window only:** the production ingest always runs with `--current-trusted-window`, so every seed's match-history request is bounded to the current patch: Riot's `startTime`/`endTime` (epoch seconds, as Riot documents for the by-puuid match-IDs endpoint) are taken from the latest verified + sourced `UNREAL_PATCH_REGISTRY` window that has started and not ended (18.3 today: `startTime=1790233200`, i.e. 2026-09-24T07:00:00Z, `endTime=1791244800`). If no such window exists -- none registered, none usable, or the latest one has already ended -- the run fails instead of crawling unbounded history. This only narrows what is requested: each match is still classified normally, and `validate-live-data` remains the authority. Locally, `tftlab ingest-riot --start-time 2026-09-24T07:00:00Z` sets an explicit lower bound instead; with neither option you get ordinary recent history. The ingest report shows the bounds, the trusted window, how many seeds had no games in it, and the earliest/latest inserted match.

**Inputs** (Run workflow form): one seed count per cohort -- `challenger_seeds` (default 10), `grandmaster_seeds`, `master_seeds`, `diamond_seeds`, `platinum_seeds` (default 0 each); each is 0-100 and the total across cohorts must be 1-100 -- plus `matches_per_player` (recent matches per seed, 1-10, default 5 -- the CLI's deeper 100-match maximum is deliberately not exposed here, since more players beats deeper histories of the same players). For example 20 / 20 / 20 / 20 / 20 is 100 seeds. The preflight prints every cohort's count and the total. The defaults reproduce the original conservative 10 x 5 Challenger run. It has **no** `push`, `pull_request`, or `schedule` trigger -- it only ever runs when someone explicitly starts it, and a failure in any step (bad key, unreachable CommunityDragon, unreachable database, a severe integrity issue) stops the run there rather than continuing partway.

**Two different `DATABASE_URL`s, on purpose:**

- The **Render web service** connects using Postgres's **Internal Connection String** -- it and the database live on Render's private network, so the internal URL is faster and never leaves Render.
- **GitHub Actions** runs on GitHub's infrastructure, which cannot reach Render's private network at all, so it needs the same database's **External Connection String** instead.

Both URLs point at the same database; only the host/network path differs. Getting this backwards (e.g. putting the internal URL in the GitHub secret) just means the workflow can't connect -- it does not affect Render's own `DATABASE_URL`.

**Required GitHub repository secrets** (Settings → Secrets and variables → Actions → New repository secret, on the repo, not in any file):

- `RIOT_API_KEY` -- the same Riot key used locally/on Render.
- `DATABASE_URL` -- the production Postgres database's **External** Connection String (from the Render Postgres dashboard, not the Internal one used by the web service).

Neither secret is ever printed in the workflow's logs.

### Patch-wide collection plan (18.3)

**Goal: broad sampling across the whole 18.3 window** (2026-09-24T07:00:00Z to 2026-10-06T00:00:00Z) -- chronological coverage of the patch through many different ladder players. It is **not** exhaustive capture of every NA ranked game, and not longitudinal capture of every game each selected player plays.

**Recommendation: two runs per day, about 12 hours apart (e.g. ~08:00 and ~20:00 UTC), each with the five-cohort allocation `challenger=15 grandmaster=15 master=20 diamond=25 platinum=25`, `matches_per_player=10`, until the window closes.** Manual `workflow_dispatch` only; nothing is scheduled.

Why, from the first successful five-cohort run (100 seeds: 589 history references, 560 unique IDs, 458 new ranked matches, 13 zero-window seeds, 4.9% in-run overlap):

- **History depth.** The 87 active seeds averaged ~6.8 window games each after ~2 days of the patch, i.e. roughly 3-5 ranked games per day for a typical sampled player. A 10-game request therefore reaches back about 2-3 days for a typical seed and about 1 day for a heavy grinder. Runs 12 hours apart leave no chronological gap even for players well above average; once a day would still cover most seeds but would drop part of the heaviest grinders' days. Raising `matches_per_player` would mostly re-read the same players' older games -- breadth beats depth because high-Elo lobbies overlap.
- **Breadth and rotation.** Each run takes 100 seeds, never-sampled first. Challenger (221 players at 15/run) runs out of never-sampled players after ~15 runs (~7.5 days at this cadence) and then re-samples the least recently sampled -- a week later, almost entirely new games. Grandmaster (331), Master (1,471) and the Diamond/Platinum candidate pools last longer.
- **Request volume.** About 690 Riot requests per run (≈27 ladder + 100 histories + ≈560 match bodies), ~13-15 minutes at development-key rate limits (100 requests / 2 minutes). Two runs a day is ~1,400 requests -- far inside the limits; the real constraint is the manual key refresh below.
- **Duplicates.** In-run overlap was 4.9%; cross-run duplicates will rise as more lobbies are already stored (the 14.3% seen during the resume run was inflated by the resume itself). Stored matches are skipped without being re-fetched, so duplicates cost only history references.
- **Remaining days.** From 2026-09-26 to 2026-10-06 there are ~9.5 days, so ~19 runs. If yields stay near the first run and decline gradually, expect roughly **6,000-8,000 more 18.3 matches** -- an estimate, not a promise.

**When to back off:** the ingest report prints `Collection value (is another run worth it?)` -- never-sampled, previously-sampled and zero-history seed percentages -- next to the already-stored duplicate rate, inserted-per-seed and new-match yield. If never-sampled seeds fall below ~50% or new-match yield stays below ~40% for two consecutive runs, drop to one run per day.

**Limitations:** seeds are ladder players, so the population is what they played (see "Data population and seed cohorts"); a heavy grinder's games older than their last 10 are not requested; Diamond/Platinum candidates come from the first pages of each division (reported as capped when so).

**Patch end is strict.** Production always runs `--current-trusted-window`. At 2026-10-06T00:00:00Z (end exclusive) there is no current trusted window unless 18.4's real verified window has been registered, so the ingest step fails instead of crawling another patch or going unbounded. 18.4 is added only when its real window is known.

### Riot development key operations

Riot's Developer Portal says development API keys deactivate every 24 hours. Before a collection day (and whenever `verify-riot` fails with 401):

1. Open the Riot Developer Portal (developer.riotgames.com) and sign in.
2. Reset / regenerate the development API key and copy it.
3. In GitHub: **Settings → Secrets and variables → Actions → `RIOT_API_KEY` → Update**, paste, save.
4. Start the run; its `Verify Riot API` step confirms the new key before anything is ingested.
5. Never paste the key into logs, issues, PRs, the README or chat.

With an expired key, `Verify Riot API` exits 1 with: *"401 Unauthorized -- RIOT_API_KEY is invalid or expired. Reset the development key in the Riot Developer Portal and replace the GitHub Actions RIOT_API_KEY secret."* and the ingest step never runs. If a key expires mid-run, the ingest prints the same guidance, the run stays incomplete, and seed rotation ignores it. The key is never printed. Logging in to Riot or regenerating keys is not automated.

**Operating rule:** after merging anything that deploys (especially schema changes), wait for the Render deployment to finish before starting a production ingest. This is defense in depth only: web requests no longer run schema maintenance, and a transient deadlock on one match is retried, so correctness does not depend on that timing.

**Running it:** GitHub → **Actions** tab → **Live ingest** in the left-hand workflow list → **Run workflow** button → **explicitly select `main`** as the branch (this repository's GitHub default branch is not `main`, so the dropdown will not default to it -- picking anything else fails immediately, see below) → **Run workflow**.

**Production safety checks:** before touching Riot or the database, a first `Validate production configuration` step fails the run (with a static error message, never a secret value) if the selected branch isn't `main`, if `RIOT_API_KEY`/`DATABASE_URL` is missing, if `DATABASE_URL` doesn't start with `postgres://`/`postgresql://` -- this exists specifically so the ingest CLI's normal local-SQLite fallback can never be silently used in production -- or if an input is out of bounds (each `<cohort>_seeds` a plain whole number 0-100, their total 1-100, `matches_per_player` 1-10). Inputs reach the shell only as environment variables. The workflow also declares `permissions: contents: read` (it never needs to write to the repo), a 60-minute job timeout, and a fixed `concurrency` group so two manually-triggered runs can never ingest into production at the same time (a second run queues rather than cancelling the first).

## Patch diagnostics via GitHub Actions

`.github/workflows/patch-diagnostics.yml` runs `tftlab patch-diagnostics` (see above) against production -- read-only, no Riot or CommunityDragon calls, no ingestion. Use it to read off the real masked-Unreal `game_datetime` range needed to fill in `UNREAL_PATCH_REGISTRY` without running another live ingest.

Same production-safety shape as `live-ingest.yml`: `workflow_dispatch`-only (no `push`/`pull_request`/`schedule`), a `Validate production configuration` step that requires `main` explicitly selected and a well-formed `DATABASE_URL` before anything else runs, `permissions: contents: read`, a bounded job timeout, and its own fixed `concurrency` group (`patch-diagnostics-production`). It needs only the `DATABASE_URL` repository secret -- never `RIOT_API_KEY`, since it makes no Riot calls at all.

**Running it:** GitHub → **Actions** tab → **Patch diagnostics** in the left-hand workflow list → **Run workflow** button → **explicitly select `main`** as the branch → **Run workflow**.
