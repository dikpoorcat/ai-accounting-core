import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";

const { readRefreshProjection, observeHotRefresh } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const selection = { key: "assets", action: "assets", companyId: "synthetic", period: "2026-09",
  quarter: "2026-Q3", root: ".asset-result", visible: ["#assets-overview"] };

function fixture() {
  const node = (innerText, visible = true) => ({ innerText, visible,
    getClientRects() { return this.visible ? [{}] : []; } });
  const summary = node("合计 ¥123.45");
  const rows = node("默认20行，包括资产及项目");
  const root = { children: [summary, rows, node("未打开的详情", false)], querySelector: () => null,
    querySelectorAll: () => [] };
  let busy = "false";
  const header = { getAttribute: () => busy };
  const company = node("合成公司"), period = { value: "2026-09" }, status = node("已关账");
  const document = { defaultView: { getComputedStyle: () => ({ visibility: "visible" }) },
    querySelector: selector => ({ ".asset-result": root, "#assets-overview": summary,
      ".module-header": header, ".company-switcher-name": company,
      ".module-header select": period, ".module-header .period-status": status })[selector] ?? null };
  return { document, root, rows, company, period, status, setBusy: value => { busy = value; } };
}

test("default projection covers every visible result block and company/period/status", () => {
  const f = fixture(), original = readRefreshProjection(f.document, selection.root);
  assert(!original.includes("未打开的详情"));
  for (const [node, field, value] of [[f.rows, "innerText", "仅19行"], [f.company, "innerText", "另一公司"],
    [f.period, "value", "2026-08"], [f.status, "innerText", "未关账"], [f.rows, "visible", false]]) {
    const old = node[field]; node[field] = value;
    assert.notEqual(readRefreshProjection(f.document, selection.root), original);
    node[field] = old;
  }
  f.root.children.push({ innerText: "遗漏的个人劳务或基金附属", getClientRects: () => [{}] });
  assert.notEqual(readRefreshProjection(f.document, selection.root), original);
});

test("report projection ignores only the known observation clock, retaining period and summary", () => {
  const f = fixture(), clock = { innerText: "2026年第三季度 · 更新于 10/4 12:30" };
  f.root.querySelector = () => clock;
  f.rows.innerText = `${clock.innerText}\n净利润 ¥123.45`;
  const original = readRefreshProjection(f.document, selection.root);
  clock.innerText = "2026年第三季度 · 更新于 10/4 12:31";
  f.rows.innerText = `${clock.innerText}\n净利润 ¥123.45`;
  assert.equal(readRefreshProjection(f.document, selection.root), original);
  f.rows.innerText = `${clock.innerText}\n净利润 ¥123.46`;
  assert.notEqual(readRefreshProjection(f.document, selection.root), original);
});

test("default projection also proves the visible selection and collapsed detail state", () => {
  const f = fixture();
  const control = { tagName: "DETAILS", open: false, getAttribute: () => null };
  f.root.querySelectorAll = () => [control];
  const original = readRefreshProjection(f.document, selection.root);
  control.open = true;
  assert.notEqual(readRefreshProjection(f.document, selection.root), original);
  assert.equal(readRefreshProjection(f.document, ".missing-result"), null);
});

function observerFixture({ projection = true } = {}) {
  const f = fixture(), events = [], frames = [], window = {};
  let now = 100, callback, mutations, disconnected = false;
  let resources = [];
  const performance = { now: () => { events.push("now"); return now; },
    setResourceTimingBufferSize: () => events.push("buffer"), clearResourceTimings: () => events.push("clear"),
    getEntriesByType: () => resources };
  class MutationObserver {
    constructor(fn) { mutations = fn; }
    observe() {}
    disconnect() { disconnected = true; }
  }
  const context = vm.createContext({ document: f.document, window, performance, MutationObserver, URL,
    requestAnimationFrame: fn => frames.push(fn) });
  const observe = vm.runInContext(`(${observeHotRefresh.toString()})`, context);
  const expected = projection ? readRefreshProjection(f.document, selection.root) : undefined;
  observe({ addEventListener: (name, fn, options) => {
    assert.equal(name, "click"); assert.equal(options.capture, true); assert.equal(options.once, true); callback = fn;
  } }, { ...selection, projection: expected });
  callback();
  return { ...f, events, window, get disconnected() { return disconnected; },
    busy() { f.setBusy("true"); mutations([{ oldValue: "false" }]); },
    ready() { f.setBusy("false"); mutations([{ oldValue: "true" }]); },
    network() { resources = ["context", "assets"].map(action => ({
      name: `https://synthetic.invalid/api/dashboard/${action}?company_id=synthetic&period=2026-09`,
      startTime: 101, responseEnd: 110 })); },
    frame(time) { now = time; assert(frames.length); frames.shift()(); } };
}

test("capture starts before resource clearing; old DOM cannot finish without busy witness", () => {
  const f = observerFixture();
  assert.deepEqual(f.events.slice(0, 3), ["now", "buffer", "clear"]);
  assert.equal(f.window.__stage9RefreshStart, 100);
  f.network(); f.frame(120); f.frame(140);
  assert.equal(f.window.__stage9RenderedAt, null);
  f.busy(); f.ready(); f.frame(160);
  assert.equal(f.window.__stage9RenderedAt, null);
  f.frame(180);
  assert.equal(f.window.__stage9RenderedAt, 180);
  assert(f.disconnected);
});

test("late default rows block completion and mismatches reset the two-frame proof", () => {
  const f = observerFixture();
  f.busy(); f.ready(); f.network();
  const full = f.rows.innerText;
  f.rows.innerText = "金额已更新但项目或第20行未完成";
  f.frame(120); f.frame(140); assert.equal(f.window.__stage9RenderedAt, null);
  f.rows.innerText = full; f.frame(160);
  f.rows.innerText = "又缺少一行"; f.frame(180);
  f.rows.innerText = full; f.frame(200); assert.equal(f.window.__stage9RenderedAt, null);
  f.frame(220); assert.equal(f.window.__stage9RenderedAt, 220);
});

test("first unprojected warmup retains the legacy guard and both RAFs", () => {
  const f = observerFixture({ projection: false });
  f.network(); f.frame(120); assert.equal(f.window.__stage9RenderedAt, null);
  f.frame(140); assert.equal(f.window.__stage9RenderedAt, 140);
});
