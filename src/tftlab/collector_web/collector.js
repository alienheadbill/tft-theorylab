// TheoryLabs Collector (local control panel). Plain browser JavaScript, no
// framework. All data is written with textContent (never innerHTML). The
// Riot key is only ever sent in a POST body and the field is cleared at once.
//
// The page shows ONE primary state at the top -- READY / NOT READY /
// COLLECTING / CREATING SNAPSHOT / COMPLETE / FAILED -- computed from the
// server's job state plus a little local UI state (`ui`). The pure functions
// below (no DOM) decide what to show; they are exported for Node so
// tests/test_collector_ui_js.py can test them without a browser.
//
// Errors persist: a status poll never clears an error the user has not seen
// resolved. A start error is replaced only by a newer job, a retry or "OK".
(function () {
  "use strict";

  // ------------------------------------------------------------ formatting (pure)

  function fmtNum(n) { return n == null ? "—" : Number(n).toLocaleString("en-US"); }
  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["bytes", "KB", "MB", "GB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return i === 0 ? n + " bytes" : v.toFixed(1) + " " + units[i];
  }
  function fmtDuration(seconds) {
    if (seconds == null || !isFinite(seconds)) return "—";
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return h + "h " + String(m).padStart(2, "0") + "m";
    if (m) return m + "m " + String(r).padStart(2, "0") + "s";
    return r + "s";
  }
  function fmtTime(ms) {
    if (ms == null) return "—";
    const d = new Date(ms);
    return d.toLocaleString() + " (" + d.toISOString().replace("T", " ").slice(0, 16) + " UTC)";
  }

  // A Pacific calendar date "2026-10-21" -> "Oct 21, 2026" (no time-zone shift).
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDay(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? MONTHS[Number(m[2]) - 1] + " " + Number(m[3]) + ", " + m[1] : "—";
  }

  // ------------------------------------------------------------ view logic (pure)

  // Patch facts from the server's patch calendar (read from the saved copy of
  // Riot's schedule; the page itself never contacts Riot).
  function scheduleView(state) {
    const pc = ((state || {}).collector || {}).patch_calendar;
    if (!pc) return null;
    const cur = pc.current, next = pc.next, bal = pc.upcoming_balance, fresh = pc.freshness || {};
    const kindText = (e) => e.kind === "balance" ? "balance update " + e.label + " (patch " + e.base_patch + ")"
      : "patch " + e.label;
    return {
      ready: !!pc.ready,
      message: pc.message || "",
      current: cur ? "Patch " + cur.patch : "None",
      next: next ? kindText(next).replace(/^./, (c) => c.toUpperCase()) + " — " + fmtDay(next.scheduled_date) +
        " (Pacific Time)" : "Not listed yet",
      balance: bal ? "Balance update " + bal.label + " — " + fmtDay(bal.scheduled_date) +
        ": collection pauses around it" : null,
      nextIsBalance: !!(next && next.kind === "balance"),
      freshness: fresh.label || "unknown",
      freshnessTone: fresh.state === "fresh" ? "ok" : fresh.state === "none" ? "warn" : "bad",
    };
  }

  const KEY_LABELS = {
    not_configured: ["No key saved", "bad"],
    unchecked: ["Saved — not checked", "warn"],
    valid: ["Key valid", "ok"],
    invalid: ["Key expired or invalid", "bad"],
    error: ["Could not check key", "warn"],
  };

  function keyLabel(key) {
    const k = key || {};
    const [text, tone] = KEY_LABELS[k.state] || ["Unknown", "warn"];
    return { text, tone, detail: k.detail || "" };
  }

  function newUiState(pageInstance) {
    return {
      pageInstance: pageInstance || null, // which run of the app served this page
      polledAt: 0,                        // client time of the last good status poll
      serverDown: false,                  // last status poll failed (cleared by the next good one)
      serverProblem: "",
      stalePage: false,                   // the app restarted since this page loaded: reload needed
      starting: null,                     // "collect" | "snapshot" while its POST is in flight
      pendingJob: null,                   // {id, kind} accepted by the server, not yet seen finished
      startError: null,                   // {message, at}: collection could not start (persists)
      snapshotError: null,                // {message, at}: snapshot could not start (persists)
      dismissed: {},                      // job ids / "start:<at>" the user acknowledged with "OK"
    };
  }

  function jobsOf(state) {
    const jobs = (state && state.jobs) || {};
    return { current: jobs.current || null, last: jobs.last || {} };
  }

  // Fold a successful /api/state poll into the UI state. Never clears an
  // unresolved error: a start error goes only when a job newer than it exists.
  function applyPoll(ui, state, nowMs) {
    ui.serverDown = false;
    ui.serverProblem = "";
    ui.polledAt = nowMs;
    if (ui.pageInstance && state.instance && state.instance !== ui.pageInstance) ui.stalePage = true;
    const { current, last } = jobsOf(state);
    const newer = (kind, at) => (current && current.kind === kind && current.started_at >= at) ||
      (last[kind] && last[kind].started_at >= at);
    if (ui.startError && newer("collect", ui.startError.at)) ui.startError = null;
    if (ui.snapshotError && newer("snapshot", ui.snapshotError.at)) ui.snapshotError = null;
    if (ui.pendingJob) {
      const finished = last[ui.pendingJob.kind] && last[ui.pendingJob.kind].id === ui.pendingJob.id;
      const running = current && current.id === ui.pendingJob.id;
      if (finished || (!running && !current)) ui.pendingJob = null;
    }
    return ui;
  }

  // Provenance for the Technical details list.
  function scheduleTechRows(pc) {
    if (!pc) return [];
    const p = pc.provenance || {};
    const win = (w) => w.client_patch + ": " + (w.starts_at_utc || "") + " → " + (w.ends_at_utc || "");
    const origin = { "built-in": "fixed history (in the code)", frozen: "from Riot's schedule, frozen when first used",
      planned: "from Riot's schedule; frozen when collection starts" };
    const notes = [p.schedule_error, p.frozen_error].concat(p.conflicts || [], p.diagnostics || []).filter(Boolean);
    return [
      ["Patch window origin", pc.current ? origin[pc.current.origin] || pc.current.origin : "—"],
      ["Schedule source", (p.source_name || "") + " — " + (p.source_url || "")],
      ["Schedule fetched", p.fetched_at ? fmtTime(p.fetched_at) : "never"],
      ["Last schedule check", (pc.freshness || {}).last_check_at ? fmtTime(pc.freshness.last_check_at) +
        ((pc.freshness.last_check_ok === false) ? " — failed: " + (pc.freshness.last_check_error || "unknown") : " — ok")
        : "never"],
      ["Schedule fingerprint", p.events_sha256 || "—"],
      ["Page fingerprint", p.content_sha256 || "—"],
      ["Parser / window rule", (p.parser_version || "—") + " / " + (p.policy_version || "—")],
      ["Scheduled entries", (p.events || []).slice(-6).map((e) => e.label + " " + e.scheduled_date +
        (e.kind === "balance" ? " (balance)" : "")).join(", ") || "—"],
      ["Planned windows", (p.planned_windows || []).map(win).join("; ") || "none"],
      ["Frozen windows", (p.frozen_windows || []).map(win).join("; ") || "none"],
      ["Schedule notes", notes.join(" ") || "none"],
    ];
  }

  function readiness(state) {
    const reasons = [];
    if (!state) return { ready: false, reasons: ["Reading the collector's status…"] };
    const c = state.collector || {};
    const tw = c.trusted_window || {};
    const key = state.key || {};
    if (state.app_files && state.app_files.ok === false) reasons.push(state.app_files.message);
    if (c.error) reasons.push(c.error);
    const pc = c.patch_calendar;
    if (!tw.inside) reasons.push((pc && pc.message) || tw.reason || "TheoryLabs does not have a trusted window for collection right now.");
    if (!key.configured) reasons.push("No Riot key saved yet — paste your development key below.");
    else if (key.state === "invalid") reasons.push("Your Riot key has expired or is invalid — save a new one below.");
    return { ready: reasons.length === 0, reasons };
  }

  function validationSummary(result) {
    const v = result && result.validation;
    if (!v || !Object.keys(v).length) return null;
    const severe = Object.values(v).some((x) => x.is_severe);
    const warnings = Object.values(v).some((x) => x.warnings && x.warnings.length);
    return severe ? "Validation FAILED" : warnings ? "Validation passed (with warnings)" : "Validation passed";
  }

  function collectFacts(job) {
    const r = job.result || {};
    return [
      ["Already stored (skipped)", fmtNum(r.duplicates_skipped)],
      ["Riot requests", fmtNum(r.riot_requests)],
      ["Rate-limit events", fmtNum(r.rate_limited)],
      ["Duration", fmtDuration(job.elapsed_s)],
      ["Discovery preparation", r.prepared || "not run"],
      ["Backup", r.backup || "—"],
      ["Report file", r.report_path || "—"],
      ["Job", job.id + " · started " + fmtTime(job.started_at)],
    ];
  }

  // What the primary status area shows.
  function primaryView(state, ui, nowMs) {
    if (!state) return { tone: "loading", label: "LOADING", headline: "Reading the collector's status…", lines: [] };
    const { current, last } = jobsOf(state);
    const view = { lines: [], steps: [], facts: [], detailText: "", reason: "", dismissKey: null };

    if (current) {
      const elapsed = current.elapsed_s + Math.max(0, (nowMs - (ui.polledAt || nowMs)) / 1000);
      const live = current.live || {};
      view.steps = current.steps || [];
      if (current.kind === "snapshot") {
        return Object.assign(view, { tone: "running", label: "CREATING SNAPSHOT",
          headline: "Creating the public snapshot", lines: [current.phase, "Elapsed: " + fmtDuration(elapsed)] });
      }
      view.lines = [current.phase, "Elapsed: " + fmtDuration(elapsed)];
      if (live.riot_requests != null) view.lines.push("Riot requests: " + fmtNum(live.riot_requests));
      if (live.rate_limited != null) view.lines.push("Rate-limit events: " + fmtNum(live.rate_limited));
      return Object.assign(view, { tone: "running", label: "COLLECTING", headline: "Collecting data" });
    }

    if (ui.startError && !ui.dismissed["start:" + ui.startError.at]) {
      return Object.assign(view, { tone: "failed", label: "FAILED", headline: "The collection did not start",
        reason: ui.startError.message, dismissKey: "start:" + ui.startError.at });
    }

    const job = last.collect;
    if (job && !ui.dismissed[job.id]) {
      const r = job.result || {};
      view.steps = job.steps || [];
      view.facts = collectFacts(job);
      view.dismissKey = job.id;
      if (job.state === "succeeded") {
        view.lines = [fmtNum(r.new_matches) + " new matches", fmtNum(r.patch_matches) + " current-patch matches",
          fmtNum(r.boards) + " boards"];
        const v = validationSummary(r);
        if (v) view.lines.push(v);
        view.lines.push("Finished " + fmtTime(job.finished_at));
        return Object.assign(view, { tone: "complete", label: "COMPLETE", headline: "Collection complete",
          reason: job.message });
      }
      const step = job.failed_step;
      view.lines = ["Failed during: " + (step ? step.name : job.phase), "Finished " + fmtTime(job.finished_at)];
      if (r.new_matches) view.lines.splice(1, 0, fmtNum(r.new_matches) + " new matches were saved before it stopped");
      view.detailText = [step ? step.name + ": " + step.detail : "", job.detail].filter(Boolean).join("\n\n");
      return Object.assign(view, { tone: "failed", label: "FAILED", headline: "Collection failed",
        reason: job.message || "The collection stopped without a reason. See the technical details." });
    }

    const ready = readiness(state);
    const tw = (state.collector || {}).trusted_window || {};
    const lastReport = (state.collector || {}).last_report;
    const sched = scheduleView(state);
    if (!ready.ready) {
      const lines = ready.reasons.slice();
      if (sched && !tw.inside) lines.push("Next scheduled: " + sched.next, "Patch schedule: " + sched.freshness);
      return Object.assign(view, { tone: "notready", label: "NOT READY", headline: "Not ready to collect", lines });
    }
    const keyText = keyLabel(state.key).text; // "Key valid" -> "Riot key valid"; else "Riot key: saved — not checked"
    view.lines = ["Patch " + tw.patch, /^Key /.test(keyText) ? "Riot key " + keyText.slice(4)
      : "Riot key: " + keyText.charAt(0).toLowerCase() + keyText.slice(1)];
    if (sched) {
      view.lines.push("Next scheduled: " + sched.next);
      if (sched.nextIsBalance) view.lines.push("Collection pauses before this balance update, then resumes with the next patch");
    }
    if (lastReport) {
      view.lines.push("Last collection: " + (lastReport.outcome === "success" ? "succeeded" : "did not succeed") +
        (lastReport.new_matches != null ? ", " + fmtNum(lastReport.new_matches) + " new matches" : ""));
    }
    return Object.assign(view, { tone: "ready", label: "READY", headline: "Ready to collect" });
  }

  function buttonsView(state, ui) {
    const { current } = jobsOf(state);
    const pending = ui.pendingJob;
    const blocked = !state || ui.stalePage || ui.serverDown;
    const db = (state && state.collector) || {};
    const out = {
      collect: { label: "Collect Data", disabled: false, title: "" },
      snapshot: { label: "Create Public Snapshot", disabled: false, title: "" },
      stop: { disabled: Boolean(current) || blocked },
      checkKey: { disabled: Boolean(current) || blocked || !(state && state.key && state.key.configured) },
    };
    const collectRunning = (current && current.kind === "collect") || (pending && pending.kind === "collect");
    const snapshotRunning = (current && current.kind === "snapshot") || (pending && pending.kind === "snapshot");
    if (ui.starting === "collect") out.collect = { label: "Starting…", disabled: true, title: "" };
    else if (collectRunning) out.collect = { label: "Collection running…", disabled: true, title: "" };
    else if (current || snapshotRunning) out.collect = { label: "Collect Data", disabled: true,
      title: "Wait for the snapshot to finish" };
    else if (blocked) out.collect = { label: "Collect Data", disabled: true, title: "Reload the page first" };
    else {
      const ready = readiness(state);
      if (!ready.ready) out.collect = { label: "Collect Data", disabled: true, title: ready.reasons.join(" ") };
    }
    if (ui.starting === "snapshot") out.snapshot = { label: "Starting…", disabled: true, title: "" };
    else if (snapshotRunning) out.snapshot = { label: "Creating snapshot…", disabled: true, title: "" };
    else if (current || collectRunning) out.snapshot = { label: "Create Public Snapshot", disabled: true,
      title: "Wait for the collection to finish" };
    else if (blocked || !db.exists) out.snapshot = { label: "Create Public Snapshot", disabled: true,
      title: db.exists ? "Reload the page first" : "Collect data first" };
    return out;
  }

  function bannerView(state, ui) {
    if (ui.stalePage) {
      return { text: "The TheoryLabs Collector app was restarted after this page was opened. Reload the page " +
        "to continue (your data is safe).", reload: true };
    }
    if (ui.serverDown) {
      return { text: ui.serverProblem || "The TheoryLabs Collector app is not responding. If you stopped it, " +
        "start it again with “TheoryLabs Collector”.", reload: true };
    }
    if (state && state.app_files && state.app_files.ok === false) {
      const missing = (state.app_files.missing || []).join(", ");
      return { text: state.app_files.message + (missing ? " Missing: " + missing : ""), reload: false };
    }
    return null;
  }

  const api = { fmtNum, fmtBytes, fmtDuration, fmtDay, keyLabel, newUiState, applyPoll, readiness, primaryView,
    buttonsView, bannerView, validationSummary, scheduleView, scheduleTechRows };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined") return;

  // ------------------------------------------------------------ DOM (browser only)

  const $ = (id) => document.getElementById(id);
  const TOKEN = document.querySelector('meta[name="collector-token"]').content;
  const ui = newUiState(document.querySelector('meta[name="collector-instance"]').content);
  const DISMISS_KEY = "theorylabs-collector-dismissed";
  try { ui.dismissed = JSON.parse(window.localStorage.getItem(DISMISS_KEY) || "{}") || {}; } catch (_) { /* private mode */ }
  let state = null;
  let timer = null;

  function facts(dl, rows) {
    dl.replaceChildren();
    for (const [label, value] of rows) {
      if (value === undefined) continue;
      const div = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value == null || value === "" ? "—" : String(value);
      div.append(dt, dd);
      dl.append(div);
    }
  }

  function notice(el, text, kind) {
    el.textContent = text || "";
    el.className = "notice" + (kind ? " notice-" + kind : "");
    el.hidden = !text;
  }

  function list(el, items, ordered) {
    el.replaceChildren();
    for (const item of items) {
      const li = document.createElement("li");
      if (ordered) {
        li.className = "s-" + item.status;
        li.textContent = item.name + ": " + item.detail;
      } else {
        li.textContent = item;
      }
      el.append(li);
    }
  }

  function renderPrimary() {
    const view = primaryView(state, ui, Date.now());
    const box = $("primary");
    box.className = "primary state-" + view.tone;
    $("primary-state").textContent = view.label;
    $("primary-headline").textContent = view.headline;
    list($("primary-lines"), view.lines || [], false);
    $("primary-reason").textContent = view.reason || "";
    $("primary-reason").hidden = !view.reason;
    $("dismiss").hidden = !view.dismissKey;
    $("dismiss").dataset.key = view.dismissKey || "";
    $("collect-note").hidden = view.tone !== "ready" && view.tone !== "running";
    const steps = view.steps || [];
    $("primary-steps-box").hidden = !steps.length;
    list($("primary-steps"), steps.slice(-20), true);
    if (view.tone === "running") $("primary-steps-box").open = true;
    const hasDetail = (view.facts && view.facts.length) || view.detailText;
    $("primary-details").hidden = !hasDetail;
    facts($("primary-detail-facts"), view.facts || []);
    $("primary-detail-text").textContent = view.detailText || "";
    $("primary-detail-text").hidden = !view.detailText;
  }

  function renderButtons() {
    const b = buttonsView(state, ui);
    for (const [id, v] of [["collect", b.collect], ["snapshot", b.snapshot]]) {
      $(id).textContent = v.label;
      $(id).disabled = v.disabled;
      $(id).title = v.title || "";
      $(id).setAttribute("aria-busy", String(/…$/.test(v.label)));
    }
    $("stop-app").disabled = b.stop.disabled;
    $("check-key").disabled = b.checkKey.disabled;
  }

  function renderBanner() {
    const banner = bannerView(state, ui);
    $("app-banner").hidden = !banner;
    $("app-banner-text").textContent = banner ? banner.text : "";
    $("app-banner-reload").hidden = !(banner && banner.reload);
  }

  function renderStatus() {
    if (!state) return;
    const c = state.collector || {};
    const tw = c.trusted_window || {};
    const cur = c.current || {};
    const store = c.store || {};
    const last = c.last_report;
    const health = c.error ? "Problem: " + c.error
      : c.read_error ? "Problem: the database could not be read (" + c.read_error + ")"
      : c.exists ? "OK" : "No database yet — the first collection creates it";
    const sched = scheduleView(state);
    facts($("status-list"), [
      ["Trusted patch", tw.patch || "none"],
      ["Collection allowed now", tw.inside ? "Yes" : "No — paused between trusted patch windows"],
      ...(sched ? [["Next scheduled", sched.next], ["Patch schedule", sched.freshness]] : []),
      ...(sched && sched.balance ? [["Heads-up", sched.balance]] : []),
      ["Current-patch matches", tw.patch ? fmtNum(cur.matches) : "—"],
      ["Current-patch boards", tw.patch ? fmtNum(cur.boards) : "—"],
      ["Latest game", fmtTime(cur.latest_game != null ? cur.latest_game : store.latest_game)],
      ["Last collection", last ? (last.outcome === "success" ? "Succeeded" : "Did not succeed") +
        (last.new_matches != null ? " · " + fmtNum(last.new_matches) + " new matches" : "") : "None yet"],
      ["Database", health],
    ]);
    const lb = c.latest_backup;
    const prepared = c.prepared || {};
    facts($("tech-list"), [
      ["Database file", c.db_path || "—"],
      ["Database size", c.exists ? fmtBytes(c.size_bytes) : "—"],
      ["All matches stored", c.exists ? fmtNum(store.matches) : "—"],
      ["Collection runs", c.exists ? fmtNum(store.runs_completed) + " completed, " +
        fmtNum(store.runs_incomplete) + " incomplete" : "—"],
      ["Patch window", tw.patch ? fmtTime(tw.starts_at) + " → " + fmtTime(tw.ends_at) : "none (the reason is shown above)"],
      ["Latest backup", lb ? lb.name + " (" + fmtBytes(lb.bytes) + "), " + c.backups + " kept" : "none yet"],
      ["Backup folder", c.backup_dir || "—"],
      ["Discovery preparation", Object.keys(prepared).length
        ? Object.entries(prepared).map(([w, st]) => w + ": " + st).join(", ") : "—"],
      ["Last report file", last ? last.file : "—"],
      ...scheduleTechRows(c.patch_calendar),
      ["TheoryLabs folder", state.root],
      ["App run", state.instance],
    ]);
  }

  function renderKey() {
    if (!state) return;
    const k = keyLabel(state.key);
    const el = $("key-state");
    el.textContent = k.text + (k.detail && (state.key.state === "invalid" || state.key.state === "error")
      ? " — " + k.detail : "");
    el.className = "key-state state-" + k.tone;
  }

  function renderSnapshot() {
    const { current, last } = jobsOf(state);
    const job = current && current.kind === "snapshot" ? null : last.snapshot;
    if (ui.snapshotError) {
      notice($("snapshot-message"), "The snapshot did not start: " + ui.snapshotError.message, "error");
      $("snapshot-facts").hidden = true;
      $("snapshot-details").hidden = true;
      return;
    }
    if (current && current.kind === "snapshot") {
      notice($("snapshot-message"), "Creating the snapshot… (" + current.phase + ")", "");
      $("snapshot-facts").hidden = true;
      $("snapshot-details").hidden = true;
      return;
    }
    if (!job) { notice($("snapshot-message"), "", ""); $("snapshot-facts").hidden = true; $("snapshot-details").hidden = true; return; }
    notice($("snapshot-message"), job.message, job.state === "succeeded" ? "ok" : "error");
    const r = job.result || {};
    facts($("snapshot-facts"), [
      ["Balance window", r.balance_windows ? r.balance_windows.join(", ") : undefined],
      ["Matches", r.matches != null ? fmtNum(r.matches) : undefined],
      ["Boards", r.boards != null ? fmtNum(r.boards) : undefined],
      ["Latest game", r.latest_game],
      ["Snapshot file", r.gz_path],
      ["Size", r.gz_bytes != null ? fmtBytes(r.gz_bytes) : undefined],
      ["SHA-256", r.gz_sha256],
      ["Verification", r.checks_passed != null ? r.checks_passed + " checks passed" : undefined],
    ]);
    $("snapshot-facts").hidden = job.state !== "succeeded";
    $("snapshot-details").hidden = !job.detail;
    $("snapshot-detail").textContent = job.detail || "";
  }

  function render() {
    renderBanner();
    renderPrimary();
    renderButtons();
    renderStatus();
    renderKey();
    renderSnapshot();
  }

  function busy() {
    return Boolean(ui.starting || ui.pendingJob || (state && state.jobs && state.jobs.current));
  }

  async function refresh() {
    clearTimeout(timer);
    try {
      const res = await fetch("/api/state", { cache: "no-store", credentials: "same-origin" });
      if (!res.ok) {
        let message = "";
        try { message = (await res.json()).error || ""; } catch (_) { /* not JSON */ }
        throw new Error(message || "The TheoryLabs Collector app reported a problem (" + res.status + "). " +
          "Stopping and starting it again usually fixes this.");
      }
      state = await res.json();
      applyPoll(ui, state, Date.now());
    } catch (err) {
      ui.serverDown = true;
      ui.serverProblem = err instanceof TypeError ? "" : err.message;
    }
    render();
    timer = setTimeout(refresh, busy() ? 2000 : 10000);
  }

  async function post(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Collector-Token": TOKEN },
        body: body ? JSON.stringify(body) : "{}",
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (_) {
      throw Object.assign(new Error("Could not reach the TheoryLabs Collector app. Is it still running?"),
        { code: "unreachable" });
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) {
      throw Object.assign(new Error(data.error || "The collector app answered with an error (" + res.status + ")."),
        { code: data.code || "http_" + res.status });
    }
    return data;
  }

  function actionError(err) {
    if (err.code === "stale_page") ui.stalePage = true;
    return { message: err.message, at: Date.now() };
  }

  async function startJob(kind) {
    if (kind === "collect") ui.startError = null; else ui.snapshotError = null;
    ui.starting = kind;
    render(); // immediate feedback: the button says "Starting…" before the server answers
    try {
      const data = await post(kind === "collect" ? "/api/collect" : "/api/snapshot");
      ui.pendingJob = { id: data.job.id, kind };
      if (state && state.jobs) { // show COLLECTING / CREATING SNAPSHOT at once, before the next poll
        state.jobs.current = data.job;
        ui.polledAt = Date.now();
      }
    } catch (err) {
      if (kind === "collect") ui.startError = actionError(err); else ui.snapshotError = actionError(err);
    } finally {
      ui.starting = null;
      render();
      refresh();
    }
  }

  $("collect").addEventListener("click", () => startJob("collect"));
  $("snapshot").addEventListener("click", () => startJob("snapshot"));
  $("app-banner-reload").addEventListener("click", () => window.location.reload());
  $("dismiss").addEventListener("click", () => {
    const key = $("dismiss").dataset.key;
    if (!key) return;
    ui.dismissed[key] = true;
    if (ui.startError && key === "start:" + ui.startError.at) ui.startError = null;
    try { window.localStorage.setItem(DISMISS_KEY, JSON.stringify(ui.dismissed)); } catch (_) { /* ignore */ }
    render();
  });

  $("key-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("api-key");
    const value = input.value.trim();
    input.value = "";  // never keep the key in the page
    if (!value) { notice($("key-message"), "Paste your key into the box first.", "warn"); return; }
    $("save-key").disabled = true;
    try {
      await post("/api/key", { api_key: value });
      notice($("key-message"), "Key saved. Click “Check key” to make sure Riot accepts it.", "ok");
    } catch (err) {
      actionError(err);
      notice($("key-message"), err.message, "error");
    } finally {
      $("save-key").disabled = false;
      refresh();
    }
  });

  $("check-key").addEventListener("click", async () => {
    $("check-key").disabled = true;
    notice($("key-message"), "Checking the key with Riot…", "");
    try {
      const data = await post("/api/key/validate");
      notice($("key-message"), data.ok ? "Riot accepted the key." : (data.key.detail || "The key did not work."),
        data.ok ? "ok" : "error");
    } catch (err) {
      actionError(err);
      notice($("key-message"), err.message, "error");
    } finally {
      refresh();
    }
  });

  $("stop-app").addEventListener("click", async () => {
    if (!window.confirm("Stop the TheoryLabs Collector app? You can start it again with “TheoryLabs Collector”.")) return;
    try {
      const data = await post("/api/shutdown");
      clearTimeout(timer);
      const main = document.body.querySelector("main");
      const p = document.createElement("p");
      p.className = "notice notice-ok";
      p.textContent = data.message;
      main.replaceChildren(p);
    } catch (err) {
      ui.startError = actionError(err);
      render();
    }
  });

  // Keep the elapsed time ticking between polls while a job runs.
  setInterval(() => { if (state && state.jobs && state.jobs.current) renderPrimary(); }, 1000);
  render();
  refresh();
})();
