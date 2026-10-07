// Champion Investigation: pick a champion by name, then read what indexed
// ranked matches (one balance window) say about carrying with it. Every
// number comes from the read-only /api/champions endpoints; nothing is
// computed here beyond formatting and filtering the picker list.

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
// Same idea as the server's name_key: case, accents, spaces and apostrophes are ignored.
const nameKey = s =>
  String(s ?? '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
const initials = name =>
  String(name || '?')
    .replace(/[^\p{L}\p{N}\s]/gu, '')
    .split(/\s+/)
    .filter(w => /^\p{L}/u.test(w))
    .map(x => x[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() || '?';
const costAccent = cost => `var(--cost-${Math.min(5, Math.max(1, Number(cost) || 1))})`;

// Below Discovery's own minimum sample (10 games) a hit/miss split is shaky.
const SMALL_SPLIT = 10;

const viewEl = document.querySelector('#champion-view');
const viewStatus = document.querySelector('#view-status');
const pickerEl = document.querySelector('#champion-picker');
const listEl = document.querySelector('#champion-list');
const countEl = document.querySelector('#picker-count');
const searchInput = document.querySelector('#champion-search');
const onlyCarried = document.querySelector('#only-carried');
const windowSelect = document.querySelector('#window-filter');
const statusDot = document.querySelector('#status-dot');

const params = new URLSearchParams(window.location.search);
const pathKey = decodeURIComponent(window.location.pathname.replace(/^\/champions\/?/, '').replace(/\/$/, ''));

const state = {
  key: pathKey || null,
  balanceWindow: params.get('balance_window') || null,
  windows: [],
  champions: [],
  championsStatus: 'idle',
  championsRequestSeq: 0,
  currentId: null,
  tab: null,
};

function stamp(kind, label) {
  return `<span class="stamp stamp-${kind}">${esc(label)}</span>`;
}

const portrait = (name, cost, url, size = 64) =>
  `<figure class="portrait${size > 64 ? ' portrait-large' : ''}${artUrl(url) ? ' has-art' : ''}" style="--c:${costAccent(cost)}" aria-hidden="true"><span class="portrait-fallback">${esc(initials(name))}</span>${artImg(url, size)}</figure>`;

const refIcon = (text, url, { cost = null, kind = '' } = {}) =>
  `<span class="ref-icon${kind ? ` ref-${kind}` : ''}${artUrl(url) ? ' has-art' : ''}"${cost ? ` style="--c:${costAccent(cost)}"` : ''} aria-hidden="true"><span class="ref-letter">${esc(String(text).charAt(0).toUpperCase())}</span>${artImg(url, 24)}</span>`;

const windowQuery = () => (state.balanceWindow ? `?balance_window=${encodeURIComponent(state.balanceWindow)}` : '');
const championHref = slug => `/champions/${encodeURIComponent(slug)}${windowQuery()}`;
const allChampionsHref = () => `/champions${windowQuery()}`;

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

function syncUrlWindow() {
  const url = new URL(window.location.href);
  if (state.balanceWindow) url.searchParams.set('balance_window', state.balanceWindow);
  else url.searchParams.delete('balance_window');
  window.history.replaceState(null, '', url);
}

function setWindowLedger() {
  const w = state.windows.find(x => x.balance_window === state.balanceWindow);
  document.querySelector('#window-matches').textContent = w ? fmtNum(w.matches) : '—';
  document.querySelector('#window-latest').textContent = w ? fmtDate(w.latest_game_datetime) : '—';
}

async function loadWindows() {
  const data = await fetchJson('/api/balance-windows');
  setSource(data.demo);
  state.windows = data.windows;
  if (!data.windows.length) {
    windowSelect.innerHTML = '<option value="">no data yet</option>';
    windowSelect.disabled = true;
    state.balanceWindow = null;
  } else {
    windowSelect.innerHTML = data.windows
      .map(w => `<option value="${esc(w.balance_window)}">${esc(w.balance_window)} · ${fmtNum(w.matches)} matches</option>`)
      .join('');
    windowSelect.disabled = false;
    // Keep a window from the URL only if it really exists; never invent one.
    if (!state.balanceWindow || !data.windows.some(w => w.balance_window === state.balanceWindow)) {
      const requested = state.balanceWindow;
      state.balanceWindow = data.default_balance_window;
      if (requested) syncUrlWindow(); // don't leave a window in the URL that isn't shown
    }
    windowSelect.value = state.balanceWindow;
  }
  setWindowLedger();
}

// ------------------------------------------------------------------ picker

async function loadChampions() {
  const seq = ++state.championsRequestSeq;
  state.championsStatus = 'loading';
  renderPicker();
  try {
    const q = state.balanceWindow ? `?balance_window=${encodeURIComponent(state.balanceWindow)}` : '';
    const data = await fetchJson(`/api/champions${q}`);
    if (seq !== state.championsRequestSeq) return;
    state.champions = data.champions;
    state.championsStatus = 'loaded';
    renderPicker();
  } catch (err) {
    if (seq !== state.championsRequestSeq) return;
    state.championsStatus = 'error';
    countEl.textContent = '';
    listEl.innerHTML = `<div class="state-note error"><div>Couldn't load the champion list: ${esc(err.message)}</div><button type="button" class="retry-btn" data-action="retry-picker">Try again</button></div>`;
  }
}

function renderPicker() {
  if (state.championsStatus === 'idle' || state.championsStatus === 'loading') {
    countEl.textContent = '';
    listEl.innerHTML = '<p class="state-note">Loading champions…</p>';
    return;
  }
  if (state.championsStatus === 'error') return;

  const wanted = nameKey(searchInput.value);
  const shown = state.champions.filter(
    c => (!wanted || nameKey(c.name).includes(wanted)) && (!onlyCarried.checked || c.carry_games > 0),
  );
  countEl.textContent = state.champions.length
    ? `${plural(shown.length, 'champion')} shown of ${fmtNum(state.champions.length)}.`
    : '';
  if (!state.champions.length) {
    listEl.innerHTML = '<p class="state-note">No champion list is available yet.</p>';
    return;
  }
  if (!shown.length) {
    listEl.innerHTML = '<p class="state-note">No champion matches that search.</p>';
    return;
  }
  const costs = [...new Set(shown.map(c => c.cost))].sort((a, b) => a - b);
  listEl.innerHTML = costs
    .map(cost => {
      const items = shown
        .filter(c => c.cost === cost)
        .map(c => {
          const current = c.character_id === state.currentId;
          const games = c.carry_games
            ? `<span class="champ-games">${plural(c.carry_games, 'carry board')}</span>`
            : '<span class="champ-games none">no carry boards</span>';
          return `<li><a class="champ-link" href="${esc(championHref(c.slug))}"${current ? ' aria-current="page"' : ''} style="--c:${costAccent(c.cost)}">${portrait(c.name, c.cost, c.art_url)}<span class="champ-name">${esc(c.name)}</span>${games}</a></li>`;
        })
        .join('');
      return `<section class="cost-group" aria-labelledby="cost-${cost}"><h3 id="cost-${cost}" class="cost-group-title" style="--c:${costAccent(cost)}"><span class="cost-marker">${cost}-cost</span></h3><ul class="champion-grid">${items}</ul></section>`;
    })
    .join('');
}

// ------------------------------------------------------------------ investigation

// The view itself is not a live region (it would read the whole page
// aloud); a one-line status announces what happened and focus moves to
// the champion's heading.
function showView(html, status = '') {
  viewEl.hidden = false;
  viewEl.innerHTML = html;
  viewStatus.textContent = status;
}

function sampleBlock(sample) {
  return `
    <div class="sample-note${sample.low_sample ? ' is-low' : ''}">
      ${stamp(sample.low_sample ? 'low' : 'observed', sample.label)}
      <p>${esc(sample.meaning)} <span class="aside">(${plural(sample.games, 'carry board')})</span></p>
    </div>`;
}

const vsAverage = (avg, fmt) => (avg == null ? '' : `<span class="vs-avg">all carries: ${fmt(avg)}</span>`);

// "How players carry with X": the page's own numbers restated (OBSERVED) and,
// kept visibly apart, what fixed rules conclude from them (INTERPRETATION).
// Both lists come from the API; nothing here is generated or inferred.
function summarySection(inv, name) {
  const summary = inv.summary || { observed: [], interpretation: [] };
  const list = lines => `<ul class="summary-list">${lines.map(l => `<li>${esc(l)}</li>`).join('')}</ul>`;
  return `
    <section class="inv-section summary" aria-labelledby="sec-summary">
      <h3 id="sec-summary">What the carry boards show for ${esc(name)}</h3>
      <div class="summary-grid">
        <div class="summary-block">
          <p class="summary-head">${stamp('observed', 'Observed')} <span>what the carry boards show</span></p>
          ${list(summary.observed)}
        </div>
        <div class="summary-block interpretation">
          <p class="summary-head">${stamp('interpretation', 'Interpretation')} <span>what TheoryLabs reads from those numbers</span></p>
          ${summary.interpretation.length ? list(summary.interpretation) : '<p class="none-note">Not enough evidence for a reading yet.</p>'}
          <p class="inv-help">Fixed rules applied to the observed numbers, nothing more: no roll timing, leveling, economy or positioning advice, and no claim that a partner or trait causes a result.</p>
        </div>
      </div>
    </section>`;
}

function carrySection(inv) {
  const carry = inv.carry;
  const avg = inv.window_average || {};
  const ts = carry.three_star;
  const hitSplit = (label, rate, place, n, sample, cls) => `
    <div class="${cls}">
      <p class="hm-label">${label}</p>
      <p class="hm-num">${fmtPct(rate)}</p>
      <p class="hm-sub">top 4 · avg place ${fmtPlace(place)} · ${plural(n, 'board')}</p>
      ${n > 0 && n < SMALL_SPLIT ? `<p class="hm-warn">${stamp('low', 'Limited sample')}</p>` : ''}
    </div>`;
  const hitMiss =
    ts.hit_games === 0
      ? `<p class="none-note">None of these carry boards reached 3★, so there is no hit vs. miss split. Every result above is from 2★ (or lower) boards.</p>`
      : ts.miss_games === 0
        ? `<p class="none-note">Every carry board here reached 3★, so there are no misses to compare against.</p>`
        : `<div class="hitmiss">${hitSplit('when it hit 3★', ts.hit_top4_rate, ts.hit_avg_placement, ts.hit_games, ts.hit_sample, 'hm-hit')}<span class="hm-vs" aria-hidden="true">vs.</span>${hitSplit('when it stayed below 3★', ts.miss_top4_rate, ts.miss_avg_placement, ts.miss_games, ts.miss_sample, 'hm-miss')}</div>`;
  return `
    <section class="inv-section" aria-labelledby="sec-carry">
      <h3 id="sec-carry">Observed results ${stamp('observed', 'Observed')}</h3>
      <p class="inv-lead">
        Built as a carry on <b>${plural(carry.games, 'observed board')}</b> in this window: <b>${fmtPct(carry.carry_conversion_rate)}</b> of the ${plural(carry.appearances, 'board')} it appeared on.
      </p>
      <dl class="results big inv-results">
        <div><dt>average placement</dt><dd>${fmtPlace(carry.avg_placement)}</dd>${vsAverage(avg.avg_placement, fmtPlace)}</div>
        <div><dt>top 4</dt><dd>${fmtPct(carry.top4_rate)}</dd>${vsAverage(avg.top4_rate, fmtPct)}</div>
        <div><dt>first place</dt><dd>${fmtPct(carry.win_rate)}</dd>${vsAverage(avg.win_rate, fmtPct)}</div>
        <div><dt>reached 3★</dt><dd>${fmtPct(ts.hit_rate)}</dd><span class="vs-avg">${plural(ts.hit_games, 'board')}</span></div>
      </dl>
      <p class="inv-help">"All carries" is every champion's carry boards in this window, for comparison (a board with two carries counts once for each).</p>
      ${sampleBlock(carry.sample)}
      <h4>Hit vs. miss</h4>
      <p class="inv-help">The ceiling when you find the 3★, and the floor when you don't. A line that only works on the hit is fragile.</p>
      ${hitMiss}
    </section>`;
}

function comparisonText(row, carryName) {
  if (!row.games_without) {
    return `<span class="cmp">on every ${esc(carryName)} carry board: nothing to compare against</span>`;
  }
  const diff = row.top4_with - row.top4_without;
  const cls = diff > 0 ? 'delta-pos' : diff < 0 ? 'delta-neg' : '';
  return `<span class="cmp">top 4 <b class="${cls}">${fmtPctShort(row.top4_with)}</b> with · ${fmtPctShort(row.top4_without)} without</span>`;
}

function rowMeta(row, carryName) {
  return `
    <span class="row-meta">
      <span class="share">on ${fmtPctShort(row.share_of_carry_games)} of carry boards · ${plural(row.games, 'board')}</span>
      ${comparisonText(row, carryName)}
      ${row.limited_sample ? stamp('low', 'Limited sample') : ''}
    </span>`;
}

// Artifact / Radiant / unrecognized items are labelled so they can't pass for
// a normal craftable build; a name taken from the Riot id (no metadata) says so.
const ITEM_TAGS = { artifact: 'Artifact', radiant: 'Radiant', unknown: 'Unrecognized item' };
function itemName(i) {
  const tag = ITEM_TAGS[i.kind] ? ` <span class="item-tag item-${esc(i.kind)}">${ITEM_TAGS[i.kind]}</span>` : '';
  const title = i.name_source === 'id' ? ` title="Name read from the Riot item id; not in the item metadata"` : '';
  return `<span class="item-name"${title}>${esc(i.name)}</span>${tag}`;
}
function itemRowInner(row, carryName) {
  const names = row.items.map(itemName).join(' + ');
  const icons = `<span class="ref-stack">${row.items.map(i => refIcon(i.name, i.art_url, { kind: 'item' })).join('')}</span>`;
  return `${icons}<span class="row-name">${names}</span>${rowMeta(row, carryName)}`;
}
const itemRow = (row, carryName) => `<li class="ev-row">${itemRowInner(row, carryName)}</li>`;

function evidenceList(rows, render, emptyText) {
  return rows.length ? `<ol class="ev-list">${rows.map(render).join('')}</ol>` : `<p class="none-note">${esc(emptyText)}</p>`;
}

// Recipe direction: DERIVED from the CommunityDragon recipes of the common
// completed items, never observed components (Match-V1 has no component
// history). Each component lists the items whose recipe uses it.
function recipeDirectionDetail(direction, minBoards) {
  if (direction.status !== 'available') {
    return `<p class="none-note">Not enough completed-item evidence yet: no item on ${plural(minBoards, 'carry board')} or more with a verified recipe.</p>`;
  }
  const rows = direction.components
    .map(
      c => `
      <li class="ev-row">
        ${refIcon(c.component.name, c.component.art_url, { kind: 'item' })}
        <span class="row-name">${esc(c.component.name)}</span>
        <span class="row-meta">
          <span>in ${c.recipes} of ${direction.recipes_considered} recipes read${c.copies > c.recipes ? ` · ${c.copies} copies` : ''}</span>
          <span class="recipe-uses">for ${c.items.map(i => `${refIcon(i.name, i.art_url, { kind: 'item' })} ${esc(i.name)}`).join(' · ')}</span>
        </span>
      </li>`,
    )
    .join('');
  const skipped = direction.items_without_recipe.length
    ? `<p class="inv-help">${plural(direction.items_without_recipe.length, 'item')} without a verified two-component recipe ${direction.items_without_recipe.length === 1 ? 'is' : 'are'} not counted.</p>`
    : '';
  return `<ol class="ev-list">${rows}</ol>${skipped}`;
}

function itemSection(inv, name) {
  const items = inv.items;
  const h = inv.how_to_play;
  const normal = items.most_common_normal_build;
  const overall = items.most_common_build;
  const callout = (label, row) =>
    `<div class="callout"><p class="callout-label">${label}</p><div class="ev-row">${itemRowInner(row, name)}</div></div>`;
  const commonLine =
    (normal ? callout('Most common normal full build', normal) : '') +
    (overall && !overall.normal_build && (!normal || overall.games > normal.games)
      ? callout('Most common full build overall (includes an Artifact, Radiant or unrecognized item)', overall)
      : '');
  return `
    <section class="inv-section" aria-labelledby="sec-recipes">
      <h3 id="sec-recipes">Recipe direction ${stamp('interpretation', 'Derived from recipes')}</h3>
      <p class="inv-help">${esc(h.component_direction.basis)} Items read: the six most common normal items on ${plural(h.support_min_boards, 'carry board')} or more, the same ones How to play shows.</p>
      ${recipeDirectionDetail(h.component_direction, h.support_min_boards)}
    </section>
    <section class="inv-section" aria-labelledby="sec-items">
      <h3 id="sec-items">Completed items ${stamp('observed', 'Observed')}</h3>
      <p class="inv-help">Completed items on the carry itself. Each row shows how often its carry boards used it, and how those boards finished compared with its carry boards that didn't. Best first: rows are ordered by that difference, adjusted so a handful of lucky boards can't top the list. A green top-4 number means those boards did better than the ones without it; red means worse. Artifact and Radiant items aren't normal crafts, so they are labelled and never lead the normal-build summary.</p>
      <h4>Individual items</h4>
      ${evidenceList(items.individual || [], r => itemRow(r, name), 'No completed-item evidence for this champion yet.')}
      <h4>Item pairs</h4>
      ${evidenceList(items.pairs, r => itemRow(r, name), 'No item pair shows up on at least 2 carry boards yet.')}
      <h4>Full builds (exact 3 items)</h4>
      ${commonLine}
      ${evidenceList(items.builds, r => itemRow(r, name), 'No full 3-item build shows up on at least 2 carry boards yet.')}
    </section>`;
}

function partnerSection(inv, name) {
  const render = row =>
    `<li class="ev-row">${refIcon(row.name, row.art_url, { cost: row.cost, kind: 'partner' })}<span class="row-name"><a href="${esc(championHref(row.slug))}">${esc(row.name)}</a>${row.cost ? ` <span class="aside">${row.cost}-cost</span>` : ''}</span>${rowMeta(row, name)}</li>`;
  return `
    <section class="inv-section" aria-labelledby="sec-partners">
      <h3 id="sec-partners">Individual teammates ${stamp('observed', 'Observed')}</h3>
      <p class="inv-help">Champions that finished on the same board as a ${esc(name)} carry, and how those boards placed compared with ${esc(name)} carry boards without them. This is an association, not proof: a partner may simply belong to the same comp rather than cause the result.</p>
      ${evidenceList(inv.partners, render, 'No partner evidence for this champion yet.')}
    </section>`;
}

// Recurring cores: the carry plus 2-3 teammates directly observed together on
// the same final boards (every member present). A subset of each board, never
// an exact comp; picked and ordered by how often it recurs, not by results.
const CORE_NOTE = 'These are unit subsets directly observed together on final carry boards. Other units were also present; these are not exact compositions.';
const corePortraits = (units, size = 'sm') =>
  `<span class="core-stack core-${size}" aria-hidden="true">${units.map(u => refIcon(u.name, u.art_url, { cost: u.cost, kind: 'partner' })).join('')}</span>`;
const coreNames = units =>
  units.map((u, i) => (i === 0 ? `<b>${esc(u.name)}</b>` : `<a href="${esc(championHref(u.slug))}">${esc(u.name)}</a>`)).join(' <span class="core-plus" aria-hidden="true">+</span> ');

function coreSection(inv, name) {
  const cores = inv.cores;
  const render = row => `
    <li class="ev-row core-row">
      ${corePortraits(row.units)}
      <span class="row-name">${coreNames(row.units)}</span>
      ${rowMeta(row, name)}
      <span class="row-meta"><span>avg place ${fmtPlace(row.avg_placement_with)}</span></span>
    </li>`;
  const list = (rows, size) =>
    rows.length
      ? `<ol class="ev-list">${rows.map(render).join('')}</ol>`
      : `<p class="none-note">No recurring ${size}-unit core has enough boards yet (${cores.min_boards}+).</p>`;
  return `
    <section class="inv-section" aria-labelledby="sec-cores">
      <h3 id="sec-cores">Recurring 3–4 unit cores ${stamp('observed', 'Observed')}</h3>
      <p class="inv-help">${esc(CORE_NOTE)} A core counts only on boards where every member was present. Each is on ${plural(cores.min_boards, 'carry board')} or more. 4-unit and 3-unit cores are listed separately, and within each section rows are ordered by how many boards they appeared on (most first); the Top 4 comparison is the core's own boards against ${esc(name)}'s other carry boards. Larger cores contain smaller ones, so these rows overlap and their numbers can't be added up.</p>
      <h4>4-unit cores</h4>
      ${list(cores.four_unit, 4)}
      <h4>3-unit cores</h4>
      ${list(cores.three_unit, 3)}
    </section>`;
}

// Traits around the carry: how often each trait was active, then the unit
// counts Riot reported for it (`num_units`). Riot's tier ordinal is never
// shown as a count, and no threshold names are guessed.
function traitCountRow(c) {
  const other = c.games_without
    ? ` <span class="count-other">other ${esc(fmtPctShort(c.top4_without))}</span>`
    : '';
  return `
    <li class="count-row">
      <span class="count-units">${plural(c.num_units, 'unit')}</span>
      <span class="count-stats">${plural(c.games, 'board')} (${fmtPctShort(c.share_of_carry_games)}) · top 4 ${fmtPctShort(c.top4_with)}${other} · avg ${fmtPlace(c.avg_placement_with)}</span>
      ${c.limited_sample ? `<span class="count-limited" title="Limited sample: fewer than ${SMALL_SPLIT} boards on one side of the comparison">limited sample</span>` : ''}
    </li>`;
}

function traitSection(inv, name) {
  const total = inv.carry.games;
  const render = t => `
    <li class="trait-group">
      <div class="ev-row">
        ${refIcon(t.name, t.art_url, { kind: 'trait' })}
        <span class="row-name">${esc(t.name)}</span>
        ${rowMeta(t, name)}
      </div>
      <ol class="count-list" aria-label="${esc(t.name)} by observed unit count">${t.counts.map(traitCountRow).join('')}</ol>
    </li>`;
  return `
    <section class="inv-section" aria-labelledby="sec-traits">
      <h3 id="sec-traits">Traits around ${esc(name)} ${stamp('observed', 'Observed')}</h3>
      <p class="inv-help">The traits most often active on ${esc(name)} carry boards, most common first. Under each, the unit counts Riot reported for that trait on those final boards ("4 units"), with how many carry boards had that count and how they placed; "other" is top 4 on the rest of its carry boards. Every share is out of the same ${plural(total, 'carry board')}, and one board counts under each trait it had active, so shares don't add up to 100%. These are associations: forcing a trait won't necessarily produce the same result.</p>
      ${inv.traits.length ? `<ol class="ev-list trait-list">${inv.traits.map(render).join('')}</ol>` : '<p class="none-note">No active traits recorded on these carry boards yet.</p>'}
    </section>`;
}

function trustSection(inv) {
  const w = inv.window;
  return `
    <section class="inv-section trust" aria-labelledby="sec-trust">
      <h3 id="sec-trust">How much to trust this</h3>
      <dl class="trust-list">
        <div><dt>Evidence</dt><dd>${stamp('observed', 'Observed')} Every number comes from indexed ranked matches. Nothing on this page is predicted or theorycrafted.</dd></div>
        <div><dt>Balance window</dt><dd><b>${esc(inv.balance_window)}</b>${w ? ` · ${plural(w.matches, 'match', 'matches')} · latest game ${esc(fmtDate(w.latest_game_datetime))}` : ''}. Other patches are never mixed in.</dd></div>
        <div><dt>Carry board</dt><dd>One player's final board where this champion finished with 2+ completed items, at least one of them a carry item. Boards that missed the 3★ still count, so bad outcomes aren't hidden.</dd></div>
        <div><dt>Boards, not matches</dt><dd>Every match has eight player boards, so a champion can be carried on more boards than there are matches in the window.</dd></div>
        <div><dt>With vs. without</dt><dd>Item, partner and trait rows compare this champion's carry boards that had the thing against its carry boards that didn't. Rows marked ${stamp('low', 'Limited sample')} have fewer than ${SMALL_SPLIT} boards on one side of that comparison.</dd></div>
        <div><dt>How to play</dt><dd>A short selection from the same observed rows, chosen by sample size and frequency, not by results: items, pairs and builds on at least 10 carry boards (most used first), teammates by how often they shared the board, trait directions by the unit count Riot reported most often. A one-champion trait at its single unit (e.g. a teammate's own trait) is not a trait direction; it stays in the Traits tab. Nothing is predicted.</dd></div>
        <div><dt>Recipe direction</dt><dd>Derived, not observed: the components in the CommunityDragon recipes of the completed items How to play shows. Riot match data lists final items only, so TheoryLabs can't see which components a player held or opened with.</dd></div>
        <div><dt>Interpretation</dt><dd>The interpretation above comes from fixed rules, not a model. The 3★ comparison needs at least 30 boards on each side before TheoryLabs interprets it; then it reports the observed Top 4 difference in percentage points. That is an association, not proof that reaching 3★ caused the result.</dd></div>
        <div><dt>Not shown yet</dt><dd>Full comp families and recurring cores (that research is still experimental and unvalidated), positioning, augments, and leveling or rolling plans.</dd></div>
      </dl>
    </section>`;
}

// ------------------------------------------------------------------ how to play

// A larger art tile with its name written next to it (the art is decorative).
const tile = (name, url, { kind = 'item', cost = null, sub = '' } = {}) =>
  `<span class="htp-tile">${refIcon(name, url, { kind, cost })}<span class="htp-tile-text"><span class="htp-tile-name">${esc(name)}</span>${sub ? `<span class="htp-tile-sub">${sub}</span>` : ''}</span></span>`;
const share = row => `${fmtPctShort(row.share_of_carry_games)} · ${plural(row.games, 'board')}`;
const itemIcons = row =>
  `<span class="ref-stack">${row.items.map(i => refIcon(i.name, i.art_url, { kind: 'item' })).join('')}</span>`;
const itemLine = row =>
  `<li class="htp-line">${itemIcons(row)}<span class="htp-line-name">${row.items.map(i => esc(i.name)).join(' + ')}</span><span class="htp-num">${share(row)}</span></li>`;
const htpEmpty = text => `<p class="none-note">${esc(text)}</p>`;

function starCard(signal) {
  const side = (label, rate, n) =>
    `<div class="htp-star-side"><span class="htp-star-label">${label}</span><span class="htp-star-num">${fmtPctShort(rate)}</span><span class="htp-num">top 4 · ${plural(n, 'board')}</span></div>`;
  let body;
  if (signal.status === 'no_hits') body = htpEmpty('No carry board reached 3★ in this sample, so there is nothing to compare.');
  else if (signal.status === 'all_hits') body = htpEmpty('Every carry board reached 3★, so there is nothing to compare.');
  else {
    body = `<div class="htp-star">${side('3★', signal.hit_top4_rate, signal.hit_games)}${side('below 3★', signal.miss_top4_rate, signal.miss_games)}</div>`;
    if (signal.status === 'limited') {
      body += `<p class="htp-note">${stamp('low', 'Limited sample')} Too few boards on one side (under ${signal.min_boards}) to compare 3★ vs below.</p>`;
    }
  }
  return `
    <section class="htp-card" aria-labelledby="htp-star">
      <h4 id="htp-star">Star signal</h4>
      ${body}
      <p class="htp-foot">Observed Top 4 by final star level. A difference here is an association, not proof that 3★ is required.</p>
    </section>`;
}

function componentCard(direction, minBoards) {
  const body =
    direction.status === 'available'
      ? `<ul class="htp-tiles" aria-label="Useful components">${direction.components
          .map(c => `<li>${tile(c.component.name, c.component.art_url, { sub: `in ${c.recipes} of ${direction.recipes_considered} recipes` })}</li>`)
          .join('')}</ul>`
      : htpEmpty('Not enough completed-item evidence yet.');
  return `
    <section class="htp-card" aria-labelledby="htp-components">
      <h4 id="htp-components">Recipe direction</h4>
      <p class="htp-sub">Components in the recipes of the items below</p>
      ${body}
      <p class="htp-foot">${stamp('interpretation', 'Derived from recipes')} Not what players opened with: match data lists final items only.</p>
    </section>`;
}

function howToPlaySection(inv) {
  const h = inv.how_to_play;
  const c = inv.champion;
  const min = h.support_min_boards;
  const items = h.items.length
    ? `<ul class="htp-tiles" aria-label="Common items">${h.items.map(r => `<li>${tile(r.items[0].name, r.items[0].art_url, { sub: share(r) })}</li>`).join('')}</ul>`
    : htpEmpty('Not enough completed-item evidence yet.');
  const lines = (rows, empty) => (rows.length ? `<ul class="htp-lines">${rows.map(itemLine).join('')}</ul>` : htpEmpty(empty));
  const mates = h.teammates.length
    ? `<ul class="htp-tiles" aria-label="Frequent teammates">${h.teammates
        .map(m => `<li><a class="htp-link" href="${esc(championHref(m.slug))}">${tile(m.name, m.art_url, { kind: 'partner', cost: m.cost, sub: share(m) })}</a></li>`)
        .join('')}</ul>`
    : htpEmpty('No teammate evidence yet.');
  const coreLines = h.recurring_cores.length
    ? `<ul class="htp-lines htp-cores" aria-label="Recurring cores">${h.recurring_cores
        .map(r => `<li class="htp-line">${corePortraits(r.units, 'md')}<span class="htp-line-name">${r.units.map(u => esc(u.name)).join(' + ')}</span><span class="htp-num">${share(r)}</span></li>`)
        .join('')}</ul>`
    : htpEmpty('No recurring 3–4 unit core has enough boards yet.');
  const traits = h.trait_directions.length
    ? `<ul class="htp-tiles" aria-label="Trait directions">${h.trait_directions
        .map(t => `<li>${tile(t.name, t.art_url, { kind: 'trait', sub: `<b>${plural(t.num_units, 'unit')}</b> · ${fmtPctShort(t.count_share)} of boards` })}</li>`)
        .join('')}</ul>`
    : htpEmpty('No useful buildable trait direction in this sample.');
  return `
    <section class="inv-section htp" aria-labelledby="sec-htp">
      <h3 id="sec-htp">When to consider ${esc(c.name)}</h3>
      <div class="htp-grid">
        ${componentCard(h.component_direction, min)}
        ${starCard(h.star_signal)}
      </div>
      <h3 class="htp-heading" id="htp-items">Item direction</h3>
      <p class="htp-sub">Each on ${min}+ carry boards, most used first. How they placed is in the Items tab.</p>
      <div class="htp-grid htp-grid-3">
        <section class="htp-card" aria-labelledby="htp-common-items"><h4 id="htp-common-items">Common items</h4>${items}</section>
        <section class="htp-card" aria-labelledby="htp-pairs"><h4 id="htp-pairs">Common observed pairs</h4>${lines(h.pairs, `No item pair on ${min}+ carry boards yet.`)}</section>
        <section class="htp-card" aria-labelledby="htp-builds"><h4 id="htp-builds">Common full builds</h4>${lines(h.builds, `No full build on ${min}+ carry boards yet.`)}</section>
      </div>
      <h3 class="htp-heading" id="htp-around">Build around</h3>
      <div class="htp-grid">
        <section class="htp-card" aria-labelledby="htp-mates"><h4 id="htp-mates">Teammates</h4>${mates}</section>
        <section class="htp-card" aria-labelledby="htp-traits"><h4 id="htp-traits">Trait direction</h4>${traits}<p class="htp-foot">Unit counts Riot reported on the final boards, not official trait thresholds.</p></section>
        <section class="htp-card htp-span" aria-labelledby="htp-cores"><h4 id="htp-cores">Recurring cores</h4>${coreLines}<p class="htp-foot">Units seen together on the same final carry boards, most common first. Other units were there too: not exact comps.</p></section>
      </div>
      <section class="htp-why" aria-labelledby="htp-why">
        <h4 id="htp-why">Why: evidence summary ${stamp('observed', 'Observed')}</h4>
        <p>${h.summary ? esc(h.summary) : 'Not enough evidence for a summary yet.'}</p>
        <p class="htp-foot">Shown: rows on ${plural(min, 'carry board')} or more, most boards first; teammates by how often they shared the board. Detailed with-vs-without results are in the other tabs. ${inv.carry.sample.low_sample ? stamp('low', inv.carry.sample.label) : ''}</p>
        <button type="button" class="htp-more" data-tab="evidence">View detailed evidence →</button>
      </section>
    </section>`;
}

// ------------------------------------------------------------------ tabs

// One tab list for the whole investigation; panels are rendered once and
// shown/hidden, so switching never fetches or navigates. The tab is kept
// in the URL hash (#items) so a view can be linked and survives a reload.
const TABS = [
  ['play', 'How to play'],
  ['items', 'Items'],
  ['teammates', 'Teammates'],
  ['traits', 'Traits'],
  ['evidence', 'Evidence'],
];
const tabFromHash = () => {
  const id = window.location.hash.replace(/^#/, '');
  return TABS.some(([t]) => t === id) ? id : null;
};

function tabList(active) {
  return `
    <div class="inv-tabs" role="tablist" aria-label="Champion views">
      ${TABS.map(
        ([id, label]) =>
          `<button type="button" role="tab" id="tab-${id}" aria-controls="panel-${id}" aria-selected="${id === active}" tabindex="${id === active ? 0 : -1}" data-tab="${id}">${label}</button>`,
      ).join('')}
    </div>`;
}

const panel = (id, active, html) =>
  `<div class="inv-panel" role="tabpanel" id="panel-${id}" aria-labelledby="tab-${id}" tabindex="0"${id === active ? '' : ' hidden'}>${html}</div>`;

function selectTab(id, { focus = false, updateHash = true } = {}) {
  const list = viewEl.querySelector('.inv-tabs');
  if (!list || !TABS.some(([t]) => t === id)) return;
  state.tab = id;
  for (const btn of list.querySelectorAll('[role="tab"]')) {
    const on = btn.dataset.tab === id;
    btn.setAttribute('aria-selected', String(on));
    btn.tabIndex = on ? 0 : -1;
    if (on && focus) btn.focus();
    if (on) btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  for (const p of viewEl.querySelectorAll('.inv-panel')) p.hidden = p.id !== `panel-${id}`;
  if (updateHash) {
    const url = new URL(window.location.href);
    url.hash = id === 'play' ? '' : id;
    window.history.replaceState(null, '', url);
  }
}

function onTabKey(e) {
  const tab = e.target.closest('[role="tab"]');
  if (!tab) return;
  const ids = TABS.map(([t]) => t);
  const i = ids.indexOf(tab.dataset.tab);
  const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: ids.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  selectTab(ids[(next + ids.length) % ids.length], { focus: true });
}

function headerBlock(inv) {
  const c = inv.champion;
  const sample = inv.carry ? stamp(inv.carry.sample.low_sample ? 'low' : 'observed', inv.carry.sample.label) : '';
  return `
    <header class="inv-head" style="--c:${costAccent(c.cost)}">
      ${portrait(c.name, c.cost, c.art_url, 96)}
      <div class="inv-title">
        <p class="notes-kicker">carry investigation · <a href="${esc(allChampionsHref())}" class="back-link">all champions</a></p>
        <h2 id="champion-name" tabindex="-1">${esc(c.name)}</h2>
        <p class="inv-meta"><span class="cost-marker">${c.cost}-cost</span> balance window <b>${esc(inv.balance_window ?? '—')}</b>${inv.carry ? ` <span>${plural(inv.carry.games, 'carry board')}</span>` : ''}</p>
        <p class="inv-stamps">${stamp('observed', 'Observed')}${sample}</p>
      </div>
    </header>`;
}

function renderInvestigation(inv) {
  const c = inv.champion;
  document.title = `${c.name} · Champion Investigation · TheoryLabs`;
  state.currentId = c.character_id;
  if (!inv.window) {
    showView(
      `<article class="investigation">${headerBlock(inv)}<p class="state-note">No indexed matches in this balance window yet, so there is nothing to show for ${esc(c.name)}. Pick another balance window above.</p></article>`,
      `${c.name}: no matches in this balance window.`,
    );
    return;
  }
  if (!inv.carry) {
    const seen = inv.appearances
      ? `${esc(c.name)} was on ${plural(inv.appearances, 'board')} in balance window ${esc(inv.balance_window)}, but never finished with 2+ completed items including a carry item, so there are no carry boards to investigate.`
      : `${esc(c.name)} wasn't seen on any indexed board in balance window ${esc(inv.balance_window)}.`;
    showView(
      `<article class="investigation">${headerBlock(inv)}<div class="state-note empty-evidence"><p>${seen}</p><p>Try another balance window, or pick a champion below with carry boards.</p></div></article>`,
      `${c.name}: no carry boards in this balance window.`,
    );
    return;
  }
  const active = state.tab || tabFromHash() || 'play';
  showView(`
    <article class="investigation">
      ${headerBlock(inv)}
      ${tabList(active)}
      ${panel('play', active, howToPlaySection(inv))}
      ${panel('items', active, itemSection(inv, c.name))}
      ${panel('teammates', active, partnerSection(inv, c.name) + coreSection(inv, c.name))}
      ${panel('traits', active, traitSection(inv, c.name))}
      ${panel('evidence', active, summarySection(inv, c.name) + carrySection(inv) + trustSection(inv))}
    </article>`,
    `${c.name} carry investigation loaded.`,
  );
  state.tab = active;
}

async function loadInvestigation({ focus = true } = {}) {
  if (!state.key) {
    viewEl.hidden = true;
    return;
  }
  showView(`<p class="state-note">Pulling up the carry evidence for “${esc(state.key)}”…</p>`, 'Loading champion…');
  statusDot.classList.remove('error');
  try {
    const q = new URLSearchParams({ top_n: '6' });
    if (state.balanceWindow) q.set('balance_window', state.balanceWindow);
    const inv = await fetchJson(`/api/champions/${encodeURIComponent(state.key)}?${q}`);
    setSource(inv.demo);
    renderInvestigation(inv);
    if (focus) viewEl.querySelector('#champion-name')?.focus({ preventScroll: true });
  } catch (err) {
    if (err.status === 404) {
      document.title = 'Champion not found · TheoryLabs';
      showView(
        `<p class="state-note">We couldn't find a champion called “${esc(state.key)}”. Pick one from the list below.</p>`,
        'Champion not found.',
      );
    } else {
      statusDot.classList.add('error');
      showView(
        `<div class="state-note error"><div>Couldn't load this champion: ${esc(err.message)}</div><button type="button" class="retry-btn" data-action="retry-view">Try again</button></div>`,
        "Couldn't load this champion.",
      );
    }
  }
}

// ------------------------------------------------------------------ wiring

function bind() {
  document.querySelector('#picker-controls').addEventListener('submit', e => {
    e.preventDefault();
    // Enter on a single match opens it: quick keyboard path.
    const links = listEl.querySelectorAll('.champ-link');
    if (links.length === 1) window.location.assign(links[0].href);
  });
  searchInput.addEventListener('input', renderPicker);
  onlyCarried.addEventListener('change', renderPicker);
  windowSelect.addEventListener('change', () => {
    state.balanceWindow = windowSelect.value || null;
    syncUrlWindow();
    setWindowLedger();
    loadInvestigation({ focus: false });
    loadChampions();
  });
  viewEl.addEventListener('keydown', onTabKey);
  window.addEventListener('hashchange', () => {
    const id = tabFromHash() || 'play';
    if (id !== state.tab) selectTab(id, { updateHash: false });
  });
  document.addEventListener('click', e => {
    const tabBtn = e.target.closest('[data-tab]');
    if (tabBtn && viewEl.contains(tabBtn)) {
      const toPanel = !tabBtn.matches('[role="tab"]');
      selectTab(tabBtn.dataset.tab, { focus: toPanel });
      if (toPanel) viewEl.querySelector('.inv-tabs')?.scrollIntoView({ block: 'start' });
    }
    if (e.target.closest('[data-action="retry-view"]')) loadInvestigation();
    if (e.target.closest('[data-action="retry-picker"]')) loadChampions();
  });
}

async function init() {
  bind();
  if (state.key) pickerEl.querySelector('#picker-title .hl').textContent = 'Investigate another champion';
  try {
    await loadWindows();
  } catch (err) {
    statusDot.classList.add('error');
    document.querySelector('#data-mode').textContent = 'unavailable';
  }
  // A fresh page load leaves focus at the top (skip link first); focus
  // moves to the champion's heading only after an in-page change (retry).
  await Promise.all([loadInvestigation({ focus: false }), loadChampions()]);
  if (state.champions.length) renderPicker(); // marks the champion now open
}

init();
