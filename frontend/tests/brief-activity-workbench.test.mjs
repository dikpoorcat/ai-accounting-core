import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp, createRenderer, h, nextTick, reactive, shallowReactive, ssrContextKey } from "vue";
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
const activity = number => ({ key: `activity-${number}`, subject_id: `business-${number}`, group: "funds", party: "甲客户", title: "经营收款", description: "经营款", date: null, recognition: { period: "2026-03" }, state: "已入账", amount_label: "收款金额", amount_fen: "100", voucher_version_id: `voucher-${number}` });

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
    await t.test("business rows keep their own exact occurrence amounts and neutral corrections", async () => {
      const items = ["0", null, "-800000", "600000", "9007199254740993"].map((amount_fen, index) => ({ ...activity(index + 1), subject_id: "same-business", amount_fen, state: index === 2 ? "更正原业务" : "已入账" }));
      const app = createSSRApp(component, { ...baseProps(), groups: [{ key: "funds", label: "收付款", event_count: 5 }], items }); app.use(await router());
      const html = await renderToString(app);
      for (const amount of ["¥0.00", "待核对", "−¥8,000.00", "¥6,000.00", "¥90,071,992,547,409.93"]) assert(html.includes(amount));
      assert.match(html, /class="state correction"[^>]*>更正原业务/);
      assert.doesNotMatch(html, /owner-activity-summary/);
    });
    await t.test("one active preview uses exact business amount and complete lines, including zero, unknown and reversal", async () => {
      for (const amount of ["0", null]) {
        const target = { ...voucher(2), business_amount_fen: amount, business_amount_label: "退款金额", reverses_version_id: "original", state: "已冲正" };
        const items = [activity(1), activity(2)];
        const props = { ...baseProps(), groups: [{ key: "funds", label: "收付款", event_count: 2 }], items, voucherPreviewIndex: new Map([["voucher-1", voucher(1)], ["voucher-2", target]]) };
        const wrapped = { ...component, setup(actual, context) { const state = component.setup(actual, context); state.showPreview(items[0]); state.showPreview(items[1]); return state; } };
        const app = createSSRApp(wrapped, props); app.use(await router());
        const html = await renderToString(app);
        assert.equal((html.match(/role="tooltip"/g) ?? []).length, 1);
        assert.match(html, /凭证 2 · 2026-03 · 按月确认/);
        assert.match(html, /退款金额/);
        assert.ok(html.includes(amount === null ? "暂无法确定" : "¥0.00"));
        assert.match(html, /银行存款/);
        assert.match(html, /应收账款/);
        assert.match(html, /已冲正/);
        assert.match(html, /class="[^"]*correction[^\"]*event-voucher-preview/);
        assert.doesNotMatch(html, /查看对应凭证/);
      }
    });
    await t.test("row keyboard toggle isolates controls and voucher hover/click emit no detail load", async () => {
      const events = [], items = [activity(1)];
      const index = shallowReactive(new Map([["voucher-1", voucher(1)]]));
      const props = reactive({ ...baseProps(), voucherPreviewIndex: index, groups: [{ key: "funds", label: "收付款", event_count: 1 }], items, focusedVoucherSelection: 0, onRequestVoucher: id => events.push(id) });
      let state;
      const host = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} });
      const wrapped = { ...component, ssrRender: undefined, setup(_actual, context) { state = component.setup(props, context); return state; }, render: () => h("div") };
      const app = host.createApp(wrapped, props); app.use(await router()); app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
      const oldElement = globalThis.Element;
      globalThis.Element = class { constructor(control = false) { this.control = control; } closest() { return this.control ? this : null; } };
      try {
        const row = new Element(), button = new Element(true);
        let prevented = 0;
        state.businessKeydown(items[0], { target: row, currentTarget: row, key: "Enter", preventDefault: () => prevented++ });
        assert.equal(state.expandedBusinessKey.value, items[0].key);
        state.toggleBusiness(items[0], { target: button });
        state.businessKeydown(items[0], { target: button, currentTarget: row, key: " ", preventDefault: () => prevented++ });
        assert.equal(state.expandedBusinessKey.value, items[0].key);
        assert.equal(prevented, 1);
        state.businessKeydown(items[0], { target: row, currentTarget: row, key: " ", preventDefault: () => prevented++ });
        assert.equal(state.expandedBusinessKey.value, "");
        state.showPreview(items[0]);
        assert.equal(state.previewVoucher.value.number, "1");
        assert.deepEqual(events, []);
        state.openBusinessVoucher(items[0]);
        assert.deepEqual(events, ["voucher-1"]);
        assert.equal(state.previewVoucher.value, undefined);
        const oldWindow = globalThis.window, oldDocument = globalThis.document;
        let measurements = 0, bounds = { top: 700, height: 220 }, anchor = { top: 850, height: 32 };
        globalThis.window = { innerHeight: 900, matchMedia: () => ({ matches: false }) };
        globalThis.document = { getElementById() { measurements++; return { previousElementSibling: { getBoundingClientRect: () => anchor }, getBoundingClientRect: () => bounds }; } };
        try {
          assert.equal(measurements, 0, "no measurements until a preview opens");
          await state.showPreview(items[0]);
          assert.equal(state.previewPosition.value.offset, -32, "bottom overflow is moved inside the viewport with a 12px margin");
          assert.equal(state.previewPosition.value.arrowTop, "198px", "arrow stays level with the button center");
          const indexedPositioning = state.showPreview(items[0]);
          index.set("voucher-2", voucher(2)); await indexedPositioning;
          assert.equal(state.previewVoucher.value.number, "1", "unrelated incremental keys preserve the visible preview");
          assert.equal(state.previewBusinessKey.value, items[0].key);
          assert.equal(state.previewPosition.value.offset, -32, "unrelated keys do not invalidate pending positioning");
          bounds = { top: -20, height: 220 }; anchor = { top: 50, height: 32 };
          await state.showPreview(items[0]);
          assert.deepEqual(state.previewPosition.value, { offset: 32, arrowTop: "54px" });
          bounds = { top: 200, height: 220 }; anchor = { top: 294, height: 32 };
          await state.showPreview(items[0]);
          assert.equal(state.previewPosition.value.offset, 0, "a visible preview retains its original position");
          const before = measurements, pending = state.showPreview(items[0]); state.clearPreview(); await pending;
          assert.equal(measurements, before, "moving away cancels the pending measurement");
          const oldPositioning = state.showPreview(items[0]), latestPositioning = state.showPreview(items[0]);
          await Promise.all([oldPositioning, latestPositioning]);
          assert.equal(measurements, before + 1, "only the latest hover or focus may measure and position");
          const changing = state.showPreview(items[0]); props.snapshotVersion = "new-snapshot";
          await changing;
          assert.equal(measurements, before + 1, "selection changes cancel late positioning");
          assert.equal(state.previewVoucher.value, undefined);
          await state.showPreview(items[0]);
          const replacedPositioning = state.showPreview(items[0]), beforeReplacement = measurements;
          props.voucherPreviewIndex = shallowReactive(new Map([["voucher-1", voucher(1)]]));
          await replacedPositioning;
          assert.equal(state.previewVoucher.value, undefined, "rebuilding the index clears the old preview");
          assert.equal(measurements, beforeReplacement, "a replaced index rejects pending positioning");
        } finally { globalThis.window = oldWindow; globalThis.document = oldDocument; }
        props.focusedVoucher = voucher(1); props.focusedVoucherSelection++;
        await nextTick();
        assert.equal(state.mode.value, "voucher");
        state.mode.value = "business";
        props.focusedVoucherSelection++;
        await nextTick();
        assert.equal(state.mode.value, "voucher");
        state.showPreview(items[0]); props.snapshotVersion = "v2";
        await nextTick();
        assert.equal(state.previewVoucher.value, undefined);
        assert.equal(state.expandedBusinessKey.value, "");
      } finally { globalThis.Element = oldElement; app.unmount(); }
    });
  } finally { await server.close(); }
});
