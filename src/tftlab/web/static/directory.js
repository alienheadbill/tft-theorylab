// Item and trait directories: what was observed in one balance window, in the
// order the API gives (most boards first). Every number comes from the
// read-only /api/items or /api/traits endpoint; nothing is computed, pooled
// or re-sorted here, and no placement number is shown.

const esc = s =>
  String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
const fmtPctShort = v => (v == null ? '—' : v > 0 && v < 0.005 ? '<1%' : `${Math.round(v * 100)}%`);
const fmtNum = v => (v == null ? '—' : Number(v).toLocaleString());
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
const KIND = document.body.dataset.directory; // "items" | "traits"
const KIND_LABELS = { standard: 'standard items', emblem: 'emblems', artifact: 'artifacts', radiant: 'radiant items', unknown: 'unrecognized items' };

const listEl = document.querySelector('#directory-list');
const countEl = document.querySelector('#directory-count');
const rulesEl = document.querySelector('#directory-rules');
const windowSelect = document.querySelector('#window-filter');
const kindSelect = document.querySelector('#kind-filter');
const minInput = document.querySelector('#min-boards');
const statusDot = document.querySelector('#status-dot');

const params = new URLSearchParams(window.location.search);
const state = {
  balanceWindow: params.get('balance_window') || null,
  kind: KIND === 'items' && KIND_LABELS[params.get('kind')] ? params.get('kind') : null,
  minBoards: Math.max(MIN_BOARDS, Number.parseInt(params.get('min_boards') || '', 10) || MIN_BOARDS),
  requestSeq: 0,
};

const icon = (text, url, { cost = null, kind = 'partner' } = {}) =>
  `<span class="ref-icon ref-${kind}${artUrl(url) ? ' has-art' : ''}"${cost ? ` style="--c:${costAccent(cost)}"` : ''} aria-hidden="true"><span class="ref-letter">${esc(String(text).charAt(0).toUpperCase())}</span>${artImg(url, 24)}</span>`;

function championHref(slug) {
  const q = state.balanceWindow ? `?balance_window=${encodeURIComponent(state.balanceWindow)}` : '';
  return `/champions/${encodeURIComponent(slug)}${q}`;
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
  const label = window.TL_sourceLabel ? window.TL_sourceLabel(demo) : (demo ? 'demo data (synthetic)' : 'indexed match data');
  document.querySelector('#data-mode').textContent = label;
  document.querySelector('#footer-mode').textContent = label;
}

function syncUrl() {
  const url = new URL(window.location.href);
  const set = (k, v) => (v == null || v === '' ? url.searchParams.delete(k) : url.searchParams.set(k, String(v)));
  set('balance_window', state.balanceWindow);
  set('kind', state.kind);
  set('min_boards', state.minBoards === MIN_BOARDS ? null : state.minBoards);
  window.history.replaceState(null, '', url);
}

// ------------------------------------------------------------------ rows

// Carries under a row: most boards first (the API's order), each with the share
// of its own carry boards. `has_trait` marks a carry that brings the trait itself.
function carryList(row) {
  if (!row.carries.length) return '<p class="htp-foot">No carry reached the board floor here.</p>';
  const items = row.carries
    .map(c => `<li><a class="dir-carry" href="${esc(championHref(c.slug))}">${icon(c.name, c.art_url, { cost: c.cost })}<span class="dir-carry-name">${esc(c.name)}</span><span class="htp-num">${plural(c.boards, 'board')} · ${fmtPctShort(c.share_of_carry_boards)} of its carry boards${c.has_trait ? ' · has this trait' : ''}</span></a></li>`)
    .join('');
  const more = row.more_carries ? `<li class="dir-more htp-num">and ${plural(row.more_carries, 'more carry', 'more carries')}</li>` : '';
  return `<ul class="dir-carries">${items}${more}</ul>`;
}

function itemRow(r) {
  const it = r.item;
  return `
    <li class="dir-row">
      <div class="ev-row">
        ${icon(it.name, it.art_url, { kind: 'item' })}
        <span class="row-name">${esc(it.name)}</span>
        <span class="htp-num">${plural(r.carry_boards, 'carry board')} · ${fmtPctShort(r.share_of_carry_boards)} of all carry boards${it.kind !== 'standard' ? ` · ${esc(KIND_LABELS[it.kind] || it.kind)}` : ''}</span>
      </div>
      <p class="dir-sub">Most often held by</p>
      ${carryList(r)}
    </li>`;
}

