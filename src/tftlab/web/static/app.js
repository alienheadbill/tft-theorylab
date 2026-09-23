const fmtPct = v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const fmtNum = v => (v == null ? '—' : Number(v).toLocaleString());
const fmtPlace = v => (v == null ? '—' : Number(v).toFixed(2));
const initials = name => name.split(/\s+/).map(x => x[0]).join('').slice(0, 2).toUpperCase();

// Champion/item/trait art isn't exposed by any existing API (it would
// require adding a live CommunityDragon fetch to every page load, which
// this milestone deliberately does not do -- see the PR description). These
// helpers only reformat identifiers the backend already returns; they never
// invent data.
const humanizeId = id =>
  String(id)
    .replace(/^TFT_Item_/, '')
    .replace(/^TFT\d*_/, '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .trim();

const humanizeItemLabel = label => label.split('+').map(humanizeId).join(' + ');

const humanizeTraitLabel = label => {
  const m = label.match(/^(.*)\s\((\d+)\)$/);
  if (!m) return humanizeId(label);
  return `${humanizeId(m[1])} (${m[2]})`;
};

// Below this many commitment games, evidence is too thin to present without
// a caveat. Mirrors `_LOW_SAMPLE_COMMITMENT_GAMES` in tftlab.cli's
// discovery-smoke command, so the web dashboard and the CLI agree on what
// "low sample" means.
const LOW_SAMPLE_COMMITMENT_GAMES = 30;

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

function evidenceBadge(kind) {
  const label = { observed: 'OBSERVED', variant: 'VARIANT', theorycrafted: 'THEORYCRAFTED' }[kind];
  return `<span class="evidence-badge evidence-${kind}">${label}</span>`;
}

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
  windowSelect.innerHTML = data.windows
    .map(w => `<option value="${w.balance_window}">${w.balance_window} (${fmtNum(w.matches)} matches)</option>`)
    .join('');

  if (!data.windows.length) {
    windowSelect.innerHTML = '<option value="">No data yet</option>';
    windowSelect.disabled = true;
  } else {
    windowSelect.disabled = false;
    state.balanceWindow = data.default_balance_window;
    windowSelect.value = state.balanceWindow;
  }

  if (data.unresolved_unreal_matches > 0) {
    gapNoteEl.hidden = false;
    gapNoteEl.textContent =
      `Note: ${fmtNum(data.unresolved_unreal_matches)} match(es) from a client-version transition period are ` +
      'intentionally excluded from analytics until their patch is confirmed. They are not mixed into any balance ' +
      'window above and are not lost -- just not yet classified.';
  } else {
    gapNoteEl.hidden = true;
  }

  return data;
}

async function loadHealth() {
  const data = await fetchJson('/api/health');
  document.querySelector('#data-mode').textContent = data.demo ? 'Demo dataset' : 'Live Riot dataset';
  document.querySelector('#footer-mode').textContent = data.demo ? 'Demo dataset' : 'Live Riot dataset';
  document.querySelector('#match-count').textContent = fmtNum(data.matches);
  document.querySelector('#participant-count').textContent = fmtNum(data.participants);
  return data;
}

function showState(message, { error = false, retry = false } = {}) {
  cardsEl.innerHTML = `
    <div class="state-card${error ? ' error' : ''}">
      <div>${message}</div>
      ${retry ? '<button type="button" id="retry-btn">Retry</button>' : ''}
    </div>`;
  if (retry) {
    document.querySelector('#retry-btn').addEventListener('click', () => loadDiscovery(true));
  }
}

async function loadDiscovery(showLoading = true) {
  if (showLoading) showState('Analyzing carry lines…');
  statusDot.classList.remove('error');
  try {
    const params = new URLSearchParams({ max_cost: '5', min_samples: '1', top_n: '5', limit: '200' });
    if (state.balanceWindow) params.set('balance_window', state.balanceWindow);
    const data = await fetchJson(`/api/discovery?${params}`);
    state.candidates = data.candidates;
    document.querySelector('#data-mode').textContent = data.demo ? 'Demo dataset' : 'Live Riot dataset';
    renderCards();
  } catch (err) {
    statusDot.classList.add('error');
    showState(`Could not load discovery data: ${err.message}`, { error: true, retry: true });
  }
}

