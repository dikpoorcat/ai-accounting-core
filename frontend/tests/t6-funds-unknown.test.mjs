import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("T6 funds keeps historical source warnings without the company-wide followup panel", async () => {
  const fixtures = JSON.parse(readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8"));
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=co" } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "t6-funds-historical-unknown", enforce: "pre", transform(code, id) {
      const path = id.replaceAll("\\", "/");
      if (path.endsWith("/src/views/FundsView.vue")) return code
        .replace("const funds = ref<FundsData | null>(null)", "const funds = ref(globalThis.t6FundsData)")
        .replace("const initializing = ref(true)", "const initializing = ref(false)")
        .replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-11")')
        .replaceAll("{ immediate: true }", "{ immediate: false }");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    globalThis.fetch = async () => new Response(JSON.stringify(fixtures.context));
    const { useDashboardContext } = await server.ssrLoadModule("/src/composables/useDashboardContext.ts");
    await useDashboardContext().load(true);
    const { default: component } = await server.ssrLoadModule("/src/views/FundsView.vue");
    for (const total of [null, "12345"]) {
      const data = structuredClone(fixtures.funds.data);
      data.total_fen = total;
      if (total === null) {
        data.collections.accounts.items[0].opening_fen = null;
        data.collections.accounts.items[0].closing_fen = null;
        data.collections.accounts.items[0].negative_balance = false;
      }
      data.fact_issues = [{ reason: "source_digest_mismatch", candidates: [
        { calculation_id: "t6-frozen-candidate-a", amount_fen: "987654321" },
        { calculation_id: "t6-frozen-candidate-b", amount_fen: "987654321" },
      ] }];
      globalThis.t6FundsData = data;
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/funds", name: "funds", component: {} }, { path: "/", name: "brief", component: {} }] });
      await router.push("/funds?company_id=co&period=2026-11");
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app), visible = html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
      assert.match(visible, /1 组历史资金依据需要核对/);
      assert.match(visible, /具体金额与流水核对状态分别见对应区块/);
      assert.match(visible, /与当前跟进状态分别列示/);
      assert.doesNotMatch(visible, /款项：已结清|所选月末核算后/);
      if (total === null) {
        assert.match(visible, /余额待确认/);
        assert.match(visible, /所选月末账面余额[\s\S]*暂无法确定/);
      } else {
        assert.match(visible, /123\.45/);
        assert.match(visible, /余额需要核查/);
      }
      assert.doesNotMatch(visible, /9,876,543\.21/);
      assert.match(html, /t6-frozen-candidate-a/); assert.match(html, /t6-frozen-candidate-b/);
      assert.match(html, /source_digest_mismatch/);
    }
  } finally {
    await server.close();
    globalThis.window = previousWindow; globalThis.fetch = previousFetch;
    delete globalThis.t6FundsData;
  }
});
