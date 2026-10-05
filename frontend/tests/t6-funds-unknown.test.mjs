import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("owner funds keep unknown balances and AI review state without technical candidates", async () => {
  const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  const fixtures = { context: samples.company_with_period.response, funds: samples.cash_funds.response };
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${fixtures.context.current_company.company_id}` } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "t6-funds-historical-unknown", enforce: "pre", transform(code, id) {
      const path = id.replaceAll("\\", "/");
      if (path.endsWith("/src/views/FundsView.vue")) return code
        .replace("const funds = ref<FundsData | null>(null)", "const funds = ref(globalThis.t6FundsData)")
        .replace("const initializing = ref(true)", "const initializing = ref(false)")
        .replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref("2026-01")')
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
      data.bank_statement.review_state = "pending";
      globalThis.t6FundsData = data;
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/funds", name: "funds", component: {} }, { path: "/", name: "brief", component: {} }] });
      await router.push(`/funds?company_id=${fixtures.context.current_company.company_id}&period=2026-01`);
      const app = createSSRApp(component); app.use(router);
      const html = await renderToString(app), visible = html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, "");
      assert.match(visible, /AI 会计核对中/);
      if (total === null) assert.match(visible, /暂无法确定|待确认/);
      else assert.match(visible, /123\.45/);
      assert.doesNotMatch(html, /frozen-candidate|source_digest_mismatch|历史资金依据|查看.*证明|<pre/);

    }
  } finally {
    await server.close();
    globalThis.window = previousWindow; globalThis.fetch = previousFetch;
    delete globalThis.t6FundsData;
  }
});
