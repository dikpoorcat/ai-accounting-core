import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("brief funds show external cash movements without technical position checks", async () => {
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, plugins: [vue()],
    server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefFinancialOverview.vue");
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: "/", name: "brief", component: {} }, { path: "/funds", name: "funds", component: {} },
    ] });
    await router.push("/");
    const app = createSSRApp(component, {
      funds: { total_fen: "12345", inflow_fen: "10000", outflow_fen: "5000", net_change_fen: "5000", internal_transfer_fen: "99999" },
    });
    app.use(router);
    const html = await renderToString(app);
    assert.match(html, /对外收款/);
    assert.match(html, /100\.00/);
    assert.match(html, /对外付款/);
    assert.match(html, /资金净变动/);
    assert.match(html, /50\.00/);
    assert.doesNotMatch(html, /999\.99|借贷|试算|来源|核算依据/);
  } finally { await server.close(); }
});
