import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import * as Vue from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";
import { compile } from "@vue/compiler-dom";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const obligation = (changes = {}) => ({ key: "salary", name: "net", direction: "payable", category_key: "payroll_payables", source_period: "2026-09", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial", ...changes });
const movement = (changes = {}) => ({ id: "salary-payment", subject_id: "salary-business", account_id: "bank-a", account_type: "bank", list_summary: "9月工资付款", display_summary: "9月工资付款", type: "工资付款", party: "张某", date: "2026-09-15", direction: "outflow", amount_fen: "600000", signed_amount_fen: "-600000", correction: false, internal_transfer: false, ...changes });
function scenario(items = [obligation()]) {
  const data = structuredClone(samples.business_status.response.data);
  data.identity.kind = "payroll";
  data.display_profiles = { business: { values: {} }, employees: [], counterparties: [], assets: [], fund_accounts: [] };
  data.latest_source = { deleted: false, period: "2026-10" };
  data.current_business_result = { amount_fen: "9999999", amount_label: "当前业务结果金额", posting_period: "2026-10" };
  data.settlements = { cutoff_period: "2026-09", status: "established", checking: false, obligations: items };
  data.current_followups.settlements = { ...data.settlements, cutoff_period: "2026-10", obligations: structuredClone(items) };
  data.collections.settlement_events.items = [{ id: "tax-correction", name: "tax", mode: "payment", posting_period: "2026-10", direction: -1, relation_state: "resolved", signed_amount_fen: "-12345" }];
  data.collections.settlement_events.page = { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null };
  return data;
}

test("fund movement details add precise progress and context without repeating the selected row", async (t) => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-funds-detail", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.fundsDetailScenario)");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-09");
    const { default: component } = await server.ssrLoadModule("/src/components/BusinessStatusDetails.vue");
    async function render(data = scenario(), fundsContext = movement(), other = {}) {
      globalThis.fundsDetailScenario = data;
      const app = Vue.createSSRApp(component, { subjectId: fundsContext.subject_id, period: "2026-09", presentation: "funds", fundsContext, expanded: true, hideSummary: true, ...other });
      app.use(router); return renderToString(app);
    }
    await t.test("clicked cash amount does not become business total or salary obligation", async () => {
      const html = await render(scenario([obligation(), obligation({ key: "tax", name: "tax", source_amount_fen: "12345", paid_fen: "0", remaining_fen: "12345", settlement_status: "open" })]));
      assert.match(html, /款项进度 · 截至2026年9月末/);
      assert.match(html, /实发工资[\s\S]*还需支付[\s\S]*¥2,000\.00/);
      assert.match(html, /个人所得税[\s\S]*¥123\.45/);
      assert.doesNotMatch(html, /当前业务结果金额|¥99,999\.99|业务对象：|9月工资付款|2026-09-15|本次事项/);
      assert.match(html, /相关款项处理[\s\S]*更正原实际收付款[\s\S]*−¥123\.45/);
      assert.match(html, /compact-status-details/);
    });
    await t.test("unknown and absent obligations never imply paid or settled", async () => {
      const empty = await render(scenario([]));
      assert.doesNotMatch(empty, /已结清|还需支付|款项进度/);
      const unknown = await render(scenario([obligation({ direction: "unknown", category_key: "unknown", remaining_fen: null, settlement_status: "settled" })]));
      assert.match(unknown, /未结金额[\s\S]*待核对/); assert.match(unknown, /收付分类待核对|AI 会计核对中/);
      assert.doesNotMatch(unknown, /已结清|还需支付/);
    });
    await t.test("refund, offset, excess and large integer amounts preserve explicit meaning", async () => {
      const advance = await render(scenario([obligation({ direction: "receivable", category_key: "supplier_advances", paid_fen: "10000", other_settled_fen: "590000" })]));
      assert.match(advance, /尚未冲抵/); assert.match(advance, /已退回[\s\S]*¥100\.00/); assert.match(advance, /已冲抵[\s\S]*¥5,900\.00/);
      assert.doesNotMatch(advance, /还需支付|原应付/);
      const large = await render(scenario([obligation({ source_amount_fen: "9007199254740993", remaining_fen: "-100", settlement_status: "over_settled" })]));
      assert.match(large, /¥90,071,992,547,409\.93/); assert.match(large, /存在超额结算/);
      const correction = await render(scenario([]), movement({ correction: true, signed_amount_fen: "-9007199254740993" }));
      assert.doesNotMatch(correction, /重新付款|重新收款|已结清/);
    });
    await t.test("full summary and purpose deduplication compare complete texts only", async () => {
      for (const display_summary of ["9月工资付款", "张某"]) {
        assert.doesNotMatch(await render(scenario([]), movement({ display_summary })), /事项说明/);
      }
      assert.match(await render(scenario([]), movement({ display_summary: "工资付款" })), /事项说明：工资付款/);
      const data = scenario([]);
      data.display_profiles.business.values = { purpose: "9月工资付款", note: "9月工资付款补充" };
      let html = await render(data, movement({ display_summary: "包含交通补助的9月工资" }));
      assert.match(html, /事项说明：包含交通补助的9月工资/);
      assert.match(html, /用途／备注：9月工资付款补充/); assert.doesNotMatch(html, /用途／备注：9月工资付款<\/p>/);
      data.display_profiles.business.values = { purpose: "包含交通补助的9月工资", note: "包含交通补助的9月工资" };
      html = await render(data, movement({ display_summary: "包含交通补助的9月工资" }));
      assert.doesNotMatch(html, /用途／备注/);
    });
    await t.test("account identity excludes only current account and preserves same names across roles", async () => {
      const profile = (entity_id, display_name) => ({ entity_id, values: { display_name } });
      const data = scenario([]);
      data.display_profiles.fund_accounts = [profile("bank-a", "当前账户"), profile("bank-b", "张某"), profile("bank-c", "当前账户")];
      data.display_profiles.employees = [profile("employee-a", "张某")];
      data.display_profiles.counterparties = [profile("party-a", "张某")];
      const html = await render(data);
      assert.match(html, /相关账户：张某、当前账户/); assert.match(html, /员工：张某/); assert.match(html, /往来方：张某/);
      data.display_profiles.fund_accounts = [profile("bank-a", "当前账户")];
      assert.doesNotMatch(await render(data), /相关账户：/);
    });
    await t.test("historical obligations remain separate from later keyed progress", async () => {
      const data = scenario();
      data.current_followups.settlements.obligations = [obligation({ remaining_fen: "0", paid_fen: "800000", settlement_status: "settled" })];
      const html = await render(data), [historical, later] = html.split("后续进展 ·");
      assert.match(historical, /还需支付[\s\S]*¥2,000\.00/); assert.match(later, /已结清[\s\S]*¥0\.00/);
    });
    await t.test("default and brief entrances retain their original layouts", async () => {
      const standard = await render(scenario(), movement(), { presentation: "default", hideSummary: false });
      assert.match(standard, /当前业务结果金额|业务所属月/); assert.match(standard, /实际清偿记录/);
      const brief = await render(scenario(), movement(), { presentation: "brief", activityContext: { key: "occurrence", subject_id: "salary-business", title: "工资", description: "9月工资", party: "张某", state: "已入账" } });
      assert.match(brief, /整笔业务的款项进度/); assert.match(brief, /这笔业务的相关收付/); assert.doesNotMatch(brief, /相关款项处理/);
    });
  } finally { await server.close(); delete globalThis.fundsDetailScenario; }
});

