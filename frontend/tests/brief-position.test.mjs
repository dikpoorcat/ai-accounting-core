import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("an incomplete position keeps known money and renders neither a zero total nor a ratio", async () => {
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    plugins: [vue()],
    server: { middlewareMode: true, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefFinancialOverview.vue");
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: "/", name: "brief", component: {} },
      { path: "/funds", name: "funds", component: {} },
    ] });
    await router.push("/");
    const app = createSSRApp(component, {
      position: {
        assets_fen: null, liabilities_fen: null, capital_fen: "0", cumulative_result_fen: "0",
        bank_fen: "12345", fixed_asset_net_fen: "0", intangible_asset_net_fen: "0", other_assets_fen: null,
        equation_valid: null, complete: false,
        issues: [{ field: "financial_position.party", message: "往来余额缺少精确稳定归属" }],
      },
      funds: { total_fen: "12345", inflow_fen: "0", outflow_fen: "0", net_change_fen: "0", internal_transfer_fen: "0" },
      cash: { coverage_state: "missing", transaction_count: 0, matched_count: 0, inflow_fen: null, outflow_fen: null },
      unmatched: { count: 0, rows: [], rows_truncated: false },
    });
    app.use(router);
    const html = await renderToString(app);
    assert.match(html, /资产 无法完整建立/);
    assert.match(html, /123\.45/);
    assert.match(html, /往来余额缺少精确稳定归属/);
    assert.doesNotMatch(html, /class="track"/);
    assert.doesNotMatch(html, /资产 0\.00/);
    assert.doesNotMatch(html, /资产负债金额需要核对/);
  } finally {
    await server.close();
  }
});
