import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import * as Vue from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";
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
      for (const display_summary of ["9月工资付款", "工资付款", "张某"]) {
        assert.doesNotMatch(await render(scenario([]), movement({ display_summary })), /事项说明/);
      }
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

async function rowHarness() {
  const key = `fundsRowsHarness${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-09" }, hash: "" });
  globalThis[key] = { Vue, route, appendDashboardCollection };
  const source = readFileSync(new URL("../src/views/FundsView.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const money = readFileSync(new URL("../src/utils/money.ts", import.meta.url), "utf8").replace(/export /g, "");
  const prefix = `const environment = globalThis.${key}; const { computed, nextTick, ref, watch } = environment.Vue;
    const { appendDashboardCollection } = environment; const onMounted = () => {}, onBeforeUnmount = () => {};
    const useRoute = () => environment.route, useRouter = () => ({ replace: async () => {}, push: async () => {} });
    const useDashboardContext = () => ({ context: ref(null), loading: ref(false), error: ref(''), load: async () => {}, refresh: async () => {} });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const fetchFundsDashboard = async () => { throw new Error('unexpected fetch'); };
    const dashboardErrorMessage = String, isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fundAccountDisplayLabel = String, fundAccountDisplayName = String, fundAccountLabel = () => null, rememberFundAccounts = () => {};`;
  const { outputText } = ts.transpileModule(prefix + money + source + "\nexport { toggleMovement, handleMovementKey, expandedMovementId, selectedPeriod, selectedAccount, selectedDetailView, snapshotVersion, movementAmount, funds };", { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const instance = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  return { ...instance, route };
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
    assert.match(source, /:key="item.id" class="book-activity-row"/);
    assert.match(source, /@click="toggleMovement\(item, \$event\)" @keydown="handleMovementKey\(item, \$event\)"/);
    assert.match(source, /presentation="funds" :funds-context="item" :expanded="expandedMovementId === item.id" hide-summary/);
    assert.match(source, /item\.correction \? formatFen\(item\.signed_amount_fen\) : movementAmount\(item\.direction, item\.amount_fen\)/);
  } finally { globalThis.Element = previousElement; }
});
