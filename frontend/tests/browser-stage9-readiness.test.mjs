import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";

const { readRefreshProjection, observeHotRefresh, verifyLongTermAssets, sumFen, employeeOutstandingFen } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const selection = { key: "assets", action: "assets", companyId: "synthetic", period: "2026-09",
  quarter: "2026-Q3", root: ".asset-result", visible: ["#assets-overview"] };
const employeeValueIds = ["cost", "count", "gross", "contributions", "paid", "outstanding"]
  .map(field => `employees-${field}-value`);

function fixture({ brief = false, employees = false } = {}) {
  const node = (innerText, visible = true) => ({ innerText, visible,
    getClientRects() { return this.visible ? [{}] : []; } });
  const summary = node("合计 ¥123.45");
  const assetAmount = node("¥678.90"), assetCounts = node("固定 2 项 · 无形 1 项");
  const asset = { ...node("长期资产净值"), querySelector: selector =>
    ({ strong: assetAmount, small: assetCounts })[selector] ?? null };
  const employeeValues = Object.fromEntries(employeeValueIds.map(id =>
    [id, node(id === "employees-count-value" ? "3人" : "¥123.45")]));
  const employeeNote = node("本月有薪酬记录 2 人 · 另有 1 人在册状态未确认");
  if (brief) Object.defineProperty(summary, "innerText", { get: () =>
    `合计 ¥123.45\n${asset.visible ? `${assetAmount.innerText}\n${assetCounts.innerText}` : ""}` });
  if (employees) Object.defineProperty(summary, "innerText", { get: () =>
    [...Object.values(employeeValues).filter(value => value.visible).map(value => value.innerText),
      employeeNote.visible ? employeeNote.innerText : ""].join("\n") });
  const rows = node("默认20行，包括资产及项目");
  const root = { children: [summary, rows, node("未打开的详情", false)], querySelector: () => null,
    querySelectorAll: () => [], getAttribute: name =>
      ({ "data-month-state": "closed", "data-owner-review-required": "false" })[name] ?? null };
  let busy = "false";
  const header = { getAttribute: () => busy };
  const company = node("合成公司"), period = { value: "2026-09", getAttribute() { return this.value; } }, status = node("已关账");
  const document = { defaultView: { getComputedStyle: () => ({ visibility: "visible" }) },
    querySelector: selector => ({ ".asset-result": root, "#assets-overview": summary,
      ".employee-result": root, "#employees-overview": summary, "#employee-list-title": rows,
      ...Object.fromEntries(Object.entries(employeeValues).map(([id, value]) => [`#${id}`, value])),
      ".brief-content": root, "#overview": summary, "#overview .kpi.asset": brief ? asset : null,
      ".module-header": header, ".company-switcher-name": company,
      ".module-header .select-trigger": period, ".module-header .period-status": status })[selector] ?? null };
  return { document, root, rows, company, period, status, asset, assetAmount, assetCounts,
    employeeValues, employeeNote,
    setBusy: value => { busy = value; } };
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

function observerFixture({ projection = true, brief = false, employees = false } = {}) {
  const f = fixture({ brief, employees }), events = [], frames = [], window = {};
  const selected = brief ? { ...selection, key: "brief", action: "brief", root: ".brief-content", visible: ["#overview"] }
    : employees ? { ...selection, key: "employees", action: "employees", root: ".employee-result",
      visible: ["#employees-overview", "#employee-list-title", ...employeeValueIds.map(id => `#${id}`)] } : selection;
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
  const expected = projection ? readRefreshProjection(f.document, selected.root) : undefined;
  observe({ addEventListener: (name, fn, options) => {
    assert.equal(name, "click"); assert.equal(options.capture, true); assert.equal(options.once, true); callback = fn;
  } }, { ...selected, projection: expected });
  callback();
  return { ...f, events, window, get disconnected() { return disconnected; },
    busy() { f.setBusy("true"); mutations([{ oldValue: "false" }]); },
    ready() { f.setBusy("false"); mutations([{ oldValue: "true" }]); },
    network() { resources = ["context", selected.action].map(action => ({
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

test("brief long-term asset amount and both complete counts are typed, preserving unknown and negative net", () => {
  for (const net_fen of [null, "0", "-123", "456"]) {
    assert.doesNotThrow(() => verifyLongTermAssets({ net_fen, fixed_active_count: 0, intangible_active_count: 2 }));
  }
  for (const value of [undefined, {}, { net_fen: 123, fixed_active_count: 0, intangible_active_count: 0 },
    { net_fen: "0", fixed_active_count: -1, intangible_active_count: 0 },
    { net_fen: "0", fixed_active_count: 1, intangible_active_count: 0.5 },
    { net_fen: "0", fixed_active_count: 1 }]) assert.throws(() => verifyLongTermAssets(value));
});

test("brief card, amount or counts missing blocks even the unprojected first warmup", () => {
  for (const [field, missing] of [["visible", false], ["amount", ""], ["counts", "固定 2 项"]]) {
    const f = observerFixture({ brief: true, projection: false });
    const target = field === "visible" ? f.asset : field === "amount" ? f.assetAmount : f.assetCounts;
    const property = field === "visible" ? "visible" : "innerText", old = target[property];
    target[property] = missing;
    f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
    assert.equal(f.window.__stage9RenderedAt, null, `${field} was missing`);
    target[property] = old; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
});

test("brief long-term net and counts participate in the existing two-frame stable projection", () => {
  for (const [field, changed] of [["assetAmount", "¥678.91"], ["assetCounts", "固定 3 项 · 无形 1 项"]]) {
    const f = observerFixture({ brief: true }), node = f[field], old = node.innerText;
    f.busy(); f.ready(); f.network(); node.innerText = changed;
    f.frame(120); f.frame(140); assert.equal(f.window.__stage9RenderedAt, null);
    node.innerText = old; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
});

test("employee combined overview amounts remain exact and preserve either unknown input", () => {
  assert.equal(sumFen("9007199254740993", "7"), "9007199254741000");
  assert.equal(sumFen("-123", "23"), "-100");
  assert.equal(sumFen("0", "0"), "0");
  for (const [first, second] of [[null, "123"], ["123", null], [null, null]])
    assert.equal(sumFen(first, second), null);
});

test("employee unpaid headline reads the full remuneration field in contract 10, preserving zero and unknown", () => {
  const data = { employees: { outstanding_net_fen: "12345" }, outstanding_remuneration_fen: "67890" };
  assert.equal(employeeOutstandingFen(data, 9), "12345");
  assert.equal(employeeOutstandingFen(data, 10), "67890");
  for (const value of ["0", null, "9007199254740993"]) {
    data.outstanding_remuneration_fen = value;
    assert.equal(employeeOutstandingFen(data, 10), value);
  }
  delete data.outstanding_remuneration_fen;
  assert.throws(() => employeeOutstandingFen(data, 10), "Missing total must not fall back to wages");
  assert.throws(() => employeeOutstandingFen(data, 11));
});

test("each of the six employee overview values must be visible and populated before the first warmup completes", () => {
  for (const id of employeeValueIds) for (const [field, missing] of [["visible", false], ["innerText", ""]]) {
    const f = observerFixture({ employees: true, projection: false }), value = f.employeeValues[id], original = value[field];
    value[field] = missing;
    f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
    assert.equal(f.window.__stage9RenderedAt, null, `${id} was not ready`);
    value[field] = original; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
  const f = observerFixture({ employees: true, projection: false });
  f.employeeValues["employees-count-value"].innerText = "人";
  f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
  assert.equal(f.window.__stage9RenderedAt, null, "Count unit alone was mistaken for a ready headcount");
});

test("all employee metrics, conditional notes and default detail rows participate in the two-frame projection", () => {
  for (const target of [...employeeValueIds, "note", "rows"]) {
    const f = observerFixture({ employees: true });
    const node = target === "note" ? f.employeeNote : target === "rows" ? f.rows : f.employeeValues[target];
    const original = node.innerText;
    node.innerText = target === "employees-count-value" ? "4人" : "内容或第20条尚未完成";
    f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
    assert.equal(f.window.__stage9RenderedAt, null, `${target} differed from the complete page`);
    node.innerText = original; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
});
