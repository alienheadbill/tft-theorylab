// Observed comps: exact final lineups from every board in one balance window,
// grouped into observed board patterns and ordered by how many boards they
// appeared on. Every number comes from the read-only /api/comps endpoint, in
// the order the API gives; nothing is computed, pooled or re-sorted here.

const esc = s =>
  String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
const fmtPct = v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const fmtPctShort = v => (v == null ? '—' : `${Math.round(v * 100)}%`);
const fmtNum = v => (v == null ? '—' : Number(v).toLocaleString());
const fmtPlace = v => (v == null ? '—' : Number(v).toFixed(2));
const fmtDate = ms => {
  const d = new Date(Number(ms));
  return !ms || Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
};
const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;
const costAccent = cost => `var(--cost-${Math.min(5, Math.max(1, Number(cost) || 1))})`;

// The product floor (the API refuses anything lower).
const MIN_BOARDS = 10;
// Carries named on a card before "and N more".
const CARRIES_NAMED = 3;

const listEl = document.querySelector('#comps-list');
const countEl = document.querySelector('#comps-count');
const rulesEl = document.querySelector('#comps-rules');
const windowSelect = document.querySelector('#window-filter');
const levelSelect = document.querySelector('#level-filter');
const costSelect = document.querySelector('#cost-filter');
const minInput = document.querySelector('#min-boards');
const statusDot = document.querySelector('#status-dot');

const params = new URLSearchParams(window.location.search);
const intParam = (name, lo, hi) => {
  const n = Number.parseInt(params.get(name) || '', 10);
  return Number.isInteger(n) && n >= lo && n <= hi ? n : null;
};

const state = {
  balanceWindow: params.get('balance_window') || null,
  level: intParam('level', 1, 10),
  cost: intParam('cost', 1, 5),
  minBoards: intParam('min_boards', MIN_BOARDS, 100000) || MIN_BOARDS,
  windows: [],
  requestSeq: 0,
};

function stamp(kind, label) {
  return `<span class="stamp stamp-${kind}">${esc(label)}</span>`;
}

const refIcon = (text, url, cost) =>
  `<span class="ref-icon ref-partner${artUrl(url) ? ' has-art' : ''}"${cost ? ` style="--c:${costAccent(cost)}"` : ''} aria-hidden="true"><span class="ref-letter">${esc(String(text).charAt(0).toUpperCase())}</span>${artImg(url, 24)}</span>`;