function costAccent(cost) {
  return `var(--cost-${Math.min(5, Math.max(1, cost))})`;
}

function filteredSortedCandidates() {
  let list = state.candidates.filter(c => state.costs.has(c.cost) && c.commitment_games >= state.minGames);

  const dir = { opportunity_score: -1, top4_rate: -1, avg_placement: 1, commitment_rate: 1 }[state.sortBy] ?? -1;
  list = [...list].sort((a, b) => dir * (a[state.sortBy] - b[state.sortBy]));
  return list;
}

function evidencePreviewRow(label, assoc, kind) {
  if (!assoc) return `<div class="evidence-row"><span class="evidence-none">No ${label.toLowerCase()} evidence yet</span></div>`;
  const niceLabel = kind === 'item' ? humanizeItemLabel(assoc.label) : kind === 'trait' ? humanizeTraitLabel(assoc.label) : assoc.label;
  return `
    <div class="evidence-row">
      <span class="ev-label">${label}: <strong>${niceLabel}</strong> ${evidenceBadge('observed')}</span>
      <span class="ev-meta">${assoc.games} games</span>
    </div>`;
}

function renderCards() {
  const list = filteredSortedCandidates();
  document.querySelector('#candidate-count').textContent = fmtNum(list.length);

  if (!state.candidates.length) {
    showState('No discovery data yet for this balance window. Once matches are ingested, candidates will appear here.');
    return;
  }
  if (!list.length) {
    showState('No candidates match these filters. Try lowering the minimum commitment games or widening the cost range.');
    return;
  }

  cardsEl.innerHTML = list
    .map(c => {
      const accent = costAccent(c.cost);
      const lowSample = c.commitment_games < LOW_SAMPLE_COMMITMENT_GAMES;
      const bestPartner = c.best_partners[0];
      const bestItem = c.best_item_packages[0];
      const bestTrait = c.best_trait_breakpoints[0];
      return `
      <article class="carry-card${c.character_id === state.selectedId ? ' selected' : ''}" data-id="${c.character_id}" style="--card-accent:${accent}">
        <div class="card-top">
          <div class="avatar">${initials(c.name)}</div>
          <div class="card-heading">
            <h3>${c.name}</h3>
            <div class="tags">
              <span class="tag cost">${c.cost}-cost</span>
              <span class="tag">${fmtNum(c.commitment_games)} commits</span>
              ${lowSample ? '<span class="tag low-sample">LOW SAMPLE</span>' : ''}
            </div>
          </div>
          <div class="card-score"><strong>${c.opportunity_score.toFixed(1)}</strong><span>Opportunity</span></div>
        </div>
        <div class="stat-row">
          <div class="stat"><small>Avg place</small><strong>${fmtPlace(c.avg_placement)}</strong></div>
          <div class="stat"><small>Top 4</small><strong>${fmtPct(c.top4_rate)}</strong></div>
          <div class="stat"><small>Win</small><strong>${fmtPct(c.win_rate)}</strong></div>
          <div class="stat"><small>3★ hit</small><strong>${fmtPct(c.hit_3star_rate)}</strong></div>
          <div class="stat"><small>Appearance</small><strong>${fmtPct(c.appearance_rate)}</strong></div>
          <div class="stat"><small>Commitment</small><strong>${fmtPct(c.commitment_rate)}</strong></div>
          <div class="stat"><small>Conversion</small><strong>${fmtPct(c.carry_conversion_rate)}</strong></div>
          <div class="stat"><small>Confidence</small><strong>${fmtPct(c.confidence)}</strong></div>
        </div>
        <div class="evidence-preview">
          ${evidencePreviewRow('Best partner', bestPartner, 'partner')}
          ${evidencePreviewRow('Item package', bestItem, 'item')}
          ${evidencePreviewRow('Trait breakpoint', bestTrait, 'trait')}
        </div>
      </article>`;
    })
    .join('');

  document.querySelectorAll('.carry-card').forEach(el =>
    el.addEventListener('click', () => {
      state.selectedId = el.dataset.id;
      renderCards();
      showDetail(el.dataset.id);
    })
  );
}

