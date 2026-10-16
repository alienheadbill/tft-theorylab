// TheoryLabs Collector (local control panel). Plain browser JavaScript, no
// framework. All data is written with textContent (never innerHTML). The
// Riot key is only ever sent in a POST body and the field is cleared at once.
(function () {
  "use strict";

  const TOKEN = document.querySelector('meta[name="collector-token"]').content;
  const $ = (id) => document.getElementById(id);
  let state = null;
  let timer = null;

  // ------------------------------------------------------------ helpers

  function fmtTime(ms) {
    if (ms == null) return "—";
    const d = new Date(ms);
    return d.toLocaleString() + " (" + d.toISOString().replace("T", " ").slice(0, 16) + " UTC)";
  }
  function fmtNum(n) { return n == null ? "—" : Number(n).toLocaleString(); }
  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["bytes", "KB", "MB", "GB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return i === 0 ? n + " bytes" : v.toFixed(1) + " " + units[i];
  }
  function fmtDuration(s) {
    if (s == null) return "";
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m ? m + " min " + r + " s" : r + " s";
  }

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

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Collector-Token": TOKEN },
      body: body ? JSON.stringify(body) : "{}",
      credentials: "same-origin",
      cache: "no-store",
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) throw new Error(data.error || "The collector app answered with an error (" + res.status + ").");
    return data;
  }

  // ------------------------------------------------------------ rendering

  function renderStatus(s) {
    const c = s.collector || {};
    const tw = c.trusted_window || {};
    const cur = c.current || {};
    const store = c.store || {};
    const lb = c.latest_backup;
    const prepared = c.prepared || {};
    const last = c.last_report;
    const rows = [
      ["Database", c.db_path || "—"],
      ["Database exists", c.exists ? "yes" : "not yet (the first collection creates it)"],
      ["Database size", c.exists ? fmtBytes(c.size_bytes) : "—"],
      ["Current trusted patch", tw.patch ? tw.patch : "none"],
      ["Collection window", tw.patch ? fmtTime(tw.starts_at) + " → " + fmtTime(tw.ends_at) : (tw.reason || "—")],
      ["Inside the window now", tw.inside ? "yes — collection is possible" : "no — collection is not possible"],
      ["Current-patch matches", tw.patch ? fmtNum(cur.matches) : "—"],
      ["Current-patch boards", tw.patch ? fmtNum(cur.boards) : "—"],
      ["Latest game", fmtTime(cur.latest_game != null ? cur.latest_game : store.latest_game)],
      ["All matches stored", c.exists ? fmtNum(store.matches) : "—"],
      ["Local backups", lb ? c.backups + " kept · latest " + lb.name + " (" + fmtBytes(lb.bytes) + ")" : "none yet"],
      ["Discovery preparation", Object.keys(prepared).length
        ? Object.entries(prepared).map(([w, st]) => w + ": " + st).join(", ") : "—"],
      ["Last collection", last ? (last.outcome === "success" ? "succeeded" : "did not succeed") + " · " +
        (last.patch ? "patch " + last.patch + " · " : "") + fmtNum(last.new_matches) + " new matches (" + last.file + ")"
        : "none yet"],
    ];
    if (c.read_error) rows.splice(2, 0, ["Problem", "The database could not be read (" + c.read_error + ")."]);
    if (c.error) rows.unshift(["Problem", c.error]);
    facts($("status-list"), rows);
  }

  function renderKey(s) {
    const k = s.key || {};
    const el = $("key-state");
    const map = {
      not_configured: ["Not configured — paste your Riot development key below.", "state-bad"],
      unchecked: ["Saved (not checked yet) — click “Check key”.", "state-warn"],
      valid: ["Valid — Riot accepted the key" + (k.checked_at ? " (" + fmtTime(k.checked_at) + ")" : "") + ".", "state-ok"],
      invalid: ["Expired or invalid. " + (k.detail || ""), "state-bad"],
      error: ["Could not check the key: " + (k.detail || "try again later."), "state-warn"],
    };
    const [text, cls] = map[k.state] || ["Unknown", "state-warn"];
    el.textContent = text;
    el.className = "key-state " + cls;
    $("check-key").disabled = !k.configured || busy(s);
  }

  function busy(s) { return Boolean(s.jobs && s.jobs.current); }

  function renderPreflight(s) {
    const c = s.collector || {};
    const tw = c.trusted_window || {};
    const k = s.key || {};
    const items = [
      [tw.inside, tw.inside ? "Patch " + tw.patch + " is open for collection." :
        "TheoryLabs does not yet have a trusted window for the current patch."],
      [k.configured, k.configured ? "A Riot key is saved." : "No Riot key saved yet."],
      [k.state === "valid" ? true : (k.state === "invalid" ? false : null),
        k.state === "valid" ? "The key was checked and works." : (k.state === "invalid"
          ? "The key is expired or invalid — save a new one first." : "The key has not been checked (optional, recommended).")],
      [!busy(s), busy(s) ? "A job is running." : "Nothing else is running."],
    ];
    const ul = $("preflight");
    ul.replaceChildren();
    for (const [ok, text] of items) {
      const li = document.createElement("li");
      li.dataset.mark = ok === true ? "✓" : ok === false ? "✗" : "•";
      li.className = ok === true ? "state-ok" : ok === false ? "state-bad" : "state-warn";
      li.textContent = text;
      ul.append(li);
    }
    const blocked = !tw.inside || !k.configured || k.state === "invalid" || busy(s);
    $("collect").disabled = blocked;
    $("collect").title = blocked ? "See the checklist above" : "";
    $("snapshot").disabled = busy(s) || !c.exists;
    $("stop-app").disabled = busy(s);
  }

  function renderJob(s) {
    const job = s.jobs.current;
    const box = $("job");
    if (!job) { box.hidden = true; return; }
    box.hidden = false;
    $("job-phase-text").textContent = (job.kind === "snapshot" ? "Snapshot: " : "") + job.phase + "…";
    $("job-elapsed").textContent = "· " + fmtDuration(job.elapsed_s);
    const live = job.live || {};
    const bits = [];
    if (live.settings) bits.push(live.settings);
    if (live.riot_requests != null) bits.push(fmtNum(live.riot_requests) + " Riot requests so far");
    if (live.rate_limited) bits.push(live.rate_limited + " rate-limit pauses (handled automatically)");
    $("job-live").textContent = bits.join(" · ");
    const ol = $("job-steps");
    ol.replaceChildren();
    for (const step of job.steps.slice(-14)) {
      const li = document.createElement("li");
      li.className = "s-" + step.status;
      li.textContent = step.name + ": " + step.detail;
      ol.append(li);
    }
  }

  function renderResult(job, prefix, rowsFor) {
    const box = $(prefix + "-result");
    if (!job) { box.hidden = true; return; }
    box.hidden = false;
    notice($(prefix + "-result-message"), job.message, job.state === "succeeded" ? "ok" : "error");
    facts($(prefix + "-result-facts"), rowsFor(job));
    const details = $(prefix + "-result-details");
    details.hidden = !job.detail;
    $(prefix + "-result-detail").textContent = job.detail || "";
  }

  function collectRows(job) {
    const r = job.result || {};
    const validation = r.validation ? Object.entries(r.validation).map(([w, v]) =>
      w + ": " + (v.is_severe ? "FAILED (" + v.severe.join(", ") + ")" : "passed") +
      (v.warnings.length ? " — warnings: " + v.warnings.join("; ") : "")).join(" · ") : undefined;
    return [
      ["Result", job.state === "succeeded" ? "Success" : "Did not succeed"],
      ["Finished", fmtTime(job.finished_at)],
      ["Patch", r.patch],
      ["New matches", fmtNum(r.new_matches)],
      ["Already stored (skipped)", fmtNum(r.duplicates_skipped)],
      ["Current-patch matches now", fmtNum(r.patch_matches)],
      ["Boards", fmtNum(r.boards)],
      ["Latest game", r.latest_game != null ? fmtTime(r.latest_game) : undefined],
      ["Riot requests", fmtNum(r.riot_requests)],
      ["Rate-limit events", fmtNum(r.rate_limited)],
      ["Validation", validation || (r.outcome ? "not run" : undefined)],
      ["Discovery preparation", r.prepared || (r.outcome ? "not run" : undefined)],
      ["Backup", r.backup],
      ["Ready for a public snapshot", r.outcome ? (r.ready_for_snapshot ? "yes" : "no") : undefined],
      ["Report", r.report_path],
    ];
  }

  function snapshotRows(job) {
    const r = job.result || {};
    return [
      ["Result", job.state === "succeeded" ? "Created and verified" : "Not created"],
      ["Balance window", r.balance_windows ? r.balance_windows.join(", ") : undefined],
      ["Matches", r.matches != null ? fmtNum(r.matches) : undefined],
      ["Boards", r.boards != null ? fmtNum(r.boards) : undefined],
      ["Latest game", r.latest_game],
      ["Snapshot file", r.gz_path],
      ["Size", r.gz_bytes != null ? fmtBytes(r.gz_bytes) : undefined],
      ["SHA-256", r.gz_sha256],
      ["Verification", r.checks_passed != null ? r.checks_passed + " checks passed" : undefined],
      ["Manifest", r.manifest_path],
    ];
  }

  function render(s) {
    state = s;
    $("global-error").hidden = true;
    renderStatus(s);
    renderKey(s);
    renderPreflight(s);
    renderJob(s);
    const last = s.jobs.last || {};
    renderResult(s.jobs.current && s.jobs.current.kind === "collect" ? null : last.collect, "collect", collectRows);
    renderResult(s.jobs.current && s.jobs.current.kind === "snapshot" ? null : last.snapshot, "snapshot", snapshotRows);
  }

  // ------------------------------------------------------------ polling (read-only)

  async function refresh() {
    clearTimeout(timer);
    try {
      const res = await fetch("/api/state", { cache: "no-store", credentials: "same-origin" });
      if (!res.ok) throw new Error("status " + res.status);
      render(await res.json());
    } catch (err) {
      notice($("global-error"), "The collector app is not responding. If you stopped it, start it again with " +
        "“TheoryLabs Collector”.", "error");
    }
    timer = setTimeout(refresh, state && busy(state) ? 2000 : 15000);
  }

  // ------------------------------------------------------------ actions

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
      notice($("key-message"), err.message, "error");
    } finally {
      refresh();
    }
  });

  async function startJob(path, button) {
    button.disabled = true;
    try {
      await post(path);
    } catch (err) {
      notice($("global-error"), err.message, "error");
    } finally {
      refresh();
    }
  }

  $("collect").addEventListener("click", () => startJob("/api/collect", $("collect")));
  $("snapshot").addEventListener("click", () => startJob("/api/snapshot", $("snapshot")));

  $("stop-app").addEventListener("click", async () => {
    if (!window.confirm("Stop the TheoryLabs Collector app? You can start it again with “TheoryLabs Collector”.")) return;
    try {
      const data = await post("/api/shutdown");
      clearTimeout(timer);
      document.body.querySelector("main").replaceChildren();
      const p = document.createElement("p");
      p.className = "notice notice-ok";
      p.textContent = data.message;
      document.body.querySelector("main").append(p);
    } catch (err) {
      notice($("global-error"), err.message, "error");
    }
  });

  refresh();
})();
