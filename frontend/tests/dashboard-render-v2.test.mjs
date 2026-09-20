import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

// Actual synthetic T4 endpoint responses. Used for UI shape, not an accounting golden result.
const fixtures = JSON.parse(readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8"));

test("current responses pass runtime consumers and all five pages render historical/current partitions", async () => {
  globalThis.t4RenderFixtures = fixtures;
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=co" } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{
      name: "seed-synthetic-response", enforce: "pre",
      transform(code, id) {
        const match = /\/src\/views\/(Brief|Funds|Employees|Assets|Reports)View\.vue$/.exec(id.replaceAll("\\", "/"));
        if (!match) return;
        const key = match[1].toLowerCase();
        const refName = key === "funds" ? "funds" : key === "reports" ? "report" : "response";
        code = code.replace(new RegExp(`const ${refName} = ref<[^;\\n]+>\\(null\\)`), `const ${refName} = ref(globalThis.t4RenderFixtures.${key}${key === "funds" ? ".data" : ""})`);
        if (key === "funds") code = code.replace('const initializing = ref(true)', 'const initializing = ref(false)').replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-11")').replaceAll('{ immediate: true }', '{ immediate: false }');
        return code;
      },
    }, vue()],
    server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const { requestJson } = await server.ssrLoadModule("/src/api/client.ts");
    for (const [key, response] of Object.entries(fixtures)) {
      const endpoint = key === "reports" ? "quarterly-report" : key;
      globalThis.fetch = async () => new Response(JSON.stringify(response));
      const read = await requestJson(`/api/dashboard/${endpoint}?period=2026-11`);
      assert.equal(read.schema_version, response.schema_version, endpoint);
    }
    globalThis.fetch = async () => new Response(JSON.stringify(fixtures.context));
    const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
    await useDashboardContext().load(true);
    for (const name of ["Brief", "Funds", "Employees", "Assets", "Reports"]) {
      const router = createRouter({ history: createMemoryHistory(), routes: [
        { path: "/", name: "brief", component: {} }, { path: "/funds", name: "funds", component: {} },
        { path: "/employees", name: "employees", component: {} }, { path: "/assets", name: "assets", component: {} }, { path: "/reports", name: "reports", component: {} },
      ] });
      await router.push("/?company_id=co&period=2026-11&quarter=2026-Q4");
      const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app);
      if (name === "Brief") {
        assert.match(html, /账务与待办/, name);
        assert.match(html, /正在检查后续待办/, name);
      } else if (name === "Funds") {
        assert.match(html, /月末账面资金/, name);
        assert.match(html, /本月实际收款/, name);
        assert.match(html, /公司资金账户/, name);
        assert.match(html, /所选月末账面余额/, name);
        assert.match(html, /余额需要核查/, name);
        assert.match(html, /本月净减少.*−¥8,000\.00/s, name);
        assert.match(html, /账户流入、流出包含公司账户之间划转/, name);
        assert.doesNotMatch(html, /点击查看|查看本账户账面明细|查看本账户银行流水|查看账户来源引用与证明/, name);
      }
      if (name !== "Brief") assert.doesNotMatch(html, /class="period-preparation|所选月末核算后/, name);
      if (!["Brief", "Funds", "Reports"].includes(name)) assert.match(html, /完整总计.*筛选总计.*已加载/s, name);
      if (name === "Employees") assert.match(html, /暂无测算记录|未提供/, name);
      if (name === "Employees") assert.match(html, /相关来源历史清偿 · 截至所选月末 · 共 \d+ 项，已加载 \d+ 项/, name);
      if (name === "Assets") {
        assert.match(html, /资产名称待补充/, name);
        assert.match(html, /所选月末还值/, name);
        assert.match(html, /2026-11-30 取得并可供使用/, name);
        assert.match(html, /已摊销 0%/, name);
        assert.match(html, /项目来源款项/, name);
        assert.match(html, /月末已结清/, name);
        assert.match(html, /公司已付 ¥8,000\.00.*抵销等 ¥8,000\.00/s, name);
        assert.doesNotMatch(html, /点击查看|来源变更记录|后续收付款记录|折旧摊销说明|相关历史清偿（含关联来源，截至所选月末）|role="progressbar"/, name);
      }
    }
  } finally { await server.close(); delete globalThis.t4RenderFixtures; }
});

