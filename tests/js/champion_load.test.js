// What the Champion Investigation page (src/tftlab/web/static/champion.js)
// requests on load, run with Node's built-in test runner from
// tests/test_champion_page_js.py. The real page script runs in a vm context
// with a minimal fake DOM and a recording fetch; API bodies come from the
// demo app (CHAMPION_FIXTURES, written by the Python runner). No browser, no npm.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "tftlab", "web", "static");
const FIXTURES = process.env.CHAMPION_FIXTURES;
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));

// Elements champion.html ships with the `hidden` attribute.
const HIDDEN_IN_HTML = new Set(["#picker-lazy"]);

function fakeElement(selector) {
  const listeners = {};
  const attrs = {};
  return {
    selector, hidden: HIDDEN_IN_HTML.has(selector), innerHTML: "", textContent: "", value: "", checked: false, disabled: false,
    dataset: {}, style: {}, listeners, attrs, focused: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    querySelector(sel) { return this.children?.[sel] || (this.children = this.children || {}, this.children[sel] = fakeElement(sel)); },
    querySelectorAll() { return []; },
    focus() { this.focused += 1; },
    scrollIntoView() {},
    closest() { return null; },
    contains() { return false; },
  };
}

// Load the page at `pathname`; `respond(url)` returns {status, body} or a pending promise.
function loadPage(pathname, { respond, search = "", hash = "" } = {}) {
  const elements = {};
  const docListeners = {};
  const document = {
    querySelector(sel) { return elements[sel] || (elements[sel] = fakeElement(sel)); },
    querySelectorAll() { return []; },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    title: "",
  };
  const requests = [];
  const winListeners = {};
  const location = { pathname, search, hash, href: `http://127.0.0.1${pathname}${search}${hash}` };
  const context = {
    document, URL, URLSearchParams, console, setTimeout, clearTimeout, Promise,
    requests,
    fetch: async url => {
      requests.push(url);
      const r = await respond(url);
      return { ok: r.status === 200, status: r.status, statusText: String(r.status), json: async () => r.body };
    },
  };
  context.window = {
    location,
    history: { replaceState(_s, _t, url) { const u = new URL(String(url)); location.search = u.search; location.hash = u.hash; location.href = u.href; } },
    addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
  };
  vm.createContext(context);
  for (const file of ["art.js", "champion.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, file), "utf8"), context, { filename: file });
  }
  const el = sel => document.querySelector(sel);
  return { requests, el, docListeners, winListeners, location };
}

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const DIRECTORY = /^\/api\/champions(\?|$)/;
const DETAIL = /^\/api\/champions\/khazix\?/;

// `held`, when given, collects a release function per detail request instead of answering it.
function api({ hangDirectory = false, failDetail = 0, held = null } = {}) {
  let detailFailures = failDetail;
  return url => {
    if (url.startsWith("/api/balance-windows")) return { status: 200, body: fixture("balance_windows") };
    if (DIRECTORY.test(url)) return hangDirectory ? new Promise(() => {}) : { status: 200, body: fixture("champions") };
    if (DETAIL.test(url)) {
      if (held) return new Promise(resolve => held.push(() => resolve({ status: 200, body: fixture("khazix") })));
      if (detailFailures > 0) { detailFailures -= 1; return { status: 500, body: { error: "boom" } }; }
      return { status: 200, body: fixture("khazix") };
    }
    return { status: 404, body: { detail: "not found" } };
  };
}

const directoryCalls = reqs => reqs.filter(u => DIRECTORY.test(u));
const detailCalls = reqs => reqs.filter(u => DETAIL.test(u));

test("/champions loads the champion directory and renders the grid", async () => {
  const page = loadPage("/champions", { respond: api() });
  await settle();
  assert.equal(directoryCalls(page.requests).length, 1);
  assert.equal(detailCalls(page.requests).length, 0);
  const list = page.el("#champion-list").innerHTML;
  assert.match(list, /cost-group/);
  assert.match(list, /carry board/);
  assert.match(list, /href="\/champions\/khazix/);
  assert.equal(page.el("#picker-body").hidden, false);
  assert.equal(page.el("#picker-lazy").hidden, true, "the lazy opener is for direct pages only");
});

test("a direct champion page never requests the full directory on load (regression)", async () => {
  const page = loadPage("/champions/khazix", { respond: api() });
  await settle();
  assert.deepEqual(page.requests.map(u => u.split("?")[0]), ["/api/balance-windows", "/api/champions/khazix"]);
  assert.equal(directoryCalls(page.requests).length, 0, "the old init fetched /api/champions alongside the detail");
  assert.match(page.el("#champion-view").innerHTML, /Kha(&#39;|')Zix/);
  // Nothing from the roster grid was rendered, so no unrelated portraits were requested.
  assert.equal(page.el("#champion-list").innerHTML, "");
  assert.equal(page.el("#picker-body").hidden, true);
  assert.equal(page.el("#picker-lazy").hidden, false);
});

test("the investigation renders even if the directory would never answer", async () => {
  const page = loadPage("/champions/khazix", { respond: api({ hangDirectory: true }) });
  await settle();
  assert.match(page.el("#champion-view").innerHTML, /role="tablist"/);
  for (const label of ["How to play", "Boards", "Items", "Teammates", "Traits", "Evidence"]) {
    assert.match(page.el("#champion-view").innerHTML, new RegExp(`>${label}</button>`), label);
  }
});

test("the lazy picker fetches the directory once, only when opened", async () => {
  const page = loadPage("/champions/khazix", { respond: api() });
  await settle();
  assert.equal(directoryCalls(page.requests).length, 0);
  const open = page.el("#picker-open");
  open.listeners.click.forEach(fn => fn({}));
  await settle();
  open.listeners.click.forEach(fn => fn({}));
  await settle();
  assert.equal(directoryCalls(page.requests).length, 1);
  assert.equal(page.el("#picker-body").hidden, false);
  assert.equal(page.el("#picker-lazy").hidden, true);
  assert.equal(open.getAttribute("aria-expanded"), "true");
  assert.ok(page.el("#champion-search").focused >= 1, "focus moves to the search field");
  assert.match(page.el("#champion-list").innerHTML, /aria-current="page"/); // the open champion is marked
});

test("a picker opened before the champion answers still marks the champion once it does", async () => {
  const held = [];
  const page = loadPage("/champions/khazix", { respond: api({ held }) });
  await settle();
  page.el("#picker-open").listeners.click.forEach(fn => fn({}));
  await settle();
  assert.doesNotMatch(page.el("#champion-list").innerHTML, /aria-current="page"/);
  held.forEach(release => release());
  await settle();
  assert.match(page.el("#champion-list").innerHTML, /aria-current="page"/);
  assert.equal(directoryCalls(page.requests).length, 1);
});

function changeWindow(page, value) {
  const select = page.el("#window-filter");
  select.value = value;
  select.listeners.change.forEach(fn => fn({}));
}

test("changing the window reloads the champion without fetching an unopened picker", async () => {
  const page = loadPage("/champions/khazix", { respond: api() });
  await settle();
  const other = fixture("balance_windows").windows.find(w => w.balance_window !== fixture("balance_windows").default_balance_window);
  changeWindow(page, other.balance_window);
  await settle();
  const details = detailCalls(page.requests);
  assert.equal(details.length, 2);
  assert.match(details[1], new RegExp(`balance_window=${other.balance_window.replace(".", "\\.")}`));
  assert.equal(directoryCalls(page.requests).length, 0);
  assert.match(page.location.search, /balance_window=/);
});

test("an opened picker follows window changes", async () => {
  const page = loadPage("/champions/khazix", { respond: api() });
  await settle();
  page.el("#picker-open").listeners.click.forEach(fn => fn({}));
  await settle();
  const other = fixture("balance_windows").windows.find(w => w.balance_window !== fixture("balance_windows").default_balance_window);
  changeWindow(page, other.balance_window);
  await settle();
  const dirs = directoryCalls(page.requests);
  assert.equal(dirs.length, 2);
  assert.match(dirs[1], new RegExp(`balance_window=${other.balance_window.replace(".", "\\.")}`));
});

test("directory search and the only-carried filter still work", async () => {
  const page = loadPage("/champions", { respond: api() });
  await settle();
  const all = fixture("champions").champions;
  const search = page.el("#champion-search");
  search.value = "kha";
  search.listeners.input.forEach(fn => fn({}));
  const list = page.el("#champion-list").innerHTML;
  assert.match(list, /Kha(&#39;|')Zix/);
  assert.equal((list.match(/class="champ-link"/g) || []).length, all.filter(c => c.name.toLowerCase().replace(/[^a-z]/g, "").includes("kha")).length);
  search.value = "";
  const carried = page.el("#only-carried");
  carried.checked = true;
  carried.listeners.change.forEach(fn => fn({}));
  const shown = (page.el("#champion-list").innerHTML.match(/class="champ-link"/g) || []).length;
  assert.equal(shown, all.filter(c => c.carry_games > 0).length);
  assert.ok(shown < all.length);
});

test("a tab named in the URL hash is the open panel", async () => {
  const page = loadPage("/champions/khazix", { respond: api(), hash: "#boards" });
  await settle();
  const html = page.el("#champion-view").innerHTML;
  assert.match(html, /id="panel-boards"[^>]*tabindex="0">/);
  assert.match(html, /id="panel-play"[^>]*hidden>/);
  assert.match(html, /aria-selected="true" tabindex="0" data-tab="boards"/);
});

test("retry reloads the champion after a failed request, still without the directory", async () => {
  const page = loadPage("/champions/khazix", { respond: api({ failDetail: 1 }) });
  await settle();
  assert.match(page.el("#champion-view").innerHTML, /data-action="retry-view"/);
  const target = { closest: sel => (sel === '[data-action="retry-view"]' ? {} : null) };
  page.docListeners.click.forEach(fn => fn({ target }));
  await settle();
  assert.equal(detailCalls(page.requests).length, 2);
  assert.match(page.el("#champion-view").innerHTML, /role="tablist"/);
  assert.equal(directoryCalls(page.requests).length, 0);
});
