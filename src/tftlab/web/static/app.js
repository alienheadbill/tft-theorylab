const fmtPct = v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const fmtNum = v => (v == null ? '—' : Number(v).toLocaleString());
const fmtPlace = v => (v == null ? '—' : Number(v).toFixed(2));
const fmtDelta = d => (d == null ? '' : `${d >= 0 ? '+' : '−'}${Math.abs(d * 100).toFixed(1)}`);
const initials = name => String(name).split(/\s+/).map(x => x[0]).join('').slice(0, 2).toUpperCase();
const pad2 = n => String(n).padStart(2, '0');
const esc = s =>
  String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);

// Champion/item/trait art isn't exposed by any existing API (it would need a
// live CommunityDragon fetch on every page load, deliberately not added
// yet). These helpers only reformat identifiers the backend already returns.
const humanizeId = id =>
  String(id)
    .replace(/^TFT_Item_/, '')
    .replace(/^TFT\d*_/, '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .trim();
const humanizeItemLabel = label => label.split('+').map(humanizeId).join(' + ');
const humanizeTraitLabel = label => {
  const m = label.match(/^(.*)\s\((\d+)\)$/);
  return m ? `${humanizeId(m[1])} (${m[2]})` : humanizeId(label);
};
const labelFor = (assoc, kind) =>
  kind === 'item' ? humanizeItemLabel(assoc.label) : kind === 'trait' ? humanizeTraitLabel(assoc.label) : assoc.label;

// Matches `_LOW_SAMPLE_COMMITMENT_GAMES` in tftlab.cli's discovery-smoke.
const LOW_SAMPLE_COMMITMENT_GAMES = 30;

const SORT_LABELS = {
  opportunity_score: 'opportunity score',
  top4_rate: 'top 4 rate',
  avg_placement: 'average placement',
  commitment_rate: 'rarity',
};

// Hand-drawn loops for the circled score. Picked by list position (or a
// hash of the champion id), never randomly, so a page always renders the same.
// Every point stays outside the digits' box (roughly x 22-98, y 16-56 in this
// viewBox), including the overshooting tail, so the stroke never crosses a number.
const CIRCLES = [
  'M30 10 C 70 2, 112 6, 115 32 C 117 56, 82 67, 52 65 C 20 63, 3 50, 5 32 C 7 14, 28 7, 60 5',
  'M88 7 C 112 12, 119 40, 105 57 C 89 68, 34 69, 14 55 C 1 45, 3 19, 26 10 C 44 3, 74 3, 98 10',
  'M14 20 C 24 5, 84 1, 108 13 C 121 24, 117 55, 92 63 C 64 70, 20 67, 6 48 C 0 38, 4 25, 14 16 C 20 11, 30 8, 42 7',
];
const ARROW =
  '<svg class="hand-arrow" viewBox="0 0 34 14" aria-hidden="true" focusable="false"><path d="M2 8 C 9 5, 18 9, 30 7"/><path d="M24 2.5 L 31 7 L 24.5 12"/></svg>';
const ARROW_UP =
  '<svg viewBox="0 0 20 40" aria-hidden="true" focusable="false"><path d="M10 38 C 8 28, 12 16, 10 4"/><path d="M4 10 L 10 3 L 16 9"/></svg>';

const state = {
  costs: new Set([1, 2, 3]),
  minGames: 10,
  sortBy: 'opportunity_score',
  balanceWindow: null,
  candidates: [],
  selectedId: null,
};

const cardsEl = document.querySelector('#cards');
const detailEl = document.querySelector('#detail');
const windowSelect = document.querySelector('#window-filter');
const minGamesInput = document.querySelector('#min-games');
const sortSelect = document.querySelector('#sort-by');
const gapNoteEl = document.querySelector('#gap-note');
const statusDot = document.querySelector('#status-dot');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const hashIndex = s => [...String(s)].reduce((a, ch) => a + ch.charCodeAt(0), 0);
const costAccent = cost => `var(--cost-${Math.min(5, Math.max(1, Number(cost) || 1))})`;

function stamp(kind) {
  const label = { observed: 'Observed', variant: 'Variant', theorycrafted: 'Theorycrafted' }[kind];
  return `<span class="stamp stamp-${kind}">${label}</span>`;
}

function scoreMark(score, variant) {
  return `
    <div class="score-mark">
      <span class="score-ring">
        <svg class="score-circle" viewBox="0 0 120 70" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="${CIRCLES[variant % CIRCLES.length]}"/></svg>
        <span class="score-num">${score.toFixed(1)}</span>
      </span>
      <span class="score-label">opportunity score</span>
    </div>`;
}

// Future art drops in as an <img> inside the same taped frame.
const portrait = (name, cost) =>
  `<figure class="portrait" style="--c:${costAccent(cost)}" aria-hidden="true"><span class="portrait-fallback">${esc(initials(name))}</span></figure>`;

const refIcon = (text, cost) =>
  `<span class="ref-icon"${cost ? ` style="--c:${costAccent(cost)}"` : ''} aria-hidden="true">${esc(String(text).charAt(0).toUpperCase())}</span>`;

const lowSampleNote = () =>
  `<p class="low-sample"><span class="stamp stamp-low">Low sample</span><span class="pencil">tiny sample — don't trust this yet</span></p>`;

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body.error || body.detail || '';
    } catch (_) {
      /* body wasn't JSON; fall through with no detail */
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function loadBalanceWindows() {
  const data = await fetchJson('/api/balance-windows');
  if (!data.windows.length) {
    windowSelect.innerHTML = '<option value="">no data yet</option>';
    windowSelect.disabled = true;
  } else {
    windowSelect.innerHTML = data.windows
      .map(w => `<option value="${esc(w.balance_window)}">${esc(w.balance_window)} · ${fmtNum(w.matches)} matches</option>`)
      .join('');
    windowSelect.disabled = false;
    state.balanceWindow = data.default_balance_window;
    windowSelect.value = state.balanceWindow;
  }

  if (data.unresolved_unreal_matches > 0) {
    gapNoteEl.hidden = false;
    document.querySelector('#gap-note-text').textContent =
      `${fmtNum(data.unresolved_unreal_matches)} match(es) from a client-version transition period are ` +
      'intentionally left out of every analysis until their patch is confirmed. They are not mixed into any ' +
      'balance window above, and not lost, just not classified yet.';
  } else {
    gapNoteEl.hidden = true;
  }
  return data;
}

function setSource(demo) {
  const label = demo ? 'demo dataset' : 'live Riot data';
  document.querySelector('#data-mode').textContent = label;
  document.querySelector('#footer-mode').textContent = label;
}

async function loadHealth() {
  const data = await fetchJson('/api/health');
  setSource(data.demo);
  document.querySelector('#match-count').textContent = fmtNum(data.matches);
  document.querySelector('#participant-count').textContent = fmtNum(data.participants);
  return data;
}

function showState(message, { error = false, retry = false } = {}) {
  cardsEl.innerHTML = `
    <div class="state-note${error ? ' error' : ''}">
      <div>${esc(message)}</div>
      ${retry ? '<button type="button" class="retry-btn" data-action="retry">Try again</button>' : ''}
    </div>`;
}

async function loadDiscovery(showLoading = true) {
  if (showLoading) showState('Working through the carry lines…');
  statusDot.classList.remove('error');
  try {
    const params = new URLSearchParams({ max_cost: '5', min_samples: '1', top_n: '5', limit: '200' });
    if (state.balanceWindow) params.set('balance_window', state.balanceWindow);
    const data = await fetchJson(`/api/discovery?${params}`);
    state.candidates = data.candidates;
    setSource(data.demo);
    renderCards();
  } catch (err) {
    statusDot.classList.add('error');
    showState(`Couldn't load discovery data: ${err.message}`, { error: true, retry: true });
  }
}

function filteredSortedCandidates() {
  const list = state.candidates.filter(c => state.costs.has(c.cost) && c.commitment_games >= state.minGames);
  const dir = { opportunity_score: -1, top4_rate: -1, avg_placement: 1, commitment_rate: 1 }[state.sortBy] ?? -1;
  return [...list].sort((a, b) => dir * (a[state.sortBy] - b[state.sortBy]));
}

function evidenceNote(kindLabel, assoc, kind) {
  if (!assoc) {
    return `<li class="none"><span class="ev-kind">${kindLabel}</span><span class="ev-what">no ${kindLabel} evidence yet</span></li>`;
  }
  const label = labelFor(assoc, kind);
  const delta = assoc.top4_delta == null ? '' : ` · Δ T4 ${fmtDelta(assoc.top4_delta)}`;
  return `
    <li>
      <span class="ev-kind">${kindLabel}</span>
      ${refIcon(label, kind === 'partner' ? assoc.cost : null)}
      <span class="ev-what">${esc(label)}</span>
      <span class="ev-num">${fmtNum(assoc.games)} g${delta}</span>
    </li>`;
}

function entryHtml(c, i, isLead) {
  const n = c.commitment_games;
  const low = n < LOW_SAMPLE_COMMITMENT_GAMES;
  const selected = c.character_id === state.selectedId;
  return `
    <article class="entry${isLead ? ' lead' : ''}${selected ? ' selected' : ''}" data-id="${esc(c.character_id)}" style="--c:${costAccent(c.cost)}">
      <div class="entry-tab">
        <span class="entry-no">№ ${pad2(i + 1)}</span>
        <span class="cost-marker">${c.cost}-cost</span>
        ${isLead ? `<span class="lead-note">top of the list, by ${SORT_LABELS[state.sortBy]}</span>` : ''}
      </div>
      <div class="entry-head">
        ${portrait(c.name, c.cost)}
        <div class="entry-title">
          <h3>${esc(c.name)}</h3>
          <p class="sample">n = <b>${fmtNum(n)}</b> committed games</p>
          ${low ? lowSampleNote() : ''}
        </div>
        ${scoreMark(c.opportunity_score, i)}
      </div>
      <div class="entry-body">
        <div class="entry-stats">
          <dl class="results">
            <div><dt>avg place</dt><dd>${fmtPlace(c.avg_placement)}</dd></div>
            <div><dt>top 4</dt><dd>${fmtPct(c.top4_rate)}</dd></div>
            <div><dt>win</dt><dd>${fmtPct(c.win_rate)}</dd></div>
            <div><dt>3★ hit</dt><dd>${fmtPct(c.hit_3star_rate)}</dd></div>
          </dl>
          <p class="usage">
            on <b>${fmtPct(c.appearance_rate)}</b> of boards, carried on <b>${fmtPct(c.commitment_rate)}</b>;
            converts <b>${fmtPct(c.carry_conversion_rate)}</b> of the time it's played.
            Confidence <b>${fmtPct(c.confidence)}</b>.
          </p>
        </div>
        <div class="entry-evidence">
          <p class="evidence-head"><span>evidence notes</span>${stamp('observed')}</p>
          <ul class="evidence-notes">
            ${evidenceNote('partner', c.best_partners[0], 'partner')}
            ${evidenceNote('items', c.best_item_packages[0], 'item')}
            ${evidenceNote('trait', c.best_trait_breakpoints[0], 'trait')}
          </ul>
        </div>
      </div>
      <div class="entry-foot">
        <button type="button" class="open-notes" data-action="open" aria-label="Open working notes for ${esc(c.name)}">open working notes ${ARROW}</button>
      </div>
    </article>`;
}

function renderCards() {
  const list = filteredSortedCandidates();
  document.querySelector('#candidate-count').textContent = fmtNum(list.length);

  if (!state.candidates.length) {
    showState('No discovery data for this balance window yet. Once matches are ingested, entries will show up here.');
    return;
  }
  if (!list.length) {
    showState('Nothing matches these filters. Try a lower minimum of committed games, or tick more costs.');
    return;
  }

  const [lead, ...rest] = list;
  cardsEl.innerHTML =
    entryHtml(lead, 0, true) +
    (rest.length ? `<div class="spread">${rest.map((c, i) => entryHtml(c, i + 1, false)).join('')}</div>` : '');
}

function markSelected(id) {
  cardsEl.querySelectorAll('.entry').forEach(el => el.classList.toggle('selected', el.dataset.id === id));
}

function renderEmptyDetail() {
  detailEl.className = 'notes-page is-empty';
  detailEl.innerHTML = `<p class="notes-empty">${ARROW_UP}No entry open yet. Pick one from the discoveries above.</p>`;
}

function ledgerList(items, kind) {
  if (!items.length) return '<p class="none-note">No evidence yet.</p>';
  const rows = items
    .map(a => {
      const label = labelFor(a, kind);
      const delta =
        a.top4_delta == null
          ? ''
          : ` · <span class="${a.top4_delta >= 0 ? 'delta-pos' : 'delta-neg'}">Δ ${fmtDelta(a.top4_delta)}</span>`;
      return `
        <li>
          ${refIcon(label, kind === 'partner' ? a.cost : null)}
          <span class="ll-name">${esc(label)}</span>
          <span class="ll-dots" aria-hidden="true"></span>
          <span class="ll-num"><span class="t4">${fmtPct(a.top4_rate)}</span> T4${delta} · ${fmtNum(a.games)} g</span>
        </li>`;
    })
    .join('');
  return `<p class="col-legend">top 4 with it · Δ top 4 vs. without it (pts, shrinkage-adjusted) · games</p><ol class="ledger-list">${rows}</ol>`;
}

async function showDetail(id) {
  detailEl.className = 'notes-page';
  detailEl.innerHTML = '<p class="state-note">Opening the working notes…</p>';
  detailEl.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
  try {
    const params = new URLSearchParams({ top_n: '8' });
    if (state.balanceWindow) params.set('balance_window', state.balanceWindow);
    const data = await fetchJson(`/api/discovery/${encodeURIComponent(id)}?${params}`);
    const c = data.candidate;
    const n = c.commitment_games;
    const low = n < LOW_SAMPLE_COMMITMENT_GAMES;
    // hit_3star_rate is hits / commitment_games, so this recovers the exact count.
    const hits = Math.round(c.hit_3star_rate * n);
    const misses = n - hits;

    detailEl.innerHTML = `
      <div class="notes-row">
        <p class="margin-note quiet">window<br>${esc(c.balance_window)}</p>
        <div class="notes-head">
          <div>
            <p class="notes-kicker">working notes</p>
            <h3 tabindex="-1">${esc(c.name)}</h3>
            <p class="notes-meta">${c.cost}-cost · n = <b>${fmtNum(n)}</b> committed games</p>
            ${low ? lowSampleNote() : ''}
          </div>
          ${scoreMark(c.opportunity_score, hashIndex(c.character_id))}
        </div>
      </div>

      <div class="notes-row">
        <p class="margin-note quiet">every game where it held 2+ completed items</p>
        <section class="notes-section" aria-label="Results when committed">
          <h4>Results when committed</h4>
          <dl class="results big">
            <div><dt>avg place</dt><dd>${fmtPlace(c.avg_placement)}</dd></div>
            <div><dt>top 4</dt><dd>${fmtPct(c.top4_rate)}</dd></div>
            <div><dt>win</dt><dd>${fmtPct(c.win_rate)}</dd></div>
            <div><dt>3★ hit</dt><dd>${fmtPct(c.hit_3star_rate)}</dd></div>
          </dl>
        </section>
      </div>

      <div class="notes-row">
        <p class="margin-note">ceiling vs. floor: a line that only works on the hit is fragile</p>
        <section class="notes-section" aria-label="Hit versus miss">
          <h4><span class="underlined">Hit vs. miss</span></h4>
          <div class="hitmiss">
            <div>
              <p class="hm-label">when it hits 3★</p>
              <p class="hm-num">${fmtPct(c.hit_top4_rate)}</p>
              <p class="hm-sub">top 4 · n = ${fmtNum(hits)}</p>
            </div>
            <span class="hm-vs" aria-hidden="true">vs.</span>
            <div>
              <p class="hm-label">when it misses</p>
              <p class="hm-num">${fmtPct(c.miss_top4_rate)}</p>
              <p class="hm-sub">top 4 · n = ${fmtNum(misses)}</p>
            </div>
          </div>
        </section>
      </div>

      <div class="notes-row">
        <p class="margin-note">ranked by shrinkage-adjusted association, not raw top 4</p>
        <section class="notes-section" aria-label="Partners">
          <h4>Partners ${stamp('observed')}</h4>
          ${ledgerList(c.best_partners, 'partner')}
        </section>
      </div>

      <div class="notes-row">
        <p class="margin-note quiet">completed items only; pairs and exact 3-item sets</p>
        <section class="notes-section" aria-label="Item packages">
          <h4>Item packages ${stamp('observed')}</h4>
          ${ledgerList(c.best_item_packages, 'item')}
        </section>
      </div>

      <div class="notes-row">
        <p class="margin-note quiet">active trait breakpoints on the carry's board</p>
        <section class="notes-section" aria-label="Trait breakpoints">
          <h4>Trait breakpoints ${stamp('observed')}</h4>
          ${ledgerList(c.best_trait_breakpoints, 'trait')}
        </section>
      </div>
    `;
    detailEl.querySelector('h3').focus({ preventScroll: true });
  } catch (err) {
    detailEl.innerHTML = `<p class="state-note error">Couldn't load these notes: ${esc(err.message)}</p>`;
  }
}

function selectEntry(id) {
  state.selectedId = id;
  markSelected(id);
  showDetail(id);
}

function bindControls() {
  document.querySelector('#controls').addEventListener('submit', e => e.preventDefault());

  document.querySelectorAll('.cost-tab input').forEach(input => {
    input.addEventListener('change', () => {
      const cost = Number(input.dataset.cost);
      if (input.checked) {
        state.costs.add(cost);
      } else if (state.costs.size === 1) {
        input.checked = true; // keep at least one cost ticked
      } else {
        state.costs.delete(cost);
      }
      renderCards();
    });
  });

  minGamesInput.addEventListener('change', () => {
    state.minGames = Math.max(1, Number(minGamesInput.value) || 1);
    renderCards();
  });

  sortSelect.addEventListener('change', () => {
    state.sortBy = sortSelect.value;
    renderCards();
  });

  windowSelect.addEventListener('change', () => {
    state.balanceWindow = windowSelect.value || null;
    state.selectedId = null;
    renderEmptyDetail();
    loadDiscovery();
  });

  cardsEl.addEventListener('click', e => {
    if (e.target.closest('[data-action="retry"]')) {
      loadDiscovery(true);
      return;
    }
    const entry = e.target.closest('.entry');
    if (entry) selectEntry(entry.dataset.id);
  });
}

async function init() {
  bindControls();
  renderEmptyDetail();
  try {
    await Promise.all([loadHealth(), loadBalanceWindows()]);
  } catch (err) {
    statusDot.classList.add('error');
    document.querySelector('#data-mode').textContent = 'unavailable';
  }
  await loadDiscovery();
}

init();
