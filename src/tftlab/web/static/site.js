// TheoryLabs: shared page chrome. Says, on every data page, where the numbers
// come from (/api/source) and how fresh they are, so synthetic demo data can
// never be mistaken for observed Riot match evidence. No cookies, no storage,
// no third-party requests.
(function () {
  'use strict';

  const banner = document.querySelector('#source-banner');

  function fmtDate(ms) {
    if (!ms) return 'unknown';
    return new Date(Number(ms)).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function fmtNum(n) {
    return Number(n || 0).toLocaleString();
  }

  function setText(selector, text) {
    document.querySelectorAll(selector).forEach(el => { el.textContent = text; });
  }

  function show(kind, title, body) {
    if (!banner) return;
    banner.hidden = false;
    banner.className = `source-banner source-${kind}`;
    banner.replaceChildren();
    const strong = document.createElement('strong');
    strong.className = 'source-banner-title';
    strong.textContent = title;
    const text = document.createElement('span');
    text.textContent = ` ${body}`;
    banner.append(strong, text);
  }

  function describe(src) {
    if (src.synthetic) {
      show('demo', 'Demo data.',
        'Every match, statistic and date on this page is synthetic, generated so the site can be tried without a ' +
        'database. None of it is observed Riot match evidence.');
      return;
    }
    const what = src.mode === 'snapshot'
      ? `A read-only snapshot of ${fmtNum(src.matches)} indexed ranked matches`
      : `${fmtNum(src.matches)} indexed ranked matches`;
    let body = `${what} (${fmtNum(src.boards)} player boards); latest game ${fmtDate(src.latest_game_datetime)}.`;
    if (src.mode === 'snapshot' && src.snapshot && src.snapshot.exported_at) {
      body += ` Snapshot exported ${src.snapshot.exported_at}.`;
    }
    if (!src.matches) {
      show('empty', 'No matches indexed yet.', 'The data source is connected but holds no matches, so there is nothing to analyze.');
    } else if (src.stale) {
      show('stale', 'Older data.', `${body} That is more than ${src.stale_after_days} days ago, so it may not reflect the current patch.`);
    } else {
      show('observed', src.mode === 'snapshot' ? 'Analytics snapshot.' : 'Historical match data.', body);
    }
  }

  function apply(src) {
    window.TL_SOURCE = src;
    document.body.classList.toggle('source-synthetic', Boolean(src.synthetic));
    document.body.classList.toggle('source-observed', Boolean(src.observed));
    setText('#data-mode', src.label);
    setText('#footer-mode', src.label);
    describe(src);
    document.dispatchEvent(new CustomEvent('tl:source', { detail: src }));
  }

  // Shared label for page scripts (app.js / champion.js call it with the
  // `demo` flag every API response carries).
  window.TL_sourceLabel = function (demo) {
    if (demo) return 'demo data (synthetic)';
    return (window.TL_SOURCE && window.TL_SOURCE.label) || 'indexed match data';
  };

  if (!banner) return;
  fetch('/api/source', { headers: { Accept: 'application/json' } })
    .then(res => res.json().then(body => ({ ok: res.ok, body })).catch(() => ({ ok: false, body: {} })))
    .then(({ ok, body }) => {
      if (!ok) {
        setText('#data-mode', 'unavailable');
        setText('#footer-mode', 'data unavailable');
        show('error', 'Data unavailable.',
          'The analytics data source could not be reached right now. The explanations on this site still work; ' +
          'statistics will appear when the data source is back.');
        return;
      }
      apply(body);
    })
    .catch(() => {
      show('error', 'Data unavailable.', 'The site could not reach its own data service. Check your connection and reload.');
    });
})();