test("business details visibly retain uncertain adopted candidates, historical debt and later settlement", async () => {
  const data = structuredClone(fixtures["business-status"].data);
  const known = { ...data.settlements.obligations[0], source_amount_fen: "10000", amount_fen: "10000", paid_fen: "0", other_settled_fen: "0", remaining_fen: "10000", settlement_status: "open" };
  data.period = "2026-01";
  data.settlements = { ...data.settlements, cutoff_period: "2026-01", obligations: [known] };
  data.current_followups = { settlements: { status: "partially_established", cutoff_period: "2026-01", current_cutoff_period: "2026-02", issues: [{ message: "局部来源缺少精确金额" }], obligations: [
    { ...known, paid_fen: "10000", remaining_fen: "0", settlement_status: "settled" },
    { ...known, source_amount_fen: null, amount_fen: null, remaining_fen: null, settlement_status: "unestablished", source_issues: [{ message: "此项原金额未知" }] },
  ] } };
  data.as_posted.unestablished_state_selections = [{ reason: "close_adoption_not_proven", candidates: [{ calculation_id: "exact-candidate-a" }, { calculation_id: "exact-candidate-b" }] }];
  globalThis.t4BusinessRender = data;
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-business-status", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.t4BusinessRender)");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-01");
    const { default: component } = await server.ssrLoadModule("/src/components/BusinessStatusDetails.vue");
    const app = createSSRApp(component, { subjectId: data.identity.subject_id, period: "2026-01", snapshotVersion: "fixture" }); app.use(router);
    const html = await renderToString(app);
    // Strip only technical disclosure contents; core status and money must remain visible.
    const visible = html.replace(/<details[^>]*>[\s\S]*?<summary>[^<]*(?:技术依据|精确来源|候选与未建立原因)[^<]*<\/summary>[\s\S]*?<\/details>/g, "");
    assert.match(html, /尚不能证明冻结采用/);
    assert.match(html, /exact-candidate-a/); assert.match(html, /exact-candidate-b/);
    assert.ok((html.match(/查看候选与未建立原因/g) ?? []).length >= 2);
    assert.match(visible, /所选月末款项/); assert.match(visible, /本项历史业务相关的当前跟进/);
    assert.match(visible, /2026-02/); assert.match(visible, /已结清/); assert.match(visible, /尚不能确认/);
    assert.match(visible, /暂无法确定/); assert.match(visible, /此项原金额未知/); assert.match(visible, /局部来源缺少精确金额/);
  } finally { await server.close(); delete globalThis.t4BusinessRender; }
});

