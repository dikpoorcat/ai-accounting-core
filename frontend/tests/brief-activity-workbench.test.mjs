import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const voucher = number => ({
  number: String(number), voucher_version_id: `voucher-${number}`, subject_id: `business-${number}`,
  reverses_version_id: null, date: null, recognition: { period: "2026-03", label: "2026-03 · 按月确认" },
  type: "收款", kind: "collection", state: "已入账", group: "funds", summary: "收到经营款", list_summary: "经营收款",
  amount_fen: "9007199254740993", business_amount_fen: "9007199254740993", business_amount_label: "收款金额",
  asset: null, asset_members: [], lines: [
    { line_number: 1, code: "1002", account: "银行存款", debit_fen: "9007199254740993", credit_fen: "0", party: "", source_label: "", parties: [], party_state: "not_applicable" },
    { line_number: 2, code: "1122", account: "应收账款", debit_fen: "0", credit_fen: "9007199254740993", party: "多位客户", source_label: "经营收款", party_state: "multiple", parties: [{ id: "a", name: "甲客户", amount_fen: "100" }, { id: "b", name: "乙客户", amount_fen: "9007199254740893" }] },
  ],
});
const baseProps = () => ({ groups: [], items: [], activityCount: 0, period: "2026-03", snapshotVersion: "v1", vouchers: Array.from({ length: 20 }, (_, i) => voucher(i + 1)), voucherCount: 45, vouchersHasMore: true });

async function router() {
  const result = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }, { path: "/assets", name: "assets", component: {} }] });
  await result.push("/?company_id=co&period=2026-03");
  return result;
}

test("voucher workbench preserves precise focus, complete paging, and integer amounts", async t => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefActivityWorkbench.vue");
    await t.test("focused voucher is opened outside the loaded page with exact totals and multiple parties", async () => {
      const focused = voucher(999);
      const app = createSSRApp(component, { ...baseProps(), focusedVoucher: focused });
      app.use(await router());
      const html = await renderToString(app);
      assert.match(html, /id="selected-voucher"[^>]*class="voucher-card is-open"/);
      assert.match(html, /凭证 999/);
      assert.match(html, /按月确认/);
      assert.match(html, /甲客户 · ¥1.00/);
      assert.match(html, /乙客户/);
      assert.match(html, /借贷合计/);
      assert.match(html, /¥90,071,992,547,409.93/);
      assert.match(html, /已加载 20 \/ 本月 45 张凭证/);
      assert.equal((html.match(/class="voucher-card/g) ?? []).length, 21);
      assert.doesNotMatch(html, /关联凭据|技术详情|voucher-999|business-999/);
    });
    await t.test("next page waits for actual rows, retains a failed target, and all mode requests full loading", async () => {
      const events = [];
      const props = reactive({ ...baseProps(), onMoreVouchers: () => events.push("more"), onAllVouchers: () => events.push("all") });
      let state;
      const host = createRenderer({
        createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
        insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null,
        nextSibling: () => null, patchProp() {},
      });
      const wrapped = { ...component, ssrRender: undefined, setup(actual, context) { state = component.setup(actual, context); return state; }, render: () => h("div") };
      const app = host.createApp(wrapped, props);
      app.use(await router());
      app.provide(ssrContextKey, { modules: new Set() });
      app.mount({});
      state.changeVoucherPage(2);
      assert.equal(state.voucherPage.value, 1);
      assert.equal(state.pendingVoucherPage.value, 2);
      assert.equal(state.visibleVouchers.value.length, 20);
      assert.deepEqual(events, ["more"]);
      props.vouchers.push(...Array.from({ length: 20 }, (_, i) => voucher(i + 21)));
      await nextTick();
      assert.equal(state.voucherPage.value, 2);
      assert.equal(state.visibleVouchers.value[0].number, "21");
      state.changeVoucherPage(3);
      assert.equal(state.pendingVoucherPage.value, 3);
      state.retryVouchers();
      assert.equal(state.voucherPage.value, 2);
      assert.deepEqual(events, ["more", "more", "more"]);
      state.toggleVoucherDisplayMode();
      assert.equal(state.voucherDisplayMode.value, "all");
      assert.equal(state.visibleVouchers.value.length, 40);
      assert.equal(events.at(-1), "all");
      props.vouchers.push(...Array.from({ length: 5 }, (_, i) => voucher(i + 41)));
      await nextTick();
      assert.equal(state.visibleVouchers.value.length, 45);
      state.toggleVoucherDisplayMode();
      state.changeVoucherPage(3);
      assert.equal(state.voucherPage.value, 3);
      assert.equal(state.visibleVouchers.value.length, 5);
      app.unmount();
    });
  } finally { await server.close(); }
});
