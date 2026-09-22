import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

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
      code = code.replace(new RegExp(`const ${refName} = ref<[^;\\n]+>\\(null\\)`), `const ${refName} = ref(${seeded})`);
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
  }
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