function traitRow(r) {
  const counts = r.counts.map(c => `<li><span class="count-units">${plural(c.num_units, 'unit')}</span><span class="htp-num">${plural(c.boards, 'board')} (${fmtPctShort(c.share)})</span></li>`).join('');
  const bp = r.breakpoints
    ? `<p class="trait-bp">breakpoints ${esc(r.breakpoints.join(' / '))} <span class="trait-bp-src">game data, CommunityDragon</span></p>`
    : r.breakpoints_note ? `<p class="trait-bp trait-bp-none">${esc(r.breakpoints_note)}</p>` : '';
  return `
    <li class="dir-row">
      <div class="ev-row">
        ${icon(r.name, r.art_url, { kind: 'trait' })}
        <span class="row-name">${esc(r.name)}</span>
        <span class="htp-num">active on ${plural(r.boards, 'board')} · ${fmtPctShort(r.share_of_window_boards)} of the window's boards</span>
      </div>
      ${bp}
      <ul class="dir-counts" aria-label="${esc(r.name)} by observed unit count">${counts}</ul>
      <p class="dir-sub">Carries most often on these boards</p>
      ${carryList(r)}
    </li>`;
}

// ------------------------------------------------------------------ page

function setLedger(data) {
  document.querySelector('#window-matches').textContent = data ? fmtNum(data.matches) : '—';
  document.querySelector('#window-boards').textContent = data ? fmtNum(data.boards) : '—';
  document.querySelector('#window-latest').textContent = data ? fmtDate(data.latest_game) : '—';
}

function render(data) {
  setLedger(data);
  rulesEl.hidden = false;
  rulesEl.textContent = `${data.definition} Order: ${data.ordering}. Rows and carries need ${data.min_boards}+ boards.`;
  const rows = KIND === 'items' ? data.items : data.traits;
  const matching = KIND === 'items' ? data.matching_items : data.matching_traits;
  const noun = KIND === 'items' ? (state.kind ? KIND_LABELS[state.kind] : 'items') : 'traits';
  countEl.textContent =
    `${fmtNum(matching)} ${noun} on ${data.min_boards}+ boards, most boards first` +
    (rows.length < matching ? `; the first ${fmtNum(rows.length)} are listed` : '') +
    ` · balance window ${data.balance_window} · ${fmtNum(data.boards)} boards · latest game ${fmtDate(data.latest_game)}.`;
  if (!rows.length) {
    listEl.innerHTML = `<div class="state-note empty-evidence"><p>Nothing reached ${data.min_boards} boards with these filters in this balance window yet.</p></div>`;
    return;
  }
  listEl.innerHTML = `<ol class="ev-list dir-list">${rows.map(KIND === 'items' ? itemRow : traitRow).join('')}</ol>`;
}

function query() {
  const q = new URLSearchParams();
  if (state.balanceWindow) q.set('balance_window', state.balanceWindow);
  if (state.kind) q.set('kind', state.kind);
  q.set('min_boards', String(state.minBoards));
  return q.toString();
}

async function load() {
  const seq = ++state.requestSeq;
  listEl.innerHTML = `<p class="state-note">Counting the ${KIND} in this balance window… the first visit after new data can take a little while.</p>`;
  try {
    const data = await fetchJson(`/api/${KIND}?${query()}`);
    if (seq !== state.requestSeq) return; // a newer filter change won
    setSource(data.demo);
    render(data);
  } catch (err) {
    if (seq !== state.requestSeq) return;
    setLedger(null);
    countEl.textContent = '';
    listEl.innerHTML = `<div class="state-note error"><p>${esc(err.status === 404 ? 'No data for this balance window.' : `Could not load the ${KIND} (${err.message}).`)}</p><button type="button" data-action="retry">Try again</button></div>`;
  }
}

async function loadWindows() {
  const data = await fetchJson('/api/balance-windows');
  setSource(data.demo);
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
  if (!state.balanceWindow || !data.windows.some(w => w.balance_window === state.balanceWindow)) {
    state.balanceWindow = data.default_balance_window;
  }
  windowSelect.value = state.balanceWindow;
}

function bind() {
  document.querySelector('#directory-controls').addEventListener('submit', e => e.preventDefault());
  windowSelect.addEventListener('change', () => {
    state.balanceWindow = windowSelect.value || null;
    syncUrl();
    load();
  });
  if (kindSelect) {
    kindSelect.addEventListener('change', () => {
      state.kind = kindSelect.value || null;
      syncUrl();
      load();
    });
  }
  minInput.addEventListener('change', () => {
    const n = Number.parseInt(minInput.value, 10);
    state.minBoards = Number.isInteger(n) && n >= MIN_BOARDS ? Math.min(n, 100000) : MIN_BOARDS;
    minInput.value = String(state.minBoards);
    syncUrl();
    load();
  });
  document.addEventListener('click', e => {
    if (e.target.closest('[data-action="retry"]')) load();
  });
}

async function init() {
  if (kindSelect) kindSelect.value = state.kind || '';
  minInput.value = String(state.minBoards);
  bind();
  try {
    await loadWindows();
  } catch (err) {
    statusDot.classList.add('error');
    document.querySelector('#data-mode').textContent = 'unavailable';
  }
  syncUrl();
  if (state.balanceWindow) await load();
  else listEl.innerHTML = `<div class="state-note empty-evidence"><p>No balance window has matches yet, so there are no ${KIND} to show.</p></div>`;
}

init();
