import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("funds show company tax-only income without a fabricated product or client tax calculation", async () => {
  const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  const context = samples.company_with_period.response;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${context.current_company.company_id}` } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "investment-income-fixture", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/views/FundsView.vue")) return code
        .replace("const funds = shallowRef<FundsData | null>(null)", "const funds = shallowRef(globalThis.investmentIncomeData)")
        .replace("const initializing = ref(true)", "const initializing = ref(false)")
        .replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-01")')
        .replaceAll("{ immediate: true }", "{ immediate: false }");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    globalThis.fetch = async () => new Response(JSON.stringify(context));
    const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
    await useDashboardContext().load(true);
    const { default: component } = await server.ssrLoadModule("/src/views/FundsView.vue");
    const data = structuredClone(samples.cash_funds.response.data);
    Object.assign(data.investments, { event_count: 1, recognized_income_fen: "0", vat_adjustment_fen: "-10000", investment_income_fen: "-10000", actual_loss_fen: "0" });
    data.collections.investment_products.items = [];
    data.collections.investment_events.items = [{ id: "tax-expiry", subject_id: "tax-source", date: null, period: "2026-01", attribution: "company_pool", fund_id: null, investment_id: null, name: "公司统一税费调整", type: "金融转让增值税调整", cost_fen: null, net_proceeds_fen: null, investment_income_fen: "-10000", actual_loss_fen: null, settlement_fen: null }];
    globalThis.investmentIncomeData = data;
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/funds", name: "funds", component: {} }, { path: "/", name: "brief", component: {} }] });
    await router.push(`/funds?company_id=${context.current_company.company_id}&period=2026-01`);
    const app = createSSRApp(component); app.use(router);
    const html = await renderToString(app);
    assert.match(html, /投资与理财/);
    assert.match(html, /公司统一税费调整/);
    assert.match(html, /已计投资增值税/);
    assert.match(html, /100\.00/);
    assert.doesNotMatch(html, /undefined|NaN|伪造产品|5111/);
  } finally {
    await server.close();
    globalThis.window = previousWindow; globalThis.fetch = previousFetch;
    delete globalThis.investmentIncomeData;
  }
});
