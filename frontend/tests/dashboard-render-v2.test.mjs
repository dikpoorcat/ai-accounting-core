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

async function withServer(run, { displayMode } = {}) {
  globalThis.stage7RenderResponses = responses;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${companyId}` } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-stage7-generated-response", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) {
        return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.stage7RenderResponses.businessStatus ?? null)")
          .replace("openedPanel = ref(false)", "openedPanel = ref(Boolean(globalThis.stage7RenderResponses.businessStatus))");
      }
      const match = /\/src\/views\/(Brief|Funds|Employees|Assets|Reports)View\.vue$/.exec(id.replaceAll("\\", "/"));
      if (!match) return;
      const key = match[1].toLowerCase();
      const refName = key === "funds" ? "funds" : key === "reports" ? "report" : "response";
      const seeded = key === "funds" ? "globalThis.stage7RenderResponses.funds.data" : `globalThis.stage7RenderResponses.${key}`;
      code = code.replace(new RegExp(`const ${refName} = (?:ref|shallowRef)<[^;\\n]+>\\(null\\)`), `const ${refName} = ref(${seeded})`);
      if (["employees", "assets"].includes(key) && displayMode !== undefined) {
        code = code.replace(/const displayMode = ref<[^;\n]+>\("cards"\)/, `const displayMode = ref("${displayMode}")`);
        if (key === "assets" && displayMode === "list") code = code.replace("const listInitialized = ref(false)", "const listInitialized = ref(true)");
      }
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
  return router.push(`${path}?company_id=${companyId}&period=2026-01&quarter=2026-Q1${path === "/assets" ? "&asset_filter=all" : ""}`).then(() => router);
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

function assetListResponse() {
  const response = structuredClone(responses.assets);
  const active = {
    asset_id: "asset-active", asset_type: "fixed", code: "ZC001", name: "合成设备完整名称",
    category: "equipment", category_label: "设备", status: "active", status_label: "使用中",
    acquisition_date: "2025-12-15", posting_period: "2025-12", recognition_label: "2025-12-15",
    settlement_scope: "本验收批次结算", payment_summary: { obligation_count: 1, checking: false,
      amount_fen: "161800", paid_fen: "10000", other_settled_fen: "20000", remaining_fen: "131800" },
    cost_fen: "900719925474099345", accumulated_charge_fen: "123456", month_charge_fen: "23456", book_value_fen: "900719925473975889",
    month_acquired: false, month_activated: true, month_exited: false, in_service_date: "2026-01-01", disposal: null,
  };
  const pending = { ...active, asset_id: "asset-pending", name: "待启用设备", status: "pending_activation", status_label: "待启用", in_service_date: "2026-02-01", month_activated: false,
    settlement_scope: "成本来源结算（不分摊为本资产付款）", payment_summary: { obligation_count: 1, checking: true, amount_fen: null, paid_fen: null, other_settled_fen: null, remaining_fen: null } };
  const disposed = { ...active, asset_id: "asset-disposed", name: "已出售设备", status: "disposed", status_label: "已处置", book_value_fen: "0", month_exited: true, settlement_scope: "本资产结算",
    disposal: { date: "2026-01-20", book_value_fen: "151800", kind: "sale", gross_proceeds_fen: "200000", gain_fen: "48200", loss_fen: "0", party: "合成买方" } };
  const { in_service_date: _date, disposal: _disposal, ...common } = active;
  const retired = { ...common, asset_id: "asset-retired", asset_type: "intangible", name: "已退役软件", status: "retired", status_label: "已退役", book_value_fen: "0", month_exited: true,
    available_for_use_date: "2026-01-01", retirement: { date: "2026-01-21", book_value_fen: "141800" } };
  const unknown = { asset_id: "asset-unknown", asset_type: "fixed", name: "资料待确认设备", selection_status: "unestablished", cost_fen: null, accumulated_charge_fen: null, month_charge_fen: null, book_value_fen: null };
  response.data.collections.assets = { items: [active, pending, disposed, retired, unknown], page: { total_count: 5, filtered_count: 5, returned_count: 5, has_more: false, next_cursor: null } };
  return response;
}

async function renderAssets(server, response) {
  assert(validateDashboardAssetsResponse(response), JSON.stringify(validateDashboardAssetsResponse.errors));
  globalThis.stage7RenderResponses = { ...responses, assets: response };
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const router = await routerFor("/assets");
  const { default: component } = await server.ssrLoadModule("/src/views/AssetsView.vue");
  const app = createSSRApp(component); app.use(router);
  return renderToString(app);
}

test("asset list rows retain precise amounts, distinct usage states and unknown amounts", async () => withServer(async server => {
  const response = assetListResponse();
  const html = await renderAssets(server, response);
  const rows = [...html.matchAll(/<summary\b[^>]*class="[^"]*\basset-list-summary\b[^"]*"[^>]*>([\s\S]*?)<\/summary>/g)].map(match => match[1]);
  const unknownArticle = [...html.matchAll(/<article\b[^>]*>([\s\S]*?)<\/article>/g)].map(match => match[1]).find(row => row.includes("资料待确认设备"));
  const unknownList = unknownArticle?.slice(unknownArticle.indexOf('class="asset-list-summary'));
  assert(unknownList, "an unknown asset remains visible as a list row without fabricated detail");
  rows.push(unknownList);
  assert.equal(rows.length, 5);
  const active = rows.find(row => row.includes("合成设备完整名称"));
  for (const amount of ["¥9,007,199,254,740,993.45", "¥1,234.56", "¥234.56", "¥9,007,199,254,739,758.89", "¥1,318.00"]) assert(active.includes(amount), amount);
  assert.match(active, /asset-list-chevron/);
  const header = html.match(/<div\b[^>]*class="asset-list-header"[^>]*>([\s\S]*?)<\/div>/)?.[1];
  assert(header, "the asset list has a shared header");
  assert.deepEqual([...header.matchAll(/<span\b[^>]*>([\s\S]*?)<\/span>/g)].map(match => match[1]),
    ["资产", "状态", "取得成本", "累计折旧／摊销", "本月折旧／摊销", "所选月末价值", "相关付款", ""]);
  assert.deepEqual([...active.matchAll(/data-label="([^"]+)"/g)].map(match => match[1]),
    ["状态", "取得成本", "累计折旧", "本月折旧", "所选月末价值", "相关付款"]);
  const unknown = rows.find(row => row.includes("资料待确认设备"));
  assert.match(unknown, /暂无法确定/);
  assert.doesNotMatch(unknown, /¥0\.00/);
  const pending = rows.find(row => row.includes("待启用设备"));
  assert.match(pending, /项目来源款项/);
  assert.match(pending, /暂无法确定/);
  assert.match(pending, /AI 会计核对中/);
  assert.match(rows.find(row => row.includes("已出售设备")), /本项付款/);
  for (const [name, label, tone] of [["合成设备完整名称", "使用中", "active"], ["待启用设备", "待启用", "pending_activation"], ["已出售设备", "已处置", "disposed"], ["已退役软件", "已退役", "retired"], ["资料待确认设备", "资料待确认", "needs-attention"]]) {
    const row = rows.find(row => row.includes(name));
    const dot = row.match(/<span\b(?=[^>]*role="img")(?=[^>]*aria-label="[^"]+")[^>]*>/)?.[0];
    assert(dot, `${name} has an accessible usage status dot`);
    assert(dot.includes(`aria-label="${label}"`), `${name} status label`);
    assert(dot.includes(tone), `${name} status tone`);
  }
  for (const [key, label, amount] of [["cost", "取得成本", "¥9,007,199,254,740,993.45"], ["accumulated", "累计折旧", "¥1,234.56"], ["month", "本月折旧", "¥234.56"], ["book", "所选月末价值", "¥9,007,199,254,739,758.89"]]) {
    const cell = active.match(new RegExp(`<strong\\b(?=[^>]*data-label="${label}")(?=[^>]*aria-labelledby="asset-column-${key}")[^>]*>([\\s\\S]*?)</strong>`));
    assert(cell, `${key} is aligned with an accessible amount column`);
    assert.equal(cell[1].trim(), amount);
    assert.match(html, new RegExp(`id="asset-column-${key}"`));
  }
}, { displayMode: "list" }));

test("asset card and list modes share a single title toolbar while list details add dates, payments and exits", async () => {
  for (const displayMode of [undefined, "cards", "list"]) await withServer(async server => {
    const html = await renderAssets(server, assetListResponse());
    assert.equal([...html.matchAll(/aria-label="资产展示方式"/g)].length, 1);
    const titleIndex = html.indexOf('id="asset-list-title"');
    const headingStart = [...html.slice(0, titleIndex).matchAll(/<div\b[^>]*class="[^"]*\bsection-heading\b[^"]*"[^>]*>/g)].at(-1)?.index;
    assert.notEqual(headingStart, undefined, "asset title has a section heading");
    let depth = 0, headingEnd = headingStart;
    for (const tag of html.slice(headingStart).matchAll(/<\/?div\b[^>]*>/g)) {
      depth += tag[0].startsWith("</") ? -1 : 1;
      if (!depth) { headingEnd += tag.index + tag[0].length; break; }
    }
    const heading = html.slice(headingStart, headingEnd);
    assert.match(heading, /aria-label="资产展示方式"/, "the single display switch belongs to the asset title area");
    assert.match(heading, new RegExp(`<button\\b[^>]*aria-pressed="${displayMode === "list" ? "false" : "true"}"[^>]*>卡片</button>`));
    assert.match(heading, new RegExp(`<button\\b[^>]*aria-pressed="${displayMode === "list" ? "true" : "false"}"[^>]*>列表</button>`));
    const details = [...html.matchAll(/<details\b[^>]*>([\s\S]*?)<\/details>/g)].map(match => match[1]).filter(detail => /asset-list-summary/.test(detail));
    assert.equal(details.length, displayMode === "list" ? 4 : 0);
    if (displayMode !== "list") {
      assert.match(html, /取得成本|累计折旧/);
      assert.match(html, /整批付款/);
      assert.match(html, /2025-12-15/);
      return;
    }
    const active = details.find(detail => detail.includes("合成设备完整名称"));
    const supplemental = active.replace(/<summary\b[^>]*>[\s\S]*?<\/summary>/, "");
    assert.match(supplemental, /2025-12-15/);
    assert.match(supplemental, /2026-01-01/);
    assert.match(active, /整批付款/);
    assert.match(supplemental, /¥100\.00/);
    assert.match(supplemental, /¥200\.00/);
    assert.doesNotMatch(supplemental, /取得成本|累计折旧|本月折旧|所选月末还值/);
    const disposalHtml = details.find(detail => detail.includes("已出售设备"));
    assert.match(disposalHtml, /<h3[^>]*>出售补充<\/h3>/, "sale remains distinguishable under the generic disposed status");
    const disposal = disposalHtml.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
    assert.match(disposal, /收款金额 ¥2,000\.00/);
    assert.match(disposal, /处置收益 ¥482\.00/);
    assert.match(disposal, /处置时账面价值 ¥1,518\.00/, "exit-time value remains distinct from the zero month-end value");
    const retired = details.find(detail => detail.includes("已退役软件")).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
    assert.match(retired, /退出时账面价值 ¥1,418\.00/);
    const pending = details.find(detail => detail.includes("待启用设备"));
    const pendingBody = pending.replace(/<summary\b[^>]*>[\s\S]*?<\/summary>/, "");
    assert.match(pendingBody, /暂无法确定/);
    assert.doesNotMatch(pendingBody, /¥0\.00/);
    const scrappedResponse = assetListResponse();
    const scrapped = scrappedResponse.data.collections.assets.items.find(item => item.asset_id === "asset-disposed");
    scrapped.name = "已报废设备";
    Object.assign(scrapped.disposal, { kind: "retirement", gross_proceeds_fen: "0", gain_fen: "0", loss_fen: "151800", party: "" });
    const scrappedHtml = await renderAssets(server, scrappedResponse);
    const scrappedDetail = [...scrappedHtml.matchAll(/<details\b[^>]*>([\s\S]*?)<\/details>/g)].map(match => match[1]).find(detail => detail.includes(scrapped.name));
    assert.match(scrappedDetail, /<h3[^>]*>报废补充<\/h3>/, "scrap remains distinguishable under the same generic disposed status");
    assert.doesNotMatch(scrappedDetail, /出售补充/);
    const scrappedText = scrappedDetail.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
    assert.match(scrappedText, /处置时账面价值 ¥1,518\.00/);
    assert.match(scrappedText, /收款金额 ¥0\.00/);
    assert.match(scrappedText, /处置损失 ¥1,518\.00/);
  }, { displayMode });
});

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

test("employee and personal labor list rows align each complete column with its rendered amount", async () => withServer(async server => {
  const response = structuredClone(samples.employees_focused.response);
  response.data.collections.labor_sources = structuredClone(samples.employees_labor_sources.response.data.collections.labor_sources);
  const [employee] = response.data.collections.employees.items;
  Object.assign(employee, {
    name: "合成员工很长的姓名用于核查完整展示",
    gross_salary_fen: "900719925474099345", annual_bonus_fen: "123456",
    employer_housing_fund_fen: "23456", employee_housing_fund_fen: "7890",
    personal_deduction_fen: "100490", net_salary_fen: "900719925473998855",
  });
  const unknown = {
    employee_id: "unestablished-employee", name: "待确认员工", selection_status: "unestablished",
    ...Object.fromEntries(Object.keys(employee).filter(key => key.endsWith("_fen")).map(key => [key, null])),
  };
  response.data.collections.employees.items.push(unknown);
  response.data.collections.employees.page = { total_count: 2, filtered_count: 2, returned_count: 2, has_more: false, next_cursor: null };
  assert(validateDashboardEmployeesResponse(response), JSON.stringify(validateDashboardEmployeesResponse.errors));
  globalThis.stage7RenderResponses = { ...responses, employees: response };
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const router = await routerFor("/employees");
  const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
  const app = createSSRApp(component); app.use(router);
  const html = await renderToString(app);
  const summaries = [...html.matchAll(/<summary\b[^>]*>([\s\S]*?)<\/summary>/g)]
    .map(match => match[1]).filter(row => /class="[^"]*\bemployee-list-summary\b[^"]*"/.test(row));
  const employeeRow = summaries.find(row => row.includes(employee.name));
  const laborRow = summaries.find(row => row.includes("合成劳务人员"));
  const unknownRow = [...html.matchAll(/<article\b[^>]*class="[^"]*employee-card[^"]*"[^>]*>([\s\S]*?)<\/article>/g)]
    .map(match => match[1]).find(row => row.includes(unknown.name));
  assert(employeeRow, "employee list row retains the complete projected name");
  assert(laborRow, "labor list row retains the projected person name");
  assert(unknownRow, "unestablished employees remain visible in list mode");
  const employeeColumns = [
    ["gross", "应发工资", "¥9,007,199,254,740,993.45"],
    ["bonus", "全年一次性奖金", "¥1,234.56"],
    ["employer-social", "公司社保", "¥1,600.00"],
    ["employer-housing", "公司公积金", "¥234.56"],
    ["employee-contribution", "个人社保公积金", "¥878.90"],
    ["tax", "工资扣税（入账）", "¥126.00"],
    ["deductions", "个人扣减合计", "¥1,004.90"],
    ["net", "应付净薪", "¥9,007,199,254,739,988.55"],
  ];
  const laborColumns = [["gross", "劳务报酬", "¥5,000.00"], ["tax", "扣税（入账）", "¥0.00"], ["net", "应付净额", "¥5,000.00"]];
  for (const [row, prefix, columns] of [
    [employeeRow, "employee", employeeColumns], [laborRow, "labor", laborColumns],
  ]) {
    const renderedLabels = [...row.matchAll(/data-label="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(renderedLabels, columns.map(([, label]) => label), `${prefix} column order`);
    for (const [key, label, amount] of columns) {
      const id = `${prefix}-column-${key}`;
      assert.match(html, new RegExp(`<span\\b(?=[^>]*id="${id}")[^>]*>${label}</span>`), `${label} shared header`);
      const cell = row.match(new RegExp(`<strong\\b(?=[^>]*data-label="${label}")(?=[^>]*aria-labelledby="${id}")[^>]*>([\\s\\S]*?)</strong>`));
      assert(cell, `${prefix}.${key} accessible amount cell`);
      assert.equal(cell[1].trim(), amount, `${prefix}.${key} displayed amount`);
    }
  }
  assert.match(unknownRow, /AI 会计核对中 · 金额暂无法确定/);
  assert.doesNotMatch(unknownRow, /¥0\.00/);
  assert.match(employeeRow, /employee-list-chevron/);
  assert.match(laborRow, /employee-list-chevron/);
  assert.match(html, /薪酬补充/);
  assert.match(html, /公司已付/);
}, { displayMode: "list" }));

test("employee and personal labor share one display switch and employee filter with cards as default", async () => {
  for (const displayModes of [{}, { displayMode: "list" }, { displayMode: "cards" }]) {
    await withServer(async server => {
      const response = structuredClone(samples.employees_focused.response);
      response.data.collections.labor_sources = structuredClone(samples.employees_labor_sources.response.data.collections.labor_sources);
      globalThis.stage7RenderResponses = { ...responses, employees: response };
      globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
      const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
      await useDashboardContext().load(true);
      const router = await routerFor("/employees");
      const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app);
      const listSummaries = [...html.matchAll(/<summary\b[^>]*>([\s\S]*?)<\/summary>/g)]
        .map(match => match[1]).filter(row => /class="[^"]*\bemployee-list-summary\b[^"]*"/.test(row));
      const [employee] = response.data.collections.employees.items;
      const [labor] = response.data.collections.labor_sources.items;
      assert.equal(listSummaries.some(row => row.includes(employee.name)), displayModes.displayMode === "list", "employee mode");
      assert.equal(listSummaries.some(row => row.includes(labor.name)), displayModes.displayMode === "list", "labor mode");
      assert.equal((html.match(/class="display-switch"/g) ?? []).length, 1, "one shared display switch");
      assert.equal((html.match(/aria-label="筛选员工"/g) ?? []).length, 1, "one employee filter");
      const employeeSection = html.match(/<section\b[^>]*class="panel"[^>]*>([\s\S]*?)<\/section>/)?.[1];
      assert(employeeSection, "employee detail section");
      assert.match(employeeSection, /id="employee-list-title"/);
      assert.match(employeeSection, /class="people-toolbar"/);
      assert.match(employeeSection, /aria-label="筛选员工"/);
      assert.match(employeeSection, /class="display-switch"/);
      assert.doesNotMatch(html.slice(0, html.indexOf('class="employees-content"')), /class="people-toolbar"|class="display-switch"|aria-label="筛选员工"/);
      for (const [label, selectedMode] of [["员工与劳务展示方式", displayModes.displayMode]]) {
        const group = html.match(new RegExp(`<div\\b(?=[^>]*role="group")(?=[^>]*aria-label="${label}")[^>]*>([\\s\\S]*?)</div>`));
        assert(group, label);
        const pressed = [...group[1].matchAll(/<button\b(?=[^>]*aria-pressed="true")[^>]*>([^<]+)<\/button>/g)].map(match => match[1]);
        assert.deepEqual(pressed, [selectedMode === "list" ? "列表" : "卡片"], `${label} pressed button`);
      }
    }, displayModes);
  }
});

test("employee list details add payment and personal splits while cards retain their salary breakdown", async () => {
  for (const displayMode of ["list", "cards"]) {
    await withServer(async server => {
      const response = structuredClone(samples.employees_focused.response);
      const [employee] = response.data.collections.employees.items;
      Object.assign(employee, { annual_bonus_fen: "12345", has_annual_bonus: true, employee_housing_fund_fen: "6789" });
      assert(validateDashboardEmployeesResponse(response), JSON.stringify(validateDashboardEmployeesResponse.errors));
      globalThis.stage7RenderResponses = { ...responses, employees: response };
      globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
      const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
      await useDashboardContext().load(true);
      const router = await routerFor("/employees");
      const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app);
      const detail = html.match(/<div\b[^>]*class="(?:[^"]*\s)?employee-detail(?:\s[^"]*)?"[^>]*>([\s\S]*?)<\/details>/)?.[1];
      assert(detail, `${displayMode} employee detail`);
      assert.match(detail, /个人社保[\s\S]*¥800\.00/);
      assert.match(detail, /个人公积金[\s\S]*¥67\.89/);
      assert.match(detail, /本月代付、抵销等[\s\S]*¥0\.00/);
      if (displayMode === "list") {
        assert(!detail.includes(employee.period_state_label), "list does not repeat the row status");
        assert.doesNotMatch(detail, /应发工资|全年一次性奖金|已扣个税|工资扣税|公司社保公积金/);
        assert.match(detail, /本月公司成本[\s\S]*¥11,600\.00/);
        assert.match(detail, /本月实际支付[\s\S]*¥0\.00/);
        assert.match(detail, /月末未付[\s\S]*¥9,074\.00/);
      } else {
        assert.match(detail, /应发工资[\s\S]*¥10,000\.00/);
        assert.match(detail, /全年一次性奖金[\s\S]*¥123\.45/);
        assert.match(detail, /公司社保公积金[\s\S]*¥1,600\.00/);
        assert.match(detail, /已扣个税[\s\S]*¥126\.00/);
      }
    }, { displayMode });
  }
});

test("personal labor parent details pass displayed facts to avoid repeating exact amounts in nested progress", async () => {
  for (const displayMode of ["list", "cards"]) {
    await withServer(async server => {
      const response = structuredClone(samples.employees_labor_sources.response);
      const [labor] = response.data.collections.labor_sources.items;
      const [obligation] = labor.obligations;
      const businessStatus = structuredClone(samples.business_status.response.data);
      Object.assign(businessStatus.identity, { subject_id: labor.subject_id, kind: "labor_accrual" });
      businessStatus.latest_source = { deleted: false, period: labor.period };
      businessStatus.current_business_result = { amount_fen: labor.gross_fen, amount_label: "劳务确认毛额", posting_period: labor.cutoff_period };
      businessStatus.display_profiles = {};
      businessStatus.settlements = { cutoff_period: labor.cutoff_period, status: "established", checking: false, obligations: [{
        key: obligation.key, name: obligation.name, direction: "payable", category_key: "labor_payables", source_period: labor.period,
        source_amount_fen: obligation.amount_fen, paid_fen: obligation.paid_fen, other_settled_fen: obligation.other_settled_fen,
        remaining_fen: obligation.remaining_fen, settlement_status: "open",
      }] };
      businessStatus.current_followups.settlements = structuredClone(businessStatus.settlements);
      businessStatus.collections.settlement_events = { items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null } };
      globalThis.stage7RenderResponses = { ...responses, employees: response, businessStatus };
      globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
      const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
      await useDashboardContext().load(true);
      const router = await routerFor("/employees");
      const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app);
      const detail = html.match(/<div\b[^>]*class="(?:[^"]*\s)?employee-detail(?:\s[^"]*)?"[^>]*>([\s\S]*?)<\/details>/)?.[1];
      assert(detail, `${displayMode} labor detail`);
      assert.match(detail, /公司已付[\s\S]*¥0\.00/);
      assert.match(detail, /代付、抵销等[\s\S]*¥0\.00/);
      assert.match(detail, /月末未付[\s\S]*¥5,000\.00/);
      assert.equal((detail.match(/¥5,000\.00/g) ?? []).length, 1, "only the additional unpaid amount remains in expanded details");
      assert.doesNotMatch(detail, /劳务确认毛额/);
      assert.match(detail, /查看收付款事项/);
    }, { displayMode });
  }
});

test("employee list status dots follow salary business scope rather than the shared roster state", async () => withServer(async server => {
  const response = structuredClone(samples.employees_focused.response);
  const [template] = response.data.collections.employees.items;
  const scopes = [["wage_income", "工资薪金"], ["contributions_only", "仅确认社保公积金"], ["mixed", "包含不同核算情形"], ["none", "本月无工资核算"]];
  const employees = scopes.map(([scope, label], index) => ({ ...template, employee_id: `employee-scope-${index}`, name: `在册员工${index + 1}`,
    period_state: "in_period", period_state_label: "已确认在册", in_period: true, wage_tax_scope: scope, wage_tax_scope_label: label }));
  const unknown = { employee_id: "unestablished-scope", name: "核对中员工", selection_status: "unestablished",
    ...Object.fromEntries(Object.keys(template).filter(key => key.endsWith("_fen")).map(key => [key, null])) };
  response.data.collections.employees.items = [...employees, unknown];
  response.data.collections.employees.page = { total_count: 5, filtered_count: 5, returned_count: 5, has_more: false, next_cursor: null };
  response.data.collections.labor_sources = structuredClone(samples.employees_labor_sources.response.data.collections.labor_sources);
  assert(validateDashboardEmployeesResponse(response), JSON.stringify(validateDashboardEmployeesResponse.errors));
  globalThis.stage7RenderResponses = { ...responses, employees: response };
  globalThis.fetch = async () => new Response(JSON.stringify(responses.context));
  const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
  await useDashboardContext().load(true);
  const router = await routerFor("/employees");
  const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
  const app = createSSRApp(component); app.use(router);
  const html = await renderToString(app);
  const summaries = [...html.matchAll(/<summary\b[^>]*>([\s\S]*?)<\/summary>/g)].map(match => match[1]);
  for (const employee of employees) {
    const row = summaries.find(value => value.includes(employee.name));
    assert(row, employee.name);
    const marker = row.match(/<span\b[^>]*class="[^"]*\bemployee-status\b[^"]*"[^>]*>/)?.[0];
    assert(marker, `${employee.wage_tax_scope} status dot`);
    const classes = marker.match(/class="([^"]+)"/)[1].split(/\s+/);
    assert.deepEqual(classes.filter(value => scopes.some(([scope]) => scope === value)), [employee.wage_tax_scope], `${employee.wage_tax_scope} keeps its own business state`);
    assert(!classes.includes("in_period"), "roster membership does not select the dot state");
    assert.match(marker, /role="img"/);
    assert(marker.includes(`aria-label="${employee.wage_tax_scope_label}"`), "the accessible status describes salary business scope");
    assert(marker.includes(`title="${employee.wage_tax_scope_label}"`), "hover text describes the same salary business scope");
  }
  const laborRow = summaries.find(value => value.includes("合成劳务人员"));
  assert.match(laborRow, /<span\b(?=[^>]*class="[^"]*\bemployee-status\b[^"]*\blabor\b[^"]*")(?=[^>]*role="img")(?=[^>]*aria-label="个人劳务")[^>]*>/);
  const unknownRow = [...html.matchAll(/<article\b[^>]*class="[^"]*employee-card[^"]*"[^>]*>([\s\S]*?)<\/article>/g)]
    .map(match => match[1]).find(row => row.includes(unknown.name));
  assert.match(unknownRow, /AI 会计核对中/);
  assert.doesNotMatch(unknownRow, /employee-status/);
}, { displayMode: "list" }));
