# TheoryLabs Roadmap

This is the project's direction document. It exists so development stays aligned with the mission instead of drifting toward whichever experiment or feature is newest.

- **Checkpoint:** `main` at `0250c6b` (after PR #48; prepared Discovery analytics deployed and production-verified), 2026-09-30.
- **Active review:** no open pull requests at this checkpoint.
- **Scope:** direction and priorities only. How things work lives in the [README](README.md) and the code. When this file and the repository disagree about what is implemented, the repository wins; fix this file.

## Mission

TheoryLabs should answer:

> What strong, realistically playable compositions could we build from the available champions, items, traits, augments and statistical evidence, including underexplored strategies?

It works upward from game rules and component-level evidence rather than reproducing published tier lists. The core investigation path is:

**champion → star level → items → partners → cores → traits → composition families/variants → practical alternatives**

Five things must always stay distinguishable:

- observed evidence;
- predictions and inference;
- popularity;
- novelty;
- sample size and confidence.

## Operating principle

**Data collection, analytics/research, and website/product development are parallel workstreams.** Research does not automatically block product work, and product work does not wait for every research question to close.

**Source-first data rule:** before inventing a metric, label, score or abstraction, check whether Riot or verified static data already supplies the information. Preserve and use source fields when they answer the player question. Derive only when the source does not answer it, document why the derivation is needed, and keep the derived result visibly distinct from observed source data. Never replace a useful source field with a lossy convenience abstraction.

Before starting substantial new work, map it to a milestone below or to a concrete blocker. If it fits neither, update this roadmap deliberately first (see the task-selection gate).

## Status labels

| Label | Meaning |
|---|---|
| **IMPLEMENTED** | On `main`, tested, and in use. |
| **ACCEPTED/CURRENT** | The agreed approach or active next milestone. It may not be built yet. |
| **EXPERIMENTAL** | On `main` as research only. It is not served to users, not production-approved and not independently validated. |
| **FUTURE** | Planned direction. Nothing is committed to yet. |

## Current checkpoint

### Foundation (IMPLEMENTED)

Details are in the README.

- **Riot TFT ingestion** (`tftlab ingest-riot`, `live-ingest.yml`): scheduled every 6 hours (bounded mode), manually dispatched, and guarded by the shared production concurrency group.
- **Production hosting/data path:** Render web/API in **Ohio** reads Neon Postgres in **AWS us-east-2 (Ohio)**. GitHub production ingestion, diagnostics and encrypted backups target the same Neon database; the former Render Postgres database is retained temporarily as rollback.
- **Raw match storage** (SQLite or Postgres), with provenance:
  - the `seed_samples` rotation ledger;
  - `match_discoveries`;
  - per-wave `ingest_runs`.
- **Deduplication** before any match-detail fetch, plus idempotent provenance.
- **Board normalization**, balance-window/patch resolution and validation (`tftlab.validate`).
- **Bounded and maximum collection modes**:
  - explicit request, time and match-fetch budgets;
  - proactive rate-limit pacing;
  - fatal-error abort when the key expires.
- **Sampling and seed rotation** across the Challenger, Grandmaster, Master, Diamond and Platinum cohorts.
- **Carry-centric analytics:** commitment, partners, item packages, trait associations, Discovery / Opportunity Score. Riot final-board trait fields (`num_units`, `style`, `tier_current`, `tier_total`) are preserved in storage; player-facing trait analysis uses Riot `num_units` for observed unit counts and does not reinterpret `tier_current` as a breakpoint.
- **Web prototype:** Discovery dashboard, "My Experiments" notebook and Comp Scout. It runs on a read-only database connection.
- **Read-only research reports**, each run by manual dispatch: data diagnostics, Discovery and board archetypes.

Riot access currently uses a **development key that must be regenerated regularly** by hand (see "Riot development key operations" in the README). This is temporary infrastructure.

### Composition-family research (EXPERIMENTAL)

- **Checkpoint:** validation run #6, recursive S2 on the Patch 18.3 development population. Classification **R6-B: promising, but needs a specific structural investigation/correction.**
- **What run #6 showed:**
  - S2 kept groups reasonably coherent.
  - The earlier severe composition-smearing regressions did not return.
  - Fragmentation improved only slightly: B went from 1,429 to 1,422 groups, and C from 1,561 to 1,556.
  - Known large merges generally kept strong cores.
- **Open problem: likely recursive lock-in / order dependence.**
  - Once S2 admits a below-threshold tail, Condition 1 (no below-tau boards on the larger side) may let that admitted tail block a later, otherwise plausible merge.
  - This is **inferred from family traces, not yet quantified.** The instrumentation to quantify it (and tie-order dependence) is implemented; the production research run has not been made yet.
- **Decision: do not modify S2 Condition 1 yet.** Keep the current S2 (`B_S2_experimental`, `C_S2_experimental`) as an experimental checkpoint. It is not production-ready and not independently validated.
- **Patch 18.3 is development data.** It has shaped this work repeatedly, so further experiments on it are development evidence, not untouched validation.

## Tracks

### Track 1: Continuous data collection

- **Goal:** grow the historical dataset automatically instead of by manual dispatch.
- **Scheduled collection code (IMPLEMENTED):** `live-ingest.yml` is on `main` with a six-hour schedule and fixed conservative settings. It uses **bounded** mode, not maximum: both production maximum-mode runs failed with 401 during deep ladder enumeration, so maximum stays manual-only until that is diagnosed. 401/403 now fail a bounded run with no ledger progress, as in maximum mode.
- **Operational verification (IMPLEMENTED):** a scheduled production run was observed successfully on 2026-09-30 (`36718342282`), including persisted ingest provenance. After the Neon cutover, an owner-only bounded smoke also succeeded against Neon (`36763413989`): 3 Challenger seeds × 3 histories, 10/10 Riot requests returned 200, 4 new ranked matches were inserted, validation passed and Discovery smoke passed.
- **Next (ACCEPTED/CURRENT):** monitor scheduled-run yield/error/storage telemetry over multiple runs, then diagnose the maximum-mode deep-ladder 401 before any change to scheduled bounded mode. Do not build a new ingestion system.
- **Zero-cost local collector (IMPLEMENTED; first real local run PENDING):**
  - `tftlab local-collect` turns a personal computer into the collector, using one SQLite file (`data/local/theorylabs.sqlite3`). There is no Postgres, Docker, Neon or other cloud database.
  - The run: preflight (local target, disk, current trusted window, Riot key, CommunityDragon), a consistent backup with retention, the existing bounded `ingest_ladder` (15/15/20/25/25 × 10, current trusted window, `10:10` ceiling), validation, then Discovery preparation for the current window only. It ends with a plain-language report.
  - Also: `local-status` (offline), `local-snapshot` (verified sanitized export), `local-init`, `local-set-key`, and launchers in `scripts/`. Guide: `docs/local-collector.md`.
  - It never reads `DATABASE_URL`, refuses database URLs, and has no scheduling.
- **Patch 18.4 window (REGISTERED):** `2026-10-08T07:00:00Z` .. `2026-10-20T00:00:00Z` (exclusive), from Riot's patch schedule. 18.4 is scheduled for 2026-10-07 PT and 18.5 for 2026-10-21 PT.
  - The 18.3 → 18.4 transition (`2026-10-06T00:00Z` .. `2026-10-08T07:00Z`) stays unresolved.
  - `--current-trusted-window` collection works again from 2026-10-08T07:00Z, locally against SQLite. The scheduled GitHub ingest uses Neon, so disable it or keep it disabled if Neon should not be written.
  - 18.5 needs its own reviewed window before 2026-10-20.
- **Preserve:**
  - request, time and match-fetch budgets;
  - concurrency protection;
  - deduplication;
  - validation;
  - provenance;
  - safe failure when the development key expires;
  - truthful sampling-ledger behaviour.
- **Measure:** new-match yield, duplicate rate, cohort and rank coverage, collection lag, Riot 429 and error rates, storage growth.
- **Do not** maximize request volume just because capacity exists.

### Track 2: Composition-family research

- **Goal:** recognize when final boards are variants of the same composition, without merging unrelated strategies.
- **Current checkpoint (EXPERIMENTAL):** run #6, recursive S2, classified R6-B.
- **Immediate milestone (instrumentation IMPLEMENTED; production research run PENDING):** the report now instruments **unchanged** S2 (`experimental_s2.recursive_lock_in`: historically admitted tails, Condition-1 attribution of every S2 similarity rejection, the historical-tail removal counterfactual, the tail trajectory, and report-only tie-order replays with partition disagreement and family/regression anchors). Next: run the read-only archetype report from `main` on Patch 18.3 (development evidence) and review the counts. The hypothesis is **not confirmed** until that run is reviewed.
  - **Run #8 (full report, Patch 18.3, 66,682 eligible boards) timed out.** Strategy A alone took about 137 minutes, and B was recomputing A's identical variants when the 170-minute limit hit, so the S2 strategies were never reached.
  - **The focused `s2-diagnostics` report mode is IMPLEMENTED.** It skips A and computes B and C only as the S2 variant sources and baselines.
  - **Exact speedups are IMPLEMENTED:** bit-identical sorted-vector similarity and multi-process refinement.
  - **Run #9 (`s2-diagnostics`, Patch 18.3, run `37087299036`, 72,068 eligible boards) COMPLETED** in about 150 of the 170 minutes. B_S2 made 571 merges and C_S2 1,097. It raised two structural concerns:
    - **C refinement did not converge.** It stopped at the 10-pass cap, with moves still falling: 18,255 → … → 338 → 205. B converged at pass 7.
    - **S2 membership is sensitive to tie order.** Core overlap is an exact small-integer ratio, so ties are the norm: 26,600 of B_S2's 27,027 judged steps had one. The current rule (lowest group ids) is arbitrary, and the five deterministic replays gave B 560–571 and C 1,097–1,104 merges.
  - **Stability research modes are IMPLEMENTED (report-only).** `s2-stability-b` and `s2-stability-c` compare the current tie behaviour with three structural, outcome-free tie rules (`centroid_similarity`, `min_member_similarity`, `mean_member_similarity`), each under five tie orders, on variants computed once. `s2-stability-c` also continues C's refinement past the production cap up to a research cap of 20; the production cap is unchanged. Canonical S2 and the default report are unchanged, byte for byte.
  - **Synthetic results so far** (development evidence on generated boards, not production evidence):
    - Every research rule cut the order dependence sharply, to one or two distinct partitions against the current rule's five.
    - Any continuous tie key does that by construction. A rule must therefore also be judged on quality, anchors and robustness to the upstream variant change, which needs the production runs.
  - **Run #10 (`s2-stability-b`, run `37218019474`, 87,210 eligible boards) COMPLETED.** The current tie rule gave 5 distinct partitions over 5 replay orders: up to 27,723 boards changed group, with a maximum disagreement of 0.183 of pairs together in either.
    - `centroid_similarity`: at most 17 boards changed between orders (3 distinct partitions), with structural quality and every named anchor stable.
  - **Run #11 (`s2-stability-c`, run `37224559749`, 87,210 eligible boards) COMPLETED** in about 168 of the 170 minutes.
    - **Convergence:** C's refinement did not cycle and converged after **14** passes (threshold 87 moved boards); the 10-pass cap stops 4 passes early. Iteration-10 and converged variants differ for 17,268 boards (0.0112 of pairs together in either).
    - **Current tie rule on the converged base:** still 5 distinct partitions, with up to 27,702 boards changed (0.2905).
    - **Structural rules:** all three gave 1 partition on the converged base. `centroid_similarity` gave 0 boards changed, stable anchors, no large tiny-core group and no group over the tail thresholds.
  - **Candidate v1 IMPLEMENTED (EXPERIMENTAL, not user-facing):** `C_S2_centroid_candidate_v1` combines C variants refined to convergence (hard cap 20) with S2 using `centroid_similarity` as the tie-break among equal core overlaps. It is evaluated by report mode `s2-candidate-c-centroid-v1`. Canonical S2, C's production cap and every existing mode are unchanged.
  - **Next (PENDING, owner dispatch):** run `s2-candidate-c-centroid-v1` from `main` on Patch 18.3 and review it. Patch 18.3 chose the candidate, so that run is development evidence. After that:
    - freeze v1;
    - test it on a held-out patch/window before any family is shown to users;
    - until then, keep families research-only: no site, API, Discover or Champion Investigation use, and no Established/Off-meta labels.

  The instrumentation measures:
  - how many later merge rejections are caused by tails S2 admitted earlier;
  - which side (larger or smaller) caused each of those rejections;
  - whether each blocked merge would have passed without the previously admitted tail members;
  - how sensitive the result is to merge order;
  - the effect on the known families: Aphelios/Nidalee, Summoner, Veigar, Caitlyn and Kha'Zix;
  - whether the earlier bad mixed-shell regressions stay absent.
- **Only after measuring** decide whether Condition 1 needs a structural correction. Then freeze the rule and test it on a held-out patch/window.
- This track runs in parallel. It does not block website development.

### Track 3: Champion intelligence

- **Goal:** answer "I want this champion to be my carry. What can I realistically build around it, and why?"
- **Evidence path:** champion → star level → items → partners → cores → traits → composition families/variants → practical alternatives.
- **Status:**
  - **IMPLEMENTED:** carry commitment, star-level hit/miss evidence, item packages/pairs, partner associations, and storage of Riot final-board trait fields.
  - **IMPLEMENTED:** source-faithful trait intelligence. Trait evidence is grouped by the observed unit count Riot Match-V1 reports (`num_units`, "Solar · 4 units"), plus a separate active-trait share. A trait counts as active when `tier_current` is 1 or more.
    - `tier_current` is an ordinal. It is never substituted for a player-facing breakpoint or unit count.
    - Canonical threshold names ("the 4-unit tier") need verified static trait metadata, which is not stored yet, so none are inferred.
  - **IMPLEMENTED:** two source-semantics corrections, applied in shared analytics so Champion Investigation, Discovery and the APIs agree. Raw Match-V1 data is unchanged.
    - **Thief's Gloves (corrected):** `tftlab.itemization` splits a unit's items into equipped and generated. On a TG holder (normal or Radiant, ids from item metadata) only the gloves are equipped. Its two rolls never become fixed-item, pair or package evidence, and a TG holder is never a carry observation (Python and SQL rules match).
    - **Lucky Gloves (deferred):** CommunityDragon lists the augments `DA_LuckyGloves` / `DA_LuckyGlovesPlus`, but the strings Match-V1 stores in participant `augments` have not been verified against real match data. Lucky Gloves boards are therefore treated like normal TG. Verify the augment ids from stored production `augments_json` (read-only) before adding the exception, and even then keep the rolls out of fixed-build evidence.
    - **Intrinsic/singleton traits (implemented):** the roster now stores each champion's canonical trait ids from CommunityDragon. A trait only one shop champion has, and that no verified emblem or trait item can add (`trait_items` in the roster snapshot, from CommunityDragon `associatedTraits` and emblem names), is that champion's intrinsic trait (Set 18: Kog'Maw → Caustic, Alune → Attuned and seven others). That classification is API-only (`intrinsic_traits`, not shown on the page) and fails closed on any current-set emblem that links to no trait; Set 18's two "Phantom Emblem" items do, so currently nothing is classified intrinsic. Trait evidence uses a separate, membership-only rule: a carry's own **singleton-provider** trait (no other shop champion naturally has it) at its guaranteed 1 unit is never ranked, while the same trait at 2+ units stays as count-level evidence (it may be extendable). Multi-unit traits and other champions' unique traits stay in.
  - **IMPLEMENTED:** a practical **"How to play {champion}"** layer, now the default view of Champion Investigation (tabs: How to play | Items | Teammates | Traits | Evidence; the dense statistics moved to the Evidence tab, nothing removed). It is built from the existing evidence rows (no new query or statistic): a recipe direction **derived** from the CommunityDragon recipes of the common completed items (`item_stats.json` now keeps each item's `composition`, verified against the live feed), common items/pairs/full builds (10+ carry boards, most used first; labelled by that rule, not as strong or best), up to four teammates by frequency, a few trait directions by the unit count Riot reported most often (never a one-champion trait at its single unit, whether the carry's or a teammate's), the existing 3★ hit/miss split with its sample state, and one cautious evidence sentence. It does not claim observed opening components, augments, positioning, leveling, mechanics or causes. The accepted brief it implements:
    - what completed items and item combinations make the champion worth considering;
    - what item **components** are useful starting directions, derived transparently from the recipes of common completed-item lines (not falsely presented as observed opening components, because Match-V1 has no component-acquisition timeline);
    - which champions/recurring cores and trait counts are most useful around the carry;
    - what star target appears important when the sample supports that comparison;
    - augments and positioning only when a verified source actually supports those claims.
    Use champion/item/trait art heavily so the section is scannable. Keep the practical recommendation distinct from the deeper observed evidence below it.
  - **IMPLEMENTED:** recurring 2–4 unit core evidence (`tftlab.analytics.cores`). The 2-unit layer is the existing individual teammate evidence; the new observed 3–4 unit packages (carry + 2–3 teammates counted only where all were on the same final board, shop champions only, 10+ boards, ordered by recurrence, never by results, no core score, no summed pair evidence) appear in How to play ("Recurring cores") and the Teammates tab. They are subsets of final boards, not compositions, and bridge individual teammates toward composition families. Experimental S2 archetype research stays separate and is not promoted by this.
  - **NEXT:** composition-family/variant evidence once that research is trustworthy.
  - **FUTURE:** practical alternatives.
- **Rules:**
  - Use Riot/source data directly when it already answers the question; derived systems must justify what additional question they answer.
  - Keep association separate from causation.
  - Keep observed statistics separate from inference and prediction.
  - Define denominators and preserve balance-window/population compatibility.

### Track 4: Website/product

- **Product loop (ACCEPTED/CURRENT):** keep the Champion Investigation vertical slice as the main player-facing refinement loop. Real production use has now shown that the page is visually clear but its top "How players carry with {champion}" prose is not actionable enough. That refinement, a practical "How to play {champion}" guidance layer backed by the same source-faithful evidence with inline item/champion/trait art, is now IMPLEMENTED as the page's default tab, and recurring 3–4 unit cores are IMPLEMENTED on top of it; composition-family/variant evidence is next once trustworthy.
- **It should eventually present:**
  - star-level and carry evidence;
  - itemization;
  - partners;
  - recurring cores;
  - relevant traits;
  - observed composition families and variants;
  - practical alternatives;
  - sample size;
  - patch/population;
  - freshness;
  - confidence and evidence type;
  - observed vs inferred results.
- **Sequencing:** the slice uses whatever evidence is ready (Track 3). Families and variants arrive when Track 2 supports them. Once the evidence is trustworthy, the same composition/board objects should support both Champion Investigation and the main discovery experience.
- **First vertical slice (IMPLEMENTED):** `/champions` (pick a champion by name) and `/champions/<name>`, one data-driven template for every champion. It currently shows carry and 3★ hit/miss evidence, item builds and pairs, partners, trait associations, sample size, balance window, freshness and observed evidence. Recurring 3–4 unit cores are now IMPLEMENTED (above); still FUTURE: composition families/variants and practical alternatives.
- **Current refinement (IMPLEMENTED; PR #36):** Discovery/Champion request work was reduced, loading states were made explicit, player-facing terminology now says "carry boards", Champion Investigation leads with "How players carry with {champion}", observed evidence is separated from interpretation, special-item presentation is cleaner, and trait presentation uses Riot's observed final-board trait data instead of ordinal "breakpoint 1/2" labels.
- **Production acceptance (PARTIAL):** PR #36 is merged and deployed on the Ohio production service. Health, Champion directory, Kha'Zix Investigation and Discovery API smoke checks all passed against live Neon data. Player-facing cold/warm navigation timing is still the acceptance step for usability.

### Track 5: Analytics/API performance

- **Goal:** website requests should read inexpensive or prepared analytics rather than recomputing expensive research-style aggregates during navigation.
- **Production evidence:** before PR #36, user-observed live timings were roughly 1:41 for the initial Discovery page, ~0:50 for Working Notes, ~0:25 for the Champions directory and ~0:45–0:50 for a Champion Investigation. These are player-observed end-to-end timings, not controlled backend benchmarks, but they establish a real product bottleneck.
- **Current optimization (IMPLEMENTED; PR #36):** the picker uses a light champion-count query, champion detail narrows aggregation to the selected champion, Discovery supports server-side exact-cost filtering, evidence queries are batched, and the repeated window-wide Discovery population has a freshness-checked in-process cache. The change is merged and deployed; local synthetic benchmarks improved substantially, but production navigation still needs cold/warm measurement.
- **Production measurement (IMPLEMENTED):** a post-PR #36 warm production measurement from a GitHub-hosted runner to Render Ohio → Neon Ohio found approximate backend critical paths of **0.18s Working Notes**, **0.79s Champions**, **1.47s Kha'Zix Investigation**, and **9.30s initial Discovery**. These include runner-to-Render network time and are not browser-render timings, but they clearly isolate Discovery as the remaining request-time outlier.
- **Prepared Discovery analytics (IMPLEMENTED and production-verified; PR #48):** `tftlab prepare-discovery` precomputes every carry's Discovery candidate per balance window after each live ingest (and on manual dispatch), publishing each window atomically. Each run is keyed by balance window, analytics-code/data digest and a source-match fingerprint. `/api/discovery` serves a current run with an indexed read and filters it cheaply; a missing or stale run falls back to the previous live computation and is labelled as such, never served as current. The API is unchanged apart from an additive `prepared` field, and prepared responses are tested to be identical to live ones (SQLite and Postgres).
- **Measured locally (synthetic, live-shaped data; local database, so no network latency):** for the production request `costs=1,2,3&min_samples=10&top_n=5&limit=200` on 6,000 matches in one window, warm backend time fell from 1.73s to 0.021s on SQLite and from 1.25s to 0.043s on Postgres 16. Per request, rows fetched fell from about 191,000 to 29 (4 → 6 SQL statements, none touching the match tables' joins). Preparing that window took about 6 seconds offline. With 20,000 matches in one window (Postgres), the live request took 5.43s warm and fetched about 637,000 rows; the prepared request took 0.063s and fetched 29, and preparation took about 19 seconds. Live cost grows with window size; prepared reads do not. Benchmark: `scripts/benchmarks/discovery_read_benchmark.py`.
- **Production verification (IMPLEMENTED):** after PR #48 deployed, owner-only ingest smoke run `36805708440` inserted 5 new matches, validated live data, passed Discovery smoke, and published current prepared runs for 18.3/18.2b/18.2a. The production request `costs=1,2,3&min_samples=10&top_n=5&limit=200` returned `prepared.status=current` with 3,842 source matches; five warm GitHub-runner → Render Ohio → Neon Ohio requests measured **0.477s, 0.476s, 0.436s, 0.447s, 0.500s**, versus the prior ~9.30s warm backend critical path.
- **Target architecture (FUTURE):** Riot/raw data → normalization → offline/versioned prepared analytics → read APIs → website. Preserve provenance, balance-window/patch keys and freshness when precomputing.

### Track 6: Patch/set lifecycle

- **Goal:** a new TFT set starts a new logical dataset, not a TheoryLabs rewrite.
- **Requirements:**
  - Preserve historical sets and patches.
  - Ingest new static game content.
  - Validate changed champions, traits, items, augments and mechanics.
  - Start fresh match collection for the new set.
  - Keep dataset partitioning and provenance appropriate.
  - Support low-sample / insufficient-evidence states early in a set.
  - Keep frontend templates data-driven.
- **Do not hardcode:**
  - Set 18 champions, traits or assets into production architecture;
  - Set 18 research anchors into production architecture. For example, `FAMILY_ANCHORS` and `REGRESSION_ANCHORS` are research lookups only.
- Today the shipped roster, item-intent and art data are single-set files. Moving to per-set static data is part of this track.
- **Current static-data monitor issue (ACCEPTED/CURRENT):** the scheduled CommunityDragon live smoke is red because the current feed exposes both Set 18 `DA_Component_*` and standard `TFT_Item_*` component entries with duplicate display names (for example B.F. Sword and Chain Vest), while the integrity test assumes cached item display names are unique. Inspect and correct the selection/test assumption without hiding real source entries or weakening provenance.

### Track 7: Riot production readiness

- **Goal:** a functioning, policy-compliant product that is suitable for persistent Riot production API access.
- Development-key collection is temporary infrastructure.
- Policy-sensitive features (overlays, live assistance) need the current Riot policy checked before any implementation. The README's "Riot policy boundary" still applies: aggregate and post-game analysis only.
- **Zero-cost public site and Riot-facing pages (IMPLEMENTED; operator switch PENDING):**
  - **Data source:** the public site runs without any cloud database. `TFT_DATA_SOURCE=demo` gives synthetic data, labelled on every page; `snapshot` gives a bundled read-only snapshot of real matches. A configured but broken `DATABASE_URL` is still a loud 503, never fake data.
  - **Pages:** About, Methodology & data, Privacy and Terms pages; Riot's legal boilerplate in every footer; `/riot.txt` from `RIOT_SITE_VERIFICATION`.
  - **Positioning:** aggregate, historical study before or after play; explicitly not a live in-game assistant, overlay or scouting tool.
  - **Operator steps:** on the public Render service, delete `DATABASE_URL` and `RIOT_API_KEY`, then set `TFT_DATA_SOURCE=demo`.
- **Real public snapshot path (IMPLEMENTED; production snapshot PENDING operator review):**
  - `tftlab export-public-snapshot` builds a sanitized, verified SQLite snapshot: opaque match ids, no PUUIDs, raw payloads or collection ledger, and versioned provenance.
  - It is fail-closed: synthetic or demo data can never be exported or served as observed.
  - The manual workflow "Build public snapshot from encrypted backup" turns the 2026-10-05 encrypted backup (artifact expires 2026-10-13) into a downloadable snapshot artifact, without Neon.
  - Next steps: dispatch it, inspect the size and contents, choose the publication path (for example a GitHub Release asset), and only then switch Render to `TFT_DATA_SOURCE=snapshot`.
- **Before applying for a production key (PENDING):**
  - publish a real analytics snapshot (built by the owner's local data engine: `tftlab local-collect`, then `tftlab local-snapshot`) so the reviewed site shows observed evidence rather than demo data;
  - register the product, then set `RIOT_SITE_VERIFICATION` from the Developer Portal;
  - re-check the current Riot policies at submission time.

### Track 8: Composition discovery/theorycrafting (FUTURE; the long-term differentiator)

- **User constraints:** carry, star target, items, trait, augment, locked units.
- **Output:** realistically playable boards.
- **Recommendations should account for:**
  - board legality;
  - frontline, damage and utility;
  - item allocation;
  - trait breakpoints;
  - unit cost and availability;
  - special mechanics;
  - practical alternatives.
- **Evidence categories, kept distinct:**
  - **Established:** observed and common.
  - **Off-meta:** observed, but underexplored.
  - **Theorycrafted:** inferred, not observed.
- Never present predicted performance as observed performance.

### Track 9: Reference / game knowledge (FUTURE)

- **Goal:** give players one searchable, patch/set-aware place to look up the game knowledge they otherwise leave TheoryLabs to find elsewhere.
- **Product shape:** a top-level **Reference** tab, separate from Champion Investigation and composition analytics.
- **Candidate sections:**
  - **Shop odds:** level-by-level champion shop probabilities and related verified shop rules.
  - **Augments:** searchable augment database with names, descriptions, tiers/categories and icons where verified.
  - **Traits:** trait descriptions, canonical thresholds/breakpoints, icons and special mechanics from verified static metadata.
  - **Loot / orbs / encounters / Wisps:** current-set tables only when the underlying source and patch coverage are verified.
  - **Game timeline:** stage/round structure, PvE/carousel timing and other stable round-flow reference information.
  - **Patch schedule / current version context:** current patch, upcoming patch timing where officially published, and TheoryLabs balance-window context.
  - **Mechanics:** searchable set-specific rules and special systems that help explain champion/trait/item behavior.
- **UX direction:** icon-heavy, searchable, mobile-friendly, and cross-linked from Champion Investigation / composition pages so a player can inspect a mechanic without losing context.
- **Source rule:** prefer Riot and CommunityDragon. Community-maintained references may fill verified gaps, but every section must document source/provenance, version coverage and refresh needs before implementation. Do not copy third-party data, assets, code or rankings merely because a competitor exposes them.
- **Sequencing:** this track should not block the current Champion Intelligence cleanup/redesign. Build it incrementally once the source for each subsection is verified, reusing the same canonical static metadata wherever possible instead of creating a second parallel game-data system.

## Near-term priorities

**Completed:** validation run #6; Champion Investigation first vertical slice; scheduled ingestion operationally observed; Render Ohio → Neon Ohio production cutover with verified encrypted backup/rollback; PR #36 performance/loading/source-faithfulness refinement merged and deployed; GitHub-hosted SQLite/Postgres baseline restored and a permanent CI gate added; PR #48 prepared Discovery deployed and production-verified, reducing the previously ~9.30s warm Discovery path to roughly 0.44–0.50s in the verification run.

**Next:**

1. **Make Champion Investigation practically useful (IMPLEMENTED):** the source-faithful "How to play {champion}" layer is the default tab, covering recipe-derived component direction, common items/pairs/builds (10+ boards, most used first), frequent teammates, buildable trait directions and the 3★ signal, with item/champion/trait icons; the deeper evidence lives in the Items/Teammates/Traits/Evidence tabs. Recurring cores followed (item 2).
2. **Develop recurring core evidence (IMPLEMENTED):** observed 3–4 unit cores around a carry, from complete co-occurrence on the same final boards, selected by recurrence, shown in How to play and the Teammates tab. Next for Champion Intelligence: composition-family/variant evidence once trustworthy (Track 3).
3. **Monitor scheduled live ingestion** across multiple runs: inspect yield, duplicate rate, collection lag, Riot errors/429s and storage growth; keep maximum mode manual until the deep-ladder 401 is understood (Track 1).
4. **Repair the CommunityDragon live smoke** by resolving the duplicate-component namespace/test assumption without discarding legitimate source data (Track 6).
5. **In parallel, run the instrumented (unchanged) S2 report** from `main` on Patch 18.3 with `report_mode: s2-diagnostics` to quantify recursive lock-in and order dependence. Status:
   - instrumentation IMPLEMENTED;
   - full run #8 timed out before S2;
   - focused mode and exact speedups IMPLEMENTED;
   - run #9 (`s2-diagnostics`) COMPLETED. It showed C non-convergence and tie-order sensitivity;
   - stability research modes COMPLETED (runs #10 and #11);
   - versioned candidate `C_S2_centroid_candidate_v1` IMPLEMENTED, with its production evaluation run (`s2-candidate-c-centroid-v1`) PENDING.

   Canonical S2, Condition 1 and C's production cap stay unchanged. The candidate is not user-facing until it is frozen and checked on a held-out window (Track 2).
6. Keep set-transition readiness in new work and move toward Riot production access as the product matures (Tracks 6/7).
7. Build the **Reference / game knowledge** tab incrementally after the current Champion Investigation cleanup/redesign, starting only with subsections whose sources and refresh requirements are verified (Track 9).
8. After cores and trustworthy family evidence are ready, connect Champion Investigation to composition boards/families and practical alternatives—the bridge into TheoryLabs' long-term composition discovery/theorycrafting differentiator (Track 8).

**Continuous collection, Champion Investigation/performance work, static-data monitoring and S2 research are parallel workstreams.** This list is not a sequence in which all research must finish before product work proceeds.

## Task-selection gate

Before creating a derived system, first ask: **Does verified Riot/static source data already answer this question?** If yes, use the source data unless there is a documented reason not to.

Substantial proposed work should then answer **yes** to at least one of these:

- Does it improve trustworthy continuous data collection?
- Does it help TheoryLabs understand why a champion or board works?
- Does it improve discovery of strong or underexplored playable boards?
- Does it make that evidence more understandable or usable on the website?
- Does it remove a concrete blocker to one of those goals?

If not, defer it, or revise this roadmap explicitly first.

## Maintaining this roadmap

- **Update it when:**
  - a meaningful milestone is completed;
  - evidence changes direction;
  - priorities deliberately change.
- Do not rewrite it after every minor PR.
- Coding agents (including Claude) and contributors should read this file before proposing or starting substantial work. They should still inspect the repository itself, because this roadmap does not prove what is implemented.
