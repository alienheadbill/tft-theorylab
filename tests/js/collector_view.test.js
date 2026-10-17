// View logic of the TheoryLabs Collector dashboard (src/tftlab/collector_web/collector.js),
// run with Node's built-in test runner (`node --test`) from tests/test_collector_ui_js.py.
// No browser and no npm packages: collector.js exports its pure functions under Node.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const view = require(path.join(__dirname, "..", "..", "src", "tftlab", "collector_web", "collector.js"));

const T0 = 1_791_450_000_000;

function state(overrides = {}) {
  return {
    instance: "run-a",
    collector: {
      exists: true,
      trusted_window: { patch: "18.4", inside: true },
      current: { matches: 173, boards: 1384 },
      last_report: null,
      ...(overrides.collector || {}),
    },
    key: { state: "valid", configured: true, detail: "" , ...(overrides.key || {}) },
    jobs: { current: null, last: {}, ...(overrides.jobs || {}) },
    app_files: { ok: true, missing: [], message: "" },
    ...(overrides.top || {}),
  };
}

function job(kind, extra = {}) {
  return { kind, id: kind + "-1", state: "running", phase: "Collecting matches from Riot", started_at: T0,
    finished_at: null, elapsed_s: 222, steps: [], message: "", detail: "", result: {},
    live: { riot_requests: 146, rate_limited: 0 }, failed_step: null, ...extra };
}

test("READY when the window is open and a key is saved", () => {
  const ui = view.newUiState("run-a");
  view.applyPoll(ui, state(), T0);
  const p = view.primaryView(state(), ui, T0);
  assert.equal(p.label, "READY");
  assert.equal(p.headline, "Ready to collect");
  assert.deepEqual(p.lines.slice(0, 2), ["Patch 18.4", "Riot key valid"]);
  const unchecked = state({ key: { state: "unchecked", configured: true } });
  assert.equal(view.primaryView(unchecked, ui, T0).lines[1], "Riot key: saved — not checked");
  const b = view.buttonsView(state(), ui);
  assert.equal(b.collect.label, "Collect Data");
  assert.equal(b.collect.disabled, false);
});

test("NOT READY explains why and disables Collect", () => {
  const s = state({ collector: { trusted_window: { patch: null, inside: false } }, key: { state: "not_configured", configured: false } });
  const ui = view.newUiState("run-a");
  const p = view.primaryView(s, ui, T0);
  assert.equal(p.label, "NOT READY");
  assert.ok(p.lines.some((l) => l.includes("trusted window")));
  assert.ok(p.lines.some((l) => l.includes("No Riot key")));
  assert.equal(view.buttonsView(s, ui).collect.disabled, true);
});

test("Collect shows Starting… immediately, then Collection running… until the job finishes", () => {
  const ui = view.newUiState("run-a");
  ui.starting = "collect";
  assert.deepEqual(view.buttonsView(state(), ui).collect, { label: "Starting…", disabled: true, title: "" });
  ui.starting = null;
  ui.pendingJob = { id: "collect-1", kind: "collect" };
  // Accepted but not yet seen in a poll: still disabled and labelled as running.
  assert.equal(view.buttonsView(state(), ui).collect.label, "Collection running…");
  const running = state({ jobs: { current: job("collect") } });
  view.applyPoll(ui, running, T0);
  const b = view.buttonsView(running, ui);
  assert.equal(b.collect.label, "Collection running…");
  assert.equal(b.collect.disabled, true);
  assert.equal(b.snapshot.disabled, true);
  assert.equal(b.stop.disabled, true);
  // Finished: the button is usable again.
  const done = state({ jobs: { current: null, last: { collect: job("collect", { state: "succeeded", finished_at: T0 + 9e5 }) } } });
  view.applyPoll(ui, done, T0 + 9e5);
  assert.equal(ui.pendingJob, null);
  assert.deepEqual(view.buttonsView(done, ui).collect, { label: "Collect Data", disabled: false, title: "" });
});