// A champion's own page; the Boards tab for a card's lead carry.
function championHref(slug, tab = '') {
  const q = state.balanceWindow ? `?balance_window=${encodeURIComponent(state.balanceWindow)}` : '';
  return `/champions/${encodeURIComponent(slug)}${q}${tab ? `#${tab}` : ''}`;
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body.error || body.detail || '';
    } catch (_) {
      /* not JSON */
    }
    const err = new Error(detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function setSource(demo) {
  // site.js owns the wording (and the source banner); this only keeps the ledger in step.
  const label = window.TL_sourceLabel ? window.TL_sourceLabel(demo) : (demo ? 'demo data (synthetic)' : 'indexed match data');
  document.querySelector('#data-mode').textContent = label;
  document.querySelector('#footer-mode').textContent = label;
}

function syncUrl() {
  const url = new URL(window.location.href);
  const set = (k, v) => (v == null || v === '' ? url.searchParams.delete(k) : url.searchParams.set(k, String(v)));
  set('balance_window', state.balanceWindow);
  set('level', state.level);
  set('cost', state.cost);
  set('min_boards', state.minBoards === MIN_BOARDS ? null : state.minBoards);
  window.history.replaceState(null, '', url);
}

// ------------------------------------------------------------------ cards

function unitItem(u) {
  const label = `${esc(u.name)}${u.is_carry ? ' <span class="board-carry-tag">carry</span>' : ''}`;
  return `<li class="board-unit${u.is_carry ? ' is-carry' : ''}"><a class="board-unit-inner" href="${esc(championHref(u.slug))}">${refIcon(u.name, u.art_url, u.cost)}<span class="board-unit-name">${label}</span></a></li>`;
}

const unitList = units => units.map(u => `${u.name}${u.is_carry ? ' (carry)' : ''}`).join(', ');

// One exact lineup's descriptive results, always under the same label.
function placed(x) {
  const p = x.how_these_boards_placed;
  return `
      <dl class="board-stats" aria-label="${esc(p.label)}">
        <div><dt>Avg place</dt><dd>${fmtPlace(p.avg_placement)}</dd></div>
        <div><dt>Top 4</dt><dd>${fmtPctShort(p.top4_rate)}</dd></div>
        <div><dt>Win</dt><dd>${fmtPctShort(p.win_rate)}</dd></div>
      </dl>`;
}

const lowStamp = x => (x.sample.low_sample ? stamp('low', 'LOW SAMPLE') : '');

function flexOption(o, level) {
  const u = o.flex_unit;
  return `
        <li class="flex-option">
          <details class="flex-results">
            <summary>${refIcon(u.name, u.art_url, u.cost)}<span class="flex-name">${esc(u.name)}</span><span class="flex-count"><b>${plural(o.boards, 'board')}</b> · ${fmtPct(o.share_of_level)}<span class="visually-hidden"> of level-${level} boards; how the boards of the exact lineup with ${esc(u.name)} placed</span></span><span class="flex-more" aria-hidden="true">how they placed</span></summary>
            <p class="pattern-label">How these boards placed</p>
            ${placed(o)}
            <p class="board-stamps">${stamp('observed', 'Observed')}${lowStamp(o)}</p>
          </details>
        </li>`;
}

function carriesLine(p) {
  const named = p.carries.slice(0, CARRIES_NAMED).map(c => `${esc(c.name)} on ${plural(c.boards, 'board')}`);
  const more = p.carries_observed > named.length ? `; ${plural(p.carries_observed - named.length, 'other carry', 'other carries')}` : '';
  const none = p.boards_without_committed_carry ? `; ${plural(p.boards_without_committed_carry, 'board')} without a committed carry` : '';
  const list = named.length ? `${named.join('; ')}${more}${none}` : 'none of these boards had a committed carry';
  return `<p class="board-special comp-carries">Committed carries: ${list}.</p>`;
}

function freshnessLine(p, data) {
  const stale = data.source && data.source.stale ? ' · older data' : '';
  const link = p.lead_carry
    ? ` · <a href="${esc(championHref(p.lead_carry.slug, 'boards'))}">${esc(p.lead_carry.name)}'s boards →</a>`
    : '';
  return `<p class="htp-foot comp-window">Balance window ${esc(data.balance_window)} · latest game ${fmtDate(data.latest_game)}${stale}${link}</p>`;
}

function compCard(p, data) {
  const title = p.lead_carry
    ? `<a href="${esc(championHref(p.lead_carry.slug, 'boards'))}">${esc(p.name)}</a>`
    : esc(p.name);
  const kind = p.type === 'flex_shell' ? 'Observed board pattern' : 'Exact lineup';
  const head = `
      <h3 class="comp-name">${title}</h3>
      <p class="board-level-tag">Level ${p.level} · ${plural(p.unit_count, 'unit')} · ${kind}</p>
      <p class="board-count"><b>${plural(p.boards, 'board')}</b> <span>${fmtPct(p.share_of_level)} of the ${fmtNum(p.window_boards_at_level)} level-${p.level} boards in this window</span></p>`;
  let body;
  if (p.type === 'flex_shell') {
    body = `
      <p class="pattern-label">Shared shell</p>
      <ul class="board-units" aria-label="${esc(`Shared shell: ${unitList(p.shared_units)}`)}">${p.shared_units.map(unitItem).join('')}</ul>
      <p class="pattern-label pattern-flex">Flex slot · ${plural(p.lineup_count, 'observed option')}<span>boards and share of level-${p.level} boards, each its own exact lineup</span></p>
      <ol class="flex-options">${p.lineups.map(o => flexOption(o, p.level)).join('')}</ol>`;
  } else {
    const row = p.lineups[0];
    body = `
      <ul class="board-units" aria-label="${esc(`Lineup: ${unitList(p.shared_units)}`)}">${p.shared_units.map(unitItem).join('')}</ul>
      <p class="pattern-label">How these boards placed</p>
      ${placed(row)}
      <p class="board-stamps">${stamp('observed', 'Observed')}${lowStamp(row)}</p>`;
  }
  return `
    <li class="board-card comp-card${p.type === 'flex_shell' ? ' board-pattern' : ''}">
      ${head}${body}
      ${carriesLine(p)}
      ${freshnessLine(p, data)}
    </li>`;
}

// ------------------------------------------------------------------ page

function setLedger(data) {
  document.querySelector('#window-matches').textContent = data ? fmtNum(data.matches) : '—';
  document.querySelector('#window-boards').textContent = data ? fmtNum(data.boards) : '—';
  document.querySelector('#window-latest').textContent = data ? fmtDate(data.latest_game) : '—';
}

function setLevels(data) {
  const levels = (data && data.levels) || [];
  levelSelect.innerHTML = '<option value="">all levels</option>' + levels
    .map(g => `<option value="${g.level}">level ${g.level} · ${plural(g.window_boards_at_level, 'board')}</option>`)
    .join('');
  // Keep a level only if the window has boards at it.
  if (state.level != null && !levels.some(g => g.level === state.level)) state.level = null;
  levelSelect.value = state.level == null ? '' : String(state.level);
}

function render(data) {
  setLedger(data);
  setLevels(data);
  rulesEl.hidden = false;
  rulesEl.textContent = `${data.promotion_rule.rule} Order: ${data.ordering}. ${data.placement_note}`;
  if (!data.available) {
    countEl.textContent = '';
    listEl.innerHTML = `<div class="state-note empty-evidence"><p>${esc(data.reason)}</p></div>`;
    return;
  }
  countEl.textContent =
    `${plural(data.matching_patterns, 'pattern')} shown of ${fmtNum(data.qualifying_patterns)} ` +
    `(${plural(data.qualifying_lineups, 'exact lineup')} on ${data.promotion_rule.min_boards}+ boards at one level), most boards first.` +
    (data.patterns.length < data.matching_patterns ? ` The first ${fmtNum(data.patterns.length)} are listed.` : '') +
    ` Balance window ${data.balance_window} · ${fmtNum(data.boards)} boards · latest game ${fmtDate(data.latest_game)}.`;
  if (!data.patterns.length) {
    listEl.innerHTML = `<div class="state-note empty-evidence"><p>${esc(data.note || 'No observed pattern matches these filters.')}</p></div>`;
    return;
  }
  listEl.innerHTML = `<ol class="board-list comp-list">${data.patterns.map(p => compCard(p, data)).join('')}</ol>`;
}

function query() {
  const q = new URLSearchParams();
  if (state.balanceWindow) q.set('balance_window', state.balanceWindow);
  if (state.level != null) q.set('level', String(state.level));
  if (state.cost != null) q.set('cost', String(state.cost));
  q.set('min_boards', String(state.minBoards));
  return q.toString();
}

async function loadComps() {
  const seq = ++state.requestSeq;
  listEl.innerHTML = '<p class="state-note">Loading observed comps…</p>';
  try {
    const data = await fetchJson(`/api/comps?${query()}`);
    if (seq !== state.requestSeq) return; // a newer filter change won
    setSource(data.demo);
    render(data);
  } catch (err) {
    if (seq !== state.requestSeq) return;
    setLedger(null);
    countEl.textContent = '';
    listEl.innerHTML = `<div class="state-note error"><p>${esc(err.status === 404 ? 'No observed comps for this balance window.' : `Could not load observed comps (${err.message}).`)}</p><button type="button" data-action="retry">Try again</button></div>`;
  }
}

async function loadWindows() {
  const data = await fetchJson('/api/balance-windows');
  setSource(data.demo);
  state.windows = data.windows;
  if (!data.windows.length) {
    windowSelect.innerHTML = '<option value="">no data yet</option>';
    windowSelect.disabled = true;
    state.balanceWindow = null;
    return;
  }
  windowSelect.innerHTML = data.windows
    .map(w => `<option value="${esc(w.balance_window)}">${esc(w.balance_window)} · ${fmtNum(w.matches)} matches</option>`)
    .join('');
  windowSelect.disabled = false;
  // Keep a window from the URL only if it really exists; never invent one.
  if (!state.balanceWindow || !data.windows.some(w => w.balance_window === state.balanceWindow)) {
    state.balanceWindow = data.default_balance_window;
  }
  windowSelect.value = state.balanceWindow;
}

function bind() {
  document.querySelector('#comps-controls').addEventListener('submit', e => e.preventDefault());
  windowSelect.addEventListener('change', () => {
    state.balanceWindow = windowSelect.value || null;
    state.level = null; // levels differ per window
    syncUrl();
    loadComps();
  });
  levelSelect.addEventListener('change', () => {
    state.level = levelSelect.value ? Number(levelSelect.value) : null;
    syncUrl();
    loadComps();
  });
  costSelect.addEventListener('change', () => {
    state.cost = costSelect.value ? Number(costSelect.value) : null;
    syncUrl();
    loadComps();
  });
  minInput.addEventListener('change', () => {
    const n = Number.parseInt(minInput.value, 10);
    state.minBoards = Number.isInteger(n) && n >= MIN_BOARDS ? Math.min(n, 100000) : MIN_BOARDS;
    minInput.value = String(state.minBoards);
    syncUrl();
    loadComps();
  });
  document.addEventListener('click', e => {
    if (e.target.closest('[data-action="retry"]')) loadComps();
  });
}

async function init() {
  costSelect.value = state.cost == null ? '' : String(state.cost);
  minInput.value = String(state.minBoards);
  bind();
  try {
    await loadWindows();
  } catch (err) {
    statusDot.classList.add('error');
    document.querySelector('#data-mode').textContent = 'unavailable';
  }
  syncUrl();
  if (state.balanceWindow) await loadComps();
  else listEl.innerHTML = '<div class="state-note empty-evidence"><p>No balance window has matches yet, so there are no observed comps to show.</p></div>';
}

init();
