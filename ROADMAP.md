# TheoryLabs Roadmap

This is the project's direction document. It exists so development stays aligned with the mission instead of drifting toward whichever experiment or feature is newest.

- **Checkpoint:** `main` at `d4ba0af` (after PR #32 and validation run #6), 2026-09-29.
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

- **Riot TFT ingestion** (`tftlab ingest-riot`, `live-ingest.yml`): scheduled every 6 hours (bounded mode) and manually dispatched, `main` only, with a fixed concurrency group.
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
- **Carry-centric analytics:** commitment, partners, item packages, trait breakpoints, Discovery / Opportunity Score.
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
  - This is **inferred from family traces, not yet quantified.**
- **Decision: do not modify S2 Condition 1 yet.** Keep the current S2 (`B_S2_experimental`, `C_S2_experimental`) as an experimental checkpoint. It is not production-ready and not independently validated.
- **Patch 18.3 is development data.** It has shaped this work repeatedly, so further experiments on it are development evidence, not untouched validation.

## Tracks

### Track 1: Continuous data collection

- **Goal:** grow the historical dataset automatically instead of by manual dispatch.
- **Scheduled collection (IMPLEMENTED):** `live-ingest.yml` runs the existing collector every 6 hours with fixed conservative settings. It uses **bounded** mode, not maximum: both production maximum-mode runs failed with 401 during deep ladder enumeration, so maximum stays manual-only until that is diagnosed. 401/403 now fail a bounded run with no ledger progress, as in maximum mode.
- **Next (ACCEPTED/CURRENT):** measure the first scheduled runs (the metrics below), then diagnose the maximum-mode ladder 401 before any change to the scheduled mode. Do not build a new ingestion system.
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
- **Immediate milestone (ACCEPTED/CURRENT):** instrument **unchanged** S2 and measure:
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
  - IMPLEMENTED: carry commitment, partners, items and traits, via the Discovery analytics.
  - FUTURE: cores, families/variants and alternatives.
- **Rules:**
  - Keep association separate from causation.
  - Keep observed statistics separate from inference and prediction.

### Track 4: Website/product

- **Immediate major milestone (ACCEPTED/CURRENT):** a **Champion Investigation vertical slice.** Build one genuinely useful champion investigation experience first, using a reusable, data-driven champion template rather than champion-specific architecture.
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
- **Sequencing:** the slice uses whatever evidence is ready (Track 3). Families and variants arrive when Track 2 supports them. Once the slice works, generalize it across champions.
- **First vertical slice (IMPLEMENTED):** `/champions` (pick a champion by name) and `/champions/<name>`, one data-driven template for every champion. It shows carry and 3★ hit/miss evidence, item builds and pairs, partners, trait breakpoints, sample size, balance window, freshness and the evidence type (observed only). Still FUTURE: recurring cores, composition families/variants and practical alternatives.
- **Refined from production use (IMPLEMENTED):** the page leads with "How players carry with {champion}" (observed facts and a separate rule-based interpretation), counts are "carry boards", special items are labelled and kept out of the normal-build summary, and Discovery no longer shows "no data" while loading.
- **Next (ACCEPTED/CURRENT):** keep refining from real use.

### Track 5: Analytics/API performance

- **Goal:** website requests read prepared analytics. They do not run expensive research calculations.
- **Target architecture (FUTURE):** Riot/raw data → normalization → offline/precomputed analytics → prepared API results → website.
- Measure actual latency before adding caching or infrastructure. Do not add them just because they sound useful.
- **Done (from measured page loads):** the champion picker uses a light count query, a champion page aggregates only that champion, and Discovery builds evidence only for the selected costs in batched queries, reusing one window-wide population per window until new matches arrive. The window-wide aggregate is still computed on request; precomputing it is the next step if page loads need it.

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

### Track 7: Riot production readiness

- **Goal:** a functioning, policy-compliant product that is suitable for persistent Riot production API access.
- Development-key collection is temporary infrastructure.
- Policy-sensitive features (overlays, live assistance) need the current Riot policy checked before any implementation. The README's "Riot policy boundary" still applies: aggregate and post-game analysis only.

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

## Near-term priorities

**Completed:** validation run #6.

**Next:**

1. Adopt this roadmap.
2. ~~Implement safe scheduled continuous collection using the existing collector~~ (done; now measuring it) (Track 1).
3. ~~Begin the Champion Investigation vertical slice~~ (first slice done; refining it) (Track 4).
4. Develop the champion, item, partner, core and trait evidence that experience needs (Track 3).
5. In parallel, instrument unchanged S2 to quantify recursive lock-in and order dependence (Track 2).
6. Improve prepared analytics and API performance as real product requirements expose bottlenecks (Track 5).
7. Keep set-transition readiness in all new work (Track 6).
8. Move toward Riot production access as the product matures (Track 7).

**Continuous collection, Champion Investigation and S2 research are parallel workstreams.** This list is not a sequence in which all research must finish before product work proceeds.

## Task-selection gate

Substantial proposed work should answer **yes** to at least one of these:

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