test("COLLECTING shows the real phase, a ticking elapsed time, Riot requests and rate-limit events", () => {
  const ui = view.newUiState("run-a");
  const running = state({ jobs: { current: job("collect") } });
  view.applyPoll(ui, running, T0);
  const p = view.primaryView(running, ui, T0 + 5000);
  assert.equal(p.label, "COLLECTING");
  assert.equal(p.headline, "Collecting data");
  assert.deepEqual(p.lines, ["Collecting matches from Riot", "Elapsed: 3m 47s", "Riot requests: 146",
    "Rate-limit events: 0"]);
  assert.ok(!p.lines.some((l) => l.includes("%")), "no invented percentage");
});

test("COMPLETE shows real result counts", () => {
  const done = job("collect", { state: "succeeded", phase: "Complete", finished_at: T0 + 9e5, message: "Collection complete: 173 new patch 18.4 matches.",
    result: { new_matches: 173, patch_matches: 346, boards: 2768, duplicates_skipped: 12,
      validation: { "18.4": { is_severe: false, severe: [], warnings: [] } } } });
  const s = state({ jobs: { last: { collect: done } } });
  const p = view.primaryView(s, view.newUiState("run-a"), T0 + 9e5);
  assert.equal(p.label, "COMPLETE");
  assert.deepEqual(p.lines.slice(0, 4), ["173 new matches", "346 current-patch matches", "2,768 boards", "Validation passed"]);
  assert.equal(p.dismissKey, "collect-1");
});

test("an immediate worker failure shows FAILED with the reason and the failing step", () => {
  const failed = job("collect", { state: "failed", phase: "Failed", finished_at: T0 + 800,
    message: "Your Riot development key has expired (or is not valid). Nothing was collected.",
    failed_step: { name: "Riot key valid", status: "fail", detail: "unauthorized" }, detail: "raw reason" });
  const s = state({ jobs: { last: { collect: failed } } });
  const ui = view.newUiState("run-a");
  view.applyPoll(ui, s, T0 + 1000);
  const p = view.primaryView(s, ui, T0 + 1000);
  assert.equal(p.label, "FAILED");
  assert.equal(p.headline, "Collection failed");
  assert.match(p.reason, /Riot development key has expired/);
  assert.equal(p.lines[0], "Failed during: Riot key valid");
  assert.match(p.detailText, /raw reason/);
  assert.equal(view.buttonsView(s, ui).collect.disabled, false, "the user can retry");
});

test("a start error persists across status polls (the old 'blink')", () => {
  const ui = view.newUiState("run-a");
  ui.startError = { message: "This page was opened before the TheoryLabs Collector app was last restarted…", at: T0 };
  for (let i = 1; i <= 5; i++) {
    view.applyPoll(ui, state(), T0 + i * 2000); // idle polls, no newer job
    const p = view.primaryView(state(), ui, T0 + i * 2000);
    assert.equal(p.label, "FAILED");
    assert.equal(p.headline, "The collection did not start");
    assert.match(p.reason, /last restarted/);
  }
  // Superseded only by a job newer than the error (a retry that started).
  const newer = state({ jobs: { current: job("collect", { started_at: T0 + 20000 }) } });
  view.applyPoll(ui, newer, T0 + 21000);
  assert.equal(ui.startError, null);
  assert.equal(view.primaryView(newer, ui, T0 + 21000).label, "COLLECTING");
});

test("an older finished job does not clear a newer start error", () => {
  const ui = view.newUiState("run-a");
  ui.startError = { message: "A collection or snapshot is already running.", at: T0 + 5000 };
  const s = state({ jobs: { last: { collect: job("collect", { state: "succeeded", started_at: T0, finished_at: T0 + 100 }) } } });
  view.applyPoll(ui, s, T0 + 6000);
  assert.equal(view.primaryView(s, ui, T0 + 6000).label, "FAILED");
});

