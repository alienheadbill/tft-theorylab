# TFT Theory Lab

A patch-aware analytics prototype for discovering **low-usage, data-backed TFT carry lines**, with an emphasis on 1/2/3-cost rerolls.

Project direction, current checkpoint and priorities: see [ROADMAP.md](ROADMAP.md).

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

### Collect data on your own computer (zero cost)

The recommended way to collect real data: one SQLite file on a personal computer, with no Postgres, Docker or cloud database. Python 3.11+ is the only thing to install. The step-by-step guide for non-programmers is in **[docs/local-collector.md](docs/local-collector.md)**.

```bash
tftlab local-init        # once: creates data/local/ and .env (never overwrites either)
tftlab local-set-key     # paste a fresh Riot development key (hidden prompt, saved in .env)
tftlab local-status      # offline: how much data is stored, current patch window, backups
tftlab local-collect     # THE daily command (or scripts/local-collect.cmd / .ps1 / .sh)
tftlab local-snapshot    # optional: sanitized public website snapshot (never uploaded)
```

`local-collect` runs these steps in order:

1. **Preflight**, before anything changes:
   - local SQLite target only;
   - disk space;
   - the current trusted window from `UNREAL_PATCH_REGISTRY`;
   - the Riot key, with `verify-riot`'s check;
   - CommunityDragon costs.
2. **Backup:** a consistent SQLite online backup into `data/local/backups/`; the newest 7 are kept.
3. **Collection:** the existing `ingest_ladder`, bounded, with 15/15/20/25/25 Challenger/Grandmaster/Master/Diamond/Platinum seeds × 10 matches. It uses the current trusted window and the production `10:10` rate ceiling.
4. **Validation:** `validate_live_data` for that window's balance window(s).
5. **Discovery:** `prepare_window` for those windows only.
6. **Report:** plain language, saved as JSON in `data/local/reports/`. It has no key, PUUIDs or match ids.

Safety rules:

- The raw database is `data/local/theorylabs.sqlite3`, or `TFT_LOCAL_DB_PATH` / `--db`.
- `DATABASE_URL` is never read, and database URLs are refused, so these commands cannot write to a cloud database.
- An expired key, a missing key or no current trusted window stops the run before the database is touched.
- A severe validation failure keeps the data, but blocks the "success" outcome and snapshot readiness.

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

### Carry eligibility (Riot item intent)

**Appearance** = the champion was on the board. **Carry commitment** = it finished with **>= 2 completed items** (2-star misses included) **and** at least one of them carries Riot-backed carry evidence. One Discovery system, no tank/hybrid/support statistics, no champion role table, no champion allowlist. Riot semantics first, TheoryLabs inference last: an item's purpose comes from Riot's own role item recommendations, never from one raw stat.

**Source.** Riot's TFT map data (`game/data/maps/shipping/map22/map22.bin`, read through CommunityDragon at `https://raw.communitydragon.org/latest/game/data/maps/shipping/map22/map22.bin.json`) holds 27 `TFTCharacterRoleData` objects, each with an internal `name` (`APTank`, `ADCarry`, `ADReaper`, `HFighter`, ...), a UI name key and an 8-item recommended `items` list. The 20 roles in Riot's current vocabulary carry a `TFT_CharacterRole_RolesRevamped_<X>_Name` key; Riot's string table (`game/en_us/data/menu/en_us/tft.stringtable.json`) resolves it to the name the client shows -- `<Attack|Magic|Hybrid> <Tank|Assassin|Caster|Fighter|Marksman|Specialist>` -- and that family word decides **Tank** (`ADTank`, `APTank`, `HTank`) vs **non-Tank** (every other current role). A new family word fails the snapshot build instead of being guessed. The 7 legacy objects without a current UI name (`ADCarryCrit`, `APCarryNoCast`, `TutorialADCarry`, ...) are recorded but give no evidence.

Each completed item is then:

- **DAMAGE** -- recommended by non-Tank roles only (Guinsoo's, Sterak's, Adaptive Helm, Infinity Edge, ...).
- **TANK** -- recommended by Tank roles only (Warmog's, Gargoyle, Spirit Visage, Dragon's Claw, Bramble, Sunfire, Protector's Vow, Evenshroud).
- **MIXED** -- recommended by both (Titan's Resolve: Hybrid Tank and the Fighters; Ionic Spark: Magic Tank and Magic Fighter/Assassin).
- **KNOWN_UNLISTED** -- an ordinary completed item **inside Riot's recommendation domain** that no role recommends (Steadfast Heart, Crownguard). This is **not** "tank"; it only means Riot considered this kind of item and gives no role evidence for it.
- **UNKNOWN** -- no usable evidence: ids missing from the snapshot (a new patch), unresolved aliases, Set 18 trait emblems (`DA_18_Emblem*`, e.g. Ravager Emblem = `DA_18_EmblemSlayer`, which have no counterpart in the recommendation namespace), and known special items **outside the recommendation domain** -- Tactician's Cape/Crown/Shield, artifacts such as Talisman of Ascension, Thief's Gloves -- whose absence from role lists says nothing.

**Recommendation domain.** A missing recommendation is only negative evidence for the kind of item Riot's role lists actually choose from. The domain comes from Riot's own item tags (map22 `TftItemData.ItemTags`): an item is inside when its tags include every tag that all role-recommended items share (Set 18: `{7ea41d13}`, carried by all 36 ordinary `DA_*` craftables) and no tag that none of them carries (allowed: `{7ea41d13}`, `Resistance` = `{15b72700}`, `AbilityPower`, `AttackDamage`, `AttackSpeed`, `CritChance`, `Heal`, `Health`, `Mana`). Tactician's items carry `TacticiansItem` (`{d304f83b}`) + `{ec243f6b}` and no `{7ea41d13}`; Talisman of Ascension carries the artifact tags `{44ace175}` + `{ec243f6b}`; emblems carry `TraitItem` (`{ebcd1bac}`); Thief's Gloves carries `{7ea41d13}` plus `{2905e581}` and `{218b53a5}`, which no recommended item has. (Tag names in words are FNV-1a matches of the hashes; the rule itself uses the hashes as Riot ships them, no English names.) Counts: 235 completed items -- DAMAGE 46, TANK 16, MIXED 4, KNOWN_UNLISTED 14, UNKNOWN 155; of the 61 Match-V1 `DA_*` items, 23 / 8 / 2 / 2 / 26.

**Rule.** A board counts when any completed item is DAMAGE, MIXED or UNKNOWN (unknown stays conservative: hiding an unusual build is worse than a false positive). TANK and KNOWN_UNLISTED items alone never prove carry intent. So Spirit Visage + Steadfast Heart (the production Leona board that ranked #1 because PR #21 read Steadfast Heart's raw `CritChance` as offensive), Warmog's + Gargoyle and Crownguard + Warmog's are not carry observations, while Titan's + Sterak's, Ravager Emblem + Guinsoo's, an unknown emblem + Warmog's, Talisman of Ascension + Warmog's and a Tactician's item + Warmog's are -- per board, never per champion, so an off-meta tank-to-carry conversion (Elise with an emblem + Guinsoo's) stays discoverable. Star level plays no part.

**Adaptive Helm: a contextual carry-evidence rule (TheoryLabs interpretation, not a Riot semantic).** Riot's recommendations make Adaptive Helm DAMAGE (non-Tank caster roles recommend it) and the snapshot keeps exactly that. For carry eligibility only, Adaptive Helm on its own is weak evidence -- frontliners often hold it next to defensive items -- so a unit holding Adaptive Helm counts only when **another, non-Adaptive completed item is DAMAGE or MIXED**. UNKNOWN, TANK and KNOWN_UNLISTED items do not corroborate it, and neither does a second Adaptive Helm: Adaptive Helm + Warmog's (+ Gargoyle), Adaptive Helm + Spirit Visage + Steadfast Heart and Adaptive Helm + an emblem + Warmog's are not carry observations; Adaptive Helm + Guinsoo's, + Titan's Resolve or + Deathcap + a tank item are. Boards without Adaptive Helm follow the rule above unchanged (UNKNOWN still counts). Adaptive Helm is recognized as every snapshot id that resolves to Riot's `TFT_Item_AdaptiveHelm` (`DA_AdaptiveHelm`, `TFT_Item_AdaptiveHelm`); the same rule runs in `carry_commitment_sql`. Eligibility is computed at query time from stored `items_json`, so no stored data changes.

**Thief's Gloves: equipped vs generated items.** Thief's Gloves takes all three item slots and equips two random items each round, and Match-V1 lists the gloves plus that round's rolls (`["DA_ThiefsGloves", "DA_Bloodthirster", "DA_DragonsClaw"]`). `tftlab.itemization.unit_itemization` is the one place that splits a unit's list into `equipped` items and `generated` ones. On a Thief's Gloves holder the gloves are the only equipped item and every other listed item is a roll. Thief's Gloves ids come from the item snapshot: every id resolving to Riot's `TFT_Item_ThiefsGloves` (`DA_ThiefsGloves`, the Academy copies) plus each one's Radiant form (`DA_ThiefsGlovesRadiant`, Riot's `<id>Radiant` naming). A Thief's Gloves holder is **never a carry observation**, whatever it rolled: the rolls were not chosen, and the gloves alone are no carry evidence. `carry_commitment_sql` excludes any unit whose `items_json` holds a quoted Thief's Gloves id. Fixed-item, pair and package evidence is built only from equipped items. Raw `items_json` and the stored `completed_item_count` are unchanged. **Lucky Gloves / Lucky Gloves+** (augments that make the rolls champion-appropriate) are **not** an exception yet. CommunityDragon lists them as the augments `DA_LuckyGloves` / `DA_LuckyGlovesPlus`, but which strings Match-V1 stores in `augments` has not been checked against real match data, so they get no special case.

**Match-V1 ids.** Riot's role lists name `TFT_Item_*` ids while Set 18 boards store `DA_*` ids. `tftlab.cdragon.item_intent_snapshot` bridges each `DA_*` item to the `TFT_Item_*` items with the same display name (case and punctuation ignored, so "Warmogs Armor" is "Warmog's Armor"), dropping candidates whose component names contradict; every Corrupted/Academy copy of that name shares its evidence. `DA_SteadfastHeart` resolves to `TFT_Item_NightHarvester`, `DA_SpiritVisage` to `TFT_Item_Redemption`, `DA_RedBuff` to `TFT_Item_RapidFireCannon`.

**Snapshot and provenance.** `src/tftlab/data/item_intent.json` is committed and read offline (analytics and the web app never call CommunityDragon). It keeps every role (object key, UI name key, UI name, family, recommended items), the recommendation domain (required/allowed Riot item tags) and, for each of the 235 completed items, the Riot items it resolved to, exactly which Tank and non-Tank roles recommend them, its own Riot item tags and domain membership, and the derived intent -- so "why is Warmog's TANK?" is answered by the file (recommended by `ADTank`, `APTank`, `HTank`, by no non-Tank role). The opt-in live test `test_committed_item_intent_matches_live_set` fails and prints a fresh snapshot whenever Riot changes a role or a recommendation; `test_riot_role_vocabulary_is_classified_and_resolves` checks every role object and that every recommended item is a known id.

**Champion roles are not used.** Riot's per-champion link (`TFTCharacterRecord.CharacterRole` in `game/characters/<id>.cdtb.bin.json`) exists for only 2 of 74 Set 18 shop champions (Alune `APCaster`, Kobuko `APTank`) as of Riot content 16.19. The snapshot records that baseline and `test_champion_role_coverage_matches_baseline` fails, printing the live coverage, as soon as it changes -- that is when champion-role-aware logic can be reconsidered.

**The item-stat snapshot (`item_stats.json`) uses the exact ids Match-V1 stores** (it defines the known item ids and components; eligibility no longer reads its stats). Set 18 boards store the `DA_*` namespace (`DA_GargoyleStoneplate`, `DA_GuinsoosRageblade`, `DA_TitansResolve`, ...). CommunityDragon has an entry for each, but with **no named `effects`** and only some readable tags (e.g. `DA_GuinsoosRageblade` = `AttackSpeed`, `DA_WarmogsArmor` = `Health`; `DA_GargoyleStoneplate` and `DA_DragonsClaw` have only hashed tags). So the snapshot keeps each exact `DA_*` id and adds the stats of its `TFT_Item_*` counterpart **only when the alias is verified unambiguous**: identical display name, component names not contradicting, and every remaining candidate sharing one stat signature (a Corrupted copy with the same stats is fine; two different "Blue Buff" entries are not). The display name, not the id, is the bridge -- a prefix rewrite would be wrong: `DA_RedBuff` ("Red Buff") is `TFT_Item_RapidFireCannon`, while `TFT_Item_RedBuff` is "Sunfire Cape". Items with no verified alias keep only their own metadata (often none, i.e. unknown). The snapshot holds 256 items: 185 `TFT_Item_*`/`TFT18_Item_*` plus 71 `DA_*` (craftables, components, `DA_18_Emblem*`, `DA_Item_*`), 41 of them aliased; `DA_*` augments are deliberately left out. Every entry also keeps its own CommunityDragon recipe, `composition` (component apiNames exactly as served; empty for components and uncraftable items; never borrowed from an alias): 106 items have one, including all 39 current-set `DA_*` crafts. `tftlab.items.item_recipe` accepts only an exact two-component recipe of recognized components (a legacy entry listing itself, e.g. `TFT_Item_CursedBlade`, is rejected). `test_committed_item_recipes_match_live_set` prints the live recipe map and fails on any difference.

**Components are never completed items.** `tftlab.items.is_component` recognizes the legacy `TFT_Item_*` components **plus every item the committed snapshot tags `component`** -- for Set 18 the ten `DA_Component_*` ids (`DA_Component_BFSword`, `..._ChainVest`, `..._FryingPan`, `..._GiantsBelt`, `..._NeedlesslyLargeRod`, `..._NegatronCloak`, `..._RecurveBow`, `..._SparringGloves`, `..._Spatula`, `..._TearOfTheGoddess`). Metadata decides, never an id prefix; unknown non-component items still count as completed/special items. So Warmog's + `DA_Component_ChainVest` is one completed item (not a carry), and Warmog's + Gargoyle + a component is a two-item board with no carry evidence. (No stored production payload has been inspected for `DA_Component_*`; CommunityDragon lists them as the Set 18 components, so they are recognized.) Rows ingested before this was recognized are corrected by a one-time migration: every **initializing** connection (`Database(...)` -- CLI, ingest, validate, admin) checks `schema_migrations` for `completed_item_count:<fingerprint of the component set>` and, if absent, recomputes each unit's count from its unchanged `items_json`, updates only rows that differ, and records the marker in the same transaction. It is idempotent, re-runs by itself if the recognized component set ever changes, and never runs from the read-only web path (`Database.open_existing`). The ingest report prints how many unit rows it corrected; in production that happens at the start of the next authorized live-ingest run.

Item intent reaches analytics without the network (above); `src/tftlab/data/item_stats.json` (CommunityDragon's readable item stats, `tftlab.cdragon.item_stats_snapshot`) still defines the current set's item ids and components. The rule runs inside SQL (`carry_commitment_sql`: no-evidence items counted exactly in `items_json`, duplicates included), identically in SQLite and Postgres, in every query that selects carry boards -- commitment stats, partners, item packages, traits, the carry detail endpoints and Comp Scout. Appearance counts, Opportunity Score weights, 3-star logic and the association formulas are unchanged; only commitment games, commitment/carry-conversion rates, carry outcomes and the evidence built on them move.


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
- **Ingest writes**: each match is stored in one transaction, with its participants, units and traits written as one batch per table (`Database.executemany`; psycopg pipelines these). Over a remote connection (GitHub Actions to Neon Postgres) this is what keeps a larger ingest within the workflow timeout -- one round trip per row made a 34-match run take about ten minutes.
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

**Currently populated with three conservative windows**, sourced from Riot's official TFT patch schedule (<https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/> -- 18.2 scheduled 2026-09-10, 18.3 scheduled 2026-09-23, 18.4 scheduled 2026-10-07 and 18.5 scheduled 2026-10-21, all Pacific Time; Riot published the 18.4 patch notes on 2026-10-06):

| Patch | Window (UTC) | Notes |
| --- | --- | --- |
| `18.2` | `2026-09-11T00:00:00Z` .. `2026-09-22T00:00:00Z` (exclusive) | Starts a full day after the scheduled release; ends before the earliest reported NA 18.3 sighting. |
| `18.3` | `2026-09-24T07:00:00Z` .. `2026-10-06T00:00:00Z` (exclusive) | Starts after the full scheduled Pacific-Time release day has elapsed everywhere in that timezone; ends before the next scheduled patch (18.4). |
| `18.4` | `2026-10-08T07:00:00Z` .. `2026-10-20T00:00:00Z` (exclusive) | Same convention: starts after the full scheduled 2026-10-07 Pacific-Time patch day (TheoryLabs' conservative classification boundary, not a claim that Riot deployed 18.4 at that instant); ends a day before the scheduled 18.5 transition on 2026-10-21. |

The gaps between windows deliberately stay `UNRESOLVED_UNREAL_PATCH`:
- `2026-09-22T00:00:00Z` through `2026-09-24T07:00:00Z` covers the reported early-NA-18.3 rollout ambiguity.
- `2026-10-06T00:00:00Z` through `2026-10-08T07:00:00Z` covers the 18.3 → 18.4 transition. It is neither patch, and 18.3 was not widened to cover it.

See `src/tftlab/unreal_patch.py`'s module comment for the full reasoning. When a match's `game_datetime` doesn't fall in any usable window:

- It resolves to the explicit `UNRESOLVED_UNREAL_PATCH` sentinel (`"unreal-unresolved"`), never a fake shared patch bucket and never the raw masked string.
- Its `balance_window` is left `None`, so it's automatically excluded from every balance-window-scoped analytics query (never silently blended into a real window's stats) and counted in `IntegrityReport.unresolved_unreal_matches` / flagged by `tftlab validate-live-data` as a severe issue (a missing balance window always is) until resolved.
- Run `tftlab patch-diagnostics` (read-only, no Riot/CommunityDragon calls -- see below) or `tftlab validate-live-data` and read the diagnostics (raw `game_version` distribution, resolved patch distribution, balance-window distribution, earliest/latest `game_datetime`, and specifically the masked-Unreal matches' own earliest/latest `game_datetime` -- store-wide, not scoped to one window) to see the actual timestamp range needing a wider or additional window, without running another ingest.

To extend this as new patches ship: read off the actual masked-Unreal timestamp range via `tftlab patch-diagnostics`, cross-reference Riot's published patch schedule, and add another conservative, verified+sourced `UnrealPatchWindow` to `UNREAL_PATCH_REGISTRY` in `src/tftlab/unreal_patch.py`, following the same margin-in-from-both-ends pattern. Every affected row -- already-ingested matches included -- picks up the correct patch automatically on the next `Database` connect; no separate backfill command or data wipe is needed.

## Static metadata (CommunityDragon)

`tftlab.cdragon.CommunityDragonClient` fetches and disk-caches TFT static metadata (champion shop costs, champion/item/trait names and art) from CommunityDragon. `tftlab ingest-riot` uses it by default to resolve authoritative champion costs instead of the `rarity + 1` heuristic (pass `--no-use-static-costs` to disable). If CommunityDragon is unreachable, ingestion **aborts** rather than silently falling back to `rarity + 1` -- see "Production safety" below; pass `--allow-degraded-costs` to proceed anyway with a prominently marked `DEGRADED INGEST`.

`src/tftlab/data/set_roster.json` (`tftlab.roster`) is the committed current-set roster: each champion's name, shop cost and **traits as canonical trait ids**. CommunityDragon lists a champion's traits by display name; `tftlab.cdragon.champion_trait_ids` resolves each name to exactly one set trait or fails. A trait is a champion's **intrinsic** trait (`Roster.intrinsic_traits`, e.g. Kog'Maw → Caustic, Alune → Attuned) only when exactly one shop champion has it AND no verified item can add it. The second condition is the snapshot's `trait_items` (`tftlab.cdragon.trait_item_ids`): per trait, every item that can grant it -- items whose CommunityDragon `associatedTraits` name the trait, and emblems, which CommunityDragon links by name only ("Ravager Emblem" → Ravager). A one-champion trait with an emblem or trait item is not intrinsic, since another champion can carry it; the snapshot's `unresolved_emblems` lists every current-set emblem (any `DA_*`, `TFT<n>_*` or `TFT_Item_*` item whose id or name says "emblem") that links to no trait. Classification fails closed: without `trait_items`, or with any unresolved emblem, nothing is intrinsic. **Current Set 18 state:** the live feed lists two "Phantom Emblem" items (`DA_PhantomEmblem18`, `DA_PhantomEmblemUpgrade18`; "Gain a temporary emblem of your most active trait.") that name no Set 18 trait, so `unresolved_emblems` records them and no trait is classified intrinsic: nothing here claims Caustic, Attuned or the other one-champion traits cannot be extended. Separately, every one-champion trait is a **singleton-provider** trait (`Roster.singleton_provider_traits`, membership only), and its guaranteed one-unit presence on its own champion's boards is never trait evidence (see Traits under Analytics). Both conditions come from this static data, never from a name list. `test_committed_roster_fixture_matches_live_set` checks names, costs, trait membership and `trait_items` against the live feed.

A separate GitHub Actions workflow (`.github/workflows/cdragon-live.yml`) smoke-tests the real feed on a manual trigger or nightly schedule. It's intentionally isolated from any PR/unit-test CI (it has no `push`/`pull_request` trigger), so a CommunityDragon outage never blocks a merge. A manual run can take an optional `select` input (a pytest `-k` expression, e.g. `intent or coverage or vocabulary`) to run only some of the live tests.

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
- **Items** (`item_package_stats`) -- the same with-vs-without comparison for individual completed items, item pairs, and exact 3-item packages (packages need `min_package_games`, default 2, before being surfaced at all). Only **equipped** items count; Thief's Gloves rolls never do (see "Thief's Gloves" above).
- **Traits** (`trait_count_associations`; `trait_breakpoint_associations` is the old name, kept as an alias) -- the same for an active trait at the unit count Riot reported (e.g. `"Juggernaut:4"` = four Juggernaut units). The count is Riot Match-V1's own `num_units`, stored unchanged; a trait is active when Riot's `tier_current` is 1 or more. `tier_current` is an ordinal ("which tier"), not a unit count, and is never shown as one. Naming a canonical threshold ("the 4-unit tier") would need verified static trait metadata (CommunityDragon's per-trait unit thresholds), which this repository does not store, so no threshold is inferred. `trait_profile` groups the same rows per trait for the champion page (see below). **Guaranteed baseline.** A carry's own **singleton-provider** traits (`Roster.singleton_provider_traits`: traits no other shop champion naturally has; see "Static metadata") are on the board at one unit just because the carry was picked. On every carry board, such a trait at `num_units` 1 is left out of the carry's trait evidence everywhere: no generic active-trait row and no `"<trait>:1"` count. The same trait at 2+ units stays in as count-level evidence (e.g. `"Caustic:2"`), since an emblem, Phantom Emblem or another mechanic may have added a unit; nothing verified rules that out. Another champion's one-champion trait on the board stays in, since it means that champion was added. This rule needs only trait membership and does not depend on the fail-closed intrinsic classification. Stored trait rows are unchanged.

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

### Prepared Discovery analytics

Discovery's expensive part, the window-wide carry population plus partner/item/trait evidence for every carry, depends only on a balance window's matches and on the analytics code, never on a request's filters. `tftlab prepare-discovery` (`tftlab.prepared_discovery`) computes it offline and publishes it to `discovery_prepared_runs` / `discovery_prepared_candidates`, and `/api/discovery` and `/api/discovery/{character_id}` serve it with an indexed read of at most `limit` rows.

- **What a run holds:** every carry in one balance window (any cost, `min_samples=1`) as its complete `DiscoveryCandidate`, built by the same code as live Discovery (`population_candidates`), with evidence lists kept to 20 (the API's `top_n` maximum). A request filters by `costs`/`max_cost` and `min_samples`, orders by Opportunity Score (ties in population order, as live), truncates evidence to `top_n` and applies `limit`. `window_carries` is the run's population size, as before.
- **Freshness:** a run is served only if its `balance_window`, `analytics_version` and `source_fingerprint` all still match. `analytics_version` is a format number plus a digest of the analytics source files (including `itemization.py` and `roster.py`) and the item and roster data snapshots, so a deploy that changes how carries or evidence are computed makes older runs stale automatically. `source_fingerprint` is the window's match count, latest and summed game timestamps and the `schema_migrations` marker, so a new, moved or removed match in the window, or an in-place data migration, makes the run stale.
- **Missing or stale:** the API computes Discovery live, exactly as it did before prepared runs existed, and says so in an additive `prepared` field (`{"status": "current" | "stale" | "missing", ...}`). Stale results are never served as current, and a request never writes or builds anything.
- **Publishing:** the run row and all of its candidate rows are inserted in one transaction, so a reader sees a whole run or none. A failed or interrupted preparation rolls back (leaving a `failed` audit row that stores only the exception type), and the previous published run stays the latest. The fingerprint is read before and after computing, and nothing is published if ingestion changed the window in between (up to 3 attempts). On Postgres an advisory lock serializes preparations of one window; a second one skips once the first has published. Each window keeps its 3 newest published runs.
- **When it runs:** live ingest runs `tftlab prepare-discovery` right after validation and the Discovery smoke test. `.github/workflows/prepare-discovery.yml` (manual, `main` only, same concurrency group as ingest) re-prepares without ingesting, e.g. after deploying analytics changes. Raw match tables are never modified.

## Production safety

`webapp._resolve_source` (used by every API endpoint) selects the data source explicitly with **`TFT_DATA_SOURCE`**, and never silently blurs sources together:

| `TFT_DATA_SOURCE` | Source | Labelled as | If unusable |
|---|---|---|---|
| `database` | `DATABASE_URL` (production Postgres), read-only; must be set | indexed ranked matches (observed) | HTTP 503 |
| `snapshot` | a sanitized public snapshot made by `tftlab export-public-snapshot` at `TFT_SNAPSHOT_PATH` (default `data/snapshot/theorylabs-snapshot.sqlite3`; a `.gz` is unpacked once into `TFT_SNAPSHOT_CACHE_DIR`, default `data/snapshot-cache`) | analytics snapshot (observed) **only when its exporter provenance certifies observed, non-synthetic data** | HTTP 503 (missing, empty, corrupt, or without valid exporter provenance -- e.g. any plain database, demo data included) |
| `demo` | the deterministic **synthetic** demo dataset, no database at all | demo data (synthetic) | -- |
| unset | automatic: `DATABASE_URL` if set, else a populated local SQLite file (`TFT_DB_PATH`), else demo | as above (`local database` for the local file) | as above |

- `snapshot` or `demo` combined with a set `DATABASE_URL`, `database` without one, or an unknown value is refused with a 503 (`"the data source is misconfigured"`). A configured production database is never silently ignored or replaced by fake data.
- 503 bodies carry only a fixed public message, never a path, DSN or credential.
- Snapshots and the production database are opened read-only. A request never creates or writes them.

In automatic mode the original three states still apply:

1. **`DATABASE_URL` unset** -- local/dev only. Falls back to a populated local SQLite file, then the deterministic demo dataset.
2. **`DATABASE_URL` set and reachable** -- always treated as live (`"demo": false`), even with zero matches so far (a fresh production database before first ingest). It is never swapped for demo data just because it's empty.
3. **`DATABASE_URL` set but unreachable** -- every endpoint returns **HTTP 503** with `{"ok": false, "demo": false, "status": "error", "error": "..."}` instead of falling through to demo data. The error message never includes the DSN or credentials. This is intentional: if you've pointed the app at a real database, a connection failure is a production incident to see immediately (including via Render's own health check, since `render.yaml` points `healthCheckPath` at `/api/health`), not something to paper over.

`/api/health` (and every other endpoint) also reports `backend` (`"sqlite"`/`"postgres"`), the active `balance_window`, and `matches`/`participants` counts, so "is this real data, and how much of it" is always answerable from one request.

`GET /api/source` describes the active source in full:
- `mode` and `label`;
- `observed` / `synthetic` (`demo` mirrors `synthetic`);
- match and board counts and `latest_game_datetime`;
- freshness: `age_days`, and `stale` (latest game older than `TFT_STALE_AFTER_DAYS`, default 14). Synthetic data gets no freshness verdict, because its dates are generated.
- for a snapshot, its public provenance fields (format, export time, windows, counts, code version, exclusions).

Every page shows this in a banner at the top (`static/site.js`):
- **Demo data** (amber): every number and date is synthetic. Evidence stamps on the page read "Observed · demo".
- **Historical match data** or **Analytics snapshot**: counts and the latest game date.
- **Older data**: the data is stale.
- **No matches indexed yet**: the source is connected but empty.
- **Data unavailable**: the source failed. The explanatory pages still work.

## Operational CLI commands

- `tftlab local-init` / `local-set-key` / `local-status [--check-riot]` / `local-collect` / `local-snapshot [--balance-window W]` -- the zero-cost local collector on one SQLite file (see "Collect data on your own computer" above and [docs/local-collector.md](docs/local-collector.md)). They never use `DATABASE_URL` and refuse database URLs.
- `tftlab verify-riot` -- one minimal authenticated Riot request to confirm `RIOT_API_KEY` works, without ingesting anything.
- `tftlab ingest-riot [--challenger-seeds N] [--grandmaster-seeds N] [--master-seeds N] [--diamond-seeds N] [--platinum-seeds N] [--max-ladder-pages N] | [--players N] [--sampling challenger|high_elo]; [--matches-per-player N] [--current-trusted-window | --start-time ISO8601] [--allow-degraded-costs]` -- see "Run on live Riot data" above.
- `tftlab validate-live-data [--db ...] [--balance-window ...] [--no-check-metadata]` -- data-integrity checks against ingested data for one balance window: total matches/participants, how many of those matches are ranked-TFT vs. a non-target queue (visibility only -- never deletes non-target rows itself), % of units with a present shop cost, CommunityDragon champion coverage and unknown champion/item/trait IDs (cross-checked against a live CommunityDragon fetch unless `--no-check-metadata`; reported as "skipped" rather than a possibly-wrong empty list/0% when metadata isn't available), matches missing a `balance_window`, malformed placements, duplicate match IDs, participants with no units at all (split in two, see below), and matches with an unresolved Unreal-era patch (see "Unreal-era client patch resolution" above). **A missing `balance_window` is split into two counts**: `matches_missing_balance_window` (every such match) and `unexpected_missing_balance_window` (everything except matches intentionally left unresolved because their masked-Unreal `game_version` doesn't fall in any usable `UNREAL_PATCH_REGISTRY` window -- a `NULL` patch, e.g. a totally missing `game_version`, always counts as unexpected). **Participants without units are split in two as well**: `source_empty_participants` -- the participant's own raw Riot entry (same position in `info.participants`, same placement, same participant count) has a `units` field that is missing or `[]`, so storage is faithful to the source (first seen in production on 18.3: a 2nd-place, level-9 board Riot sent with no units) -- and `unexpected_participants_without_units` -- the raw entry lists units that aren't stored, or the raw/stored mapping can't be trusted. Source-empty boards are printed as a warning and kept exactly as stored; only the unexpected kind is severe. **Exits non-zero** on the structural checks -- `unexpected_missing_balance_window`, malformed placement, duplicate ID, `unexpected_participants_without_units`. Intentionally-unresolved Unreal rollout-gap matches are still printed prominently as a warning (with their own earliest/latest `game_datetime`), but do **not** by themselves fail validation: a production database sitting entirely inside a documented Unreal rollout gap ends "No severe integrity issues detected." Unknown IDs, a low cost-presence/metadata-coverage rate, and non-target-queue matches are also printed as warnings, not failures, since those can legitimately happen right after a patch before CommunityDragon updates, or before this milestone's queue filtering existed. Also prints store-wide (not window-scoped) diagnostics -- raw `game_version`/resolved-patch/balance-window distributions and the earliest/latest `game_datetime` -- specifically to help identify real Unreal-era patch cutover timestamps; never prints full match payloads.
- `tftlab prepare-discovery [--db ...] [--balance-window W ...] [--force]` -- computes and atomically publishes prepared Discovery analytics for every balance window (or the given ones), skipping windows whose latest run is already current. A failed window keeps its previous run, and the command exits non-zero after trying the others. Never contacts Riot. See "Prepared Discovery analytics".
- `tftlab discovery-smoke [--db ...] [--max-cost N] [--limit N]` -- runs the discovery engine against the latest balance window and prints the top candidates (carry, cost, commitment games, appearance/commitment/conversion rates, avg placement, Top4, win rate, 3-star hit rate, opportunity score, and the single best-evidenced partner/item-package/trait-breakpoint). Candidates under 30 commitment games are labeled `LOW SAMPLE`.
- `tftlab patch-diagnostics [--db ...]` -- store-wide, read-only game_version/patch/balance-window diagnostics: total matches, earliest/latest `game_datetime`, the three distributions from `validate-live-data` above, the masked-Unreal (unresolved) match count and its own earliest/latest `game_datetime`, and the Unreal patch registry's usable/total window count. Makes **no Riot API or CommunityDragon calls** and runs no discovery/analytics queries or deletions -- purpose-built to read off the real timestamp range needed to fill in `UNREAL_PATCH_REGISTRY` without running another ingest. Connecting still runs the normal, safe, idempotent Unreal-patch backfill migration (see above), which may move a still-unresolved row to the explicit sentinel; that's expected.

`validate-live-data`/`discovery-smoke`'s `--db` accepts either a SQLite path or a `postgres://` URL (defaulting to `DATABASE_URL`, then `TFT_DB_PATH`), and is deliberately typed as a plain string rather than a filesystem path -- `pathlib.Path` collapses a URL's `//` after the scheme, which would otherwise silently break it.

## API endpoints

Every carry/discovery endpoint below is balance-window scoped (`?balance_window=`, defaulting to the latest window):

- `GET /api/balance-windows` -- every balance window actually present in the store (`balance_window`, `matches`, `latest_game_datetime`), plus `default_balance_window` and a store-wide `unresolved_unreal_matches` count. Backs the dashboard's window selector; never lists the Unreal-unresolved sentinel as a selectable window.
- `GET /api/discovery` -- ranked `DiscoveryCandidate` list (`max_cost`, `min_samples`, `top_n`, `limit`; `costs=4` or `costs=1,3,5` selects exact costs instead of `max_cost`). Only the carries that pass the cost and `min_samples` filters get partner/item/trait evidence built, in three batched queries. `window_carries` is the number of carries of any cost in the window, so the page can tell "these filters match nothing" from "no data yet". Served from the window's current prepared run when there is one (see "Prepared Discovery analytics"); the additive `prepared` field reports `current`, `stale` or `missing`. Otherwise it is computed live: the window-wide carry population is kept in process memory per (database, window) and recomputed whenever that window's match count or latest game time changes.
- `GET /api/discovery/{character_id}` -- one carry's full `DiscoveryCandidate` (prepared run when current, otherwise live; same `prepared` field)
- `GET /api/carries/{character_id}/partners` -- partner associations
- `GET /api/carries/{character_id}/items` -- `{"items": [...], "pairs": [...], "packages": [...]}`
- `GET /api/carries/{character_id}/traits` -- trait associations keyed by observed unit count
- `GET /api/carries`, `GET /api/carries/{character_id}` -- unchanged; predate `/api/discovery` and are kept for their own existing tests/consumers
- `GET /api/experiments`, `GET /api/experiments/{slug-or-id}` -- the read-only theorycraft notebook (see below); not balance-window scoped
- `GET /api/champions` -- every current-set champion (the art manifest's list) with display name, cost, slug, art and its carry-board count in the window (`carry_games`: committed player boards, eight per match), from one light count query
- `GET /api/champions/{name-or-id}` -- one champion's carry investigation (`top_n` rows per list); the key is a name or slug (`khazix`, `Kha'Zix`) or a Riot id. Unknown champions are 404; a known champion with no carry games in the window is a 200 with `carry: null` (and `how_to_play: null`). Additive fields: `how_to_play` (the concise layer: `component_direction`, `items`, `pairs`, `builds`, `teammates`, `recurring_cores`, `trait_directions`, `star_signal`, `summary`), `items.individual` and `cores` (recurring 3–4 unit cores; see Recurring cores)
- `GET /api/source` -- the active data source (mode, label, observed/synthetic, counts, latest game, freshness, snapshot manifest); see "Production safety"
- `GET /riot.txt` -- Riot site verification string from `RIOT_SITE_VERIFICATION` (plain text; 404 when unset)

## Frontend: Discovery Dashboard

The web frontend (`src/tftlab/web/`) is a single-page discovery dashboard built entirely on the API endpoints above -- no backend logic lives in the frontend. It defaults to 1-3 cost, sorted by Opportunity Score, and fetches the full candidate pool for the selected balance window once (`min_samples=1`, `max_cost=5`) so cost/sample/sort controls re-filter instantly client-side rather than round-tripping on every change. Each candidate card previews its single best-evidenced partner/item-package/trait-breakpoint (already returned by `/api/discovery`); clicking a card opens a full evidence panel backed by `/api/discovery/{character_id}` at a higher `top_n`. Candidates under 30 commitment games are labeled `LOW SAMPLE`, matching the CLI's `discovery-smoke` threshold.

Visually it's a "strategist's notebook": system serif/monospace type (no web fonts or other external assets), a faint graph-paper surface, index-card entries with cost-colored tabs, a hand-circled Opportunity Score, rubber-stamp evidence labels, and margin annotations in the working-notes panel. All decoration is CSS or small inline SVG marked `aria-hidden`, and every imperfection (rotations, uneven corners, alternating rules, which circle shape a score gets) is fixed by CSS `nth-child` or list position, never randomized, so the page renders identically on every load. Reduced-motion preferences are respected.

Champion/item/trait art isn't fetched from CommunityDragon here (that would add a live external dependency to every page load). Instead, champions get a taped `figure.portrait` frame and partners/items/traits get a small clipped `.ref-icon` slot, both showing initials for now; both frames already style a child `<img>` (`object-fit: cover`), so real art can drop into the same frames later without a layout change.

Evidence is currently always labeled `OBSERVED` (a real statistical result); `VARIANT` and `THEORYCRAFTED` styles exist in the CSS for later use but are never applied yet, since nothing in the backend synthesizes either kind of result today.

## Frontend: Champion Investigation

`/champions` lets a player pick a champion by name (search, or browse by cost), and `/champions/<name>` answers "I want to carry with this champion: what does our data say?" for one balance window. The page has its own window selector, kept in the URL (`?balance_window=`). It is one data-driven template for every champion; nothing champion-specific is hardcoded. All numbers come from `tftlab.champion_investigation`, a read-only view over the existing carry analytics (`carry_commitment_stats`, `item_package_stats`, `carry_partner_associations`, `trait_profile`); nothing is recalculated in JavaScript.

The page is split into tabs (one tab list, keyboard-accessible with arrow/Home/End keys, scrolling sideways on phones; the selected tab is kept in the URL hash, e.g. `#items`, so switching never reloads). **How to play** is the default.

- **How to play** (`how_to_play` in the API, built by `tftlab.how_to_play` from the same evidence rows as the other tabs: no extra query, no new statistic). It answers the 10-second questions with icons and small numbers:
  - **Recipe direction:** components shared by the CommunityDragon recipes of the completed items shown below, e.g. "Needlessly Large Rod · in 3 of 6 recipes". It is **derived from recipes** (`derived_from_recipes: true`), never presented as observed components: Match-V1 lists final items only, with no component history. The six most common normal items (the ones shown under Common items) are read; only an exact two-component recipe of recognized components counts; Artifact/Radiant/unrecognized items and Thief's Gloves never contribute. Components are ordered by how many of those recipes contain them, then by the contributing items' carry boards, then name. Without a common item with a verified recipe it says so instead of guessing.
  - **Star signal:** Top 4 at 3★ vs below 3★ with board counts, from the existing hit/miss split; "Limited sample" when either side has fewer than 10 boards; a plain note when nothing reached 3★ (or everything did). Never a causal or "reroll" claim.
  - **Item direction:** **Common items**, **Common observed pairs** and **Common full builds**: each on at least 10 carry boards (Discovery's existing minimum), ordered by carry boards, most first (ties by display name, then id). This is a sample/frequency rule only, so nothing here is labelled supported, strong or best; a common row can have a negative with-vs-without result. How those boards placed stays on the Items tab, in its existing analytics order. Six items, three pairs, three full builds; normal craftable items only.
  - **Build around:** up to four teammates by how often they shared the carry's board (frequency, not a score), and up to three trait directions with the unit count Riot reported most often (`num_units`, "4 units", never called a breakpoint). A trait with exactly one natural shop-champion provider in the committed roster is skipped when its selected count is 1 unit: that only says its one champion was on the board -- the carry itself (its guaranteed baseline, already removed from trait evidence) or a teammate the teammates list already shows (e.g. Draven's Bounty Seeker on a Kog'Maw board). The same trait selected at 2+ units stays eligible, and the Traits tab keeps every row.
  - **Recurring cores:** up to three 3–4 unit packages (`recurring_cores`) -- the carry plus 2 or 3 teammates directly observed together on the same final carry boards (see Recurring cores below), each on 10+ boards. A fixed presentation quota, not a score: at most two 4-unit cores (most common first), then the most common 3-unit cores. 2-unit cores are the Teammates list and are not repeated. Labelled as subsets of boards: other units were there too.
  - **Why: evidence summary:** one sentence restating those picks as observed associations, with a link to the Evidence tab. No mechanic or causal claim.
- **Items:** the recipe direction in detail (each component with the items whose recipe uses it), then individual completed items (`items.individual`, the existing `item_package_stats` individual rows), item pairs and exact 3-item builds. Each row shows how often its carry boards used it, and Top 4 with vs. without it. Rows are ordered by the existing shrinkage-adjusted association, never called best-in-slot. Artifact and Radiant items (classified from Riot's own id naming, since the snapshots carry no class tag) and unrecognized ids stay in the lists with a label, but never lead the normal-build summary. An id with no metadata shows readable text from the id itself, marked as such, never another item's name or icon.
- **Teammates:** **Individual teammates** (every partner row with its with-vs-without view, labelled as association, not causation), then **Recurring 3–4 unit cores** (`cores`): up to six 4-unit and six 3-unit cores with portraits, board count/share, Top 4 with vs. without the whole core and average placement, plus an honest empty state when none reaches 10 boards.
- **Traits:** the traits most often active on its carry boards (share of carry boards; ties by trait name, then id), and under each the unit counts Riot reported for that trait (`num_units`, "4 units"), lowest count first, with carry boards, share, Top 4 (and Top 4 on its other carry boards) and average placement. Every share has the champion's carry boards as its denominator; one board counts under every trait it had active, and under exactly one count of each. The with-vs-without comparison is secondary evidence and never sets the order. Riot's `tier_current` is not shown and not converted into a threshold (see Traits above). The champion's own singleton-provider traits are never ranked at their guaranteed one unit. A row for such a trait appears only when boards reported it at 2+ units; it then covers only those boards, is marked `above_baseline_only`, and is never restated in the summary as an "active trait" share. The page shows no separate intrinsic block; the API payload keeps the fail-closed `intrinsic_traits` classification (name, icon, reason), currently empty for Set 18.
- **Evidence:** the dense material that used to lead the page. **What the carry boards show** keeps the **Observed** lines (3★ rate and Top 4 when it hit vs. stayed below 3★, the most common normal full build, the best-supported build, the most frequent and best-supported partner, the most common active traits) and the separate fixed-rule **Interpretation** (the 3★ reading needs at least 30 boards on each side; it reports the observed Top 4 difference as an association). Then **Observed results** (carry boards, carry conversion, average placement / Top 4 / first place next to the window's all-carry average, 3★ rate and hit-vs-miss) and:
  - **How much to trust this:** what a carry board is (one player's final board: a match has eight, so board counts can exceed match counts), LOW SAMPLE at under 30 carry boards (Discovery's existing product threshold), "Limited sample" on rows with fewer than 10 boards on either side of the comparison, how the Interpretation rules work, the balance window with its match count and latest game, and what the page does not show yet. Research-report evidence bands are not presented as production confidence classes.

Everything on the page is **Observed**, except the recipe direction, which is labelled **Derived from recipes**. Composition families, exact boards, positioning, augments and leveling plans are not shown: the archetype research is experimental and is never served. Live Match-V1 units carry no display names and Set 18 items use their own ids (`DA_GiantSlayer`), so names and icons are resolved through the committed art manifest, roster and item snapshot (`alias_of`; for a normal item the id lookup misses, e.g. `DA_Component_BFSword`, the one cached item with exactly its display name -- Artifact and Radiant items never borrow an icon). Discovery cards now use the same display names and link to the champion's investigation.

### Recurring cores

`tftlab.analytics.cores` answers "which small groups of units actually recur together around this carry?". A **core** of size N is the carry plus N-1 teammates, and it is counted on a board only when **every** member was on that same final board: direct co-occurrence, never a combination of pair statistics.

- **Universe / denominator:** the champion's committed carry boards in the selected balance window -- exactly the boards `carry_commitment_games_with_partners` returns, the same ones the individual teammates use. Champion Investigation runs that one board query once and derives both the teammates and the cores from it (no extra query).
- **Members:** player-selectable shop champions only (`Roster.is_shop_champion`: roster cost 1–5 and at least one set trait -- 74 in Set 18). Cost-1 jungle camps, dummies and summons in the feed (`TFT_Krug`, `TFT_Voidspawn`, ...) never become members; their raw rows are untouched. Duplicate copies of a champion on one board count once, and a board counts at most once for any core.
- **Candidates:** every 1-, 2- and 3-teammate combination of each board's own eligible partners (no top-N teammate prefilter), so a package of individually less common units is found too. Teammates on fewer than 10 boards are dropped before enumerating -- exact, because a core is on no more boards than any member (a test checks the result is identical).
- **Numbers:** `games` = boards containing every member; `share` = games / committed carry boards; average placement, Top 4 and first place describe those same boards. The with-vs-without comparison is `compute_associations` applied to the whole core as one key (its own boards vs. the carry's other boards, with the existing shrinkage and the Limited sample flag); member statistics are never added.
- **Selection / order:** cores on at least 10 boards (Discovery's existing minimum, `how_to_play.MIN_BOARDS`), ordered by boards together (most first), then canonical unit ids. Results never select or order them: picking the best-looking Top 4 out of hundreds of combinations would mostly find noise. Nothing is called strong, best or recommended, and there is no core score.
- **What a core is not:** an exact board (other units were also present), a complete composition, a cause of the result, or anything about when units were bought, where they stood, economy or leveling. Larger cores contain smaller ones, so their samples overlap; their numbers must never be added together.
- **API:** `cores` = `{evidence, subset_of_final_board: true, definition, min_boards, carry_boards, ordering, qualifying: {three_unit, four_unit}, three_unit: [...], four_unit: [...]}`; each row has `size`, `units` (carry first, then teammates by cost and name, each with `character_id`, `name`, `cost`, `slug`, `art_url`), `member_ids` (canonical), `subset_of_final_board: true` and the observed fields (`games`, `share_of_carry_games`, `top4_with`, `avg_placement_with`, `win_with`, `games_without`, `top4_without`, `avg_placement_without`, `adjusted_top4_difference`, `limited_sample`). `null` without carry boards. `how_to_play.recurring_cores` holds the concise picks.
- **Cost:** in memory, for the selected carry only. Measured locally: ~21 ms for a 500-board carry with a realistic long tail of partners, ~180 ms at 3,000 boards (~340 ms in a flat worst case where every partner is common); the 180-board champion endpoint stayed at 7 queries and ~15 ms.

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

**Public compliance pages** (static, no data source needed):
- `/about`: what TheoryLabs is, how to use it, and what it is not;
- `/methodology` (alias `/data`): data source, balance windows, carry definition, sample sizes, observed vs inferred vs theorycrafted, limitations, freshness;
- `/privacy`: describes the site as it actually is: no accounts, cookies, browser storage, analytics or third-party requests. It also covers hosting logs and Riot data usage;
- `/terms`: simple terms for a non-commercial individual project.

Every page links to Discover, Champions, Experiments, About and Methodology in the header, and to all of those plus Privacy and Terms in the footer. The footer carries Riot's legal boilerplate from the General Policies, verbatim: "TheoryLabs isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc."

**Riot site verification (`/riot.txt`):** the site serves the value of the **`RIOT_SITE_VERIFICATION`** environment variable at `/riot.txt`.
- **Format:** plain text, `Cache-Control: no-store`, surrounding whitespace removed, nothing before or after the string.
- **Unset:** a 404, never a placeholder.
- **API-key guard:** a value that looks like a Riot API key (`RGAPI-...`) or equals `RIOT_API_KEY` is refused with a 404. The web app itself uses no Riot API key.

When the Developer Portal gives you the verification string:
1. Render dashboard → the public web service → **Environment** → add `RIOT_SITE_VERIFICATION` with exactly that string.
2. Save, which redeploys the service.
3. Open `https://<your-domain>/riot.txt` and check it shows only the string.
4. Then verify in the Developer Portal.

## Website

Milestone 1.5 includes a functional web UI for the discovery engine.

```bash
pip install -e .
tftlab web
```

Then open `http://127.0.0.1:8000`.

The website uses the data source selected by `TFT_DATA_SOURCE` (see "Production safety"). Unset, it automatically uses `DATABASE_URL` (Postgres) when configured -- reachable is enough, even with zero matches so far -- or `TFT_DB_PATH` (SQLite) when that file contains matches, and otherwise the deterministic synthetic demo dataset, labelled **demo data (synthetic)** in a banner on every page. See "Production safety" above for exactly what happens if a configured `DATABASE_URL` is unreachable (a loud 503, not a quiet fallback to demo). Use demo data only to test the product flow; it is not live TFT performance data.

Current web pages/features:
- TheoryLabs landing/discovery page
- About, Methodology & data, Privacy and Terms pages
- Data-source banner (demo / snapshot / indexed matches, freshness, unavailable)
- 1/2/3/4/5-cost filter
- Minimum-sample filter
- Hidden reroll candidate cards
- Opportunity Score
- Hit-vs-miss 3-star performance
- Recurring partner analysis
- Observed item packages
- Responsive desktop/mobile layout

The next web milestone is a dedicated comp page with a hex board, champion/item assets, traits, patch selector, known-vs-theorycrafted labels, and live Riot/CommunityDragon data.

## Public website snapshots

`tftlab export-public-snapshot --source <sqlite-path-or-postgres-url> --out public.sqlite3 [--balance-window 18.3 ...] [--compress]` builds the SQLite file the website serves in `TFT_DATA_SOURCE=snapshot` mode, from a real TheoryLabs database such as the local store or a restored backup. The source is opened read-only. `--source` is required and never falls back to `DATABASE_URL`. `tftlab verify-public-snapshot FILE` re-runs the integrity checks on any file.

**What it contains** -- only what the public routes read (`Database.REQUIRED_READ_SCHEMA`):

| Table | Columns copied | Sanitized (fixed value) |
|---|---|---|
| `matches` | `match_id` (remapped), `game_datetime`, `game_version`, `patch`, `balance_window`, `queue_id` | `payload_json` = `{}`; `game_type`, `set_number`, `set_core_name` = NULL |
| `participants` | `match_id` (remapped), `participant_index`, `placement` | `augments_json` = `[]`, `level` = 0 |
| `units` | all: `match_id` (remapped), `participant_index`, `unit_index`, `character_id`, `unit_name`, `cost`, `tier`, `items_json`, `completed_item_count` | -- |
| `traits` | all: `match_id` (remapped), `participant_index`, `trait_name`, `num_units`, `style`, `tier_current`, `tier_total` | -- |
| `experiments`, `experiment_tags`, `experiment_field_notes` | the columns the notebook pages read, for the operator's own entries (`origin = 'manual'`); demo entries are never exported | -- |
| `discovery_prepared_runs` / `_candidates` | Discovery prepared on the snapshot itself, so Discover is fast. A failed preparation is recorded and that window is computed live instead | -- |
| `schema_migrations` | migration markers (part of the prepared-run fingerprint) | -- |
| `public_snapshot_metadata` | the provenance record (below) | -- |

**Never exported:**
- the collection tables `seed_samples`, `match_discoveries` and `ingest_runs`;
- PUUIDs, Riot IDs and raw Riot match ids;
- raw Match-V1 payloads and augment payloads;
- credentials and API keys.

**Opaque match ids:** matches are renumbered `S0000001`, `S0000002`, ... in (game time, source id) order, consistently in every table. The mapping is never stored. Analytics don't depend on match ids: aggregates are unchanged, and only the order of exactly tied list entries can differ. The source population is recorded only as a one-way fingerprint (the sha256 of the sorted source ids).

**Provenance and the fail-closed rule:** the `public_snapshot_metadata` table holds one versioned record:
- `format: theorylabs-public-snapshot`, `format_version: 1`;
- `synthetic: false`, `observed: true`, `source_kind: riot-match-v1-ranked-tft`;
- `exported_at`, `code_version` (git SHA / run id when available, plus the analytics version);
- `balance_windows`, per-window counts and dates, match/board/unit/trait counts and the latest game;
- the source fingerprint, `tables`, `sanitized_columns` and `excluded_tables`, and the exclusions statement;
- `real_ingestion_checks`.

An export is refused, and nothing is written, unless the source passes every real-ingestion check:
- it has a completed live-ingest run in `ingest_runs`, and its `match_discoveries` ledger links to exported matches;
- every exported match is ranked queue 1100, with a regional Riot match id (for example `NA1_…`);
- its stored raw payload has a matching `metadata.match_id`, a `data_version`, and a participant list consistent with the stored boards;
- its patch is a numeric client patch, and its balance window is trusted (numeric).

The deterministic demo dataset fails all of these (`DEMO_…` ids, `Version DEMO`, no ledger), even with a forged ledger. The website serves a snapshot as observed only when this provenance is present and certifies observed data. Any other file, however valid its schema, is a 503. These checks guard against mistakes, not deliberate forgery.

**Verification before success:**
- **Schema and read access:** the read schema and a read-only open through the website's own path.
- **Provenance:** valid.
- **Exclusions:** no collection tables or `puuid` columns, no raw payloads or augments, and no secret markers (`RGAPI-`, `postgres://`), source connection string, Riot match id or PUUID-shaped token anywhere in the file.
- **Ids and windows:** opaque ids only, referential consistency after remapping, and only trusted (and, if requested, only the requested) windows.
- **Counts:** reconciled against the source.

The output is one self-contained file (no WAL sidecars), a `<out>.manifest.json` with provenance, verification and sha256s, and with `--compress` a `<out>.gz`.

**Size:** about 16 MB per 1,000 matches as SQLite, or about 3.4 MB gzipped, at about 8.5 units and 10 traits per board. Patch 18.3's ~11k matches give roughly 170 MB, or about 37 MB gzipped. That is too large to commit to git comfortably but fine on free Render's disk; serve the `.gz` and the website unpacks it once. A practical $0 publication path, not automated here, is to attach the `.gz` to a GitHub Release of this public repository, then have the Render build download it and check the manifest's sha256.

**From the encrypted backup:** `.github/workflows/public-snapshot-from-backup.yml` ("Build public snapshot from encrypted backup") is manual only, from `main`, with permissions `contents: read` and `actions: read`. It never contacts Neon. Its inputs name the backup run id and artifact; the defaults are the verified 2026-10-05 backup, run `37308579783`, artifact `theorylabs-postgres-backup-20261005T121811Z`. The workflow:
1. downloads that artifact from this repository and checks its sha256;
2. decrypts it with `DB_BACKUP_PASSPHRASE`, using exactly `db-backup.yml`'s `openssl enc -d -aes-256-cbc -pbkdf2 -iter 250000 -md sha256`;
3. restores it into an ephemeral `postgres:18` container on the runner's loopback, with a random masked password;
4. runs the exporter and the verifier;
5. uploads **only** the `.gz` snapshot and its manifest, with 7-day retention.

The plaintext dump is deleted right after `pg_restore`, by a trap even on failure. An always-run final step removes the container, the backup files and the private file holding the restore URL. Nothing is deployed.

## Deploying on Render

The repo includes a `render.yaml` Blueprint that runs the FastAPI app with:

```
uvicorn tftlab.webapp:app --host 0.0.0.0 --port $PORT
```

**Public snapshot (real data, still no cloud database):** see "Public website snapshots" below.

**Zero-cloud-database public site:** set `TFT_DATA_SOURCE=demo` (synthetic, labelled on every page) and leave `DATABASE_URL` unset. The site then needs no database service at all, so it keeps working when a cloud database is unavailable or out of quota. When a real analytics snapshot is available, ship it at `TFT_SNAPSHOT_PATH` and switch to `TFT_DATA_SOURCE=snapshot`; the frontend is unchanged. Heavy ingestion and analytics are meant to run on the owner's machine, which is never exposed to the Internet; that is a separate follow-up.

With no `DATABASE_URL` and no `TFT_DATA_SOURCE` configured, the app automatically falls back to a generated demo dataset, so it boots and serves data even with zero configuration. `/api/health` and `/api/carries` report `"demo"` (true/false) and `"backend"` (`"sqlite"`/`"postgres"`) so it's always clear which one is live. Once `DATABASE_URL` **is** set, that changes: see "Production safety" above -- an unreachable configured database now fails loudly (HTTP 503 from every endpoint, including `/api/health`) instead of quietly serving demo data. Since `render.yaml`'s `healthCheckPath` points at `/api/health`, this means Render will correctly flag the service as unhealthy if the configured database goes down -- that's the intended behavior, not a bug to work around.

**Current production hosting:** the web/API service runs on Render in **Ohio** (`tft-theory-lab-ohio.onrender.com`, the service `verify-ohio.yml` checks). Until the operator switches it to the zero-cloud-database mode above, its runtime `DATABASE_URL` points to the Neon **Ohio** production branch (`neondb`). `render.yaml` pins `region: ohio` and declares `DATABASE_URL` as `sync: false`, so the credential stays in Render's environment rather than the repository. After any hosting/database change, confirm `/api/health` reports `"backend": "postgres"` and `"demo": false` before relying on it. The previous Oregon Render Postgres database is temporarily retained as a rollback snapshot, not as the active production database.

## Live ingestion via GitHub Actions

`.github/workflows/live-ingest.yml` pulls real Riot data into the Neon production database, **automatically every 6 hours**, on manual dispatch, and through the owner-only Ops Control smoke command `/ingest smoke`. Every path uses the same guarded steps: `tftlab verify-riot`, then `tftlab ingest-riot`, then `tftlab validate-live-data`, then `tftlab discovery-smoke`, then `tftlab prepare-discovery` (publishes the prepared Discovery analytics the website reads).

### Scheduled collection (every 6 hours)

- **When:** cron `41 */6 * * *`, i.e. 00:41, 06:41, 12:41 and 18:41 UTC (four chances a day). The minute is off the hour on purpose: GitHub delays, and under load can drop, scheduled runs at busy times such as the top of the hour. Scheduled runs always use the default branch, `main`.
- **What:** fixed, conservative settings that dispatch inputs never change: **bounded** mode, 15 Challenger / 15 Grandmaster / 20 Master / 25 Diamond / 25 Platinum seeds x 10 recent matches each, inside the current trusted window, with the same `10:10` rate ceiling and proactive pacing as every run. This is the configuration of the two successful production runs 36286242686 and 36289198154: about 600-730 Riot requests and 15-20 minutes each, zero 429s. Seeds rotate through the sampling ledger, never-sampled players first, so each run reaches different players, and already-stored matches are skipped before any match-detail request.
- **Why bounded, not maximum:** both production maximum-mode runs so far (36276659753, 36284091564) failed with 401 "Unknown apikey" during deep Diamond/Platinum ladder enumeration, before collecting any match. That is not yet understood, so maximum mode stays manual-only until it is.
- **Manual runs are unchanged:** the Run workflow form below still controls a manual run completely (bounded or maximum). Scheduled and manual runs share one `concurrency` group, so they never overlap: a run that starts while another is in progress waits. GitHub keeps at most one waiting run per group, so a newer waiting run replaces an older one that never started; a run in progress is never cancelled.
- **Development key:** the key still expires about every 24 hours and is replaced by hand (see "Riot development key operations" below). A scheduled run after expiry fails at `Verify Riot API`, before anything is ingested. A key that expires mid-run fails the ingest: 401/403 are fatal in both modes, so the run stops at once, is marked failed and finalizes no seed-ledger or provenance rows, while matches already stored stay stored. Either way the run is red and needs nothing else: the next scheduled run after `RIOT_API_KEY` is updated resumes normally, with the same players still at the front of the rotation. Nothing retries a failed run.
- **Where to look:** each run's summary page has a *Live ingest collection summary* table:
  - trigger, mode and outcome;
  - matches inserted, already stored and fetched;
  - seeds sampled and requested;
  - failed and empty histories;
  - ledger and provenance rows;
  - Riot requests by status, 429s and elapsed time;
  - the validation and Discovery smoke results.

  The full report is in the `Ingest live sample` step log; the `ingest-telemetry` artifact (JSON, kept 30 days) has every aggregate; stored data is checked by `Validate ingested data`.
- **Patch boundaries:** collection is bounded to the current trusted window (next paragraph). Once the latest registered window ends, scheduled runs fail with "No current trusted window" before any request is made, until the next patch's verified window is added to `UNREAL_PATCH_REGISTRY` in a reviewed change. The same happens inside a transition gap: 18.3 ended at 2026-10-06T00:00Z, and 18.4 is current from 2026-10-08T07:00Z until 2026-10-20T00:00Z. That is deliberate: collection never guesses a patch.
- **Pausing or changing it:** GitHub → Actions → Live ingest → "..." → **Disable workflow** pauses both the schedule and the Run button, and **Enable workflow** restores them. To change the cadence or the scheduled settings, edit the `cron` line or the job-level `env` block in a reviewed PR. GitHub also disables scheduled workflows in public repositories after 60 days without repository activity; re-enable it the same way.

**Current trusted window only:** the production ingest always runs with `--current-trusted-window`, so every seed's match-history request is bounded to the current patch: Riot's `startTime`/`endTime` (epoch seconds, as Riot documents for the by-puuid match-IDs endpoint) are taken from the latest verified + sourced `UNREAL_PATCH_REGISTRY` window that has started and not ended (18.4: `startTime=1791442800`, i.e. 2026-10-08T07:00:00Z, `endTime=1792454400`, i.e. 2026-10-20T00:00:00Z; 18.3 was `startTime=1790233200`, `endTime=1791244800`). If no such window exists -- none registered, none usable, or the latest one has already ended -- the run fails instead of crawling unbounded history. This only narrows what is requested: each match is still classified normally, and `validate-live-data` remains the authority. Locally, `tftlab ingest-riot --start-time 2026-09-24T07:00:00Z` sets an explicit lower bound instead; with neither option you get ordinary recent history. The ingest report shows the bounds, the trusted window, how many seeds had no games in it, and the earliest/latest inserted match.

**Inputs** (Run workflow form): one seed count per cohort -- `challenger_seeds` (default 10), `grandmaster_seeds`, `master_seeds`, `diamond_seeds`, `platinum_seeds` (default 0 each); each is 0-100 and the total across cohorts must be 1-100 -- plus `matches_per_player` (recent matches per seed, 1-10, default 5 -- the CLI's deeper 100-match maximum is deliberately not exposed here, since more players beats deeper histories of the same players). For example 20 / 20 / 20 / 20 / 20 is 100 seeds. The preflight prints every cohort's count and the total. The defaults reproduce the original conservative 10 x 5 Challenger run. It has **no** `push`, `pull_request`, or `schedule` trigger -- it only ever runs when someone explicitly starts it, and a failure in any step (bad key, unreachable CommunityDragon, unreachable database, a severe integrity issue) stops the run there rather than continuing partway.

**Production database credentials, by purpose:**

- The **Ohio Render web service** receives a runtime environment variable named `DATABASE_URL`; its value is the Neon Ohio production connection string.
- **GitHub Actions production workflows** use the repository secret `NEON_DATABASE_URL` and expose it to the CLI as the process-level `DATABASE_URL` variable. Live ingest, production backups, patch diagnostics, and read-only research reports therefore all target the same Neon production database.
- The repository secret `DATABASE_URL` is temporarily retained as the **legacy Render Postgres rollback/migration-source URL**. Production workflows must not write to it after cutover.

**Required GitHub repository secrets** (Settings → Secrets and variables → Actions → New repository secret, on the repo, not in any file):

- `RIOT_API_KEY` -- the Riot API key used by live ingestion.
- `NEON_DATABASE_URL` -- the Neon Ohio production branch Postgres connection string.
- `DATABASE_URL` -- legacy Render Postgres URL retained temporarily for rollback/migration tooling.
- `DB_BACKUP_PASSPHRASE` -- independent encryption passphrase for verified database backup artifacts.

No secret value is printed in workflow logs.

### Patch-wide collection plan (18.3)

**Goal: broad sampling across the whole 18.3 window** (2026-09-24T07:00:00Z to 2026-10-06T00:00:00Z) -- chronological coverage of the patch through many different ladder players. It is **not** exhaustive capture of every NA ranked game, and not longitudinal capture of every game each selected player plays.

**Recommendation: two runs per day, about 12 hours apart (e.g. ~08:00 and ~20:00 UTC), each with the five-cohort allocation `challenger=15 grandmaster=15 master=20 diamond=25 platinum=25`, `matches_per_player=10`, until the window closes.** (Now automated: the schedule runs exactly this allocation every 6 hours -- see "Scheduled collection" above.)

Why, from the first successful five-cohort run (100 seeds: 589 history references, 560 unique IDs, 458 new ranked matches, 13 zero-window seeds, 4.9% in-run overlap):

- **History depth.** The 87 active seeds averaged ~6.8 window games each after ~2 days of the patch, i.e. roughly 3-5 ranked games per day for a typical sampled player. A 10-game request therefore reaches back about 2-3 days for a typical seed and about 1 day for a heavy grinder. Runs 12 hours apart leave no chronological gap even for players well above average; once a day would still cover most seeds but would drop part of the heaviest grinders' days. Raising `matches_per_player` would mostly re-read the same players' older games -- breadth beats depth because high-Elo lobbies overlap.
- **Breadth and rotation.** Each run takes 100 seeds, never-sampled first. Challenger (221 players at 15/run) runs out of never-sampled players after ~15 runs (~7.5 days at this cadence) and then re-samples the least recently sampled -- a week later, almost entirely new games. Grandmaster (331), Master (1,471) and the Diamond/Platinum candidate pools last longer.
- **Request volume.** About 690 Riot requests per run (≈27 ladder + 100 histories + ≈560 match bodies), ~13-15 minutes at development-key rate limits (100 requests / 2 minutes). Two runs a day is ~1,400 requests -- far inside the limits; the real constraint is the manual key refresh below.
- **Duplicates.** In-run overlap was 4.9%; cross-run duplicates will rise as more lobbies are already stored (the 14.3% seen during the resume run was inflated by the resume itself). Stored matches are skipped without being re-fetched, so duplicates cost only history references.
- **Remaining days.** From 2026-09-26 to 2026-10-06 there are ~9.5 days, so ~19 runs. If yields stay near the first run and decline gradually, expect roughly **6,000-8,000 more 18.3 matches** -- an estimate, not a promise.

**When to back off:** the ingest report prints `Collection value (is another run worth it?)` -- never-sampled, previously-sampled and zero-history seed percentages -- next to the already-stored duplicate rate, inserted-per-seed and new-match yield. If never-sampled seeds fall below ~50% or new-match yield stays below ~40% for two consecutive runs, drop to one run per day.

**Limitations:** seeds are ladder players, so the population is what they played (see "Data population and seed cohorts"); a heavy grinder's games older than their last 10 are not requested; Diamond/Platinum candidates come from the first pages of each division (reported as capped when so).

**Patch end is strict.** Production always runs `--current-trusted-window`. At 2026-10-06T00:00:00Z (end exclusive) 18.3 collection stopped. Until 18.4's conservative window opens at 2026-10-08T07:00:00Z there is no current trusted window, so the ingest step fails instead of crawling another patch or going unbounded. 18.5 needs its own reviewed window before 2026-10-20T00:00:00Z.

**Collecting 18.4 locally:** `tftlab ingest-riot --current-trusted-window ...` with no `DATABASE_URL` writes to the local SQLite store (`TFT_DB_PATH`) once 18.4's window has opened. It writes the same ledger (`ingest_runs`, `seed_samples`, `match_discoveries`) that `export-public-snapshot` later requires.

### Maximum collection mode and Riot rate limits

**COLLECTION vs ANALYSIS.** Collection decides *which Riot requests to make*: which ladder players to read, how far into their (trusted-window) history, which lobbies to fetch. Analysis decides *what stored matches mean*: patch windows, classification, carry eligibility, Discovery and the Opportunity Score. Maximum mode changes collection only. It never reads champions, items, traits, placements or performance to choose what to fetch, and it writes the same tables the bounded mode writes (`matches`/`participants`/`units`/`traits`, `seed_samples`, `match_discoveries`, `ingest_runs`), so every analytical rule reads the new data exactly as before. More matches in, same rules out. No schema change.

**Modes.** `--collection-mode bounded` (the default, unchanged) is N seeds per cohort x M recent matches. `--collection-mode maximum` is opt-in and:

1. **Enumerates all five cohorts** -- the Challenger, Grandmaster and Master league lists, and every page of Diamond I-IV and Platinum I-IV -- and nothing else (no Emerald or lower, no combined cohort). Riot documents neither the entries page size nor a last-page marker, so a short page is *not* treated as the last: a division ends only at an **empty page** (reported `complete`). Guards end a division early, reported as incomplete, on a page whose PUUID set repeats the previous page (`repeated_page`), two consecutive pages with no new PUUID (`duplicate_only_pages`), or `--max-division-pages` (default 500, `page_cap`). A PUUID listed in two cohorts seeds once, in the higher one; the report counts such cross-listed PUUIDs.
2. **Orders every candidate breadth-first:** never-sampled players first, then the least recently sampled; within the same last-sampled time, interleaved across cohorts and spread across each ladder (golden-ratio stride), so stopping early still covers every cohort and rank band.
3. **Exhausts each seed's history inside the time bounds** (`--current-trusted-window` or `--start-time` is required): Match-V1 by-puuid pages of 20 IDs (Riot's documented default `count`; no maximum is documented, so none is guessed), `start` advancing until a page shorter than 20 or empty (`exhausted`). Guards: a page with no ID new to that seed (`repeated_page`) and `--max-history-pages` (default 50 = 1,000 IDs, `page_cap`, reported separately). Pages go round-robin across the seeds of a wave.
4. **Dedupes aggressively before any match-detail request:** every ID is deduped the moment it is seen (across seeds, cohorts and waves) and checked against the store (`has_match`); a lobby is fetched and stored at most once. Non-ranked queues are fetched but never stored. Raw observations are never discarded: every seed that surfaced a stored match gets its own `match_discoveries` row, even when another seed found it first.
5. **Works in waves** of `--wave-size` seeds (default 25). Each wave is its own `ingest_runs` row (`<run id>-w001`, `-w002`, ...) finalized with the same atomic transaction as a bounded run. A seed gets a `seed_samples` row only when its history reached a terminal point (exhausted, a guard, or the page cap) **and** every new match ID it surfaced was handled: stored, already stored, non-ranked, or answered 404 (Riot no longer serves it). A match whose fetch failed transiently (network error, or 5xx / 429 after the client's bounded retries) is counted, not retried again in that run, and **not** handled, so the seeds that surfaced it stay unledgered and a later run reads them again. A seed interrupted by a budget, or whose history request failed, is not ledgered and stays at the front of the rotation. An unexpected error (e.g. a database failure) marks the current wave `failed` and stops the run; earlier waves stay completed.

   **Fatal vs. skippable Riot errors** (`tftlab.riot.is_fatal_riot_error`): **401, 403, 400 and every other 4xx except 404 and 429 abort the collection.** They mean the key or the request construction is wrong, so every remaining seed or match would fail the same way (a key expiring mid-run must not turn into thousands of refused requests). Nothing further is requested, the current wave is marked `failed` (no ledger or provenance rows for it, and the half-handled seed is not ledgered), earlier finalized waves and every stored match stay as they are, and the error propagates so the step fails visibly (a 401 prints the expired-key guidance). A **404** is isolated -- that one match or PUUID -- and is counted and skipped. Network errors and exhausted 429/5xx retries are transient: counted and skipped, the run goes on.
6. **Stops cleanly at the first budget reached.** All three budgets are required, and there is no "unlimited" default: `--max-duration-minutes` (wall clock, checked before every request and before every pacing wait), `--max-requests` (every Riot attempt counts, retries and ladder pages included), and `--max-match-fetches` (match-detail requests). The request and wall-clock budgets are enforced inside the client by one check immediately before every HTTP attempt -- first attempts and 429 / 5xx / network retries alike -- and no pacing or backoff sleep starts if it would reach the deadline. The deadline is exclusive: once `clock() >= deadline` nothing more is sent; a request strictly before it may still go out. On a stop, no new work starts, the current wave is finalized with only its fully handled seeds, and the report shows `Stop reason: request_budget | duration_budget | match_fetch_budget` (or `complete`).

**Resuming** extends the PR #20 semantics: completed waves have advanced the rotation, so rerunning continues with the players this run did not finish, stored matches are skipped without being refetched (never duplicated), and provenance rows are idempotent.

**Rate limiting (all modes).** Riot's Developer Portal (Rate Limiting, https://developer.riotgames.com/docs/portal) defines application limits (per key, per region), method limits (per endpoint, per key, per region) and service limits (per service, shared). `RiotClient` now paces *proactively*:

- Every response's `X-App-Rate-Limit` / `X-Method-Rate-Limit` headers (and their `-Count` companions) are parsed as comma-separated `<requests>:<seconds>` windows (e.g. `20:1,100:120`, each window independent). The portal page names the limits but does not spell out the header format; any other value is counted as malformed in telemetry and ignored (the previous limits, or the bootstrap pace, stay in force). No production-key limit is hard-coded.
- Scopes: application = routing host (`na1.api.riotgames.com` for league, `americas.api.riotgames.com` for Match-V1 -- separate regions, separate budgets); method = host + endpoint (`tft-league-v1.getLeagueEntries`, `tft-match-v1.getMatchIdsByPUUID`, `tft-match-v1.getMatch`, ...). Before each request the client waits until **every** window of both scopes has room at `--safety-utilization` (default **90%**, i.e. `floor(limit * 0.9)` per window), so the tightest window -- app or method, short or long -- governs. Riot-reported counts only ever raise the local count, never lower it. Until a host's first response arrives, the client sends at most one request per second.
- `--rate-ceiling` (e.g. `10:10`) adds an operator policy cap per host, applied at 100% on top of the header budgets. The production workflow sets `10:10` because Riot's API Terms say a Development Key may not make more than 10 calls every 10 seconds; the key's type is **not verified** from here (the repository's key-reset instructions indicate a development key), so the cap stays until a reviewed change confirms a Personal or Production key.
- **429** is the fallback, not the mechanism: the client honours `Retry-After` and pauses the scope `X-Rate-Limit-Type` names -- `application` pauses the whole host, `method` that endpoint, `service` or a missing/unknown type (Riot: an underlying service may 429 without the edge's headers) that API service on that host. Without a usable `Retry-After` it backs off 1 s, 2 s, 4 s. A request is retried at most 3 times after a 429; a 4th consecutive 429 fails it with `Repeated rate limiting ...` (counted like any failed request); a `Retry-After` over 900 s fails at once while the scope stays paused. Timeouts, network errors and 500/502/503/504 get at most 2 retries (1 s, 2 s backoff). 400/401/403/404 are never retried. Every wait sleeps exactly until the next slot opens: no busy loops. The API key is only sent as a header and is redacted from any error text.

**Telemetry** (printed in the report; `--telemetry-out FILE` writes JSON; the workflow uploads it as the `ingest-telemetry` artifact even when the step fails): requests total / by method / by host / by status, successes, 429s total and by type, transient and network retries, pacing / 429 / backoff sleep time, the latest advertised app and method limits per scope, high-water utilization per window (Riot's count / limit), malformed and missing headers, ladder / history / match-detail request counts, and elapsed time. Aggregates only: no PUUIDs, match IDs, names or secrets. Nothing is stored in the database.

**Planning:** `tftlab ingest-riot --collection-mode maximum --current-trusted-window --max-duration-minutes 180 --max-requests 9000 --max-match-fetches 6000 --rate-ceiling 10:10 --plan` prints the budgets, the pacing policy, an upper bound on requests (budget vs. time x ceiling) and the request cost model, plus stored-match / ledger / completed-run counts through a read-only connection. It is **network-free**: no Riot or CommunityDragon request and no write; it does not need `RIOT_API_KEY`.

**Throughput model** (for sizing budgets; every figure below is a HYPOTHETICAL example, not a measurement or a promise):

- Requests = ladder (3 + one per Diamond/Platinum division page, unknown until enumerated) + history (per seed, `floor(window games / 20) + 1` pages) + match detail (one per new, not-yet-stored lobby). Ladder requests use the platform host and the rest the regional host, but the collector is sequential, so the regional host's budget is the binding one.
- Sustained rate = the tightest window at the utilization and ceiling. *Hypothetical:* with `100:120` advertised and 90% utilization, 90 requests per 120 s = 0.75 requests/s = 2,700 requests/hour; a `10:10` ceiling alone would allow 1 request/s = 3,600/hour, so the 120 s window would govern. With a 180-minute budget that is at most ~8,100 requests.
- *Hypothetical split:* if 3,000 of those went to seed histories, at most ~5,100 remain for match detail; how many are new lobbies depends on overlap and on what is already stored (the report's duplicate counts show it after a run).
- Enumerating every Diamond/Platinum page costs one request per page per division on every maximum run; the report's per-division page counts show the real cost after the first run.

**Workflow** (`live-ingest.yml`; maximum mode is manual dispatch only -- scheduled runs are bounded; fixed `concurrency`, no automatic retry, no run starting another run): new inputs `collection_mode` (`bounded` default / `maximum`), `max_duration_minutes` (1-200, default 60), `max_requests` (1-12,000, default 3,000) and `max_match_fetches` (0-12,000, default 2,000), all validated in the preflight before checkout. In maximum mode the seed and `matches_per_player` inputs are ignored (the preflight says so). The job timeout is 60 minutes for bounded runs and 240 for maximum runs: the ingest stops itself at `max_duration_minutes` (at most 200), and the rest covers install, `verify-riot`, validation and the smoke test, with the timeout as the hard backstop.

**Open questions (not verified from here):** the key's type (development vs personal/production) and therefore its real limits; the exact header format (the portal does not document it; the parser accepts only `<n>:<s>[,...]` and counts anything else); the entries page size and last-page marker (undocumented -- hence empty-page termination plus guards); a maximum Match-V1 `count` (undocumented -- hence the default 20). The first maximum run's telemetry answers the first two (advertised limits, malformed-header counts) without any extra request.

### Riot development key operations

Riot's Developer Portal says development API keys deactivate every 24 hours. Before a collection day (and whenever `verify-riot` fails with 401):

1. Open the Riot Developer Portal (developer.riotgames.com) and sign in.
2. Reset / regenerate the development API key and copy it.
3. In GitHub: **Settings → Secrets and variables → Actions → `RIOT_API_KEY` → Update**, paste, save.
4. Start the run; its `Verify Riot API` step confirms the new key before anything is ingested.
5. Never paste the key into logs, issues, PRs, the README or chat.

With an expired key, `Verify Riot API` exits 1 with: *"401 Unauthorized -- RIOT_API_KEY is invalid or expired. Reset the development key in the Riot Developer Portal and replace the GitHub Actions RIOT_API_KEY secret."* and the ingest step never runs. If a key expires mid-run, the ingest prints the same guidance, the run stays incomplete, and seed rotation ignores it. The key is never printed. Logging in to Riot or regenerating keys is not automated.

**Operating rule:** after merging anything that deploys (especially schema changes), wait for the Render deployment to finish before starting a production ingest. This is defense in depth only: web requests no longer run schema maintenance, and a transient deadlock on one match is retried, so correctness does not depend on that timing.

**Running it:** GitHub → **Actions** tab → **Live ingest** in the left-hand workflow list → **Run workflow** button → keep **`main`** selected as the branch (it is the repository's default branch; picking anything else fails immediately, see below) → **Run workflow**.

**Production safety checks:** before touching Riot or the database, a first `Validate production configuration` step fails the run (with a static error message, never a secret value) if the selected branch isn't `main`, if `RIOT_API_KEY`/`NEON_DATABASE_URL` is missing, if the resolved production URL doesn't start with `postgres://`/`postgresql://` -- this exists specifically so the ingest CLI's normal local-SQLite fallback can never be silently used in production -- or if an input is out of bounds. Inputs reach the shell only as environment variables. The workflow declares `permissions: contents: read`, a bounded timeout, and shares the fixed `live-ingest-production` concurrency group with backups so production writes and dumps cannot overlap. Allowed triggers are the schedule, a manual dispatch, and the owner-only exact `/ingest smoke` command on Ops Control issue #38; all other issue comments create only a skipped job.

## Patch diagnostics via GitHub Actions

`.github/workflows/patch-diagnostics.yml` runs `tftlab patch-diagnostics` (see above) against production -- read-only, no Riot or CommunityDragon calls, no ingestion. Use it to read off the real masked-Unreal `game_datetime` range needed to fill in `UNREAL_PATCH_REGISTRY` without running another live ingest.

Same production-safety shape as `live-ingest.yml`: `workflow_dispatch`-only (no `push`/`pull_request`/`schedule`), a `Validate production configuration` step that requires `main` explicitly selected and a well-formed Neon production URL before anything else runs, `permissions: contents: read`, a bounded job timeout, and its own fixed `concurrency` group (`patch-diagnostics-production`). It consumes the `NEON_DATABASE_URL` repository secret as the process-level `DATABASE_URL` -- never `RIOT_API_KEY`, since it makes no Riot calls at all.

**Running it:** GitHub → **Actions** tab → **Patch diagnostics** in the left-hand workflow list → **Run workflow** button → **explicitly select `main`** as the branch → **Run workflow**.

## Read-only Discovery research report

`tftlab discovery-report --db <sqlite path or postgres URL> --out-dir discovery-report [--balance-window W] [--no-compare-pr21]` writes a full research report for one balance window (default: the latest): `discovery_<window>_full.json`, `discovery_<window>_candidates.csv` and `discovery_<window>_pr21_vs_pr22.csv`.

- **What it contains:** dataset counts and integrity checks using `tftlab.validate`'s own definitions -- window: matches, participants, unit-observable participants, participants without units split into **source-empty** (Riot's payload entry itself has no units) and **unexpected** (the payload lists units, or the raw/stored mapping can't be trusted), date range, malformed placements; store-wide: matches, duplicate ids, missing balance windows split into the expected Unreal rollout gap and unexpected ones, malformed placements, source-empty/unexpected participants, patch and balance-window distributions; **every** carry with at least one commitment game (cost 1-5) with the canonical `carry_commitment_stats` fields, the Discovery Opportunity Score, its components, confidence and best partners/items/traits; overall, per-cost and default-web-Discovery ranks; and, unless disabled, the same analytics under the PR #21 stat-only carry rule plus, per champion **board** (the same `match_id` + `participant_index` + `character_id` granularity as commitment games; a board commits when any copy qualifies), the boards that stopped or started committing, each attributed to the canonical copy's item package (`CANONICAL_UNIT_TIEBREAK_SQL`) with each item's current Riot intent.
- **Evidence bands** (a reporting aid, not a production classification) reuse existing thresholds: `A_substantial` >= 60 commitment games (confidence prior strength), `B_moderate` >= 30 (`LOW_SAMPLE_COMMITMENT_GAMES`), `C_early` >= 10 (web Discovery `min_samples`), `D_too_little` below that.
- **Read-only by construction:** it opens the database with `Database.open_existing` only (no schema setup, migration or backfill), refuses a writable connection, and on Postgres requires the server to report `transaction_read_only = on`. No Riot or CommunityDragon call; output is aggregates and item/champion/trait ids only.

`.github/workflows/read-only-discovery-report.yml` runs it against production on manual dispatch from `main` only (same preflight as the other production workflows) and uploads the files as the `discovery-report` workflow artifact; nothing is committed.

## Board archetype research report (RESEARCH / VALIDATION INFRASTRUCTURE)

**This is research infrastructure, not a production composition system.** The archetype methodology is experimental, the results are **not served to users** (no web API, page, Discovery or Experiments integration, no table), and every grouping parameter is a candidate under validation. It exists to answer one question with real data before any product work: do real final boards form coherent, recognizable composition families?

`tftlab archetype-report --db <sqlite path or postgres URL> --balance-window 18.3 --out-dir archetype-report` (`src/tftlab/archetype_research.py`):

- **Population:** the balance window's standard Ranked TFT matches (`queue_id` = `RANKED_TFT_QUEUE_ID`, 1100) and their unit-observable participants. The report prints window matches in any queue, excluded non-Ranked matches, and -- all for the same Ranked population -- matches, participants, unit-observable participants, source-empty and unexpected participants without units (`tftlab.validate`'s own classification, scoped to the window and the Ranked queue via `classify_participants_without_units(..., queue_id=...)`), a consistency check that every Ranked participant is unit-observable or classified, and boards eligible for grouping (>= 4 identity units); every percentage names its denominator.
- **Normalized board:** structural **identity** is the set of champions in the committed art manifest's list (roster units with cost 1-5 and at least one trait -- shop champions plus trait-bearing specials such as the Lux forms). Roster summons, jungle camps, anvils and the training dummy are excluded even where the roster lists them at cost 1; duplicate copies count once; an id the roster does not know is kept under its raw id (shown `UNRESOLVED: <id>`) when its stored cost is 1-5. Champion membership is the fundamental board identity: strategies A and B group from champion structure alone, and strategy C additionally uses completed-item count, star/cost splash weighting and active traits as predeclared secondary structural signals (see below). Placement and every other outcome, and the current carry qualification, are post-group analytics only and never influence grouping.
- **Strategies** (predeclared; every threshold in `ArchetypeConfig` is printed with the results): **A. structural baseline** (the control: champion-set similarity only), **B. flex-tolerant** (A plus a structural variant merge for flex slots and board size: cores -- units on >= 75% of a variant's boards -- sharing >= max(3, 60% of the larger core) with at most one core swap; each tentative merge is kept only if the *merged* group stays coherent -- its core keeps >= 5 units or >= 60% of the smaller pre-merge core, and every member board is still >= tau similar to the merged profile -- so a chain of individually acceptable merges cannot erode the core and pull in a different comp), **C. structure-aware** (units with >= 2 completed items x2, un-itemized 1-star 4/5-cost units x0.5, active traits 25% of similarity, and B's merge, with the same merged-result checks, restricted so a swapped *itemized* unit keeps groups apart; the one exception is a variant whose core is another's full core of >= 5 units plus exactly one unit, e.g. an itemized splash, which may merge). The report prints how many tentative merges each check rejected (`merge_checks`). It also measures every tentative merge that reaches the similarity check, without influencing any decision (`merge_diagnostics`, research only): each board's similarity to its own side's profile before the merge and to the merged profile after it, how many boards fall below tau and from which side, the weakest board's pre/post similarity and shortfall, cores and core overlap -- aggregated into fixed buckets (boards below tau, distance from tau, pre-merge margin, change, core overlap, merged size) for rejected and accepted merges, plus a small deterministic sample of attempts selected by structure and threshold only (never placement). Only unit ids and anonymous observation ids appear. A report-only **shadow evaluation** (`merge_diagnostics.shadow`, printed in full in the Markdown) then asks, for each of those attempts, whether five predeclared candidate rules would have accepted it: S0 (the current rule; must match the actual decision), S1 (<= 1% of the merged boards below tau, all >= tau - 0.10), S2 (S1 plus no larger-side board below tau and <= 10% of the smaller side below tau), S3 (S2 plus core overlap >= 0.8, identical cores or C's splash exception) and S4 (post-merge p10 >= tau, median >= tau + 0.05, all >= tau - 0.10). It reports acceptances, the attempts each candidate would recover, danger flags (whole / majority of the smaller side below tau, core overlap < 0.6, failures on both sides, a board below tau - 0.10), descriptive family proxies, and what each added constraint removes. This is a one-step counterfactual on the actual merge trajectory: no grouping decision reads it, and what a candidate would build as the real rule is not simulated.
- **Experimental S2 strategies (research only, not production-approved):** validation run #5's one-step shadow evaluation found S2 the most promising candidate, but a one-step counterfactual cannot show what S2 builds when its merges really happen. `B_S2_experimental` and `C_S2_experimental` therefore run it for real: each reuses the exact variants of its control (B / C -- the leader pass and refinement depend only on the board vectors and the config) and repeats that control's merge -- same candidate pairs, pairwise core checks, C's restriction and splash exception, and merged-core check -- with ONE difference, the merged-result similarity check (`Strategy.similarity_rule`): instead of every board >= tau against the merged profile, S2 requires (against the tentative merged profile; sides by pre-merge board count, ties to the lower group id) no larger-side board below tau, at most 10% of the smaller side below tau, at most 1% of the merged group below tau, and every board >= tau - 0.10 (`s2_conditions`, also used by the shadow S2; thresholds frozen before run #5, not tuned). Accepted merges change the groups, so later candidates follow the S2 trajectory. A, B and C are unchanged controls (their report sections are identical with or without the experimental strategies). Each experimental section adds: a table against its control (groups, assigned boards, merges, rejections, near-duplicates, within-group similarity, final members below tau, size bands -- structural outcomes, not quality judgments), every accepted merge measured when accepted, every grouped board measured against its FINAL group after all merges (below-tau members; groups with > 1% / 5% / 10% below tau; core drift), and bounded deterministic review samples found by structure only (Aphelios/Nidalee, Summoner-like, Veigar / Caitlyn / Kha'Zix reroll-like families; the shells of run #2's mixed groups; chaining suspects). Results on the same 18.3 population are model-development evidence, not independent validation; adoption would need another patch window or held-out data. Similarity is Ruzicka (weighted Jaccard) between a board and a group's mean member vector; grouping is a deterministic leader pass in a purely structural order (more complete boards first, then each board's own structural vector -- never placement or any other outcome, so group membership is invariant to outcome; placement is only summarized afterwards) with profile refinement, and a candidate-prune heuristic that the report audits against brute force.
- **Recursive lock-in and merge-order instrumentation (S2 strategies only, REPORT ONLY; S2 itself is unchanged):** each experimental section adds `experimental_s2.recursive_lock_in` (printed in full in the Markdown). It answers whether tails that an earlier accepted S2 merge let in later block otherwise plausible merges through Condition 1 (no larger-side board below tau). Definitions:
  - **Historically admitted tail.** A board that was below tau against the tentative merged profile of an *accepted* S2 merge when that merge was accepted. The harness keeps its anonymous observation id, attempt, side (a/b, larger/smaller), similarity and tau. The board stays historical through later merges, and a board that only falls below tau later, without an accepted merge admitting it below tau, is never historical. Only the accepted merge's tentative merged profile decides. Pre-merge similarity, a board's similarity to its own side's or original variant's profile, is a separate diagnostic: being below tau there neither qualifies nor disqualifies a board.
  - **Similarity-rejection attribution.** The denominator is every S2 similarity rejection: merges that passed the pairwise core checks and the merged-core check and were then rejected by S2. Merged-core rejections are excluded. Reported per rejection:
    - which S2 conditions failed, as multi-label counts and as exact failure sets;
    - fixed categories (only the smaller-side condition failed, only the merged-tail condition failed, the floor failed, several conditions failed, ...);
    - a mutually exclusive Condition-1 attribution: *all historical-tail*, *partial*, *no historical-tail* or *Condition 1 did not fail*.

    A historical tail now at or above tau is not counted as a blocker.
  - **`historical_tail_removal_counterfactual`.** For a rejection whose current larger side holds historical tails, exactly those boards are removed and no others, with no search for a passing subset. The harness then recomputes the merged profile of the remaining larger side plus the unchanged smaller side and re-evaluates the unchanged S2 conditions, re-deriving sides by board count. It reports pass/fail, failed conditions before and after, removed boards, merged size, minimum similarity and below-tau counts. It changes no grouping decision and is a diagnostic, not evidence that a merge should happen.
  - **Tail trajectory.** Later attempts that involve historical tails, their outcomes, Condition-1 rejections among them, and counterfactual recoveries. Each accepted merge in the trajectory also records how many boards it admitted below tau.
  - **Merge-order sensitivity.** In-memory replays of the same S2 merge on the same variants (`ORDER_REPLAYS`):
    - **baseline order:** must reproduce the real grouping, and the report checks this;
    - **reversed order:** highest group ids first among tied candidates;
    - **three fixed seeds:** pseudo-random permutations of the tied candidates.

    They change only which of several candidates tied at the same highest core overlap is judged first; a lower-overlap candidate is never judged while a valid higher-overlap one exists. Each replay reports:
    - groups, assigned boards, merges and rejections;
    - Condition-1 and historical-tail counts, counterfactual recoveries and final below-tau members;
    - tiny-core large groups and tie counts;
    - pairwise co-membership disagreement with the real grouping, computed from group intersections over eligible boards and given against all pairs and against pairs together in either partition;
    - family and regression anchor lookups compared with the baseline.

    The real S2 grouping stays canonical.

  Patch-window results on a population that already shaped the model, such as 18.3, are development evidence, not independent validation.
- **Output:** a human-readable Markdown report printed to the job log and saved with a JSON summary and an anonymized membership CSV (`archetypes_<window>_report.md`, `_report.json`, `_membership.csv`). Per strategy: global grouping statistics (groups, assigned/ungrouped boards, size distribution and bands 2-4 / 5-9 / 10-29 / 30-59 / 60-189 / 190+, within-group and nearest-other-group similarity, convergence, prune audit), a unit-presence histogram for core/flex diagnostics, a niche test by size band, candidates for manual review (listed by size, not performance), and a deterministic validation sample of real groups (largest, medium, low-frequency, 1/2/3-cost reroll-looking, high-cost, most flexible, most low-placement) with core candidates, other common units, presence distribution, traits, itemized / carry-qualified / defensive-item units, Thief's Gloves, Adaptive Helm, UNKNOWN-intent and unmapped items, stars, performance with a Top 4 interval, item sets (exact sets and 2-item cores, flagged `THIEFS_GLOVES_AMBIGUOUS`, `UNMAPPED_INTENT_ID`, `NO_CANONICAL_NAME`, `SMALL_SAMPLE`; never called best) and member boards. It also lists the same heavily itemized champion in structurally different groups side by side, similar shells with different primary itemized units, and the statistical caveats (survivorship, incomplete boards, 3-star conditioning, same-lobby non-independence, selected NA ladder population, provenance imbalance, small samples, multiple comparisons, item-set selection bias, no causality). No corrections are applied.
- **Read-only by construction:** `Database.open_existing` only; a writable connection is refused and on Postgres the server must report `transaction_read_only = on` before any analysis. SELECTs of ids, placements, stars, items and traits only; raw payloads are read solely by `tftlab.validate`'s source-empty classification and never exported. Artifacts hold no secrets, PUUIDs, Riot IDs or match ids (observations are numbered anonymously). No Riot request, no CommunityDragon request (committed metadata only), no production write.

- **Execution, progress and partial results:** the database is used only to load the population into memory (`load_inputs`) and is **closed before any clustering**; the analysis (`analyze`) runs on in-memory data only. Progress lines with elapsed time (`[progress +12.3s] ...`: connection, read-only check, loading, connection closed, normalization, each strategy's leader pass / refinement passes / merge / prune audit / summaries, report generation) are printed and appended to `archetypes_<window>_progress.log`. Each report section is printed as soon as its phase finishes and saved once, atomically, to `partial/NN_<phase>.md|.json`, with `partial/00_STATUS.md|.json` listing completed and pending phases; every partial file is labelled **PARTIAL / INCOMPLETE RESEARCH RESULT**. The final `archetypes_<window>_report.md|.json` and membership CSV are written only when every phase finished (then `partial/` is removed), and the log ends with the line `RESEARCH REPORT COMPLETE`. Stale outputs of an earlier run are cleared first.
- **Report modes (`--report-mode`, default `full`):**
  - **`full`** is the complete report. It is unchanged, byte for byte.
  - **`s2-diagnostics`** is a focused research run for the experimental S2 strategies on the same complete population (never a sample):
    - Strategy A is not computed. Nothing S2 reports depends on it.
    - B and C are computed only as what `B_S2` and `C_S2` need: their variants (the leader pass and refinement that S2 reuses) and their grouping, metrics and group summaries (the S2 comparison with its control, and the control columns of the family/regression review). Their own report sections are omitted.
    - `B_S2` and `C_S2` run through the same code as in `full`, with every diagnostic (recursive lock-in, order replays, family and regression anchors), so their sections are identical. Tests compare them line for line.
    - Run order is B → B_S2 → C → C_S2, so B_S2's results are saved as a partial result before C's long phase.
    - The output says it is a focused run, and the files are named `archetypes_<window>_s2-diagnostics_*`.
  - **`s2-stability-b`** and **`s2-stability-c`** are REPORT-ONLY stability research runs for `B_S2_experimental` and `C_S2_experimental` (production run #9 follow-up). Canonical S2 (its thresholds, Condition 1, tau, tail, core and merge rules, and its tie behaviour) is unchanged and nothing is promoted:
    - The strategy's variants (its control's leader pass and refinement) are computed **once** and every merge below reuses them. The canonical S2 merge is run first and reported.
    - **Current tie behaviour:** the S2 merge queue judges the valid candidate with the highest core-overlap score, `|shared core| / |core union|`. That is an exact ratio of small integers, so equal ratios tie exactly (run #9: 26,600 of B_S2's 27,027 judged steps had a tie). Among tied candidates the lowest `(lo, hi)` group-id pair goes first. Ids come from size order and a merged group keeps the lower id, so ties currently favour the largest original variants.
    - **Research tie rules** (structural and outcome-free; they rank only candidates tied at the same highest core overlap): `centroid_similarity` (the two current group profiles most alike), `min_member_similarity` (the tentative merged group whose weakest member fits its profile best) and `mean_member_similarity` (the highest mean member fit). They never read placement, Top 4, performance, popularity, later merges, labels or the anchors.
    - Every rule, including the current one, is replayed under five deterministic tie orders (baseline ids, reversed ids, three fixed seeds). The report gives pairwise partition stability across the replay pairs plus merges, groups, grouped and ungrouped boards, below-tau counts, groups over 1/5/10%, tiny-core large groups, within-group and nearest-other similarity, family/regression anchor stability (diagnostics only), lock-in counts and runtime.
    - **`s2-stability-c` also studies C's convergence.** C's refinement is continued past the production cap (10, unchanged) up to a research-only cap (`--research-max-refine-iterations`, default 20). A repeated partition (a cycle) stops the run and is reported, and convergence is never forced. It reports the extra moves, whether refinement converged, and the partition disagreement between the iteration-10 and research variants. It runs the current rule and every research rule on both variant bases.
    - Each rule's results are saved as a partial result as soon as they finish. Files are named `archetypes_<window>_s2-stability-b_*` / `_s2-stability-c_*`.
  - **`s2-candidate-c-centroid-v1`** evaluates the versioned composition-family candidate **`C_S2_centroid_candidate_v1`** (`FamilyCandidate`, version 1), chosen after production runs #10 and #11. It is EXPERIMENTAL and development evidence only: nothing serves, publishes or labels its families, and Discover, Champion Investigation and the APIs never read it. It is additive: A/B/C, canonical `B_S2`/`C_S2`, C's 10-pass production cap and every other mode are unchanged (a test pins their output to `main`'s, byte for byte).
    - **Definition:** `C_S2_experimental` unchanged except for two things:
      - **Refinement:** C's refinement continues until the **existing** convergence criterion holds: a pass moves at most `max(1, floor(convergence_moved_share × eligible boards))` boards. The hard cap is **20 passes**. Run #11 converged after 14 passes; 20 leaves 6 passes of margin, is the research cap under which that convergence was observed, and fits the workflow budget.
      - **Tie-break:** the S2 merge priority is still core overlap, `|shared core| / |core union|`. Only among pairs tied at the same highest core overlap, the pair whose two current group mean profiles are most similar goes first (`centroid_similarity`, the strategy's own similarity). Pairs whose centroid similarity is also exactly equal go to the lowest group-id pair, as in canonical S2. A lower-overlap pair can never go first.
    - **Inputs:** board structure only. Never placement, Top 4, win rate, item or carry performance, popularity, later merges, labels or the named anchors. The anchors are reported as diagnostics only.
    - **Failure is visible.** The candidate is **NOT CONSTRUCTED** if any of these happen: the hard cap is reached without convergence, a refinement cycle occurs (a repeated partition), or the result is invalid (a variant split across families, a board lost, an S2 decision that disagrees with the S2 rule). When that happens:
      - the report says so prominently;
      - no families or membership rows are emitted;
      - `tftlab archetype-report` exits with code 3 after writing the report, so the workflow run fails while still uploading the artifact.
    - **Runs once:** variants are computed once. The candidate's S2 merge runs once. Canonical `C_S2_experimental` is merged once, on the production-cap (10-pass) state of the same refinement run, for comparison. There are no tie-order replays.
    - **Report contents:**
      - **provenance:** algorithm and version, source strategy, convergence rule, hard cap, iterations used, merge criteria, tau and S2 thresholds, config, balance window, population counts, and the commit SHA / run id from the GitHub Actions environment;
      - **refinement:** moves per pass, convergence threshold, converged or not, variant count and sizes, boards in and out of variants;
      - **families:** merges, groups, grouped and ungrouped boards with their denominators, size distribution, below-tau counts and groups over 1/5/10% below tau, large tiny-core groups, within-family and nearest-other-family similarity, near-duplicates, recursive lock-in counts and tie statistics;
      - **largest families:** each record carries the candidate name, version and balance window;
      - **comparison with canonical C_S2:** metrics side by side with differences (candidate minus canonical, never labelled better or worse), partition disagreement for variants and families, and family/regression anchor changes;
      - **fingerprints:** group-id-independent fingerprints of both partitions.
    - Files are named `archetypes_<window>_s2-candidate-c-centroid-v1_*`.
- **Exact speedups (results unchanged):** production run #8 (66,682 eligible boards) spent about 137 minutes on strategy A alone: a ~18-minute leader pass plus six ~19-minute refinement passes. Profiling showed about 90% of that time in `ruzicka`.
  - The grouping hot loops (leader pass, refinement, prune audit, merges, global metrics, final-group diagnostics) now use `similarity_sorted` on key-sorted vectors built once. It performs the same additions in the same order as `similarity`, so results are bit-identical, and it is about 3× faster.
  - Each refinement pass, whose profiles are fixed within the pass, can run in several forked processes (`--workers`, default every available CPU) and is reassembled in order.
  - The argmax over groups (leader pass, refinement, prune audit, nearest-other metric) first screens every candidate with a mathematically equal, cheaper formula and then re-evaluates exactly, with `similarity_sorted`, every candidate within `1e-9` of the best screened value, keeping the same lowest-id tie-break. The chosen group and the similarity values are therefore unchanged. This roughly halves the argmax time on large populations.
  - The full report is byte-identical to the previous implementation on the fixtures and on synthetic 4,000-board populations, with 1 or 4 workers.
- **Runtime benchmark (synthetic, local):** `python scripts/benchmarks/archetype_runtime_benchmark.py --boards N --diversity {low,medium,high,extreme} [--strategies ...] [--mode full|s2-diagnostics|s2-stability-b|s2-stability-c|s2-candidate-c-centroid-v1] [--workers N]` times the unchanged harness on in-memory synthetic boards of increasing structural diversity (speed only; says nothing about clustering quality).

`.github/workflows/read-only-archetype-report.yml` runs it against production on **manual dispatch only**, from `main` only, with no schedule, trigger chaining or retry (same preflight as the other production workflows, plus a format check on the `balance_window` input, default `18.3`, and a `report_mode` choice, `full` (default), `s2-diagnostics`, `s2-stability-b`, `s2-stability-c` or `s2-candidate-c-centroid-v1`, validated before checkout; no Riot key). The job budget is 180 minutes and the analysis step stops at 170, so the `archetype-report` artifact (final report, or the labelled partial files and progress log) is uploaded with `if: always()` even when the analysis fails or times out -- the job itself still ends failed. Nothing is committed or written to Postgres. Tests use synthetic fixtures and check the harness's mechanics only -- they do not validate real clustering quality.
