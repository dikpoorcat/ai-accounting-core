import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from "vue";
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
      subject_id: id,
      category_key: "refundable_deposit_receivables",
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
        }],
      },
      items: [
        item("still-open", "仍待收", "open"),
        item("settled-later", "后来收回", "settled"),
        item("historical-only", "仅有历史结果", null),
      ],
      periodLabel: "2026 年 3 月",
      periodStatus: "closed",
      period: "2026-03",
    });
    app.use(router);
    const html = await renderToString(app);

    assert.match(html, /class="status business-list-state"[^>]*>当前待收<\/span>/);
    assert.match(html, /class="status business-list-state status-settled"[^>]*>当前已收回<\/span>/);
    assert.match(html, /class="status business-list-state status-historical"[^>]*>关账时待收<\/span>/);
    assert.equal((html.match(/<details\b[^>]*class="[^"]*\bbusiness-status-details\b[^"]*"/g) ?? []).length, 3);
    assert.equal((html.match(/class="[^"]*\bcompact-status-trigger\b[^"]*"/g) ?? []).length, 3);
    assert.equal((html.match(/role="button" tabindex="0" aria-expanded="false"/g) ?? []).length, 3);
    assert.equal((html.match(/compact-status-trigger hidden-summary/g) ?? []).length, 3);
    assert.doesNotMatch(html, /column-action|settlement-progress/);
    assert.doesNotMatch(html, /业务月份|业务月份未提供/);
    assert.doesNotMatch(html, /精确来源与候选依据/);
  } finally {
    await server.close();
  }
});


test("open-item rows control a single expansion and collapse on each scope change", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefOpenItems.vue");
    const items = ["a", "b", "no-id"].map(id => ({ id, category_key: "payroll_payables", party: id, description: "工资", subject_id: id === "no-id" ? null : id, status: "partial", outstanding_fen: "200000", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0" }));
    const props = reactive({ items, period: "2026-09", periodLabel: "9月", periodStatus: "open", snapshotVersion: "v1", openItems: { categories: [{ key: "payroll_payables", direction: "payable", label: "待付工资", count: 3, unit: "笔", outstanding_fen: "600000" }], total_count: 3, cutoff_period: "2026-09", current_cutoff_period: "2026-09" } });
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] }); await router.push("/?company_id=a");
    let state;
    const host = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} });
    const wrapped = { ...component, ssrRender: undefined, setup(_actual, context) { state = component.setup(props, context); return state; }, render: () => h("div") };
    const app = host.createApp(wrapped, props); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
    const target = {}, event = key => ({ key, target, currentTarget: target, preventDefault() { this.prevented = true; } });
    state.itemKeydown(items[0], event("Enter")); assert.equal(state.expandedItemId.value, "a");
    state.itemKeydown(items[1], event(" ")); assert.equal(state.expandedItemId.value, "b");
    state.itemKeydown(items[1], { ...event("Enter"), target: {} }); assert.equal(state.expandedItemId.value, "b");
    state.toggleItem(items[2], event("Enter")); assert.equal(state.expandedItemId.value, "b");
    for (const change of [() => props.snapshotVersion = "v2", () => props.period = "2026-10", () => state.selectedCategoryKey.value = "different", () => router.push("/?company_id=b")]) {
      state.toggleItem(items[0], event("Enter")); assert.equal(state.expandedItemId.value, "a"); await change(); await nextTick(); assert.equal(state.expandedItemId.value, "");
    }
    app.unmount();
    const view = createSSRApp(component, props); view.use(router); const html = await renderToString(view);
    assert.equal((html.match(/role="button" tabindex="0"/g) ?? []).length, 2);
    assert.equal((html.match(/class="row-chevron/g) ?? []).length, 2);
    assert.doesNotMatch(html, /原金额|settlement-progress/);
  } finally { await server.close(); }
});