test("unestablished employee details keep exact candidates while asset cards stay owner-only", async () => {
  const candidates = [{ reason: "manifest_root_role_unestablished", candidates: [
    { calculation_id: "unknown-exact-candidate-a", amount_fen: "987654321" },
    { calculation_id: "unknown-exact-candidate-b", amount_fen: "987654321" },
  ] }];
  const responses = structuredClone(fixtures);
  // These IDs are entities declared by source facts, deliberately unlike the business subject.
  const employee = { employee_id: "employee-declared-17", name: "待证员工", selection_status: "unestablished", candidate_selections: candidates, trace_targets: [] };
  const asset = { asset_id: "asset-declared-29", asset_type: null, name: "待证资产", selection_status: "unestablished", candidate_selections: candidates, trace_targets: [] };
  const page = { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null };
  responses.employees.data.employees = { ...responses.employees.data.employees, unestablished_count: 1, items: [employee], net_salary_fen: null, gross_salary_fen: null, controlled_cost_fen: null };
  responses.employees.data.collections.employees = { items: [employee], page };
  responses.assets.data.unestablished_count = 1;
  responses.assets.data.collections.assets = { items: [asset], page };
  responses.assets.data.fixed.items = [];
  responses.assets.data.intangible.items = [];
  responses.assets.data.ledger_net_fen = null;
  responses.assets.data.reconciled = null;
  globalThis.t4PlaceholderFixtures = responses;
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=co" } };
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-unestablished-entities", enforce: "pre", transform(code, id) {
      const match = /\/src\/views\/(Employees|Assets)View\.vue$/.exec(id.replaceAll("\\", "/"));
      if (!match) return;
      code = code.replace(/const response = ref<[^;\n]+>\(null\)/, `const response = ref(globalThis.t4PlaceholderFixtures.${match[1].toLowerCase()})`);
      return match[1] === "Employees"
        ? code.replace('const employeeDisplayMode = ref<"cards" | "list">("cards")', 'const employeeDisplayMode = ref(globalThis.t4PlaceholderMode)')
        : code.replace('const displayMode = ref<"cards" | "list">("cards")', 'const displayMode = ref(globalThis.t4PlaceholderMode)');
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { requestJson } = await server.ssrLoadModule("/src/api/client.ts");
    for (const key of ["employees", "assets"]) {
      globalThis.fetch = async () => new Response(JSON.stringify(responses[key]));
      await requestJson(`/api/dashboard/${key}?period=2026-11`);
    }
    for (const [name, mode] of [["Employees", "cards"], ["Employees", "list"], ["Assets", "cards"]]) {
      globalThis.t4PlaceholderMode = mode;
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
      await router.push("/?company_id=co&period=2026-11");
      const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app), visible = html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
      assert.match(visible, name === "Employees" ? /待证员工/ : /待证资产/);
      assert.doesNotMatch(visible, /9,876,543\.21/);
      if (name === "Assets") {
        assert.match(visible, /资料待确认/);
        assert.match(visible, /所选月末还值/);
        assert.match(visible, /暂不计入资产数量和金额/);
        assert.match(visible, /由 AI 会计核对/);
        assert.doesNotMatch(html, /unknown-exact-candidate-a|unknown-exact-candidate-b|查看核算依据|冻结采用未建立|相关金额尚未建立|点击查看|role="progressbar"/);
      } else {
        assert.match(visible, /冻结采用未建立/);
        assert.match(visible, /相关金额尚未建立/);
        assert.match(visible, /完整范围内 1 项/);
        assert.match(visible, /查看本组候选与未建立原因/);
        assert.match(html, /unknown-exact-candidate-a/); assert.match(html, /unknown-exact-candidate-b/);
        if (mode === "list") assert.match(visible, /<strong[^>]*>未建立<\/strong>/);
      }
    }
    const actual = JSON.parse(readFileSync(new URL("./t4-unestablished-responses.json", import.meta.url), "utf8"));
    globalThis.t4PlaceholderFixtures = actual;
    for (const name of ["Employees", "Assets"]) {
      const response = actual[name.toLowerCase()];
      globalThis.fetch = async () => new Response(JSON.stringify(response));
      await requestJson(`/api/dashboard/${name.toLowerCase()}?period=${response.selected_period.key}`);
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
      await router.push(`/?company_id=co&period=${response.selected_period.key}`);
      const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app);
      if (name === "Assets") {
        assert.match(html, /资料待确认/);
        assert.doesNotMatch(html, /冻结采用未建立|相关金额尚未建立|查看核算依据|点击查看/);
      } else {
        assert.match(html, /冻结采用未建立/);
        assert.match(html, /相关金额尚未建立/);
        assert.match(html, /查看本组候选与未建立原因/);
      }
    }
  } finally { await server.close(); delete globalThis.t4PlaceholderFixtures; delete globalThis.t4PlaceholderMode; }
});

