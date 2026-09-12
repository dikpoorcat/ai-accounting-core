import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("historical UI separates source uncertainty, missing materials, identity and confirmed state counts", async (t) => {
  const fixtures = JSON.parse(readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8"));
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=co" } };
  globalThis.historicalUi = structuredClone(fixtures);
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-historical-ui-responses", enforce: "pre", transform(code, id) {
      const match = /\/src\/views\/(Brief|Funds|Employees|Assets|Reports)View\.vue$/.exec(id.replaceAll("\\", "/"));
      if (!match) return;
      const key = match[1].toLowerCase(), refName = key === "funds" ? "funds" : key === "reports" ? "report" : "response";
      code = code.replace(new RegExp(`const ${refName} = ref<[^;\\n]+>\\(null\\)`), `const ${refName} = ref(globalThis.historicalUi.${key}${key === "funds" ? ".data" : ""})`);
      if (key === "funds") code = code.replace("const initializing = ref(true)", "const initializing = ref(false)")
        .replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-11")').replaceAll("{ immediate: true }", "{ immediate: false }");
      return code;
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  async function render(name, extraQuery = "") {
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: "/", name: "brief", component: {} }, ...["funds", "employees", "assets", "reports"].map(name => ({ path: `/${name}`, name, component: {} })),
    ] });
    await router.push(`/${name === "Brief" ? "" : name.toLowerCase()}?company_id=co&period=2026-11&quarter=2026-Q4${extraQuery}`);
    const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
    const app = createSSRApp(component); app.use(router);
    return renderToString(app);
  }
  const visible = html => html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
  try {
    globalThis.fetch = async () => new Response(JSON.stringify(fixtures.context));
    const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
    await useDashboardContext().load(true);
    await t.test("partial coverage does not imply missing materials or changed matching facts", async () => {
      const data = globalThis.historicalUi.brief.data;
      Object.assign(data.cash, { coverage_state: "partial", missing_account_count: 0, needs_review_count: 2, unmatched_count: 1 });
      data.unmatched_bank_activity.count = 3;
      const html = visible(await render("Brief"));
      assert.match(html, /银行流水覆盖尚不能完整确认/);
      assert.match(html, /3 笔匹配状态待核对/);
      assert.match(html, /1 笔流水待识别/);
      assert.match(html, /2 笔流水匹配需复核/);
      assert.doesNotMatch(html, /原匹配依据已发生变化|银行流水资料尚不完整|3 笔流水待处理|仅列已提供部分/);
      data.cash.missing_account_count = 1;
      assert.match(visible(await render("Brief")), /1 个银行账户尚未提供本月流水/);
    });
    await t.test("bank proof is Chinese at first level and exact references remain expandable", async () => {
      const data = globalThis.historicalUi.funds.data;
      const check = { state: "confirmed", message: "流水已由封存对账精确采用为来源。", statement_calculation_id: "exact-statement", reconciliation_calculation_id: "exact-reconciliation", statement_fact_id: "exact-statement-fact", reconciliation_fact_id: "exact-reconciliation-fact", selection_source: "close_manifest", selection_proof: { manifest: "exact-manifest" }, proof_method: "reconciliation_dependency" };
      Object.assign(data.bank_statement, { coverage_state: "partial", missing_account_count: 0, needs_review_count: 1, unmatched_count: 0 });
      data.accounts[0].reconciliation.source_check = check;
      data.bank_statement.rows = [
        { id: "synthetic-confirmed", date: "2026-11-01", account_id: "synthetic-bank", account_name: "测试账户", account_code: "测试", direction: "inflow", amount_fen: "12345", signed_amount_fen: "12345", party: "测试来源", memo: "测试流水", state: "matched", source_check: check },
        { id: "synthetic-unknown", date: "2026-11-01", account_id: "synthetic-bank", account_name: "测试账户", account_code: "测试", direction: "inflow", amount_fen: "100", signed_amount_fen: "100", party: "另一来源", memo: "另一流水", state: "needs_review", source_check: { ...check, state: "unestablished", message: "该来源的历史采用尚不能确认。", proof_method: null } },
      ];
      data.fact_issues = [{ reason: "independent_adoption_not_proven", candidates: [{ calculation_id: "preserved-candidate" }] }];
      const html = await render("Funds", "&funds_view=bank"), text = visible(html);
      assert.match(text, /流水已由封存对账精确采用为来源/);
      assert.match(text, /该来源的历史采用尚不能确认/);
      assert.match(text, /查看账户来源引用与证明/); assert.match(text, /查看流水来源引用与证明/);
      assert.match(text, /尚不能证明独立封存采用/);
      assert.doesNotMatch(text, /相关金额暂无法完整确定|exact-statement|exact-manifest|reconciliation_dependency/);
      for (const id of ["exact-statement", "exact-reconciliation", "exact-manifest", "preserved-candidate"]) assert.match(html, new RegExp(id));
    });
    await t.test("employee differing source IDs do not invent temporal changes", async () => {
      const employee = globalThis.historicalUi.employees.data.employees.items[0];
      assert.ok(employee);
      const source = { source_id: "synthetic-wage", kind: "payroll", period: "2026-11", label: "测试工资来源", obligations: [], movements: [], declarations: [] };
      employee.payroll_sources = [source];
      source.disbursements = [{ calculation_id: "different-basis", recording_period: "2026-11", needs_review: true, matches_displayed_wage: false, target_net_fen: "100", held_fen: "0" }];
      const html = visible(await render("Employees"));
      assert.match(html, /方案依据需复核/);
      assert.match(html, /方案采用的工资来源与本页展示来源不同/);
      assert.doesNotMatch(html, /后来更新的工资|依据变化，需复核/);
      assert.match(html, /代发方案表示拟发金额，实际付款以上方记录为准/);
    });
    await t.test("company-wide unknown asset counts stay qualified on a filtered page with no unknown rows", async () => {
      const data = globalThis.historicalUi.assets.data;
      data.unestablished_count = 4;
      data.collections.assets.items = [];
      data.collections.assets.page = { total_count: 8, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null };
      const html = visible(await render("Assets", "&asset_filter=active"));
      assert.match(html, /全公司有 4 项资产来源的冻结采用尚未建立/);
      assert.match(html, /数量仅列已确认部分，不代表完整数量/);
      assert.match(html, /状态筛选仅列已确认匹配项/);
      assert.match(html, /当前在用<\/span><strong[^>]*>已确认 /);
      assert.match(html, /已识别 .* 项卡片身份/);
      assert.match(html, /当前筛选共 0 项，已加载 0 项/);
    });
    await t.test("monthly preparation counts hints and task results without summing them as work", async () => {
      const prep = globalThis.historicalUi.reports.period_preparations[0];
      prep.current_followups.file_jobs.issue_count = 1;
      prep.current_followups.materials.issues = [{ message: "来源采用尚不能确认" }];
      const html = visible(await render("Reports"));
      assert.match(html, /1 项文件任务结果或引用依据待核对/);
      assert.match(html, /1 条核对提示/);
      assert.match(html, /不合计为待办总数/);
      assert.match(html, /来源采用尚不能确认/);
    });
  } finally {
    await server.close();
    globalThis.window = previousWindow; globalThis.fetch = previousFetch;
    delete globalThis.historicalUi;
  }
});
