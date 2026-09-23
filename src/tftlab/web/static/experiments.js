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

const carryPortrait = (e, size = '') =>
  `<figure class="portrait portrait-neutral ${size}" aria-hidden="true"><span class="portrait-fallback">${esc(
    initials(e.carry?.name || e.title)
  )}</span></figure>`;

const unitLabel = u =>
  `${esc(u.name)}${u.star ? ` <span class="star">${u.star}★</span>` : ''}${u.note ? ` <span class="aside">(${esc(u.note)})</span>` : ''}`;

// Hand-circled breakpoint number; shape alternates by position, never randomly.
const traitLabel = (t, i) =>
  `${t.breakpoint ? `<span class="bp bp-${i % 3}">${t.breakpoint}</span> ` : ''}${esc(t.name)}${t.note ? ` <span class="aside">(${esc(t.note)})</span>` : ''}`;

function itemsLine(comp) {
  const parts = [];
  if (comp.carry_items.length) parts.push(esc(comp.carry_items.join(', ')));
  if (comp.tank_items.length) parts.push(`<span class="aside">tank:</span> ${esc(comp.tank_items.join(', '))}`);
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
    ['core', c.core_units.map(unitLabel).join(', ')],
    ['trait target', c.target_traits.map(traitLabel).join(', ')],
    ['items', itemsLine(c)],
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

function itemsBlock(c) {
  const parts = [];
  if (c.carry_items.length) parts.push(`<p><span class="aside">carry:</span> ${esc(c.carry_items.join(', '))}</p>`);
  if (c.tank_items.length) parts.push(`<p><span class="aside">tank:</span> ${esc(c.tank_items.join(', '))}</p>`);
  if (c.secondary_carry) {
    const s = c.secondary_carry;
    parts.push(
      `<p><span class="aside">secondary${s.unit ? ` (${esc(s.unit)})` : ''}:</span> ${esc((s.items || []).join(', ')) || '—'}</p>`
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

function fieldNotesBlock(notes) {
  if (!notes.length) {
    return `<p class="field-empty">No field notes yet. This theory hasn't been scouted.</p>`;
  }
  return `<ol class="field-log">${notes
    .map(n => {
      const url = safeUrl(n.source_url);
      const source = n.source_name || url;
      return `<li>
        <span class="log-date">${fmtDate(n.noted_at)}</span>
        ${n.evidence_status ? stamp(n.evidence_status) : ''}
        <span class="log-body">${esc(n.body || n.kind)}${
          source ? ` <span class="aside">(${url ? `<a href="${esc(url)}" rel="noopener noreferrer">${esc(source)}</a>` : esc(source)})</span>` : ''
        }</span>
      </li>`;
    })
    .join('')}</ol>`;
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
        { label: 'Core', body: c.core_units.length ? bulletList(c.core_units.map(unitLabel)) : '', margin: 'the units the idea lives or dies on' },
        { label: 'Flex', body: c.optional_units.length ? bulletList(c.optional_units.map(unitLabel)) : '' },
        { label: 'Trait targets', body: c.target_traits.length ? bulletList(c.target_traits.map(traitLabel)) : '' },
        { label: 'Items', body: itemsBlock(c) },
        { label: 'Levels & rolling', body: levelsBlock(c) },
        { label: 'Positioning', body: c.positioning_notes ? `<p>${esc(c.positioning_notes)}</p>` : '' },
        { label: 'Augments', body: c.augment_notes ? `<p>${esc(c.augment_notes)}</p>` : '' },
        { label: 'Notes', body: e.author_notes ? `<p>${esc(e.author_notes)}</p>` : '' },
        ...(e.tags.length ? [{ label: 'Tags', body: `<p class="tags">${e.tags.map(t => `#${esc(t)}`).join(' ')}</p>` }] : []),
      ])}
    </article>

    <section class="field-notes" aria-labelledby="field-notes-title">
      <div class="notes-row">
        <p class="margin-note quiet">dated sightings, match evidence and status changes get logged here</p>
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
