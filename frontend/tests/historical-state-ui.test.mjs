import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("owner history preserves business uncertainty and batch amounts without source diagnostics", async (t) => {
  const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  const fixtures = { context: samples.company_with_period.response, brief: samples.brief.response, funds: samples.cash_funds.response, employees: samples.employees.response, assets: samples.assets.response, reports: samples.quarterly_report.response };
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${fixtures.context.current_company.company_id}` } };
  globalThis.historicalUi = structuredClone(fixtures);
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-historical-ui-responses", enforce: "pre", transform(code, id) {
      const match = /\/src\/views\/(Brief|Funds|Employees|Assets|Reports)View\.vue$/.exec(id.replaceAll("\\", "/"));
      if (!match) return;
      const key = match[1].toLowerCase(), refName = key === "funds" ? "funds" : key === "reports" ? "report" : "response";
      code = code.replace(new RegExp(`const ${refName} = (?:ref|shallowRef)<[^;\\n]+>\\(null\\)`), `const ${refName} = ref(globalThis.historicalUi.${key}${key === "funds" ? ".data" : ""})`);
      if (key === "funds") code = code.replace("const initializing = ref(true)", "const initializing = ref(false)")
        .replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-01")')
        .replace('const expandedBatchId = ref("")', 'const expandedBatchId = ref(globalThis.historicalUi.funds.data.collections.statements.items[0]?.id || "")')
        .replaceAll("{ immediate: true }", "{ immediate: false }");
      return code;
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  async function render(name, extraQuery = "") {
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: "/", name: "brief", component: {} }, ...["funds", "employees", "assets", "reports"].map(name => ({ path: `/${name}`, name, component: {} })),
    ] });
    await router.push(`/${name === "Brief" ? "" : name.toLowerCase()}?company_id=${fixtures.context.current_company.company_id}&period=2026-01&quarter=2026-Q1${extraQuery}`);
    const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
    const app = createSSRApp(component); app.use(router);
    return renderToString(app);
  }
  const visible = html => html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
  try {
    globalThis.fetch = async () => new Response(JSON.stringify(fixtures.context));
    const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
    await useDashboardContext().load(true);
    await t.test("AI review remains distinct from a missing bank statement", async () => {
      const data = globalThis.historicalUi.funds.data;
      Object.assign(data.bank_statement, { coverage_state: "partial", missing_account_count: 0, review_state: "pending" });
      const html = await render("Funds");
      assert.match(html, /AI 会计核对中/);
      assert.doesNotMatch(html, /source_digest_mismatch|selection_proof|查看.*来源证明|<pre/);
      data.bank_statement.missing_account_count = 1;
      assert.match(await render("Funds"), /1 个银行账户未提供本月流水/);
    });
    await t.test("bank rows stay distinct while a shared payment batch shows every recipient", async () => {
      const data = globalThis.historicalUi.funds.data;
      data.collections.statements.items = [
        { id: "bank-row-1", reference: "20260710042541917000001", date: "2026-07-10", account_id: "synthetic-bank", account_name: "兴业银行", account_code: "尾号9170", direction: "outflow", amount_fen: "3906617", signed_amount_fen: "-3906617", party: "工资批量代发 · 2 人", memo: "代发工资", state: "matched", batch_payment: { bank_row_count: 2, total_fen: "6290756", items: [{ party: "张三", amount_fen: "2223667" }, { party: "李四", amount_fen: "4067089" }] } },
        { id: "bank-row-2", reference: "20260710042542175500001", date: "2026-07-10", account_id: "synthetic-bank", account_name: "兴业银行", account_code: "尾号9170", direction: "outflow", amount_fen: "2384139", signed_amount_fen: "-2384139", party: "工资批量代发 · 2 人", memo: "代发工资", state: "matched", batch_payment: { bank_row_count: 2, total_fen: "6290756", items: [{ party: "张三", amount_fen: "2223667" }, { party: "李四", amount_fen: "4067089" }] } },
      ];
      data.collections.statements.page = { total_count: 2, filtered_count: 2, returned_count: 2, has_more: false, next_cursor: null };
      const html = visible(await render("Funds", "&funds_view=bank"));
      assert.equal((html.match(/class="bank-activity-item"/g) ?? []).length, 2);
      assert.equal((html.match(/工资批量代发 · 2 人/g) ?? []).length, 2);
      assert.doesNotMatch(html, /20260710042541917000001/);
      assert.doesNotMatch(html, /20260710042542175500001/);
      assert.match(html, /−¥39,066\.17/);
      assert.match(html, /−¥23,841\.39/);
      assert.match(html, /整批付款明细/);
      assert.match(html, /2 笔银行流水 · 批次合计 ¥62,907\.56/);
      assert.match(html, /张三.*¥22,236\.67.*李四.*¥40,670\.89/s);
      assert.match(html, /不能据此把某位收款人归到本条/);
      assert.doesNotMatch(html, /张三、李四/);
    });
    await t.test("employee uncertainty keeps unknown amounts and AI responsibility", async () => {
      globalThis.historicalUi.employees.data.employees.checking = true;
      const html = await render("Employees");
      assert.match(html, /AI 会计核对中/);
      assert.doesNotMatch(html, /calculation_id|selection_proof|查看工资确认依据|<pre/);
    });
    await t.test("company-wide unknown asset counts stay qualified on a filtered page with no unknown rows", async () => {
      const data = globalThis.historicalUi.assets.data;
      data.unestablished_count = 4;
      data.collections.assets.items = [];
      data.collections.assets.page = { total_count: 8, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null };
      assert.doesNotMatch(visible(await render("Assets", "&asset_filter=active")), /完整总计 8 项 · 筛选总计 0 项/);
      data.asset_filter = "active";
      const html = visible(await render("Assets", "&asset_filter=active"));
      assert.match(html, /全公司有 4 项资产资料尚未确认/);
      assert.match(html, /数量仅列已确认部分，不代表完整数量/);
      assert.match(html, /当前筛选只显示资料已确认的资产/);
      assert.match(html, /当前在用<\/span><strong[^>]*>已确认 /);
      assert.match(html, /已确认 .* 项资产/);
      assert.match(html, /完整总计 8 项 · 筛选总计 0 项 · 已加载 0 项/);
    });
    await t.test("reports omit repeated preparation and technical selectors", async () => {
      const html = await render("Reports");
      assert.match(html, /季度财务报表/);
      assert.doesNotMatch(html, /查看报表准备详情|采用来源|selected_fact_id|calculation_id|<pre/);
    });

  } finally {
    await server.close();
    globalThis.window = previousWindow; globalThis.fetch = previousFetch;
    delete globalThis.historicalUi;
  }
});