test("historical source pages expose totals and keep their own continuation without company-wide preparation", async () => {
  const responses = structuredClone(fixtures);
  const labor = responses.employees.data.workforce_cost.personal_labor.items[0];
  labor.movements = labor.movements.slice(0, 1);
  labor.movements_page = { total_count: 120, filtered_count: 120, returned_count: 1, has_more: true, next_cursor: "sealed-history-cursor" };
  globalThis.t4HistoryFixtures = responses;
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-historical-page", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/views/EmployeesView.vue")) return code.replace(/const response = ref<[^;\n]+>\(null\)/, "const response = ref(globalThis.t4HistoryFixtures.employees)");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-11");
    const { default: component } = await server.ssrLoadModule("/src/views/EmployeesView.vue");
    const app = createSSRApp(component); app.use(router);
    const html = await renderToString(app), visible = html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
    assert.match(visible, /相关来源历史清偿 · 截至所选月末 · 共 120 项，已加载 1 项/);
    assert.match(visible, /查看更多历史清偿与精确来源/);
    assert.match(visible, /含关联来源明细；本来源金额见上方汇总/);
    assert.match(visible, /非现金抵销 · ¥8,000\.00/);
    assert.doesNotMatch(visible, /查看精确来源业务/);
    assert.doesNotMatch(visible, /class="period-preparation|所选月末核算后/);
  } finally { await server.close(); delete globalThis.t4HistoryFixtures; }
});

test("R2 source movements stay in operating details and are omitted from owner asset cards", async () => {
  const responses = structuredClone(fixtures);
  const template = responses.assets.data.collections.assets.items[0].settlements[0];
  const page = { total_count: 2, filtered_count: 2, returned_count: 2, has_more: false, next_cursor: null };
  function source(slot) {
    const result = structuredClone(template);
    result.source_id = slot;
    result.movements_page = page;
    result.movements = [
      { ...result.movements[0], id: `${slot}-unknown`, label: `R2-${slot}`, relation_state: "unresolved", amount_fen: null, source_calculation_id: `exact-B-${slot}`, calculation_id: `exact-payment-${slot}` },
      { ...result.movements[1], id: `${slot}-resolved`, label: `R2-resolved-${slot}`, relation_state: "resolved", amount_fen: "123", source_calculation_id: `resolved-B-${slot}` },
    ];
    return result;
  }
  const employee = responses.employees.data.employees.items[0];
  employee.has_payroll_activity = true;
  employee.payroll_sources = [{ ...source("payroll"), period: "2026-11", kind: "opening_payroll", label: "工资来源", opening_period: null, component: "net", declarations: [], disbursements: [] }];
  employee.payroll_source_page = { ...page, total_count: 1, filtered_count: 1, returned_count: 1 };
  globalThis.t4LazySettlementCollection = { items: source("payroll").movements, page };
  Object.assign(responses.employees.data.workforce_cost.personal_labor.items[0], source("labor"));
  const asset = responses.assets.data.collections.assets.items[0];
  asset.settlements = [source("asset")];
  asset.retirement = { date: "2026-11-30", book_value_fen: null, reference: "R2", settlement: source("exit") };
  responses.assets.data.projects = [{ source_id: "R2-project", period: "2026-11", label: "项目来源", party: "相关收款方", cost_fen: null, remaining_fen: null, settlement: source("project") }];
  globalThis.t4UnresolvedFixtures = responses;
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-unresolved-source-movements", enforce: "pre", transform(code, id) {
      const path = id.replaceAll("\\", "/");
      if (path.endsWith("/src/components/DashboardSourceHistory.vue")) {
        return code.replace("const collection = ref<DashboardCollection | null>(null)", "const collection = ref(globalThis.t4LazySettlementCollection)");
      }
      const match = /\/src\/views\/(Employees|Assets)View\.vue$/.exec(path);
      if (match) return code.replace(/const response = ref<[^;\n]+>\(null\)/, `const response = ref(globalThis.t4UnresolvedFixtures.${match[1].toLowerCase()})`);
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    for (const [name, slots] of [["Employees", ["labor"]], ["Assets", ["project"]]]) {
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
      await router.push("/?company_id=co&period=2026-11");
      const { default: component } = await server.ssrLoadModule(`/src/views/${name}View.vue`);
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app), visible = html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
      assert.equal((visible.match(/清偿关系尚未确认，未计入已结金额。/g) ?? []).length, slots.length);
      for (const slot of slots) {
        assert.match(visible, new RegExp(`R2-${slot}[^<]*暂无法确定</p>`));
        assert.match(visible, new RegExp(`R2-resolved-${slot}[^<]*1\\.23</p>`));
      }
      if (name === "Employees") {
        assert.match(visible, /当前后续事项 · 查看精确关联的清偿事件/);
        assert.match(visible, /R2-payroll[\s\S]*?金额 暂无法确定/);
        assert.match(visible, /R2-resolved-payroll[\s\S]*?金额 ¥1\.23/);
      }
      if (name === "Assets") {
        assert.doesNotMatch(visible, /R2-asset|R2-exit/);
      }
    }
  } finally { await server.close(); delete globalThis.t4UnresolvedFixtures; delete globalThis.t4LazySettlementCollection; }
});

