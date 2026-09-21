const fmtPct = v => v == null ? '—' : `${(v * 100).toFixed(1)}%`;
const fmtNum = v => Number(v).toLocaleString();
const initials = name => name.split(/\s+/).map(x => x[0]).join('').slice(0,2).toUpperCase();

const cardsEl = document.querySelector('#cards');
const detailEl = document.querySelector('#detail');
const costFilter = document.querySelector('#cost-filter');
const sampleFilter = document.querySelector('#sample-filter');

async function loadCarries() {
  cardsEl.innerHTML = '<div class="loading-card">Analyzing carry lines…</div>';
  const res = await fetch(`/api/carries?max_cost=${costFilter.value}&min_samples=${sampleFilter.value}`);
  const data = await res.json();
  document.querySelector('#data-mode').textContent = data.demo ? 'Demo dataset' : 'Live Riot dataset';
  document.querySelector('#match-count').textContent = fmtNum(data.matches);
  document.querySelector('#participant-count').textContent = fmtNum(data.participants);
  renderCards(data.carries);
}

function renderCards(carries) {
  if (!carries.length) {
    cardsEl.innerHTML = '<div class="loading-card">No carries meet those filters yet.</div>';
    return;
  }
  cardsEl.innerHTML = carries.map((c, i) => `
    <article class="carry-card" data-id="${c.character_id}">
      <div class="rank-orb">${initials(c.name)}</div>
      <div class="card-main">
        <div class="tags"><span class="tag">${c.cost}-cost</span><span class="tag">reroll signal</span><span class="tag">${c.commitment_games} commits</span></div>
        <h3>${c.name}</h3>
        <div class="stat-row">
          <div class="stat"><small>Avg</small><strong>${c.avg_placement.toFixed(2)}</strong></div>
          <div class="stat"><small>Top 4</small><strong>${fmtPct(c.top4_rate)}</strong></div>
          <div class="stat"><small>Win</small><strong>${fmtPct(c.win_rate)}</strong></div>
          <div class="stat"><small>3★ hit</small><strong>${fmtPct(c.hit_3star_rate)}</strong></div>
          <div class="stat"><small>Usage</small><strong>${fmtPct(c.usage_rate)}</strong></div>
        </div>
      </div>
      <div class="score"><strong>${c.opportunity_score.toFixed(1)}</strong><span>opportunity</span></div>
    </article>
  `).join('');
  document.querySelectorAll('.carry-card').forEach(el => el.addEventListener('click', () => loadDetail(el.dataset.id)));
}

async function loadDetail(id) {
  detailEl.className = 'detail';
  detailEl.innerHTML = '<div class="loading-card">Building evidence panel…</div>';
  const res = await fetch(`/api/carries/${encodeURIComponent(id)}`);
  const d = await res.json();
  const c = d.carry;
  const partners = d.partners.length ? d.partners.map(p => `<div class="partner"><span>${p.name} · ${p.games} games</span><span>${fmtPct(p.top4_rate)} T4</span></div>`).join('') : '<p class="muted">No recurring partner has enough sample yet.</p>';
  const items = d.item_sets.length ? d.item_sets.map(s => `<div class="itemset"><span>${s.items.map(x => x.replace(/^TFT_Item_/, '')).join(' · ')}</span><span>${s.games} games</span></div>`).join('') : '<p class="muted">No item package sample yet.</p>';
  detailEl.innerHTML = `
    <div class="detail-header">
      <div><div class="eyebrow">EVIDENCE PANEL</div><h3>${c.name} reroll</h3><p>${c.cost}-cost carry · ${c.commitment_games} committed games</p></div>
      <div class="detail-score"><strong>${c.opportunity_score.toFixed(1)}</strong><span>opportunity score</span></div>
    </div>
    <div class="detail-grid">
      <div class="detail-panel"><h4>Commitment profile</h4><div class="metric-list">
        <div><span>Top 4</span><strong>${fmtPct(c.top4_rate)}</strong></div>
        <div><span>Avg place</span><strong>${c.avg_placement.toFixed(2)}</strong></div>
        <div><span>3★ hit</span><strong>${fmtPct(c.hit_3star_rate)}</strong></div>
        <div><span>Win</span><strong>${fmtPct(c.win_rate)}</strong></div>
        <div><span>Top 4 when hit</span><strong>${fmtPct(c.hit_top4_rate)}</strong></div>
        <div><span>Top 4 when missed</span><strong>${fmtPct(c.miss_top4_rate)}</strong></div>
      </div></div>
      <div class="detail-panel"><h4>Recurring partners</h4>${partners}</div>
      <div class="detail-panel"><h4>Observed item sets</h4>${items}</div>
    </div>
  `;
  detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

costFilter.addEventListener('change', loadCarries);
sampleFilter.addEventListener('change', loadCarries);
loadCarries().catch(err => {
  cardsEl.innerHTML = `<div class="loading-card">Could not load stats: ${err.message}</div>`;
});