test("FAILED stays until dismissed with OK, then READY", () => {
  const failed = job("collect", { state: "failed", finished_at: T0 + 800, message: "No luck." });
  const s = state({ jobs: { last: { collect: failed } } });
  const ui = view.newUiState("run-a");
  for (let i = 0; i < 3; i++) view.applyPoll(ui, s, T0 + i * 10000);
  assert.equal(view.primaryView(s, ui, T0 + 30000).label, "FAILED");
  ui.dismissed[failed.id] = true;
  assert.equal(view.primaryView(s, ui, T0 + 30000).label, "READY");
});

test("a restarted app makes the page stale: banner with reload, actions disabled", () => {
  const ui = view.newUiState("run-a");
  view.applyPoll(ui, state({ top: { instance: "run-b" } }), T0);
  assert.equal(ui.stalePage, true);
  const banner = view.bannerView(state(), ui);
  assert.equal(banner.reload, true);
  assert.match(banner.text, /restarted/);
  const b = view.buttonsView(state(), ui);
  assert.equal(b.collect.disabled, true);
  assert.equal(b.snapshot.disabled, true);
});

test("a lost connection shows a banner and is cleared by the next good poll", () => {
  const ui = view.newUiState("run-a");
  ui.serverDown = true;
  assert.match(view.bannerView(state(), ui).text, /not responding/);
  assert.equal(view.buttonsView(state(), ui).collect.disabled, true);
  view.applyPoll(ui, state(), T0);
  assert.equal(view.bannerView(state(), ui), null);
});

test("missing app files are reported in the banner and block collection", () => {
  const s = state({ top: { app_files: { ok: false, missing: ["/x/collector_web/index.html"], message: "Some files are missing." } } });
  const ui = view.newUiState("run-a");
  assert.match(view.bannerView(s, ui).text, /Some files are missing.*index\.html/);
  assert.equal(view.primaryView(s, ui, T0).label, "NOT READY");
});

test("key labels are the five simple states", () => {
  const labels = ["not_configured", "unchecked", "valid", "invalid", "error"].map((s) => view.keyLabel({ state: s }).text);
  assert.deepEqual(labels, ["No key saved", "Saved — not checked", "Key valid", "Key expired or invalid", "Could not check key"]);
  assert.equal(view.readiness(state({ key: { state: "invalid", configured: true } })).ready, false);
  assert.equal(view.readiness(state({ key: { state: "unchecked", configured: true } })).ready, true);
});

test("a running snapshot is its own state and blocks collecting", () => {
  const ui = view.newUiState("run-a");
  const s = state({ jobs: { current: job("snapshot", { phase: "Exporting and verifying the sanitized snapshot" }) } });
  view.applyPoll(ui, s, T0);
  assert.equal(view.primaryView(s, ui, T0).label, "CREATING SNAPSHOT");
  const b = view.buttonsView(s, ui);
  assert.equal(b.snapshot.label, "Creating snapshot…");
  assert.equal(b.collect.disabled, true);
});

test("durations are formatted for people", () => {
  assert.equal(view.fmtDuration(42), "42s");
  assert.equal(view.fmtDuration(222), "3m 42s");
  assert.equal(view.fmtDuration(3725), "1h 02m");
});

// ------------------------------------------------------------ patch schedule (from the saved copy of Riot's page)

function calendar(overrides = {}) {
  return {
    ready: true, code: "ready", message: "Patch 18.4 is trusted until Oct 19, 2026 17:00 Pacific.",
    current: { patch: "18.4", starts_at: 1791442800000, ends_at: 1792454400000, origin: "built-in" },
    next: { label: "18.5", kind: "patch", base_patch: "18.5", scheduled_date: "2026-10-21" },
    next_window: null, upcoming_balance: { label: "18.6B", base_patch: "18.6", scheduled_date: "2026-11-11" },
    freshness: { state: "fresh", label: "Riot — checked today", last_check_at: T0, last_check_ok: true },
    provenance: { source_name: "Riot Games Player Support: Patch Schedule - Teamfight Tactics",
      source_url: "https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/",
      fetched_at: T0, events_sha256: "sha256:abc", content_sha256: "sha256:def",
      parser_version: "riot-support-schedule-table/1", policy_version: "pt-d+1-0000_to_n-2-1700/1",
      events: [{ label: "18.6B", scheduled_date: "2026-11-11", kind: "balance" }],
      planned_windows: [{ client_patch: "18.5", starts_at_utc: "2026-10-22T07:00:00Z", ends_at_utc: "2026-11-03T01:00:00Z" }],
      frozen_windows: [], conflicts: [], diagnostics: ["18.3: ambiguous"], schedule_error: null, frozen_error: null },
    ...overrides,
  };
}

