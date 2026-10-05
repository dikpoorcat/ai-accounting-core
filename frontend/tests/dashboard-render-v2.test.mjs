import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";
import { validateDashboardAssetsResponse } from "../src/api/generated/dashboardAssets.js";
import { validateDashboardEmployeesResponse } from "../src/api/generated/dashboardEmployees.js";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const responses = {
  context: samples.company_with_period.response,
  brief: samples.brief.response,
  funds: samples.cash_funds.response,
  employees: samples.employees.response,
  assets: samples.assets.response,
  reports: samples.quarterly_report.response,
};
const companyId = responses.context.current_company.company_id;

async function withServer(run) {
  globalThis.stage7RenderResponses = responses;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${companyId}` } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-stage7-generated-response", enforce: "pre", transform(code, id) {
      const match = /\/src\/views\/(Brief|Funds|Employees|Assets|Reports)View\.vue$/.exec(id.replaceAll("\\", "/"));
      if (!match) return;
      const key = match[1].toLowerCase();
      const refName = key === "funds" ? "funds" : key === "reports" ? "report" : "response";
      const seeded = key === "funds" ? "globalThis.stage7RenderResponses.funds.data" : `globalThis.stage7RenderResponses.${key}`;
      code = code.replace(new RegExp(`const ${refName} = (?:ref|shallowRef)<[^;\\n]+>\\(null\\)`), `const ${refName} = ref(${seeded})`);
      if (key === "funds") code = code.replace("const initializing = ref(true)", "const initializing = ref(false)")
        .replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-01")').replaceAll("{ immediate: true }", "{ immediate: false }");
      return code;
    } }, vue()],
    server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try { return await run(server); }
  finally { await server.close(); delete globalThis.stage7RenderResponses; }
}

function routerFor(path) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/", name: "brief", component: {} },
    { path: "/funds", name: "funds", component: {} },
    { path: "/employees", name: "employees", component: {} },
    { path: "/assets", name: "assets", component: {} },
    { path: "/reports", name: "reports", component: {} },
  ] });
  return router.push(`${path}?company_id=${companyId}&period=2026-01&quarter=2026-Q1`).then(() => router);
}

test("current generated responses render all five owner dashboard pages", async () => withServer(async server => {
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const pages = [
    ["Brief", "/", /账务与待办|经营简报/],
    ["Funds", "/funds", /月末账面资金/],
    ["Employees", "/employees", /员工与薪酬概览/],
    ["Assets", "/assets", /长期资产概览/],
    ["Reports", "/reports", /季度财务报表/],
  ];
  for (const [name, path, expected] of pages) {
    const router = await routerFor(path);
    const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
    const app = createSSRApp(component); app.use(router);
    const html = await renderToString(app);
    assert.match(html, expected, name);
    assert.doesNotMatch(html, /input[^>]+type="password"/, name);
    if (name === "Funds") assert.doesNotMatch(html, /全部账户/);
    if (name === "Assets") assert.doesNotMatch(html, /查看整批付款情况|查看收付款事项|展开查看项目付款|查看处置事项|查看终止使用事项/);
  }
}));

test("nonempty asset and project cards show asset values without business drilldowns", async () => withServer(async server => {
  const response = structuredClone(responses.assets);
  const paymentSummary = {
    obligation_count: 1, checking: false, amount_fen: "161800", paid_fen: "10000",
    other_settled_fen: "0", remaining_fen: "151800",
  };
  const asset = {
    asset_id: "asset-active", asset_type: "fixed", code: "ZC001", name: "空调",
    category: "equipment", category_label: "设备", status: "active", status_label: "使用中",
    acquisition_date: "2026-01-01", posting_period: "2026-01", recognition_label: "2026-01",
    settlement_scope: "本验收批次结算", payment_summary: paymentSummary,
    cost_fen: "161800", accumulated_charge_fen: "0", month_charge_fen: "0", book_value_fen: "161800",
    month_acquired: true, month_activated: true,
    month_exited: false, in_service_date: "2026-01-01", disposal: null,
  };
  const disposed = { ...asset, asset_id: "asset-disposed", name: "已出售设备", status: "disposed", status_label: "已出售",
    book_value_fen: "0", month_exited: true, disposal: { date: "2026-01-20", book_value_fen: "161800",
      kind: "sale", gross_proceeds_fen: "161800", gain_fen: "0", loss_fen: "0", party: "设备买方" } };
  const { in_service_date: _date, disposal: _disposal, ...common } = asset;
  const retired = { ...common, asset_id: "asset-retired", asset_type: "intangible", name: "已退役软件",
    status: "retired", status_label: "已退役", book_value_fen: "0", month_exited: true,
    available_for_use_date: "2026-01-01", retirement: { date: "2026-01-21", book_value_fen: "161800" } };
  response.data.collections.assets.items = [asset, disposed, retired];
  response.data.collections.assets.page = { total_count: 3, filtered_count: 3, returned_count: 3, has_more: false, next_cursor: null };
  response.data.collections.projects.items = [{ source_id: "project-cost", project_id: "project", period: "2026-01",
    kind: "labor_project_cost", label: "新办公室装修", party: "装修公司", cost_fen: "300000", remaining_fen: "250000" }];
  response.data.collections.projects.page = { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null };
  assert(validateDashboardAssetsResponse(response), JSON.stringify(validateDashboardAssetsResponse.errors));
  globalThis.stage7RenderResponses = { ...responses, assets: response };
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const router = await routerFor("/assets");
  const { default: component } = await server.ssrLoadModule("/src/views/AssetsView.vue");
  const app = createSSRApp(component); app.use(router);
  const html = await renderToString(app);
  for (const name of ["空调", "已出售设备", "已退役软件", "新办公室装修"]) assert(html.includes(name));
  assert.match(html, /取得成本|累计折旧/);
  assert.match(html, /月末待付 ¥1,518\.00/);
  assert.match(html, /已投入成本 ¥3,000\.00/);
  assert.match(html, /¥2,500\.00/);
  assert.doesNotMatch(html, /查看整批付款情况|查看收付款事项|展开查看项目付款|查看处置事项|查看终止使用事项/);
}));

test("incomplete business classification labels confirmed amounts without making a boss task", async () => withServer(async server => {
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const partial = structuredClone(responses.brief);
  partial.data.position.complete = false;
  partial.data.owner_tasks = [];
  partial.data.risks = [{ key: "month_amounts", title: "本月经营金额尚需核对",
    impact: "本月收入、费用仅包含已确认分类的金额", status: "ai_reviewing" }];
  globalThis.stage7RenderResponses = { ...responses, brief: partial };
  const router = await routerFor("/");
  const { default: component } = await server.ssrLoadModule("/src/views/BriefView.vue");
  const app = createSSRApp(component); app.use(router);
  const html = await renderToString(app);
  assert.match(html, /仅已确认部分 · AI 会计核对中/);
  assert.match(html, /本月经营金额尚需核对/);
  assert.match(html, /目前没有明确需要您处理的事项/);
}));

test("rendered detail pages consume collection items and keep explicit unknown money", async () => withServer(async server => {
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  for (const [name, path, expected] of [
    ["Funds", "/funds", /资金明细/],
    ["Employees", "/employees", /员工明细/],
    ["Assets", "/assets", /资产明细/],
  ]) {
    const router = await routerFor(path);
    const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
    const app = createSSRApp(component); app.use(router);
    const html = await renderToString(app);
    assert.match(html, expected);
    assert.match(html, /暂无法确定|¥/);
  }
}));

test("employee dates render with their confirmed precision", async () => withServer(async server => {
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
  for (const name of ["employees_month_dates", "employees_mixed_dates"]) {
    const response = samples[name].response;
    globalThis.stage7RenderResponses = { ...responses, employees: response };
    const router = await routerFor("/employees");
    const app = createSSRApp(component); app.use(router);
    const html = await renderToString(app);
    const employee = response.data.collections.employees.items.find(item => item.selection_status === "established");
    assert(employee);
    for (const key of ["employment_start_date", "employment_end_date"]) {
      const date = employee[key];
      if (date === null) continue;
      assert(html.includes(date.length === 7 ? `${date}（按月确认）` : date), `${name}.${key}`);
      if (date.length === 10) assert(!html.includes(`${date}（按月确认）`), `${name}.${key}`);
    }
  }
}));

test("nonempty focused employee renders current month money without historical wage lists", async () => withServer(async server => {
  const response = samples.employees_focused.response;
  assert(validateDashboardEmployeesResponse(response), JSON.stringify(validateDashboardEmployeesResponse.errors));
  globalThis.stage7RenderResponses = { ...responses, employees: response };
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const { formatFen } = await server.ssrLoadModule("/src/utils/money.ts");
  const router = await routerFor("/employees");
  const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
  const app = createSSRApp(component); app.use(router);
  const html = await renderToString(app);
  const [employee] = response.data.collections.employees.items;
  assert(employee);
  for (const label of ["本月薪酬", "应发工资", "本月应付净薪", "本月实际支付", "月末未付"]) assert(html.includes(label), label);
  for (const key of ["company_cost_fen", "gross_salary_fen", "net_salary_fen", "direct_net_payments_fen", "outstanding_net_fen"]) {
    assert(html.includes(formatFen(employee[key])), key);
  }
  assert.doesNotMatch(html, /各月份工资与付款|查看工资与付款事项|正在读取工资与付款|继续查看工资来源/);
}));

test("personal labor sources render the projected person name", async () => withServer(async server => {
  const response = samples.employees_labor_sources.response;
  globalThis.stage7RenderResponses = { ...responses, employees: response };
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const router = await routerFor("/employees");
  const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
  const app = createSSRApp(component); app.use(router);
  const html = await renderToString(app);
  assert.match(html, /个人劳务/);
  const sources = response.data.collections.labor_sources.items;
  assert(sources.length);
  for (const source of sources) {
    assert.equal("party" in source, false);
    assert(html.includes(source.name), source.person_id);
  }
}));