let sequence = 0;
async function detailHarness() {
  const key = `fundsDetailsHarness${++sequence}`, calls = [], cleanup = [], events = [];
  const route = Vue.reactive({ query: { company_id: "company-a" } });
  const props = Vue.reactive({ subjectId: "salary-business", period: "2026-09", snapshotVersion: "v1", settlementView: "historical", presentation: "funds", fundsContext: movement(), hideSummary: true, expanded: false });
  globalThis[key] = { Vue, props, route, cleanup, events, appendDashboardCollection, fetch: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) };
  const source = readFileSync(new URL("../src/components/BusinessStatusDetails.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const prefix = `const environment = globalThis.${key}; const { ref, computed, watch } = environment.Vue;
    const { appendDashboardCollection } = environment; const onBeforeUnmount = callback => environment.cleanup.push(callback);
    const useRoute = () => environment.route, defineProps = () => environment.props;
    const withDefaults = (value, defaults) => { for (const [key, fallback] of Object.entries(defaults)) if (value[key] === undefined) value[key] = fallback; return value; };
    const defineEmits = () => (...args) => environment.events.push(args), fetchBusinessStatus = environment.fetch;
    const dashboardErrorMessage = error => error.message, isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';`;
  const { outputText } = ts.transpileModule(prefix + source + "\nexport { data, load, loadMore, panelOpen, paginationScope, pausePages, moreError, responseVersion, notice };", { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const instance = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  return { ...instance, calls, props, route, events, unmount: () => { cleanup.forEach(callback => callback()); delete globalThis[key]; } };
}
const result = (id = "first", next = "next") => ({ snapshot_version: "v1", data: { collections: { settlement_events: { items: [{ id }], page: { total_count: 2, filtered_count: 2, returned_count: 1, has_more: Boolean(next), next_cursor: next } } } } });
const tick = async () => { await Promise.resolve(); await Vue.nextTick(); };

test("fund details bind movement/account identity and reject stale scope or snapshot responses", async () => {
  for (const field of ["company", "period", "snapshotVersion", "subjectId", "id", "account_id"]) {
    const view = await detailHarness();
    try {
      assert.equal(view.calls.length, 0);
      view.props.expanded = true; await tick(); assert.equal(view.calls.length, 1);
      assert.equal(view.calls[0].args[0], "2026-09"); assert.equal(view.calls[0].args[1], "salary-business");
      assert.equal(view.calls[0].args[3].expected_version, "v1"); assert.equal(view.calls[0].args[3].settlement_view, "historical");
      if (field === "company") view.route.query.company_id = "company-b";
      else if (["id", "account_id"].includes(field)) view.props.fundsContext[field] += "-changed";
      else view.props[field] += "-changed";
      view.props.expanded = false;
      assert.equal(view.calls[0].args[2].aborted, true, field);
      view.calls[0].resolve(result()); await tick(); assert.equal(view.data.value, null, field);
      assert.equal(view.calls.length, 1, "scope change alone must not fetch");
      view.props.expanded = true; await tick(); assert.equal(view.calls.length, 2);
      view.calls[1].resolve(result()); await tick();
      view.props.expanded = false; await tick(); view.props.expanded = true; await tick();
      assert.equal(view.calls.length, 2, "collapse and reopen reuse valid detail");
    } finally { view.unmount(); }
  }
});

test("fund related payments keep exact pagination scope, cancel collapse continuation and invalidate changed snapshots", async () => {
  const view = await detailHarness();
  try {
    view.props.expanded = true; await tick(); view.calls[0].resolve(result()); await tick();
    const original = view.data.value.collections.settlement_events.items;
    const pending = view.loadMore(); assert.equal(view.calls[1].args[3].cursor, "next"); assert.equal(view.calls[1].args[3].expected_version, "v1");
    view.pausePages("old-scope"); assert.equal(view.calls[1].args[2].aborted, false);
    view.props.expanded = false; await tick(); view.pausePages(view.paginationScope());
    assert.equal(view.calls[1].args[2].aborted, true);
    view.calls[1].resolve(result("stale", null)); await pending;
    assert.deepEqual(original.map(item => item.id), ["first"]);
    view.props.expanded = true; await tick();
    const retry = view.loadMore(); view.calls[2].resolve(result("second", null)); await retry;
    assert.equal(view.data.value.collections.settlement_events.items, original); assert.deepEqual(original.map(item => item.id), ["first", "second"]);
    view.data.value.collections.settlement_events.page = result().data.collections.settlement_events.page;
    const changed = view.loadMore(); view.calls[3].reject(Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" })); await changed;
    assert.equal(view.data.value, null); assert.equal(view.responseVersion.value, ""); assert.deepEqual(view.events, [["changed"]]);
    assert.match(view.notice.value, /业务资料已变化/); assert.equal(view.calls.length, 4);
  } finally { view.unmount(); }
});

async function rowHarness({ deferRequests = false } = {}) {
  const key = `fundsRowsHarness${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-09" }, hash: "" });
  const mounted = [], cleanup = [], calls = [];
  globalThis[key] = { Vue, route, appendDashboardCollection, mounted, cleanup,
    fetch: (...args) => {
      if (!deferRequests) return Promise.reject(new Error("unexpected fetch"));
      return new Promise((resolve, reject) => calls.push({ args, resolve, reject }));
    } };
  const source = readFileSync(new URL("../src/views/FundsView.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const money = readFileSync(new URL("../src/utils/money.ts", import.meta.url), "utf8").replace(/export /g, "");
  const prefix = `const environment = globalThis.${key}; const { computed, nextTick, ref, watch } = environment.Vue;
    const { appendDashboardCollection } = environment;
    const onMounted = callback => environment.mounted.push(callback);
    const onBeforeUnmount = callback => environment.cleanup.push(callback);
    const useRoute = () => environment.route, useRouter = () => ({ replace: async () => {}, push: async () => {} });
    const useDashboardContext = () => ({ context: ref(null), loading: ref(false), error: ref(''), load: async () => {}, refresh: async () => {} });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const fetchFundsDashboard = environment.fetch;
    const dashboardErrorMessage = String, isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fundAccountDisplayLabel = String, fundAccountDisplayName = String, fundAccountLabel = () => null, rememberFundAccounts = () => {};`;
  const { outputText } = ts.transpileModule(prefix + money + source + "\nexport { toggleMovement, handleMovementKey, expandedMovementId, selectedPeriod, selectedAccount, selectedBankAccount, selectedDetailView, snapshotVersion, activeSection, movementAmount, funds, expandedBatchId, previewBatchId, previewBatchRow, batchPreviewPanel, batchPreviewList, batchPreviewPosition, showBatchPreview, toggleBatchDetails, clearBatchPreview, resetBatchDetails, leaveBatchTrigger, leaveBatchPreview, keepBatchPreview, handleBatchTriggerKey, handleBatchPreviewKey, handleBatchEscape, handleBatchViewportChange, invalidateRequests, loadFunds, loadMore, pausePages, paginationScope, pageStates, loading, bankRows, batchDetailsTitle, batchScopeNote, cancelBatchClose, scheduleBatchClose, formatFen };", { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const instance = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  return { ...instance, route, calls, mount: async () => { for (const callback of mounted) await callback(); }, unmount: () => { cleanup.forEach(callback => callback()); delete globalThis[key]; } };
}

test("movement rows toggle by stable occurrence id, support row keyboard input and keep exact cash amount", async () => {
  const view = await rowHarness();
  const previousElement = globalThis.Element;
  class ElementStub { constructor(interactive = false) { this.interactive = interactive; } closest() { return this.interactive ? this : null; } }
  globalThis.Element = ElementStub;
  try {
    const first = movement(), second = movement({ id: "salary-refund", correction: true });
    view.toggleMovement(first); assert.equal(view.expandedMovementId.value, first.id);
    view.toggleMovement(second); assert.equal(view.expandedMovementId.value, second.id, "same subject has distinct occurrence ids");
    view.toggleMovement(second); assert.equal(view.expandedMovementId.value, "");
    view.toggleMovement(movement({ subject_id: "" })); assert.equal(view.expandedMovementId.value, "");
    view.toggleMovement(first, { target: new ElementStub(true) }); assert.equal(view.expandedMovementId.value, "");
    const target = new ElementStub(); let prevented = 0;
    const event = key => ({ key, target, currentTarget: target, preventDefault: () => { prevented += 1; } });
    view.handleMovementKey(first, event("Enter")); assert.equal(view.expandedMovementId.value, first.id);
    view.handleMovementKey(first, event(" ")); assert.equal(view.expandedMovementId.value, ""); assert.equal(prevented, 2);
    view.handleMovementKey(first, { ...event("Enter"), target: new ElementStub(true) }); assert.equal(view.expandedMovementId.value, "");
    view.handleMovementKey(first, event("Escape")); assert.equal(prevented, 2);
    assert.equal(view.movementAmount("outflow", first.amount_fen), "−¥6,000.00");
    assert.equal(view.movementAmount("inflow", "9007199254740993"), "+¥90,071,992,547,409.93");
    for (const change of [() => { view.route.query.company_id = "company-b"; }, () => { view.route.query.period = "2026-10"; }, () => { view.selectedPeriod.value = "2026-10"; }, () => { view.selectedAccount.value = "bank:bank-b"; }, () => { view.snapshotVersion.value = "v2"; }, () => { view.selectedDetailView.value = "bank"; }]) {
      view.toggleMovement(first); change(); assert.equal(view.expandedMovementId.value, "", "scope change closes old row");
    }
    const source = readFileSync(new URL("../src/views/FundsView.vue", import.meta.url), "utf8");
    assert.match(source, /:key="item.id" class="book-activity-row business-list-row"/);
    assert.match(source, /@click="toggleMovement\(item, \$event\)" @keydown="handleMovementKey\(item, \$event\)"/);
    assert.match(source, /presentation="funds" :funds-context="item" :expanded="expandedMovementId === item.id" hide-summary/);
    assert.match(source, /item\.correction \? formatFen\(item\.signed_amount_fen\) : movementAmount\(item\.direction, item\.amount_fen\)/);
  } finally { globalThis.Element = previousElement; }
});

const batchRow = (id, batch = true) => ({
  id,
  batch_payment: batch ? { items: [{ party: "张某", amount_fen: "10000" }], bank_row_count: 2, total_fen: "20000" } : null,
});

function batchDOM() {
  const original = { window: globalThis.window, document: globalThis.document, Node: globalThis.Node, HTMLElement: globalThis.HTMLElement, HTMLButtonElement: globalThis.HTMLButtonElement };
  const listeners = new Map();
  let finePointer = true;
  class NodeStub {
    constructor() { this.isConnected = true; }
  }
  class HTMLElementStub extends NodeStub {
    constructor(rect = { left: 300, right: 400, top: 100, bottom: 130, width: 100, height: 30 }) {
      super(); this.rect = rect; this.focusCount = 0; this.onFocus = null;
    }
    getBoundingClientRect() { return this.rect; }
    getClientRects() { return [this.rect]; }
    focus() { if (this.props?.disabled) return; this.focusCount += 1; document.activeElement = this; this.onFocus?.(); }
    contains(node) { for (let current = node; current; current = current.parent) if (current === this) return true; return false; }
    closest() { return null; }
  }
  class HTMLButtonElementStub extends HTMLElementStub {}
  const listen = (type, callback) => { const callbacks = listeners.get(type) ?? new Set(); callbacks.add(callback); listeners.set(type, callbacks); };
  const unlisten = (type, callback) => listeners.get(type)?.delete(callback);
  globalThis.Node = NodeStub;
  globalThis.HTMLElement = HTMLElementStub;
  globalThis.HTMLButtonElement = HTMLButtonElementStub;
  globalThis.window = { innerWidth: 1000, innerHeight: 700, matchMedia: query => ({ matches: finePointer && query.includes("min-width: 761px") && query.includes("hover: hover") && query.includes("pointer: fine") }), addEventListener: listen, removeEventListener: unlisten };
  globalThis.document = { activeElement: null, addEventListener: listen, removeEventListener: unlisten };
  return {
    NodeStub, HTMLElementStub, HTMLButtonElementStub, listeners,
    setFinePointer(value) { finePointer = value; },
    restore() { for (const [name, value] of Object.entries(original)) { if (value === undefined) delete globalThis[name]; else globalThis[name] = value; } },
  };
}

function setBatchRows(view, rows) {
  view.funds.value = { collections: { statements: { items: rows } } };
}

async function mountBatchTemplate(view, dom, { controls = false } = {}) {
  const source = readFileSync(new URL("../src/views/FundsView.vue", import.meta.url), "utf8");
  const popover = source.slice(source.lastIndexOf('  <Teleport to="body">'), source.lastIndexOf("</template>"));
  const button = source.match(/<button v-if="item\.batch_payment\?\.items\.length"[\s\S]*?<\/button>/)[0];
  const pagination = source.match(/<DashboardPagination automatic[^\n]+paginationScope\('bank'\)[^\n]+\/>/)[0];
  const template = controls
    ? `<button id="before-batch">Before</button><div v-for="item in bankRows" :key="item.id">${button}</div><button id="after-batch">After</button>${pagination}${popover}`
    : `<span>{{ funds.collections.statements.items.length }}</span>${popover}`;
  const render = new Function("Vue", compile(template, { mode: "function", prefixIdentifiers: true }).code)(Vue);
  const { descriptor } = parse(readFileSync(new URL("../src/components/DashboardPagination.vue", import.meta.url), "utf8"));
  let paginationSource = ts.transpileModule(compileScript(descriptor, { id: "funds-batch-pagination", inlineTemplate: true }).content,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
  const key = `fundsBatchRender${++sequence}`;
  globalThis[key] = Vue;
  paginationSource = paginationSource.replace(/import \{([^}]+)\} from ["']vue["'];?/g,
    (_match, names) => `const {${names.replace(/\bas\b/g, ":")}} = globalThis.${key};`);
  const { default: paginationComponent } = await import(`data:text/javascript;base64,${Buffer.from(paginationSource).toString("base64")}`);
  delete globalThis[key];
  const makeNode = (tag, text = "") => {
    const node = new (tag === "button" ? dom.HTMLButtonElementStub : dom.HTMLElementStub)();
    Object.assign(node, { tag, text, children: [], props: {}, parent: null });
    return Vue.markRaw(node);
  };
  const root = makeNode("root"), body = makeNode("body");
  const renderer = Vue.createRenderer({
    createElement: tag => makeNode(tag), createText: text => makeNode("#text", text), createComment: () => makeNode("#comment"),
    insert(node, target, anchor) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
      const index = anchor ? target.children.indexOf(anchor) : -1;
      target.children.splice(index < 0 ? target.children.length : index, 0, node); node.parent = target;
    },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
    setText(node, text) { node.text = text; }, setElementText(node, text) { node.text = text; node.children = []; },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    querySelector: selector => selector === "body" ? body : null,
    patchProp(node, key, _previous, value) { node.props[key] = value; if (key === "onFocus") node.onFocus = () => value({ currentTarget: node }); },
  });
  const app = renderer.createApp({ setup: () => ({ ...view }), render });
  app.component("DashboardPagination", paginationComponent);
  app.mount(root);
  const nodes = () => {
    const result = [], visit = node => { result.push(node); node.children.forEach(visit); };
    visit(root); visit(body); return result;
  };
  document.querySelectorAll = () => nodes().filter(node => node.tag === "button" && !node.props.disabled);
  return { nodes, close: () => app.unmount() };
}

test("closed bank previews perform zero row identity reads through 100/200/400 reactive pages", async () => {
  for (const count of [100, 200, 400]) {
    const dom = batchDOM(), view = await rowHarness(); let mounted;
    try {
      let reads = 0;
      setBatchRows(view, []);
      const items = view.funds.value.collections.statements.items;
      mounted = await mountBatchTemplate(view, dom);
      for (let start = 0; start < count; start += 20) {
        items.push(...Array.from({ length: 20 }, (_, offset) => ({ ...batchRow(`row-${start + offset}`),
          get id() { reads += 1; return `row-${start + offset}`; } })));
        await Vue.nextTick();
        assert.equal(reads, 0, `closed preview after ${start + 20} rows`);
      }
      const last = items.at(-1), anchor = new dom.HTMLElementStub();
      await view.showBatchPreview(last, anchor, "pointer");
      assert.equal(view.previewBatchRow.value, last);
      assert.equal(view.batchPreviewPosition.value.ready, true);
      assert(reads >= count, "an open preview still locates its loaded row");
      setBatchRows(view, [batchRow("replacement")]); await Vue.nextTick();
      assert.equal(view.previewBatchRow.value, undefined);
      assert.equal(view.previewBatchId.value, "");
      assert(!mounted.nodes().some(node => node.props.id === "bank-batch-preview"));
    } finally { mounted?.close(); view.unmount(); dom.restore(); }
  }
});

test("loaded batch controls remain usable while real automatic bank pagination is pending", async () => {
  const dom = batchDOM(), view = await rowHarness({ deferRequests: true }); let mounted;
  const page = (cursor, total = 60) => ({ total_count: total, filtered_count: total, returned_count: 20, has_more: Boolean(cursor), next_cursor: cursor });
  try {
    await view.mount();
    view.selectedPeriod.value = "2026-09"; view.selectedDetailView.value = "bank";
    view.activeSection.value = "bank-details"; view.snapshotVersion.value = "v1";
    setBatchRows(view, Array.from({ length: 20 }, (_, i) => batchRow(`statement-${i}`)));
    view.funds.value.collections.statements.page = page("next-20");
    mounted = await mountBatchTemplate(view, dom, { controls: true }); await tick();
    assert.equal(view.calls.length, 1); assert.equal(view.calls[0].args[2].cursor, "next-20");
    assert.equal(view.pageStates.value.bank.loading, true);
    const anchor = mounted.nodes().find(node => node.props.class === "bank-batch-trigger");
    const event = () => ({ target: anchor, currentTarget: anchor, stopPropagation() {} });
    assert.equal(anchor.props.disabled, false);
    await anchor.props.onMouseenter(event());
    assert.equal(view.previewBatchId.value, "statement-0");
    for (const listener of dom.listeners.get("keydown")) listener({ key: "Escape", preventDefault() {} }); await tick();
    assert.equal(document.activeElement, anchor); assert.equal(view.previewBatchId.value, "");
    await anchor.props.onClick(event()); await tick();
    assert.equal(view.expandedBatchId.value, "statement-0");
    await anchor.props.onClick(event()); await tick();
    assert.equal(view.expandedBatchId.value, "");
    anchor.focus(); await tick();
    assert.equal(view.previewBatchId.value, "statement-0");
    anchor.props.onKeydown({ key: "Tab", shiftKey: false, preventDefault() {} });
    assert.equal(document.activeElement, view.batchPreviewList.value);
    const footer = mounted.nodes().find(node => node.tag === "button" && node.parent.props.class === "bank-batch-preview-footer");
    view.batchPreviewPanel.value.props.onKeydown({ key: "Tab", shiftKey: false, target: footer, preventDefault() {} });
    const buttons = mounted.nodes().filter(node => node.props.class === "bank-batch-trigger");
    assert.equal(document.activeElement, buttons[1]);
    assert.equal(view.calls.length, 1); assert.equal(view.calls[0].args[1].aborted, false);
    view.calls[0].resolve({ snapshot_version: "v1", data: { collections: { statements: {
      items: Array.from({ length: 20 }, (_, i) => batchRow(`statement-${20 + i}`)), page: page("next-40"),
    } } } }); await tick(); await tick();
    assert.equal(view.bankRows.value.length, 40);
    assert.equal(view.calls.length, 2); assert.equal(view.calls[1].args[2].cursor, "next-40");
    assert.equal(view.calls[1].args[1].aborted, false, "batch interaction must not pause continuation");
    view.loading.value = true; await tick(); assert.equal(anchor.props.disabled, true);
  } finally { mounted?.close(); view.unmount(); dom.restore(); }
});

test("bank batches open only for populated rows and remain independent by statement id", async () => {
  const dom = batchDOM(), view = await rowHarness();
  try {
    const first = batchRow("statement-a"), second = batchRow("statement-b"), ordinary = batchRow("ordinary", false);
    setBatchRows(view, [first, second, ordinary]);
    const anchor = new dom.HTMLElementStub();
    view.toggleBatchDetails(ordinary, anchor);
    view.toggleBatchDetails({ id: "empty", batch_payment: { items: [] } }, anchor);
    assert.equal(view.expandedBatchId.value, "");
    view.toggleBatchDetails(first, anchor);
    assert.equal(view.expandedBatchId.value, first.id);
    await view.showBatchPreview(first, anchor, "pointer");
    assert.equal(view.previewBatchId.value, "", "the expanded row does not duplicate itself as a hover preview");
    view.toggleBatchDetails(second, anchor);
    assert.equal(view.expandedBatchId.value, second.id, "a second statement in the same batch owns its own expansion");
    view.toggleBatchDetails(second, anchor);
    assert.equal(view.expandedBatchId.value, "");
    view.toggleBatchDetails(first, anchor);
    assert.equal(view.expandedBatchId.value, first.id);
    setBatchRows(view, [second, ordinary]);
    assert.equal(view.expandedBatchId.value, "", "replacement rows remove an expansion whose statement disappeared");
  } finally { view.unmount(); dom.restore(); }
});

test("batch preview uses current loaded row, fine pointer eligibility and newest positioning", async () => {
  const dom = batchDOM(), view = await rowHarness();
  try {
    const first = batchRow("statement-a"), second = batchRow("statement-b");
    setBatchRows(view, [first, second]);
    const anchorA = new dom.HTMLElementStub(), anchorB = new dom.HTMLElementStub({ left: 750, right: 850, top: 220, bottom: 250, width: 100, height: 30 });
    const panel = new dom.HTMLElementStub({ left: 0, right: 180, top: 0, bottom: 100, width: 180, height: 100 });
    view.batchPreviewPanel.value = panel;
    dom.setFinePointer(false);
    await view.showBatchPreview(first, anchorA, "pointer");
    assert.equal(view.previewBatchId.value, "");
    dom.setFinePointer(true);
    await view.showBatchPreview(batchRow("ordinary", false), anchorA, "pointer");
    await view.showBatchPreview(first, {}, "pointer");
    assert.equal(view.previewBatchId.value, "");
    const stale = view.showBatchPreview(first, anchorA, "pointer");
    const latest = view.showBatchPreview(second, anchorB, "pointer");
    await Promise.all([stale, latest]);
    assert.equal(view.previewBatchId.value, second.id);
    assert.equal(view.previewBatchRow.value?.id, second.id);
    assert.equal(view.batchPreviewPosition.value.ready, true);
    assert.equal(view.batchPreviewPosition.value.side, "left");
    assert.equal(view.batchPreviewPosition.value.left, 560);
    setBatchRows(view, [first]);
    assert.equal(view.previewBatchId.value, "", "an unloaded statement cannot leave an orphan preview");
    assert.equal(view.previewBatchRow.value, undefined);
    const disconnected = view.showBatchPreview(first, anchorA, "pointer");
    anchorA.isConnected = false;
    await disconnected;
    assert.equal(view.batchPreviewPosition.value.ready, false, "detached triggers cannot position a late preview");
  } finally { view.unmount(); dom.restore(); }
});

test("batch scope changes, refresh invalidation and viewport movement clear both states", async () => {
  const dom = batchDOM();
  try {
    const row = batchRow("statement-a"), other = batchRow("statement-b"), anchor = new dom.HTMLElementStub();
    for (const change of [
      view => { view.route.query.company_id = "company-b"; },
      view => { view.route.query.period = "2026-10"; },
      view => { view.selectedPeriod.value = "2026-10"; },
      view => { view.selectedAccount.value = "bank:other"; },
      view => { view.selectedBankAccount.value = "other"; },
      view => { view.snapshotVersion.value = "v2"; },
      view => { view.selectedDetailView.value = "bank"; },
      view => { view.activeSection.value = "bank-details"; },
    ]) {
      const view = await rowHarness();
      try {
        setBatchRows(view, [row, other]);
        view.toggleBatchDetails(row, anchor);
        await view.showBatchPreview(other, anchor);
        assert.equal(view.expandedBatchId.value, row.id);
        assert.equal(view.previewBatchId.value, other.id);
        change(view);
        assert.equal(view.expandedBatchId.value, "");
        assert.equal(view.previewBatchId.value, "");
      } finally { view.unmount(); }
    }
    const view = await rowHarness();
    try {
      setBatchRows(view, [row]);
      const closedPosition = view.batchPreviewPosition.value;
      view.handleBatchViewportChange({ type: "scroll", target: new dom.NodeStub() });
      view.handleBatchViewportChange({ type: "resize", target: window });
      assert.equal(view.batchPreviewPosition.value, closedPosition, "viewport events without a preview do not write reactive position");
      await view.showBatchPreview(row, anchor);
      const panel = new dom.HTMLElementStub(); view.batchPreviewPanel.value = panel;
      view.handleBatchViewportChange({ type: "scroll", target: view.batchPreviewPanel.value });
      assert.equal(view.previewBatchId.value, row.id, "scrolling inside the preview keeps it open");
      view.handleBatchViewportChange({ type: "scroll", target: new dom.NodeStub() });
      assert.equal(view.previewBatchId.value, "");
      await view.showBatchPreview(row, anchor);
      view.handleBatchViewportChange({ type: "resize", target: window });
      assert.equal(view.previewBatchId.value, "");
      view.toggleBatchDetails(row, anchor);
      view.invalidateRequests();
      assert.equal(view.expandedBatchId.value, "");
      setBatchRows(view, [row, other]);
      view.toggleBatchDetails(row, anchor);
      await view.showBatchPreview(other, anchor);
      await view.loadFunds("2026-09");
      assert.equal(view.expandedBatchId.value, "");
      assert.equal(view.previewBatchId.value, "");
    } finally { view.unmount(); }
  } finally { dom.restore(); }
});

test("batch keyboard Tab visits preview then resumes after the original trigger", async () => {
  const dom = batchDOM(), view = await rowHarness();
  try {
    const row = batchRow("statement-a"), anchor = new dom.HTMLButtonElementStub(), next = new dom.HTMLButtonElementStub();
    const list = new dom.HTMLElementStub(), footer = new dom.HTMLButtonElementStub();
    setBatchRows(view, [row]);
    view.batchPreviewList.value = list;
    await view.showBatchPreview(row, anchor, "focus");
    let prevented = 0;
    const tab = target => ({ key: "Tab", shiftKey: false, target, preventDefault: () => { prevented += 1; } });
    view.handleBatchTriggerKey(tab(anchor));
    assert.equal(list.focusCount, 1);
    globalThis.document.querySelectorAll = () => [anchor, next];
    view.handleBatchPreviewKey(tab(footer));
    assert.equal(next.focusCount, 1);
    assert.equal(view.previewBatchId.value, "");
    assert.equal(prevented, 2);
  } finally { view.unmount(); dom.restore(); }
});

test("batch leave grace, Escape focus restoration and unmount cancel delayed closing", async () => {
  const dom = batchDOM(), view = await rowHarness();
  let unmounted = false;
  try {
    const row = batchRow("statement-a"), anchor = new dom.HTMLElementStub(), panel = new dom.HTMLElementStub();
    await view.mount();
    assert.equal(dom.listeners.get("scroll")?.size, 1);
    assert.equal(dom.listeners.get("resize")?.size, 1);
    assert.equal(dom.listeners.get("keydown")?.size, 1);
    setBatchRows(view, [row]); view.batchPreviewPanel.value = panel;
    anchor.onFocus = () => { void view.showBatchPreview(row, anchor, "focus"); };
    await view.showBatchPreview(row, anchor, "pointer");
    view.leaveBatchTrigger(); view.keepBatchPreview();
    await new Promise(resolve => setTimeout(resolve, 170));
    assert.equal(view.previewBatchId.value, row.id, "entering the panel cancels the trigger leave timeout");
    view.leaveBatchPreview();
    await new Promise(resolve => setTimeout(resolve, 170));
    assert.equal(view.previewBatchId.value, "", "leaving both surfaces closes after the grace period");
    await view.showBatchPreview(row, anchor, "focus");
    let prevented = 0;
    view.handleBatchEscape({ key: "Escape", target: panel, preventDefault: () => { prevented += 1; } });
    await tick();
    assert.equal(prevented, 1);
    assert.equal(anchor.focusCount, 1);
    assert.equal(view.previewBatchId.value, "", "focus restoration must not reopen the dismissed preview");
    await view.showBatchPreview(row, anchor, "pointer");
    view.leaveBatchTrigger();
    const originalClearTimeout = globalThis.clearTimeout;
    let clearedTimeouts = 0;
    globalThis.clearTimeout = timer => { clearedTimeouts += 1; originalClearTimeout(timer); };
    try { view.unmount(); }
    finally { globalThis.clearTimeout = originalClearTimeout; }
    unmounted = true;
    assert.ok(clearedTimeouts > 0, "unmount cancels a pending close timeout");
    assert.equal(dom.listeners.get("scroll")?.size, 0);
    assert.equal(dom.listeners.get("resize")?.size, 0);
    assert.equal(dom.listeners.get("keydown")?.size, 0);
    await new Promise(resolve => setTimeout(resolve, 170));
    assert.equal(view.previewBatchId.value, "", "unmount clears the preview and its pending close");
  } finally { if (!unmounted) view.unmount(); dom.restore(); }
});