test("READY shows the current patch, the next scheduled patch and its date", () => {
  const s = state({ collector: { patch_calendar: calendar() } });
  const p = view.primaryView(s, view.newUiState("run-a"), T0);
  assert.equal(p.label, "READY");
  assert.equal(p.lines[0], "Patch 18.4");
  assert.ok(p.lines.includes("Next scheduled: Patch 18.5 — Oct 21, 2026 (Pacific Time)"));
  const sv = view.scheduleView(s);
  assert.equal(sv.freshness, "Riot — checked today");
  assert.equal(sv.balance, "Balance update 18.6B — Nov 11, 2026: collection pauses around it");
});

test("NOT READY between patches gives the plain reason, the next patch and the schedule freshness", () => {
  const msg = "Patch 18.5 is scheduled for Oct 21, 2026 (Pacific Time). TheoryLabs leaves a gap around every patch " +
    "change and starts trusting patch 18.5 at Oct 22, 2026 00:00 Pacific, automatically. Collection is paused " +
    "rather than mixing patches. No Riot data was collected.";
  const pc = calendar({ ready: false, code: "between_patches", message: msg, current: null,
    freshness: { state: "cached_refresh_failed", label: "cached — refresh failed (Riot checked yesterday)" } });
  const s = state({ collector: { trusted_window: { patch: null, inside: false, reason: msg }, patch_calendar: pc } });
  const ui = view.newUiState("run-a");
  const p = view.primaryView(s, ui, T0);
  assert.equal(p.label, "NOT READY");
  assert.equal(p.lines[0], msg);
  assert.ok(p.lines.includes("Next scheduled: Patch 18.5 — Oct 21, 2026 (Pacific Time)"));
  assert.ok(p.lines.includes("Patch schedule: cached — refresh failed (Riot checked yesterday)"));
  assert.equal(view.buttonsView(s, ui).collect.disabled, true);
});

test("an upcoming balance update is announced while its patch is current", () => {
  const pc = calendar({ current: { patch: "18.6", origin: "frozen" },
    next: { label: "18.6B", kind: "balance", base_patch: "18.6", scheduled_date: "2026-11-11" } });
  const s = state({ collector: { trusted_window: { patch: "18.6", inside: true }, patch_calendar: pc } });
  const p = view.primaryView(s, view.newUiState("run-a"), T0);
  assert.ok(p.lines.includes("Next scheduled: Balance update 18.6B (patch 18.6) — Nov 11, 2026 (Pacific Time)"));
  assert.ok(p.lines.some((l) => l.startsWith("Collection pauses before this balance update")));
});

test("provenance goes to the technical details", () => {
  const rows = Object.fromEntries(view.scheduleTechRows(calendar()));
  assert.match(rows["Schedule source"], /support\.riotgames\.com/);
  assert.equal(rows["Schedule fingerprint"], "sha256:abc");
  assert.equal(rows["Parser / window rule"], "riot-support-schedule-table/1 / pt-d+1-0000_to_n-2-1700/1");
  assert.equal(rows["Patch window origin"], "fixed history (in the code)");
  assert.equal(rows["Planned windows"], "18.5: 2026-10-22T07:00:00Z → 2026-11-03T01:00:00Z");
  assert.equal(rows["Schedule notes"], "18.3: ambiguous");
  assert.deepEqual(view.scheduleTechRows(undefined), []);
  assert.equal(view.fmtDay("2026-12-01"), "Dec 1, 2026");
});
