const esc = s =>
  String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
const initials = name =>
  String(name || '?')
    .replace(/[^\p{L}\p{N}\s]/gu, '')
    .split(/\s+/)
    .filter(w => /^\p{L}/u.test(w))
    .map(x => x[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() || '?';
const fmtDate = iso => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
};

const view = document.querySelector('#experiments-view');

// The owner's workflow state, written as the kind of note you'd scrawl in a margin.
const LIFECYCLE_NOTES = {
  idea: 'needs testing',
  testing: 'testing now',
  watching: 'keeping an eye on it',
  archived: 'shelved',
};

function stamp(status) {
  const kind = String(status || 'THEORYCRAFTED').toLowerCase();
  const label = kind.charAt(0).toUpperCase() + kind.slice(1);
  return `<span class="stamp stamp-${esc(kind)}">${esc(label)}</span>`;
}

const PAPERCLIP =
  '<svg class="paperclip" viewBox="0 0 24 60" aria-hidden="true" focusable="false"><path d="M8 20 V 46 C 8 54, 20 54, 20 46 V 12 C 20 3, 5 3, 5 12 V 42 C 5 46, 13 46, 13 42 V 20"/></svg>';

// Local art for the entry, index-aligned with its comp lists (see the API's
// `art` key); anything missing just means text only.
const artOf = (e, key, i) => (i == null ? e.art?.[key] : e.art?.[key]?.[i]) ?? null;

const carryPortrait = (e, size = '') => {
  const url = e.carry?.art_url;
  return `<figure class="portrait portrait-neutral ${size}${artUrl(url) ? ' has-art' : ''}" aria-hidden="true"><span class="portrait-fallback">${esc(
    initials(e.carry?.name || e.title)
  )}</span>${artImg(url, size ? 48 : 80)}</figure>`;
};

const unitLabel = (u, url) =>
  `${artChip(url, 'chip-unit')}${esc(u.name)}${u.star ? ` <span class="star">${u.star}★</span>` : ''}${u.note ? ` <span class="aside">(${esc(u.note)})</span>` : ''}`;
const unitLabels = (e, key) => e.comp[key].map((u, i) => unitLabel(u, artOf(e, key, i)));

// Hand-circled breakpoint number; shape alternates by position, never randomly.
const traitLabel = (t, i, url) =>
  `${t.breakpoint ? `<span class="bp bp-${i % 3}">${t.breakpoint}</span> ` : ''}${artChip(url, 'chip-trait')}${esc(t.name)}${t.note ? ` <span class="aside">(${esc(t.note)})</span>` : ''}`;
const traitLabels = e => e.comp.target_traits.map((t, i) => traitLabel(t, i, artOf(e, 'target_traits', i)));

const itemList = (names, urls = []) =>
  names.map((name, i) => `<span class="item-ref">${artChip(urls[i], 'chip-item')}${esc(name)}</span>`).join(', ');

function itemsLine(e) {
  const comp = e.comp;
  const parts = [];
  if (comp.carry_items.length) parts.push(itemList(comp.carry_items, e.art?.carry_items));
  if (comp.tank_items.length) parts.push(`<span class="aside">tank:</span> ${itemList(comp.tank_items, e.art?.tank_items)}`);
  return parts.join(' · ');
}

function rollLine(comp) {
  const parts = [];
  if (comp.reroll_level) parts.push(`roll at ${comp.reroll_level}`);
  if (comp.target_level) parts.push(`cap at ${comp.target_level}`);
  if (comp.roll_timing) parts.push(esc(comp.roll_timing));
  return parts.join(' · ');
}

// ------------------------------------------------------------------ list

function sheetHtml(e, i) {
  const c = e.comp;
  const rows = [
    ['core', unitLabels(e, 'core_units').join(', ')],
    ['trait target', traitLabels(e).join(', ')],
    ['items', itemsLine(e)],
    ['roll', rollLine(c)],
  ].filter(([, v]) => v);
  const sketchy = !rows.length;

  return `
    <article class="sheet sheet-v${i % 3}${e.lifecycle === 'archived' ? ' is-archived' : ''}">
      ${i % 3 === 1 ? '' : PAPERCLIP}
      <div class="sheet-head">
        ${carryPortrait(e, 'portrait-sm')}
        <div class="sheet-title">
          <h3><a class="sheet-link" href="/experiments/${encodeURIComponent(e.slug)}">${esc(e.title)}</a></h3>
          ${e.carry ? `<p class="sheet-carry">carry: ${esc(e.carry.name || e.carry.character_id)}</p>` : ''}
        </div>
        ${stamp(e.evidence_status)}
      </div>
      ${e.summary ? `<p class="thesis">&ldquo;${esc(e.summary)}&rdquo;</p>` : ''}
      ${
        rows.length
          ? `<dl class="scribbles">${rows.map(([k, v]) => `<div><dt>${k}:</dt><dd>${v}</dd></div>`).join('')}</dl>`
          : ''
      }
      ${sketchy ? `<p class="sketch-note">${e.summary ? 'just the thesis so far; the rest isn’t written yet' : 'a one-line idea. fill in later'}</p>` : ''}
      <p class="margin-scrawl">${esc(LIFECYCLE_NOTES[e.lifecycle] || e.lifecycle)}</p>
      <p class="sheet-foot">
        jotted ${fmtDate(e.created_at)}${e.updated_at !== e.created_at ? ` · revised ${fmtDate(e.updated_at)}` : ''}
        ${e.tags.length ? `<span class="tags">${e.tags.map(t => `#${esc(t)}`).join(' ')}</span>` : ''}
      </p>
      ${e.is_example ? '<p class="example-note">example entry, made up for the demo notebook</p>' : ''}
    </article>`;
}

async function renderList() {
  document.title = 'My Experiments · TFT Theory Lab';
  const res = await fetch('/api/experiments');
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = await res.json();
  const list = data.experiments;

  if (!list.length) {
    view.innerHTML = `
      <header class="page-head"><h2><span class="hl">Clipped in</span></h2></header>
      <div class="state-note">
        <p>The notebook's empty. Ideas are added from the command line, for example:</p>
        <pre class="cli-example">tftlab experiment-add --title "6 Ravager Kha'Zix" --carry "Kha'Zix" --trait "6 Ravager"</pre>
      </div>`;
    return;
  }

  view.innerHTML = `
    <header class="page-head">
      <h2><span class="hl">Clipped in</span></h2>
      <p class="page-sub">${list.length} idea${list.length === 1 ? '' : 's'}, most recently touched first.</p>
    </header>
    <div class="sheets">${list.map(sheetHtml).join('')}</div>`;
}

// ------------------------------------------------------------------ detail

function noteRow(label, body, margin = '') {
  return `
    <div class="notes-row">
      <p class="margin-note quiet">${margin}</p>
      <section class="notes-section" aria-label="${esc(label)}">
        <h4>${esc(label)}</h4>
        ${body}
      </section>
    </div>`;
}

// Filled sections get their own rows; the empty ones are listed together on
// one line, so a half-formed idea reads as unfinished rather than as a blank form.
function sectionRows(sections) {
  const filled = sections.filter(s => s.body);
  const blank = sections.filter(s => !s.body).map(s => s.label.toLowerCase());
  const rows = filled.map(s => noteRow(s.label, s.body, s.margin));
  if (blank.length) {
    rows.push(`
      <div class="notes-row is-blank">
        <p class="margin-note">still to fill in</p>
        <p class="blank-list">${blank.map(b => `<span class="blank-slot">${esc(b)}</span>`).join('')}</p>
      </div>`);
  }
  return rows.join('');
}

const bulletList = items => `<ul class="scrawl-list">${items.map(x => `<li>${x}</li>`).join('')}</ul>`;

function itemsBlock(e) {
  const c = e.comp;
  const parts = [];
  if (c.carry_items.length) parts.push(`<p><span class="aside">carry:</span> ${itemList(c.carry_items, e.art?.carry_items)}</p>`);
  if (c.tank_items.length) parts.push(`<p><span class="aside">tank:</span> ${itemList(c.tank_items, e.art?.tank_items)}</p>`);
  if (c.secondary_carry) {
    const s = c.secondary_carry;
    const who = s.unit ? ` (${artChip(artOf(e, 'secondary_carry'), 'chip-unit')}${esc(s.unit)})` : '';
    parts.push(
      `<p><span class="aside">secondary${who}:</span> ${itemList(s.items || [], e.art?.secondary_items) || '—'}</p>`
    );
  }
  return parts.join('');
}

function levelsBlock(c) {
  const parts = [];
  if (c.reroll_level) parts.push(`<p>roll down at level <span class="bp bp-1">${c.reroll_level}</span></p>`);
  if (c.target_level) parts.push(`<p>aim for level ${c.target_level}</p>`);
  if (c.roll_timing) parts.push(`<p>${esc(c.roll_timing)}</p>`);
  return parts.join('');
}

const safeUrl = url => (/^https?:\/\//i.test(url || '') ? url : null);

const pct = v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const readable = label =>
  String(label)
    .split('+')
    .map(p => p.replace(/^(TFT\d*_Item_|TFT\d*_|DA_\d*_?)/, '').replace(/([a-z])([A-Z])/g, '$1 $2'))
    .join(' + ');

const researchStamp = n =>
  n.research_label ? `<span class="stamp stamp-research">${esc(n.research_label_text || n.research_label)}</span>` : '';

// The "our data" note, laid out as a small stats slip clipped into the log.
// `art` is the note's local art (index-aligned with the evidence lists).
function riotSlip(d, art) {
  if (!d || d.status !== 'ok') return '';
  const a = art || {};
  const extras = [
    ...(d.core_units || []).map(
      (u, i) => `with ${artChip(a.core_units?.[i], 'chip-unit')}${esc(u.name)}: ${u.games} of ${d.commitment_games} games`
    ),
    ...(d.trait_targets || []).map(
      (t, i) =>
        `${t.breakpoint ? `${t.breakpoint} ` : ''}${artChip(a.trait_targets?.[i], 'chip-trait')}${esc(t.name)} active: ${t.games} of ${d.commitment_games} games`
    ),
  ];
  const partner = d.best_partners?.[0];
  const pkg = d.best_item_packages?.[0];
  const pkgRefs = a.best_item_packages?.[0] || [];
  const trait = d.best_trait_breakpoints?.[0];
  const traitArt = a.best_trait_breakpoints?.[0] || {};
  const tier = trait ? (String(trait.label).match(/\((\d+)\)$/) || [])[1] : null;
  const best = [
    partner ? `strongest partner: ${artChip(a.best_partners?.[0], 'chip-unit')}${esc(readable(partner.label))} (${partner.games} g)` : '',
    pkg
      ? `best items: ${
          pkgRefs.length
            ? pkgRefs.map(r => `<span class="item-ref">${artChip(r.art_url, 'chip-item')}${esc(r.name || readable(r.id))}</span>`).join(' + ')
            : esc(readable(pkg.label))
        } (${pkg.games} g)`
      : '',
    trait
      ? `best trait: ${artChip(traitArt.art_url, 'chip-trait')}${
          traitArt.trait_name && tier ? `${esc(traitArt.trait_name)} (${tier})` : esc(readable(trait.label))
        } (${trait.games} g)`
      : '',
  ].filter(Boolean).join(' · ');
  return `
    <div class="stat-slip">
      <p class="slip-head">window ${esc(d.balance_window)} · n = <b>${d.commitment_games}</b> committed games
        ${d.low_sample ? '<span class="stamp stamp-low">Low sample</span>' : ''}</p>
      <dl class="slip-stats">
        <div><dt>avg</dt><dd>${d.avg_placement.toFixed(2)}</dd></div>
        <div><dt>top 4</dt><dd>${pct(d.top4_rate)}</dd></div>
        <div><dt>win</dt><dd>${pct(d.win_rate)}</dd></div>
        <div><dt>3★</dt><dd>${pct(d.hit_3star_rate)}</dd></div>
      </dl>
      ${best || extras.length ? `<p class="slip-extra">${[best, ...extras].filter(Boolean).join(' · ')}</p>` : ''}
    </div>`;
}

function fieldNotesBlock(notes) {
  if (!notes.length) {
    return `<p class="field-empty">No field notes yet. This theory hasn't been scouted.</p>`;
  }
  return `<ol class="field-log">${notes
    .map((n, i) => {
      const url = safeUrl(n.source_url);
      const source = n.source_name
        ? url
          ? `<a href="${esc(url)}" rel="noopener noreferrer" target="_blank">${esc(n.source_name)}</a>`
          : esc(n.source_name)
        : url
          ? `<a href="${esc(url)}" rel="noopener noreferrer" target="_blank">link</a>`
          : '';
      return `<li class="log-entry log-${esc(n.kind)} log-v${i % 3}">
        <p class="log-date">${fmtDate(n.noted_at)}</p>
        <div class="log-main">
          <p class="log-head">
            <span class="log-kind">${esc(n.kind_label || n.kind)}</span>
            ${source ? `<span class="log-source">${source}</span>` : ''}
            ${n.evidence_status ? stamp(n.evidence_status) : ''}${researchStamp(n)}
          </p>
          ${n.kind === 'riot_evidence' && riotSlip(n.data) ? riotSlip(n.data, n.art) : `<p class="log-body">${esc(n.body)}</p>`}
          ${url && n.source_name ? `<p class="log-url">${esc(url)}</p>` : ''}
        </div>
      </li>`;
    })
    .join('')}</ol>`;
}

// Research bookkeeping: a line is ticked only when a field note came from it.
function checklistBlock(items) {
  return `<ul class="scout-checklist">${items
    .map(
      item => `<li class="${item.checked ? 'is-checked' : ''}">
        <span class="box" aria-hidden="true"></span>
        <span class="check-label">${esc(item.label)}</span>
        <span class="check-meta">${item.checked ? `noted ${fmtDate(item.last_noted_at)}` : 'not recorded yet'}</span>
        <span class="visually-hidden">${item.checked ? '(has a field note)' : '(no field note yet)'}</span>
      </li>`
    )
    .join('')}</ul>`;
}

async function renderDetail(key) {
  const res = await fetch(`/api/experiments/${encodeURIComponent(key)}`);
  if (res.status === 404) {
    document.title = 'Not found · TFT Theory Lab';
    view.innerHTML = `
      <p class="crumbs"><a href="/experiments">← all experiments</a></p>
      <p class="state-note">There's no page called &ldquo;${esc(key)}&rdquo; in this notebook.</p>`;
    return;
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const e = (await res.json()).experiment;
  const c = e.comp;
  document.title = `${e.title} · My Experiments`;

  view.innerHTML = `
    <p class="crumbs"><a href="/experiments">← all experiments</a></p>
    <article class="notes-page theory-page">
      <div class="notes-row theory-head-row">
        <p class="margin-note">${esc(LIFECYCLE_NOTES[e.lifecycle] || e.lifecycle)}</p>
        <div>
        <header class="theory-head">
          ${carryPortrait(e)}
          <div class="theory-title">
            <h2>${esc(e.title)}</h2>
            <p class="notes-meta">${e.carry ? `carry: <b>${esc(e.carry.name || e.carry.character_id)}</b> · ` : ''}jotted ${fmtDate(e.created_at)}${
              e.updated_at !== e.created_at ? ` · revised ${fmtDate(e.updated_at)}` : ''
            }</p>
            ${e.is_example ? '<p class="example-note">example entry, made up for the demo notebook</p>' : ''}
          </div>
          <div class="theory-stamp">${stamp(e.evidence_status)}</div>
        </header>
        ${e.summary ? `<p class="thesis thesis-lg">&ldquo;${esc(e.summary)}&rdquo;</p>` : ''}
        </div>
      </div>
      ${sectionRows([
        { label: 'Core', body: c.core_units.length ? bulletList(unitLabels(e, 'core_units')) : '', margin: 'the units the idea lives or dies on' },
        { label: 'Flex', body: c.optional_units.length ? bulletList(unitLabels(e, 'optional_units')) : '' },
        { label: 'Trait targets', body: c.target_traits.length ? bulletList(traitLabels(e)) : '' },
        { label: 'Items', body: itemsBlock(e) },
        { label: 'Levels & rolling', body: levelsBlock(c) },
        { label: 'Positioning', body: c.positioning_notes ? `<p>${esc(c.positioning_notes)}</p>` : '' },
        { label: 'Augments', body: c.augment_notes ? `<p>${esc(c.augment_notes)}</p>` : '' },
        { label: 'Notes', body: e.author_notes ? `<p>${esc(e.author_notes)}</p>` : '' },
        ...(e.tags.length ? [{ label: 'Tags', body: `<p class="tags">${e.tags.map(t => `#${esc(t)}`).join(' ')}</p>` }] : []),
      ])}
    </article>

    <section class="field-notes" aria-labelledby="field-notes-title">
      <div class="notes-row">
        <p class="margin-note">research still needed<br><span class="quiet-inline">ticked only once a note from that source exists</span></p>
        <div>
          <h3 id="scout-title">Scout checklist</h3>
          ${checklistBlock(e.scout_checklist || [])}
          ${e.fingerprint && e.fingerprint.signature ? `<p class="fingerprint">fingerprint · ${esc(e.fingerprint.signature)}</p>` : ''}
        </div>
      </div>
      <div class="notes-row">
        <p class="margin-note quiet">dated research, oldest first: our data, scout reports, mechanics, sightings, status changes</p>
        <div>
          <h3 id="field-notes-title"><span class="underlined">Field Notes</span></h3>
          ${fieldNotesBlock(e.field_notes || [])}
        </div>
      </div>
    </section>`;
}

async function route() {
  const m = window.location.pathname.match(/^\/experiments\/([^/]+)\/?$/);
  try {
    if (m) await renderDetail(decodeURIComponent(m[1]));
    else await renderList();
  } catch (err) {
    view.innerHTML = `<p class="state-note error">Couldn't open the notebook: ${esc(err.message)}</p>`;
  }
}

route();
