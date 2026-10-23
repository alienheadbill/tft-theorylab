// What the Observed comps page (src/tftlab/web/static/comps.js) requests and
// renders, run with Node's built-in test runner from tests/test_webapp_comps.py.
// The real page script runs in a vm context with a minimal fake DOM and a
// recording fetch; API bodies come from a seeded app (COMPS_FIXTURES, written
// by the Python runner). No browser, no npm.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "tftlab", "web", "static");
const FIXTURES = process.env.COMPS_FIXTURES;
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));

function fakeElement(selector) {
  const listeners = {};
  const attrs = {};
  return {
    selector, hidden: false, innerHTML: "", textContent: "", value: "", disabled: false,
    dataset: {}, style: {}, listeners, attrs,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    querySelector() { return fakeElement("child"); },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

function loadPage({ respond, search = "" } = {}) {
  const elements = {};
  const docListeners = {};
  const document = {
    querySelector(sel) { return elements[sel] || (elements[sel] = fakeElement(sel)); },
    querySelectorAll() { return []; },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  };
  const requests = [];
  const location = { pathname: "/comps", search, hash: "", href: `http://127.0.0.1/comps${search}` };
  const context = {
    document, URL, URLSearchParams, console, setTimeout, clearTimeout, Promise,
    fetch: async url => {
      requests.push(url);
      const r = await respond(url);
      return { ok: r.status === 200, status: r.status, statusText: String(r.status), json: async () => r.body };
    },
  };
  context.window = {
    location,
    history: { replaceState(_s, _t, url) { const u = new URL(String(url)); location.search = u.search; location.href = u.href; } },
    addEventListener() {},
  };
  vm.createContext(context);
  for (const file of ["art.js", "comps.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, file), "utf8"), context, { filename: file });
  }
  return { requests, el: sel => document.querySelector(sel), docListeners, location };
}

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const COMPS = /^\/api\/comps\?/;

function api({ comps = null, held = null } = {}) {
  return url => {
    if (url.startsWith("/api/balance-windows")) return { status: 200, body: fixture("balance_windows") };
    if (COMPS.test(url)) {
      const q = new URLSearchParams(url.split("?")[1]);
      const body = comps || (q.get("level") === "9" ? fixture("comps_level9") : q.get("cost") === "1" ? fixture("comps_empty") : fixture("comps"));
      if (held) return new Promise(resolve => held.push(() => resolve({ status: 200, body })));
      return body.status ? body : { status: 200, body };
    }
    return { status: 404, body: { detail: "not found" } };
  };
}

const compsCalls = reqs => reqs.filter(u => COMPS.test(u));
const cards = html => (html.match(/class="board-card comp-card/g) || []).length;

test("the page asks for the window list, then the comps at the product floor", async () => {
  const page = loadPage({ respond: api() });
  await settle();
  assert.deepEqual(page.requests.map(u => u.split("?")[0]), ["/api/balance-windows", "/api/comps"]);
  const q = new URLSearchParams(compsCalls(page.requests)[0].split("?")[1]);
  assert.equal(q.get("min_boards"), "10");
  assert.equal(q.get("balance_window"), fixture("balance_windows").default_balance_window);
});

test("cards render in the API's order with boards, level, window and freshness", async () => {
  const page = loadPage({ respond: api() });
  await settle();
  const body = fixture("comps");
  const html = page.el("#comps-list").innerHTML;
  assert.equal(cards(html), body.patterns.length);
  let last = -1;
  for (const p of body.patterns) {
    const at = html.indexOf(`<b>${p.boards} boards</b>`, last + 1);
    assert.ok(at > last, `pattern with ${p.boards} boards in API order`);
    last = at;
  }
  assert.match(html, /Level 8 · 4 units · Observed board pattern/);
  assert.match(html, /Kog(&#39;|')Maw · Draven · Karma \+ 1/);
  assert.match(html, /href="\/champions\/kogmaw\?balance_window=[^"#]+#boards"/);
  assert.equal((html.match(/Balance window /g) || []).length, body.patterns.length, "every card shows its window");
  assert.equal((html.match(/latest game /g) || []).length, body.patterns.length, "every card shows freshness");
  assert.match(html, /How these boards placed/);
  assert.match(html, /Committed carries: /);
  assert.match(page.el("#comps-count").textContent, /most boards first/);
  assert.equal(page.el("#window-boards").textContent, Number(body.boards).toLocaleString());
});

test("a filter change requests that filter and keeps it in the URL", async () => {
  const page = loadPage({ respond: api() });
  await settle();
  const level = page.el("#level-filter");
  level.value = "9";
  level.listeners.change.forEach(fn => fn({}));
  await settle();
  const last = compsCalls(page.requests).at(-1);
  assert.match(last, /level=9/);
  assert.match(page.location.search, /level=9/);
  assert.equal(cards(page.el("#comps-list").innerHTML), fixture("comps_level9").patterns.length);
});

test("filters from the URL are used, and min boards never goes under the floor", async () => {
  const page = loadPage({ respond: api(), search: "?cost=1&min_boards=4" });
  await settle();
  const q = new URLSearchParams(compsCalls(page.requests)[0].split("?")[1]);
  assert.equal(q.get("cost"), "1");
  assert.equal(q.get("min_boards"), "10");
  assert.match(page.el("#comps-list").innerHTML, /No observed pattern matches these filters/);
});

test("the unavailable state shows the API's own reason", async () => {
  const unavailable = { ...fixture("comps"), available: false, reason: "Player levels not preserved in this data.", patterns: [] };
  const page = loadPage({ respond: api({ comps: unavailable }) });
  await settle();
  assert.match(page.el("#comps-list").innerHTML, /Player levels not preserved in this data/);
});

test("an API failure shows an error with a retry", async () => {
  const page = loadPage({ respond: api({ comps: { status: 500, body: { error: "boom" } } }) });
  await settle();
  assert.match(page.el("#comps-list").innerHTML, /Could not load observed comps \(boom\)/);
  assert.match(page.el("#comps-list").innerHTML, /data-action="retry"/);
});

test("an older response never overwrites a newer filter", async () => {
  const held = [];
  const page = loadPage({ respond: api({ held }) });
  await settle();
  const level = page.el("#level-filter");
  level.value = "9";
  level.listeners.change.forEach(fn => fn({}));
  await settle();
  assert.equal(held.length, 2);
  held[1](); // the level-9 answer first
  await settle();
  held[0](); // then the stale first answer
  await settle();
  assert.equal(cards(page.el("#comps-list").innerHTML), fixture("comps_level9").patterns.length);
});
