import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import * as Vue from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse } from "@vue/compiler-dom";
import { createMemoryHistory, createRouter } from "vue-router";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const obligation = (changes = {}) => ({ key: "salary", name: "net", direction: "payable", category_key: "payroll_payables", source_period: "2026-09", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial", ...changes });
const voucher = (changes = {}) => ({ number: 9, voucher_version_id: "voucher-a", subject_id: "salary-business", reverses_version_id: null, date: "2026-09-30", recognition: { period: "2026-09" }, type: "工资确认", kind: "payroll", state: "已入账", group: "payroll", summary: "9月工资确认", list_summary: "9月工资", amount_fen: "900000", business_amount_fen: "900000", business_amount_label: "税前工资", asset: null, asset_members: [], lines: [], ...changes });
function scenario(items = [obligation()]) {
  const data = structuredClone(samples.business_status.response.data);
  data.identity.kind = "payroll";
  data.display_profiles = { business: { values: {} }, employees: [], counterparties: [], assets: [], fund_accounts: [] };
  data.latest_source = { deleted: false, period: "2026-10" };
  data.current_business_result = { amount_fen: "9999999", amount_label: "当前业务结果金额", posting_period: "2026-10" };
  data.settlements = { cutoff_period: "2026-09", status: "established", checking: false, obligations: items };
  data.current_followups.settlements = { ...data.settlements, cutoff_period: "2026-10", obligations: structuredClone(items) };
  data.collections.settlement_events = { items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null } };
  return data;
}

test("voucher progress adds business facts without repeating the voucher", async (t) => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-voucher-progress", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.voucherProgressScenario)");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-09");
    const { default: component } = await server.ssrLoadModule("/src/components/BusinessStatusDetails.vue");
    async function render(data = scenario(), voucherContext = voucher()) {
      globalThis.voucherProgressScenario = data;
      const app = Vue.createSSRApp(component, { subjectId: voucherContext.subject_id, period: "2026-09", presentation: "voucher", voucherContext, expanded: true, hideSummary: true });
      app.use(router);
      const html = await renderToString(app), mobileRanges = [];
      function visit(node) {
        if (node.type === 1 && node.props.some(prop => prop.type === 6 && prop.name === "class" && prop.value?.content.split(/\s+/).includes("voucher-progress-mobile"))) {
          mobileRanges.push([node.loc.start.offset, node.loc.end.offset]); return;
        }
        for (const child of node.children ?? []) visit(child);
      }
      visit(parse(html));
      return mobileRanges.sort((a, b) => b[0] - a[0]).reduce((content, [start, end]) => content.slice(0, start) + content.slice(end), html);
    }
    await t.test("salary progress distinguishes net wages, tax and social payments", async () => {
      const data = scenario([obligation(), obligation({ key: "tax", name: "tax", source_amount_fen: "12345", paid_fen: "0", remaining_fen: "12345", settlement_status: "open" }), obligation({ key: "social", name: "employer_social", source_amount_fen: "30000", paid_fen: "30000", remaining_fen: "0", settlement_status: "settled" }), obligation({ key: "housing", name: "employee_housing", source_amount_fen: "10000", paid_fen: "0", remaining_fen: "10000", settlement_status: "open" })]);
      const html = await render(data);
      assert.match(html, /还需支付/); assert.match(html, /实发工资[\s\S]*¥2,000\.00/);
      assert.match(html, /个人所得税[\s\S]*¥123\.45/); assert.match(html, /公司社保[\s\S]*已结清/);
      assert.match(html, /个人公积金/);
      assert.equal((html.match(/<thead\b/g) ?? []).length, 1);
      assert.equal((html.match(/原应付/g) ?? []).length, 1);
      assert.equal((html.match(/已付/g) ?? []).length, 1);
      assert.doesNotMatch(html, /税前工资|9月工资确认|当前业务结果金额|¥99,999\.99|业务对象：|salary-business|后续进展/);
    });
    await t.test("income and receipt vouchers share exact receivable progress", async () => {
      const data = scenario([obligation({ key: "revenue", name: "primary", direction: "receivable", category_key: "customer_receivables" })]);
      data.identity.kind = "revenue";
      for (const type of ["收入确认", "收入收款"]) {
        const html = await render(data, voucher({ type, business_amount_label: type === "收入确认" ? "含税收入" : "实际收款", business_amount_fen: "600000" }));
        assert.match(html, /原应收[\s\S]*¥8,000\.00/); assert.match(html, /已收[\s\S]*¥6,000\.00/); assert.match(html, /还需收回[\s\S]*¥2,000\.00/);
        assert.doesNotMatch(html, /还需支付|含税收入|实际收款/);
      }
    });
    await t.test("deposit and supplier advance retain refund and offset meaning", async () => {
      const deposit = await render(scenario([obligation({ name: "primary", direction: "receivable", category_key: "refundable_deposit_receivables" })]));
      assert.match(deposit, /部分收回/); assert.match(deposit, /还需收回/); assert.doesNotMatch(deposit, /还需支付/);
      const advance = await render(scenario([obligation({ name: "primary", direction: "receivable", category_key: "supplier_advances", paid_fen: "10000", other_settled_fen: "590000" })]));
      assert.match(advance, /已退回[\s\S]*¥100\.00/); assert.match(advance, /已冲抵[\s\S]*¥5,900\.00/); assert.match(advance, /尚未冲抵/);
      assert.doesNotMatch(advance, /原应付|还需支付/);
    });
    await t.test("mixed payment meanings use separate headers and retain exact unknown amounts", async () => {
      const html = await render(scenario([
        obligation(),
        obligation({ key: "advance", name: "primary", direction: "receivable", category_key: "supplier_advances", other_settled_fen: "10000" }),
        obligation({ key: "unknown", name: "primary", direction: "unknown", category_key: "unknown", remaining_fen: "9007199254740993", source_amount_fen: "9007199254740993" }),
      ]));
      const tables = html.match(/<table\b[\s\S]*?<\/table>/g) ?? [];
      assert.equal(tables.length, 3);
      assert(tables.some(table => /原应付/.test(table) && !/预付金额|款项总额/.test(table)));
      assert(tables.some(table => /预付金额/.test(table) && /已退回/.test(table) && /已冲抵/.test(table) && !/原应付/.test(table)));
      assert(tables.some(table => /款项总额/.test(table) && /AI 会计核对中/.test(table) && /¥90,071,992,547,409\.93/.test(table)));
    });
    await t.test("later progress compares obligation keys and retains historical balances", async () => {
      const data = scenario();
      assert.doesNotMatch(await render(data), /后续进展/);
      data.current_followups.settlements.obligations = [obligation({ remaining_fen: "0", paid_fen: "800000", settlement_status: "settled" }), obligation({ key: "new-tax", name: "tax", remaining_fen: "10000", source_amount_fen: "10000", paid_fen: "0", settlement_status: "open" })];
      const [historical, later] = (await render(data)).split("后续进展 ·");
      assert.match(historical, /还需支付[\s\S]*¥2,000\.00/); assert.match(later, /已结清/); assert.match(later, /后续新增款项/);
      data.current_followups.settlements.obligations = [];
      assert.match(await render(data), /最新进度待核对/);
    });
    await t.test("closed corrections and withdrawal never imply another payment", async () => {
      const data = scenario([obligation({ settlement_status: "reversed", remaining_fen: "0" })]);
      data.latest_source.deleted = true;
      let html = await render(data, voucher({ reverses_version_id: "original-voucher", state: "更正原业务" }));
      assert.match(html, /原业务已更正/); assert.match(html, /撤回/); assert.doesNotMatch(html, /重新付款|重新收款|已结清/);
      html = await render(scenario([obligation({ settlement_status: "withdrawn" })])); assert.match(html, /业务已撤回/);
    });
    await t.test("unknown amounts preserve exact nonnull values and missing progress remains concise", async () => {
      const data = scenario([obligation({ direction: "unknown", category_key: "unknown", source_amount_fen: "9007199254740993", remaining_fen: "9007199254740993", settlement_status: "settled" })]);
      const html = await render(data);
      assert.match(html, /AI 会计核对中/); assert.match(html, /¥90,071,992,547,409\.93/); assert.doesNotMatch(html, /已结清|还需支付/);
      data.collections.settlement_events = { items: [{ id: "unresolved-payment", name: "net", mode: "payment", posting_period: "2026-09", direction: 1, relation_state: "unresolved", signed_amount_fen: "9007199254740993" }], page: { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null } };
      const unresolved = (await render(data)).split("这笔业务的相关收付")[1];
      assert.match(unresolved, /AI 会计核对中/); assert.match(unresolved, /¥90,071,992,547,409\.93/);
      const missing = await render(scenario([obligation({ remaining_fen: null })])); assert.match(missing, /待核对/);
      const empty = await render(scenario([]));
      assert.doesNotMatch(empty, /款项进度|已结清|后续进展|当前业务结果金额/);
      assert.equal((empty.match(/暂无/g) ?? []).length, 1);
    });
    await t.test("purpose deduplication uses visible complete voucher texts", async () => {
      for (const purpose of ["9月工资确认", "9月工资", "工资确认", "税前工资"]) {
        const data = scenario([]); data.display_profiles.business.values = { purpose };
        assert.doesNotMatch(await render(data), /用途／备注：/);
      }
      const data = scenario([]); data.display_profiles.business.values = { purpose: "9月工资确认补充", note: "9月工资确认补充" };
      assert.equal(((await render(data)).match(/用途／备注：9月工资确认补充/g) ?? []).length, 1);
      for (const asset of [{ asset_id: "asset-a", name: "设备", code: "A" }, { asset_id: "asset-a", name: "", code: "A" }, { asset_id: "asset-a", name: "", code: "" }]) {
        data.display_profiles.business.values = { purpose: asset.name || asset.code || "资产卡片" };
        assert.doesNotMatch(await render(data, voucher({ asset })), /用途／备注：/);
      }
    });
    await t.test("assets and people deduplicate only precise identities and preserve roles", async () => {
      const profile = (entity_id, name) => ({ entity_id, values: { display_name: name } });
      const data = scenario([]);
      data.display_profiles.assets = [profile("asset-a", "设备"), profile("asset-b", "设备")];
      data.display_profiles.employees = [profile("employee-a", "张某"), profile("employee-b", "张某")];
      data.display_profiles.counterparties = [profile("party-a", "张某")];
      const context = voucher({ asset: { asset_id: "asset-a", asset_type: "fixed_asset", name: "设备", code: "A" }, lines: [{ line_number: 1, code: "2211", account: "应付职工薪酬", debit_fen: "0", credit_fen: "800000", party: "张某", parties: [{ id: "employee-a", name: "张某", amount_fen: "800000" }], party_state: "resolved", source_label: "" }] });
      const html = await render(data, context);
      assert.match(html, /相关资产：设备<\/p>/); assert.match(html, /员工：张某<\/p>/); assert.match(html, /往来方：张某<\/p>/);
      const unknownIdentity = voucher({ asset: { asset_id: "", name: "设备" }, lines: [{ party: "张某", parties: [] }] });
      const unknown = await render(data, unknownIdentity);
      assert.match(unknown, /相关资产：设备、设备/); assert.match(unknown, /员工：张某、张某/);
      data.display_profiles.counterparties = [profile("employee-a", "张某")];
      const multipleRoles = await render(data, context);
      assert.match(multipleRoles, /员工：张某、张某/); assert.match(multipleRoles, /往来方：张某/);
    });
  } finally { await server.close(); delete globalThis.voucherProgressScenario; }
});

