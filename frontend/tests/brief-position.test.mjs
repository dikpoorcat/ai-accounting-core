import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const funds = { total_fen: "12345", bank_fen: "10000", cash_fen: "2000", payment_platform_fen: "345", inflow_fen: "10000", outflow_fen: "5000", net_change_fen: "5000", internal_transfer_fen: "99999" };
const position = { assets_fen: "24345", liabilities_fen: "2000", capital_fen: "20000", equity_fen: "22345", bank_fen: "10000", fixed_asset_cost_fen: "12000", accumulated_depreciation_fen: "2000", fixed_asset_net_fen: "10000", intangible_asset_cost_fen: "3000", accumulated_amortization_fen: "1000", intangible_asset_net_fen: "2000", other_assets_fen: "2345", cumulative_result_fen: "2345", complete: true, equation_valid: true, issues: [], bank_calculation: { opening_fen: "5000", inflow_fen: "10000", outflow_fen: "5000" }, liability_calculation: { current_fen: "1500", non_current_fen: "500" } };
const workforce = { has_activity: true, total_fen: "16000", capitalized_labor_fen: "3000", employee: { has_activity: true, breakdown_available: true, reason: null, total_fen: "14000", controlled_total_fen: "13000", settlement_adjustment_fen: "1000", gross_salary_fen: "10000", annual_bonus_fen: "1000", employer_social_insurance_fen: "1500", employer_housing_fund_fen: "500", employee_social_insurance_fen: "1000", employee_housing_fund_fen: "500" }, personal_labor: { has_activity: true, breakdown_available: true, reason: null, total_fen: "2000", gross_remuneration_fen: "2000", withholding_note: "代扣个税包含在报酬毛额中，不重复计入公司成本。" } };

async function withRenderer(run) {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", name: "brief", component: {} }, { path: "/funds", name: "funds", component: {} }] });
    await router.push("/?company_id=company-a&period=2026-01");
    const render = async (name, props) => { const { default: component } = await server.ssrLoadModule(`/src/components/brief/${name}.vue`); const app = createSSRApp(component, props); app.use(router); return renderToString(app); };
    await run(render);
  } finally { await server.close(); }
}

test("brief restores v2.1 financial cards, bars and each amount calculation", async () => withRenderer(async render => {
  const html = await render("BriefFinancialOverview", { funds, position });
  for (const label of ["本月公司收付款", "月末资产与负债", "对外收款", "对外付款", "平衡关系", "所有者权益", "资本及公积", "未分配利润", "期初余额", "累计折旧", "累计摊销", "其余资产", "非流动负债"]) assert.ok(html.includes(label), label);
  for (const id of ["bank-asset-tooltip", "fixed-asset-tooltip", "intangible-asset-tooltip", "other-assets-tooltip", "liability-tooltip"]) assert.match(html, new RegExp(`aria-describedby="${id}"`));
  assert.match(html, /class="track"/);
  assert.match(html, /¥243\.45/);
  assert.doesNotMatch(html, /匹配状态|待识别|来源引用|原始流水|核算依据/);
}));

test("unknown financial amounts remain unknown and do not create bars or zero calculations", async () => withRenderer(async render => {
  const unknown = { ...position, bank_fen: null, assets_fen: null, equation_valid: null, complete: false };
  const html = await render("BriefFinancialOverview", { funds, position: unknown });
  assert.match(html, /资产负债金额尚不能完整确认/);
  assert.match(html, /暂无法确定/);
  assert.match(html, /期初及本月收支构成暂不能完整建立/);
  assert.doesNotMatch(html, /class="component-calculation"[^]*?期初余额/);
}));

test("workforce restores two cards with costs, generic settlement adjustment and separate capitalization", async () => withRenderer(async render => {
  const html = await render("BriefWorkforceSection", { workforce, periodLabel: "1月" });
  for (const label of ["本月用工成本", "正式员工", "非员工个人劳务", "全年一次性奖金", "个人承担社保医保", "工资结算调整", "资本化劳务", "不会在付款时再次计入成本"]) assert.ok(html.includes(label), label);
  assert.match(html, /¥160\.00/);
  assert.doesNotMatch(html, /以前月份|来源和清偿/);
  const unknown = structuredClone(workforce); unknown.employee.employee_social_insurance_fen = null; unknown.capitalized_labor_fen = null;
  const unknownHtml = await render("BriefWorkforceSection", { workforce: unknown, periodLabel: "1月" });
  assert.match(unknownHtml, /个人承担社保医保 暂无法确定/);
  assert.match(unknownHtml.replace(/<[^>]*>/g, ""), /资本化劳务 暂无法确定/);
}));


test("brief API requires main summaries and permits summary-free continuation pages", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  const oldWindow = globalThis.window, oldFetch = globalThis.fetch;
  try {
    const { fetchDeferredBrief } = await server.ssrLoadModule("/src/api/brief.ts");
    const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
    const value = structuredClone(samples.brief.response);
    value.schema_version = 18; value.data.financial_position = position; value.data.workforce_cost = workforce;
    const company = value.read_context.company_id, period = value.selected_period.key;
    globalThis.window = { location: { origin: "http://offline.invalid", search: `?company_id=${company}` } };
    globalThis.fetch = async () => new Response(JSON.stringify(value));
    await fetchDeferredBrief(company, period);
    for (const field of ["financial_position", "workforce_cost", "long_term_assets"]) {
      const missing = structuredClone(value); delete missing.data[field];
      globalThis.fetch = async () => new Response(JSON.stringify(missing));
      await assert.rejects(fetchDeferredBrief(company, period), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
    }
    const page = structuredClone(value); delete page.data.financial_position; delete page.data.workforce_cost; delete page.data.long_term_assets;
    globalThis.fetch = async () => new Response(JSON.stringify(page));
    await fetchDeferredBrief(company, period, undefined, value.snapshot_version, { section: "activity" });
    assert.equal('vouchers' in page.data.collections, false, 'activity groups do not preload vouchers');
    const voucherPage = structuredClone(samples.brief_vouchers.response);
    globalThis.fetch = async () => new Response(JSON.stringify(voucherPage));
    await fetchDeferredBrief(company, period, undefined, value.snapshot_version, { section: "vouchers" });
    const focused = structuredClone(value), target = voucherPage.data.collections.vouchers.items[0];
    assert.ok(target);
    focused.data.focused_voucher = target;
    globalThis.fetch = async () => new Response(JSON.stringify(focused));
    await fetchDeferredBrief(company, period, undefined, value.snapshot_version, { voucher_version_id: target.voucher_version_id });
    await assert.rejects(fetchDeferredBrief(company, period, undefined, value.snapshot_version, { voucher_version_id: "wrong-identity" }), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
    await assert.rejects(fetchDeferredBrief(company, period, undefined, value.snapshot_version, { voucher_number: Number(target.number) + 1 }), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
    focused.data.focused_voucher = null;
    await assert.rejects(fetchDeferredBrief(company, period, undefined, value.snapshot_version, { voucher_version_id: target.voucher_version_id }), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
  } finally { await server.close(); globalThis.window = oldWindow; globalThis.fetch = oldFetch; }
});
