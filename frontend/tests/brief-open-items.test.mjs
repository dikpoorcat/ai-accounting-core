import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("closed-period open items prefer each item's current settlement status", async () => {
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    plugins: [vue()],
    server: { middlewareMode: true, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const { default: component } = await server.ssrLoadModule(
      "/src/components/brief/BriefOpenItems.vue",
    );
    const item = (id, party, currentStatus) => ({
      id,
      voucher: "",
      party_key: id,
      party,
      description: "可退保证金",
      source_business: { kind: "refundable_deposit", subject_id: id },
      source_period: "2026-03",
      source_amount_fen: "10000",
      paid_fen: "0",
      other_settled_fen: "0",
      status: "open",
      current_status: currentStatus,
      outstanding_fen: "10000",
    });
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-03");
    const app = createSSRApp(component, {
      openItems: {
        complete: true,
        unestablished_count: 0,
        issues: [],
        cutoff_period: "2026-03",
        current_cutoff_period: "2026-09",
        receivable_count: 3,
        receivable_fen: "30000",
        payable_count: 0,
        payable_fen: "0",
        total_count: 3,
        categories: [{
          key: "refundable_deposit_receivables",
          label: "待收回保证金",
          direction: "receivable",
          unit: "笔",
          count: 3,
          outstanding_fen: "30000",
          groups: [],
          items: [
            item("still-open", "仍待收", "open"),
            item("settled-later", "后来收回", "settled"),
            item("historical-only", "仅有历史结果", null),
          ],
        }],
      },
      periodLabel: "2026 年 3 月",
      periodStatus: "closed",
      period: "2026-03",
    });
    app.use(router);
    const html = await renderToString(app);

    assert.match(html, /class="status"[^>]*>当前待收<\/span>/);
    assert.match(html, /class="status status-settled"[^>]*>当前已收回<\/span>/);
    assert.match(html, /class="status status-historical"[^>]*>关账时待收<\/span>/);
    assert.equal((html.match(/class="compact-status-trigger"/g) ?? []).length, 3);
    assert.match(html, /核对依据/);
    assert.doesNotMatch(html, /精确来源与候选依据/);
  } finally {
    await server.close();
  }
});