test("T6 preparation keeps owner-facing issue summaries without technical source navigation", async () => {
  const preparation = structuredClone(fixtures.employees.data.period_preparation);
  preparation.current_followups.materials.issues = [
    { message: "同文资料问题", subject_id: "source-a" },
    { message: "同文资料问题", subject_id: "source-b" },
  ];
  Object.assign(preparation.current_followups.settlements, { complete: false, paid_fen: "12345", remaining_fen: "30000" });
  Object.assign(preparation.current_followups.external, { obligation_count: 0, completion_status_counts: {} });
  preparation.current_followups.file_jobs.issue_count = 3;
  preparation.current_followups.accounting.pending_subject_id = "pending-correction";
  preparation.current_followups.accounting.issues = [
    { message: "另一业务尚未正式处理", field: "unpublished-other" },
    { message: "显式定位的问题", subject_id: "explicit-accounting-source" },
  ];
  globalThis.t6PreparationTargets = [];
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "record-preparation-targets", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) return code.replace("const route = useRoute();", "globalThis.t6PreparationTargets.push(props.subjectId);\nconst route = useRoute();");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }, { path: "/funds", component: {} }] });
    await router.push("/?company_id=co&period=2026-11");
    const { default: component } = await server.ssrLoadModule("/src/components/PeriodPreparation.vue");
    const app = createSSRApp(component, { preparation, ownerNavigation: true }); app.use(router);
    const html = await renderToString(app);
    assert.match(html, /所选月末核算后，还有什么需要处理/);
    assert.match(html, new RegExp(`全公司 · 截至 ${preparation.as_of} 的相关后续事项`));
    assert.match(html, /尚未关账；以下事项会持续更新/);
    assert.match(html, /当前资料核对 · 2 条核对提示/);
    assert.doesNotMatch(html, /source-a|source-b/);
    assert.match(html, /class="needs-check"[^>]*>\s*当前款项金额尚不能完整建立/);
    assert.match(html, /收付款跟进<\/span><strong[^>]*>¥300\.00/);
    assert.match(html, /<button[^>]*class="followup-card attention clickable"[^>]*>.*点击查看<\/span><\/button>/s);
    assert.match(html, /申报与外部事项<\/span><strong[^>]*>暂无<\/strong><small[^>]*>所选月份相关共 0 项/);
    assert.match(html, /class="needs-check"[^>]*>\s*3 项文件任务结果或引用依据待核对/);
    assert.match(html, /另一业务尚未正式处理/);
    assert.doesNotMatch(html, /查看待更正业务依据|技术状态与完整投影/);
    assert.deepEqual(globalThis.t6PreparationTargets, []);
    assert.doesNotMatch(html, /总待办数[：:]\s*\d/);
  } finally { await server.close(); delete globalThis.t6PreparationTargets; }
});