function evidenceGroup(title, items, kind) {
  if (!items.length) return `<div class="evidence-group"><h4>${title}</h4><p class="evidence-none">No evidence yet.</p></div>`;
  const rows = items
    .map(a => {
      const label = kind === 'item' ? humanizeItemLabel(a.label) : kind === 'trait' ? humanizeTraitLabel(a.label) : a.label;
      return `<div class="evidence-item"><span>${label}</span><span>${fmtPct(a.top4_rate)} T4 · ${a.games}g</span></div>`;
    })
    .join('');
  return `<div class="evidence-group"><h4>${title} ${evidenceBadge('observed')}</h4>${rows}</div>`;
}

async function showDetail(id) {
  detailEl.className = 'detail';
  detailEl.innerHTML = '<div class="state-card">Building evidence panel…</div>';
  detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    const params = new URLSearchParams({ top_n: '8' });
    if (state.balanceWindow) params.set('balance_window', state.balanceWindow);
    const data = await fetchJson(`/api/discovery/${encodeURIComponent(id)}?${params}`);
    const c = data.candidate;
    const lowSample = c.commitment_games < LOW_SAMPLE_COMMITMENT_GAMES;
    detailEl.innerHTML = `
      <div class="detail-header">
        <div>
          <div class="eyebrow">EVIDENCE PANEL</div>
          <h3>${c.name}</h3>
          <p>${c.cost}-cost carry · ${fmtNum(c.commitment_games)} committed games${lowSample ? ' · <span style="color:var(--amber)">LOW SAMPLE</span>' : ''} · balance window ${c.balance_window}</p>
        </div>
        <div class="detail-score"><strong>${c.opportunity_score.toFixed(1)}</strong><span>opportunity score</span></div>
      </div>
      <div class="detail-grid">
        <div class="detail-panel"><h4>Commitment profile</h4><div class="metric-list">
          <div><span>Top 4</span><strong>${fmtPct(c.top4_rate)}</strong></div>
          <div><span>Avg place</span><strong>${fmtPlace(c.avg_placement)}</strong></div>
          <div><span>3★ hit</span><strong>${fmtPct(c.hit_3star_rate)}</strong></div>
          <div><span>Win</span><strong>${fmtPct(c.win_rate)}</strong></div>
          <div><span>Top 4 when hit</span><strong>${fmtPct(c.hit_top4_rate)}</strong></div>
          <div><span>Top 4 when missed</span><strong>${fmtPct(c.miss_top4_rate)}</strong></div>
        </div></div>
        ${evidenceGroup('Recurring partners', c.best_partners, 'partner')}
        ${evidenceGroup('Item packages', c.best_item_packages, 'item')}
        ${evidenceGroup('Trait breakpoints', c.best_trait_breakpoints, 'trait')}
      </div>
    `;
  } catch (err) {
    detailEl.innerHTML = `<div class="state-card error">Could not load evidence: ${err.message}</div>`;
  }
}

function bindControls() {
  document.querySelectorAll('#cost-pills .pill').forEach(btn => {
    btn.addEventListener('click', () => {
      const cost = Number(btn.dataset.cost);
      if (state.costs.has(cost)) {
        if (state.costs.size === 1) return; // keep at least one cost selected
        state.costs.delete(cost);
        btn.classList.remove('active');
      } else {
        state.costs.add(cost);
        btn.classList.add('active');
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
    detailEl.className = 'detail empty-detail';
    detailEl.innerHTML = '<div class="empty-orb">?</div><h3>Choose a candidate</h3><p>The evidence panel builds from the same statistically-adjusted data behind the discovery feed above.</p>';
    loadDiscovery();
  });
}

async function init() {
  bindControls();
  try {
    await Promise.all([loadHealth(), loadBalanceWindows()]);
  } catch (err) {
    statusDot.classList.add('error');
    document.querySelector('#data-mode').textContent = 'Unavailable';
  }
  await loadDiscovery();
}

init();