let sequence = 0;
async function harness(cache = new Map(), voucherContext = voucher()) {
  const key = `voucherProgressHarness${++sequence}`, calls = [], cleanup = [], events = [];
  const route = Vue.reactive({ query: { company_id: "company-a" } });
  const props = Vue.reactive({ subjectId: "salary-business", period: "2026-09", snapshotVersion: "v1", settlementView: "historical", presentation: "voucher", voucherContext, detailCache: cache, expanded: false, hideSummary: true });
  globalThis[key] = { Vue, props, route, cleanup, events, appendDashboardCollection, fetch: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) };
  const source = readFileSync(new URL("../src/components/BusinessStatusDetails.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const prefix = `const environment = globalThis.${key}; const { ref, computed, watch } = environment.Vue;
    const { appendDashboardCollection } = environment; const onBeforeUnmount = callback => environment.cleanup.push(callback);
    const useRoute = () => environment.route, defineProps = () => environment.props;
    const withDefaults = (value, defaults) => { for (const [key, fallback] of Object.entries(defaults)) if (value[key] === undefined) value[key] = fallback; return value; };
    const defineEmits = () => (...args) => environment.events.push(args), fetchBusinessStatus = environment.fetch;
    const dashboardErrorMessage = error => error.message, isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';`;
  const { outputText } = ts.transpileModule(prefix + source + "\nexport { data, load, loadMore, panelOpen, paginationScope, pausePages, moreError, error, responseVersion, notice };", { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const instance = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  return { ...instance, calls, props, route, events, cache, unmount: () => { cleanup.forEach(callback => callback()); delete globalThis[key]; } };
}
const result = (id = "first", next = "next") => {
  const data = scenario();
  data.collections.settlement_events = { items: [{ id }], page: { total_count: 2, filtered_count: 2, returned_count: 1, has_more: Boolean(next), next_cursor: next } };
  return { snapshot_version: "v1", data };
};
const tick = async () => { await Promise.resolve(); await Vue.nextTick(); };

test("voucher progress requests only on expansion and rejects late first responses after collapse", async () => {
  const view = await harness();
  try {
    assert.equal(view.calls.length, 0);
    view.props.expanded = true; await tick(); assert.equal(view.calls.length, 1);
    assert.equal(view.calls[0].args[3].expected_version, "v1"); assert.equal(view.calls[0].args[3].settlement_view, "historical");
    view.props.expanded = false; await tick(); assert.equal(view.calls[0].args[2].aborted, true);
    view.calls[0].resolve(result()); await tick(); assert.equal(view.data.value, null); assert.equal(view.cache.size, 0);
    view.props.expanded = true; await tick(); assert.equal(view.calls.length, 2);
    view.calls[1].reject(new Error("首屏暂不可用")); await tick(); assert.match(view.error.value, /首屏暂不可用/); assert.equal(view.cache.size, 0);
    const retry = view.load(); view.calls[2].resolve(result("retried", null)); await retry; assert.equal(view.cache.size, 1);
  } finally { view.unmount(); }
});

test("successful cached pages survive close and unmount while cancelled pages never append", async () => {
  const cache = new Map(); let view = await harness(cache);
  try {
    view.props.expanded = true; await tick(); view.calls[0].resolve(result()); await tick();
    const items = view.data.value.collections.settlement_events.items;
    const pending = view.loadMore(); assert.equal(view.calls[1].args[3].cursor, "next");
    view.props.expanded = false; await tick(); assert.equal(view.calls[1].args[2].aborted, true);
    view.calls[1].resolve(result("late", null)); await pending; assert.deepEqual(items.map(item => item.id), ["first"]);
    view.props.expanded = true; await tick(); assert.equal(view.calls.length, 2, "same detail cache must avoid another first page");
    const second = view.loadMore(); view.calls[2].resolve(result("second", "third")); await second;
    assert.equal(view.data.value.collections.settlement_events.items, items); assert.deepEqual(items.map(item => item.id), ["first", "second"]);
    const failed = view.loadMore(); view.calls[3].reject(new Error("续页暂不可用")); await failed;
    assert.match(view.moreError.value, /续页暂不可用/); view.unmount(); assert.equal(cache.size, 1);
    view = await harness(cache, voucher({ voucher_version_id: "another-voucher-same-business" })); view.props.expanded = true; await tick(); assert.equal(view.calls.length, 0);
    assert.deepEqual(view.data.value.collections.settlement_events.items.map(item => item.id), ["first", "second"]);
    assert.match(view.moreError.value, /续页暂不可用/);
    const continued = view.loadMore(); assert.equal(view.calls[0].args[3].cursor, "third"); view.calls[0].resolve(result("third", null)); await continued;
    assert.deepEqual(view.data.value.collections.settlement_events.items.map(item => item.id), ["first", "second", "third"]);
  } finally { view.unmount(); }
});

test("changing the exact voucher cancels its first response without eager loading", async () => {
  const view = await harness();
  try {
    view.props.expanded = true; await tick();
    view.props.expanded = false; view.props.voucherContext.voucher_version_id = "voucher-b"; await tick();
    assert.equal(view.calls[0].args[2].aborted, true);
    view.calls[0].resolve(result()); await tick(); assert.equal(view.data.value, null); assert.equal(view.cache.size, 0); assert.equal(view.calls.length, 1);
  } finally { view.unmount(); }
});

test("unmount cancels a pending page and keeps only previously accepted cache data", async () => {
  const cache = new Map(), view = await harness(cache);
  try {
    view.props.expanded = true; await tick(); view.calls[0].resolve(result()); await tick();
    const pending = view.loadMore(); view.unmount(); assert.equal(view.calls[1].args[2].aborted, true); assert.equal(cache.size, 1);
    view.calls[1].resolve(result("late-after-unmount", null)); await pending;
    const reopened = await harness(cache);
    try {
      reopened.props.expanded = true; await tick(); assert.equal(reopened.calls.length, 0);
      assert.deepEqual(reopened.data.value.collections.settlement_events.items.map(item => item.id), ["first"]);
      assert.equal(reopened.data.value.collections.settlement_events.page.next_cursor, "next");
    } finally { reopened.unmount(); }
  } finally { view.unmount(); }
});

test("company, month, snapshot, subject and settlement view invalidate cached selection without fetching", async () => {
  for (const field of ["company", "period", "snapshotVersion", "subjectId", "settlementView"]) {
    const view = await harness();
    try {
      view.props.expanded = true; await tick(); view.calls[0].resolve(result()); await tick();
      view.props.expanded = false; await tick();
      if (field === "company") view.route.query.company_id = "company-b";
      else if (field === "settlementView") view.props.settlementView = "current";
      else view.props[field] += "-changed";
      await tick(); assert.equal(view.data.value, null, field); assert.equal(view.calls.length, 1, field);
      view.props.expanded = true; await tick(); assert.equal(view.calls.length, 2, field);
      view.calls[1].resolve(result("fresh", null)); await tick();
    } finally { view.unmount(); }
  }
});

test("snapshot rejection clears voucher progress and its cache", async () => {
  const view = await harness();
  try {
    view.props.expanded = true; await tick(); view.calls[0].resolve(result()); await tick();
    const pending = view.loadMore(); view.calls[1].reject(Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" })); await pending;
    assert.equal(view.data.value, null); assert.equal(view.cache.size, 0); assert.equal(view.responseVersion.value, "");
    assert.deepEqual(view.events, [["changed"]]); assert.match(view.notice.value, /业务资料已变化/);
  } finally { view.unmount(); }
});

test("the main row binds one interactive progress popover with a scope-local cache", () => {
  const source = readFileSync(new URL("../src/components/brief/BriefActivityWorkbench.vue", import.meta.url), "utf8");
  const inlineDetail = source.match(/<section v-if="selectedVoucher === voucher.voucher_version_id"[\s\S]*?<\/section>/)?.[0];
  assert.ok(inlineDetail);
  assert.doesNotMatch(inlineDetail, /BusinessStatusDetails|voucher-detail-summary|会计分录/);
  assert.match(source, /class="voucher-progress-button"/);
  assert.match(source, /v-if="voucher.subject_id && voucher.has_business_progress"/);
  assert.match(source, /class="voucher-progress-empty"[^>]*>—<\/span>/);
  assert.match(source, /id="voucher-progress-popover"/);
  assert.match(source, /role="dialog"/);
  assert.match(source, /<Teleport to="body">/);
  const progressComponents = (source.match(/<BusinessStatusDetails\b[^>]*>/g) || []).filter(tag => tag.includes('presentation="voucher"'));
  assert.equal(progressComponents.length, 1);
  assert.match(source, /:detail-cache="voucherProgressCache"/);
  assert.match(source, /:expanded="true"/);
  assert.match(progressComponents[0], /(?:\bhide-summary(?:\s|\/>))|:hide-summary="true"/);
  assert.doesNotMatch(source, /fetchBusinessStatus/);
  assert.match(source, /watch\(\(\) => \[route.query.company_id, props.period, props.snapshotVersion\], \(\) => \{\s*?voucherProgressCache.clear\(\)/);
  const preview = source.match(/id="activity-voucher-preview"[\s\S]*?点击打开凭证详情/)?.[0];
  assert.ok(preview); assert.doesNotMatch(preview, /BusinessStatusDetails|业务进展/);
});

test("business progress, business voucher and bank batch previews share one frame and directional arrow", () => {
  const brief = readFileSync(new URL("../src/components/brief/BriefActivityWorkbench.vue", import.meta.url), "utf8");
  const funds = readFileSync(new URL("../src/views/FundsView.vue", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(brief, /class="event-voucher-preview dashboard-hover-preview"/);
  assert.match(brief, /class="voucher-progress-popover dashboard-hover-preview"/);
  assert.match(funds, /class="bank-batch-preview dashboard-hover-preview"/);
  assert.match(css, /\.dashboard-hover-preview\s*\{/);
  assert.match(css, /\.dashboard-hover-preview\[data-side="left"\]::after\s*\{[^}]*right:\s*-6px/);
});
