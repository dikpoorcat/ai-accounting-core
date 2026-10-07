import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";
import { createBriefOpenItemGrouping } from "./helpers/briefOpenItemGrouping.mjs";

let sequence = 0;
async function harness(contextValue = null) {
  const source = readFileSync(new URL("../src/views/BriefView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-02" } });
  const environment = { Vue, route, context: Vue.ref(contextValue), appendDashboardCollection, createBriefOpenItemGrouping, calls: [], contextCalls: [], unmount: [] };
  const key = `briefVoucherLoading${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};
    export function instantiate() {
      const { computed, ref, shallowReactive, shallowRef, watch } = environment.Vue;
      const { appendDashboardCollection, createBriefOpenItemGrouping } = environment;
      const useRoute = () => environment.route;
      const useRouter = () => ({ replace() {}, push() {} });
      const useDashboardContext = () => ({ context: environment.context, load: async () => null,
        refresh: () => new Promise((resolve, reject) => environment.contextCalls.push({ resolve, reject })) });
      const useDashboardSections = () => ({ activeSection: ref("overview"), focusSection() {} });
      const fetchDeferredBrief = (...args) => new Promise((resolve, reject) => environment.calls.push({ args, resolve, reject }));
      const onMounted = () => {};
      const onBeforeUnmount = callback => environment.unmount.push(callback);
      const dashboardErrorMessage = error => error.message;
      const isDashboardSnapshotChanged = () => false;
      ${source}
      return { response, data, selectedPeriod, error, vouchers, vouchersReady, activeSection, initializeVouchers, loading, sectionLoading, sectionErrors, loadData, loadMore, loadAllVouchers, openVoucher, indexVouchers, voucherPreviewIndex, focusedVoucherSelection, invalidateRequests, paginationScope, pausePages, refreshCurrent };
    }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  const scope = Vue.effectScope(), state = scope.run(() => module.instantiate());
  return { ...state, ...environment, close() { environment.unmount.forEach(callback => callback()); scope.stop(); } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const rows = (start, count) => Array.from({ length: count }, (_, index) => ({
  voucher_version_id: `v${start + index}`, number: String(start + index + 1), subject_id: `s${start + index}`,
  date: null, recognition: { period: "2026-02", label: "2026-02 · 按月确认" },
  type: "收款", state: "已入账", list_summary: "经营收款", summary: "经营收款",
  business_amount_label: "收款金额", business_amount_fen: "100", reverses_version_id: null,
  asset: null, asset_members: [], lines: [],
}));
const response = (start, count, total = 45) => ({
  schema_version: 14, snapshot_version: "same-snapshot", read_context: { company_id: "a" },
  selected_period: { key: "2026-02" }, data: {
    month_state: "closed", owner_review_request: null,
    ...(start === 0 ? { financial_position: { assets_fen: "32100" }, workforce_cost: { has_activity: true, total_fen: "77700" } } : {}),
    position: { month_result_fen: "12345" }, voucher_count: total, focused_voucher: null, focused_activity: null,
    collections: { vouchers: { items: rows(start, count), page: {
      total_count: total, filtered_count: total, returned_count: count,
      has_more: start + count < total, next_cursor: start + count < total ? `cursor-${start + count}` : null,
    } } },
  },
});

test("all vouchers loads the entire month through bounded pages using one snapshot", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20);
    h.vouchersReady.value = true;
    const all = h.loadAllVouchers();
    assert.equal(h.loading.value, false, "whole-page loading must stay off");
    assert.deepEqual(h.calls[0].args[4], { section: "vouchers", cursor: "cursor-20" });
    assert.equal(h.calls[0].args[3], "same-snapshot");
    h.calls[0].resolve(response(20, 20)); await tick();
    assert.equal(h.calls.length, 2);
    assert.equal(h.calls[1].args[4].cursor, "cursor-40");
    h.calls[1].resolve(response(40, 5)); await all;
    assert.deepEqual(h.response.value.data.collections.vouchers.items.map(item => item.voucher_version_id), rows(0, 45).map(item => item.voucher_version_id));
    assert.equal(h.response.value.data.collections.vouchers.page.has_more, false);
    assert.equal(h.response.value.data.position.month_result_fen, "12345");
    assert.equal(h.response.value.data.financial_position.assets_fen, "32100");
    assert.equal(h.response.value.data.workforce_cost.total_fen, "77700");
  } finally { h.close(); }
});

test("100/200/400 voucher pages keep a reactive array and update only new index keys", async () => {
  for (const total of [100, 200, 400]) {
    const h = await harness();
    try {
      const main = h.loadData("2026-02");
      h.calls[0].resolve(response(0, 20, total)); await main;
      const first = h.initializeVouchers();
      h.calls[1].resolve(response(0, 20, total)); await first;
      const originalItems = h.response.value.data.collections.vouchers.items, originalIndex = h.voucherPreviewIndex.value;
      assert.equal(Vue.isReactive(originalItems), true);
      const visible = Vue.computed(() => h.response.value.data.collections.vouchers.items.map(row => row.voucher_version_id));
      const rawIndex = Vue.toRaw(originalIndex);
      let indexWrites = 0;
      rawIndex.set = function (key, value) { indexWrites++; return Map.prototype.set.call(this, key, value); };
      rawIndex[Symbol.iterator] = () => { throw new Error("index must not be cloned or traversed during paging"); };
      for (let start = 20; start < total; start += 20) {
        const pending = h.loadMore("vouchers");
        h.calls.at(-1).resolve(response(start, 20, total)); await pending;
        assert.equal(h.response.value.data.collections.vouchers.items, originalItems);
        assert.equal(h.voucherPreviewIndex.value, originalIndex);
        assert.equal(visible.value.length, start + 20);
        assert.equal(visible.value.at(-1), `v${start + 19}`);
      }
      assert.equal(indexWrites, total - 20);
      h.indexVouchers(response(0, 20, total));
      h.indexVouchers({ data: { collections: { open_items: { items: [] } } } });
      assert.equal(indexWrites, total - 20, "repeated keys and unrelated pages do not write the index");
      assert.equal(h.voucherPreviewIndex.value, originalIndex);
    } finally { h.close(); }
  }
});

test("failed all-voucher loading preserves prior rows and resumes from the same cursor", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20);
    h.vouchersReady.value = true;
    const first = h.loadAllVouchers(); h.calls[0].reject(new Error("读取失败")); await first;
    assert.equal(h.response.value.data.collections.vouchers.items.length, 20);
    assert.equal(h.sectionErrors.value.vouchers, "读取失败");
    const retry = h.loadAllVouchers();
    assert.equal(h.calls[1].args[4].cursor, "cursor-20");
    h.calls[1].resolve(response(20, 25)); await retry;
    assert.equal(h.response.value.data.collections.vouchers.items.length, 45);
    assert.equal(h.sectionErrors.value.vouchers, "");
  } finally { h.close(); }
});

test("company changes cancel voucher paging and late replies cannot restore the old company", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20);
    h.vouchersReady.value = true;
    const all = h.loadAllVouchers();
    h.route.query.company_id = "b"; await Vue.nextTick();
    assert.equal(h.calls[0].args[2].aborted, true);
    h.calls[0].resolve(response(20, 20)); await all;
    assert.equal(h.calls.length, 1);
    assert.equal(h.response.value, null);
  } finally { h.close(); }
});

test("opening loaded and focused vouchers is local, repeats focus, and never guesses missing matches", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20);
    const firstCollection = h.response.value.data.collections.vouchers;
    h.response.value.data.focused_voucher = { voucher_version_id: "v44" };
    h.indexVouchers(h.response.value);
    h.openVoucher("v0");
    assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v0");
    h.openVoucher("v44");
    assert.equal(h.loading.value, false);
    assert.equal(h.response.value.data.collections.vouchers, firstCollection);
    assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v44");
    h.openVoucher("v44");
    assert.equal(h.focusedVoucherSelection.value, 3);
    assert.equal(h.calls.length, 0);
    h.openVoucher("missing");
    assert.match(h.sectionErrors.value.vouchers, /未找到.*对应凭证/);
    assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v44");
    assert.equal(h.response.value.data.collections.vouchers, firstCollection);
    assert.equal(h.route.query.voucher, undefined, "opening a local record must not trigger whole-page route loading");
  } finally { h.close(); }
});

test("activity continuation previews its exact vouchers without advancing voucher paging", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20); h.indexVouchers(h.response.value);
    h.response.value.data.collections.activity = { items: [{ key: "first" }], page: { has_more: true, next_cursor: "activity-next" } };
    const voucherCollection = h.response.value.data.collections.vouchers;
    const pending = h.loadMore("activity");
    const page = response(40, 2);
    page.data.collections.activity = { items: [{ key: "second", voucher_version_id: "v41" }], page: { has_more: false, next_cursor: null } };
    h.calls[0].resolve(page); await pending;
    assert.equal(h.response.value.data.collections.vouchers, voucherCollection);
    assert.equal(h.voucherPreviewIndex.value.get("v41").voucher_version_id, "v41");
    h.openVoucher("v41");
    assert.equal(h.calls.length, 1);
    assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v41");
  } finally { h.close(); }
});

test("refresh clears preview indexes even while retaining content and rejects late continuation", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20); h.indexVouchers(h.response.value);
    const pending = h.loadMore("vouchers");
    h.invalidateRequests(true);
    assert.equal(h.response.value.data.collections.vouchers.items.length, 20);
    assert.equal(h.voucherPreviewIndex.value.size, 0);
    assert.equal(h.calls[0].args[2].aborted, true);
    h.calls[0].resolve(response(20, 20)); await pending;
    assert.equal(h.voucherPreviewIndex.value.size, 0);
    const main = h.loadData("2026-02");
    const fresh = response(0, 1); fresh.snapshot_version = "new-snapshot";
    h.calls[1].resolve(fresh); await main;
    assert.deepEqual([...h.voucherPreviewIndex.value.keys()], ["v0"]);
  } finally { h.close(); }
});

test("an external voucher route is located in the first main request", async () => {
  const h = await harness();
  try {
    h.route.query.voucher = "44"; await Vue.nextTick();
    const pending = h.loadData("2026-02");
    assert.deepEqual(h.calls[0].args[4], { voucher_number: 44 });
    const fresh = response(0, 20); fresh.data.focused_voucher = { voucher_version_id: "v44" };
    h.calls[0].resolve(fresh); await pending;
    assert.equal(h.voucherPreviewIndex.value.has("v44"), true);
  } finally { h.close(); }
});

test("paired previews do not initialize the voucher-number list; first manual read replaces them", async () => {
  const h = await harness();
  try {
    const main = h.loadData("2026-02");
    const paired = response(20, 20);
    paired.data.collections.vouchers.page.next_cursor = "paired-date-cursor";
    h.calls[0].resolve(paired); await main;
    assert.equal(h.vouchersReady.value, false);
    assert.deepEqual(h.vouchers.value, []);
    h.openVoucher("v21");
    assert.equal(h.calls.length, 1, "precise local lookup does not request an independent list");
    const first = h.initializeVouchers();
    assert.deepEqual(h.calls[1].args[4], { section: "vouchers" });
    assert.equal(h.calls[1].args[3], "same-snapshot");
    const duplicate = await h.initializeVouchers();
    assert.equal(duplicate, false); assert.equal(h.calls.length, 2);
    h.calls[1].resolve(response(0, 20)); await first;
    assert.equal(h.vouchersReady.value, true);
    assert.deepEqual(h.vouchers.value.map(row => row.voucher_version_id), rows(0, 20).map(row => row.voucher_version_id));
    assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v21");
    assert.equal(h.voucherPreviewIndex.value.has("v21"), true);
    assert.equal(await h.initializeVouchers(), true);
    assert.equal(h.calls.length, 2, "same-scope revisit reuses the first page");
    const more = h.loadMore("vouchers");
    assert.equal(h.calls[2].args[4].cursor, "cursor-20");
    h.calls[2].resolve(response(20, 20)); await more;
  } finally { h.close(); }
});

test("failed voucher initialization retries without the paired cursor and preserves local previews", async () => {
  const h = await harness();
  try {
    h.response.value = response(30, 10); h.indexVouchers(h.response.value);
    const first = h.initializeVouchers();
    h.calls[0].reject(new Error("首屏失败")); await first;
    assert.equal(h.vouchersReady.value, false);
    assert.deepEqual(h.vouchers.value, []);
    assert.equal(h.voucherPreviewIndex.value.has("v30"), true);
    assert.equal(h.sectionErrors.value.vouchers, "首屏失败");
    const retry = h.initializeVouchers();
    assert.deepEqual(h.calls[1].args[4], { section: "vouchers" });
    h.calls[1].resolve(response(0, 20)); await retry;
    assert.equal(h.sectionErrors.value.vouchers, "");
  } finally { h.close(); }
});

test("all vouchers initializes first, then follows only independent number-order cursors", async () => {
  const h = await harness();
  try {
    h.response.value = response(30, 10);
    const all = h.loadAllVouchers();
    assert.deepEqual(h.calls[0].args[4], { section: "vouchers" });
    h.calls[0].resolve(response(0, 20)); await tick();
    assert.deepEqual(h.calls[1].args[4], { section: "vouchers", cursor: "cursor-20" });
    h.calls[1].resolve(response(20, 20)); await tick();
    h.calls[2].resolve(response(40, 5)); await all;
    assert.equal(h.vouchers.value.length, 45);
  } finally { h.close(); }
});

test("switching away cancels voucher initialization; late replies cannot mark its list ready", async () => {
  const h = await harness();
  try {
    h.response.value = response(30, 10);
    const first = h.initializeVouchers();
    h.pausePages("vouchers", h.paginationScope());
    assert.equal(h.calls[0].args[2].aborted, true);
    h.calls[0].resolve(response(0, 20)); await first;
    assert.equal(h.vouchersReady.value, false);
    assert.equal(h.response.value.data.collections.vouchers.items[0].voucher_version_id, "v30");
    const next = h.initializeVouchers();
    h.route.query.period = "2026-03"; await Vue.nextTick();
    h.calls[1].resolve(response(0, 20)); await next;
    assert.equal(h.vouchersReady.value, false);
    assert.equal(h.response.value, null);
  } finally { h.close(); }
});

test("leaving the activity module stops all-voucher paging and revisiting resumes the cached cursor", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20); h.vouchersReady.value = true;
    h.activeSection.value = "activity"; await Vue.nextTick();
    const all = h.loadAllVouchers();
    h.activeSection.value = "open-items"; await Vue.nextTick();
    assert.equal(h.calls[0].args[2].aborted, true);
    h.calls[0].resolve(response(20, 20)); await all;
    assert.equal(h.calls.length, 1);
    assert.equal(h.vouchers.value.length, 20);
    assert.equal(h.vouchersReady.value, true);
    h.activeSection.value = "activity"; await Vue.nextTick();
    const resumed = h.loadAllVouchers();
    assert.deepEqual(h.calls[1].args[4], { section: "vouchers", cursor: "cursor-20" });
    h.calls[1].resolve(response(20, 20)); await tick();
    h.calls[2].resolve(response(40, 5)); await resumed;
    assert.equal(h.vouchers.value.length, 45);
  } finally { h.close(); }
});

const currentContext = () => ({ current_company: { company_id: "a" }, periods: [{ key: "2026-02" }] });

async function mountWorkbench(parent) {
  const { descriptor } = parse(readFileSync(new URL("../src/components/brief/BriefActivityWorkbench.vue", import.meta.url), "utf8"));
  let source = ts.transpileModule(compileScript(descriptor, { id: "voucher-lifecycle", inlineTemplate: true }).content,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
  source = source.replace(/import \{([^}]+)\} from ["']vue["'];?/g, (_match, names) =>
    `const {${names.replace(/\bas\b/g, ":")}} = environment.Vue;`).replace(/import[^;]+;/g, "");
  const key = `voucherLifecycle${++sequence}`;
  globalThis[key] = { Vue, route: parent.route };
  source = `const environment = globalThis.${key}; const useRoute = () => environment.route;
    const fen = value => BigInt(value ?? 0), formatFen = value => value == null ? "待核对" : String(value);
    const BusinessStatusDetails = { render: () => null };\n${source}`;
  const { default: component } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  delete globalThis[key];
  const makeNode = (tag, text = "") => ({ tag, text, props: {}, children: [], parent: null });
  const root = makeNode("root");
  const renderer = Vue.createRenderer({
    createElement: tag => makeNode(tag), createText: text => makeNode("#text", text), createComment: () => makeNode("#comment"),
    insert(node, target, anchor) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
      const index = anchor ? target.children.indexOf(anchor) : -1;
      target.children.splice(index < 0 ? target.children.length : index, 0, node); node.parent = target;
    },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
    setText(node, text) { node.text = text; }, setElementText(node, text) { node.text = text; node.children = []; },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp(node, key, _previous, value) { node.props[key] = value; },
  });
  const app = renderer.createApp({ render() {
    const data = parent.data.value;
    return data ? Vue.h(component, {
      groups: [], items: [], activityCount: 0, period: parent.selectedPeriod.value,
      snapshotVersion: parent.response.value.snapshot_version, vouchers: parent.vouchers.value,
      vouchersReady: parent.vouchersReady.value, refreshing: parent.loading.value, active: parent.activeSection.value === "activity",
      voucherPreviewIndex: parent.voucherPreviewIndex.value, voucherCount: data.voucher_count,
      focusedVoucher: data.focused_voucher, focusedVoucherSelection: parent.focusedVoucherSelection.value,
      vouchersHasMore: parent.vouchersReady.value && data.collections.vouchers?.page.has_more,
      vouchersLoading: parent.sectionLoading.value.vouchers, vouchersError: parent.sectionErrors.value.vouchers,
      onInitializeVouchers: parent.initializeVouchers, onMoreVouchers: () => parent.loadMore("vouchers"),
      onAllVouchers: parent.loadAllVouchers, onPauseVouchers: () => parent.pausePages("vouchers", parent.paginationScope()),
      onRequestVoucher: parent.openVoucher,
    }) : Vue.h("p", parent.error.value);
  } });
  app.component("RouterLink", { render: () => null });
  app.mount(root);
  const allNodes = () => { const result = []; const visit = node => { result.push(node); node.children.forEach(visit); }; visit(root); return result; };
  const text = node => node.text + node.children.map(text).join("");
  return {
    text: (node = root) => text(node), cards: () => allNodes().filter(node => node.tag === "article" && node.props.class?.split(" ").includes("voucher-card")),
    async click(label) {
      const button = allNodes().find(node => node.tag === "button" && (text(node) === label || node.props["aria-label"] === label));
      assert(button, `button ${label} was not rendered`); assert(!button.props.disabled, `button ${label} is disabled`);
      button.props.onClick({ target: button, currentTarget: button }); await Vue.nextTick();
    },
    close() { app.unmount(); },
  };
}

async function readyParent(total = 45) {
  const h = await harness(currentContext());
  const main = h.loadData("2026-02"); h.calls.at(-1).resolve(response(0, 20, total)); await main;
  const first = h.initializeVouchers(); h.calls.at(-1).resolve(response(0, 20, total)); await first;
  h.activeSection.value = "activity"; await Vue.nextTick();
  return h;
}

test("rendered voucher modes retain pages and all rows only after the same-snapshot refresh gate succeeds", async () => {
  for (const mode of ["paged", "all"]) {
    const h = await readyParent(); let workbench;
    try {
      const all = h.loadAllVouchers(); h.calls.at(-1).resolve(response(20, 20)); await tick(); h.calls.at(-1).resolve(response(40, 5)); await all;
      workbench = await mountWorkbench(h); await workbench.click("按凭证");
      if (mode === "paged") await workbench.click("下一页");
      else await workbench.click("改为全部显示凭证");
      const originalRows = h.vouchers.value, oldIndex = h.voucherPreviewIndex.value, before = h.calls.length;
      const pending = h.refreshCurrent(true); await Vue.nextTick();
      assert.notStrictEqual(h.voucherPreviewIndex.value, oldIndex); assert.equal(h.voucherPreviewIndex.value.size, 0);
      h.calls.at(-1).resolve(response(0, 20)); await tick();
      assert.equal(h.loading.value, true, "new main response cannot finish refresh before context validation");
      h.contextCalls.at(-1).resolve(currentContext()); await pending; await Vue.nextTick();
      assert.strictEqual(h.vouchers.value, originalRows); assert.equal(h.voucherPreviewIndex.value.size, 45);
      assert.equal(h.calls.length, before + 1, "same-snapshot refresh must not reread independent voucher pages");
      assert.equal(workbench.cards().length, mode === "all" ? 45 : 20);
      assert.match(workbench.text(), mode === "all" ? /已加载 45/ : /2 \/ 3/);
      if (mode === "paged") assert.match(workbench.text(workbench.cards()[0]), /凭证 21/);
    } finally { workbench?.close(); h.close(); }
  }
});

test("refresh during initialization aborts the old request and resumes the manual first page after validation", async () => {
  const h = await harness(currentContext()); let workbench;
  try {
    const main = h.loadData("2026-02"); h.calls[0].resolve(response(30, 10)); await main;
    h.activeSection.value = "activity"; workbench = await mountWorkbench(h); await workbench.click("按凭证");
    const oldFirst = h.calls.at(-1), pending = h.refreshCurrent(true); await Vue.nextTick();
    assert.equal(oldFirst.args[2].aborted, true);
    oldFirst.resolve(response(0, 20)); await tick(); assert.equal(h.vouchersReady.value, false);
    h.calls.at(-1).resolve(response(30, 10)); await tick();
    assert.equal(h.calls.length, 3, "refresh gate must finish before restarting initialization");
    h.contextCalls.at(-1).resolve(currentContext()); await pending; await Vue.nextTick();
    assert.equal(h.calls.length, 4); assert.deepEqual(h.calls.at(-1).args[4], { section: "vouchers" });
    h.calls.at(-1).resolve(response(0, 20)); await tick();
    assert.equal(workbench.cards().length, 20); assert.equal(h.vouchersReady.value, true);
  } finally { workbench?.close(); h.close(); }
});

test("all-voucher refresh resumes one bounded chain during initialization or continuation", async () => {
  for (const stage of ["initialization", "continuation"]) {
    const h = stage === "continuation" ? await readyParent() : await harness(currentContext()); let workbench;
    try {
      if (stage === "initialization") {
        const main = h.loadData("2026-02"); h.calls.at(-1).resolve(response(30, 10)); await main;
        h.activeSection.value = "activity";
      }
      workbench = await mountWorkbench(h); await workbench.click("按凭证"); await workbench.click("改为全部显示凭证");
      const oldRequest = h.calls.at(-1), before = h.calls.length, pending = h.refreshCurrent(true);
      await Vue.nextTick(); assert.equal(oldRequest.args[2].aborted, true);
      const mainRequest = h.calls.at(-1); oldRequest.resolve(stage === "initialization" ? response(0, 20) : response(20, 20));
      await tick(); mainRequest.resolve(response(30, 10)); await tick();
      assert.equal(h.calls.length, before + 1, "old all loop and incomplete gate cannot read new pages");
      h.contextCalls.at(-1).resolve(currentContext()); await pending; await Vue.nextTick();
      assert.equal(h.calls.length, before + 2, "refresh completion starts exactly one pending request");
      if (stage === "initialization") {
        assert.deepEqual(h.calls.at(-1).args[4], { section: "vouchers" });
        h.calls.at(-1).resolve(response(0, 20)); await tick();
        assert.equal(h.calls.length, before + 3, "readiness and resume watchers must share one all-voucher chain");
      }
      assert.equal(h.calls.at(-1).args[4].cursor, "cursor-20");
      h.calls.at(-1).resolve(response(20, 20)); await tick();
      assert.equal(h.calls.at(-1).args[4].cursor, "cursor-40");
      h.calls.at(-1).resolve(response(40, 5)); await tick();
      assert.equal(workbench.cards().length, 45); assert.equal(h.calls.length, before + (stage === "initialization" ? 4 : 3));
      assert.doesNotMatch(workbench.text(), /尚未读取完/);
    } finally { workbench?.close(); h.close(); }
  }
});

test("pending rendered voucher page resumes from its retained cursor after refresh or module revisit", async () => {
  for (const transition of ["refresh", "revisit"]) {
    const h = await readyParent(); let workbench;
    try {
      workbench = await mountWorkbench(h); await workbench.click("按凭证"); await workbench.click("下一页");
      const oldPage = h.calls.at(-1), originalRows = h.vouchers.value;
      assert.match(workbench.text(), /正在读取第 2 页/);
      if (transition === "refresh") {
        const pending = h.refreshCurrent(true); await Vue.nextTick();
        h.calls.at(-1).resolve(response(0, 20)); h.contextCalls.at(-1).resolve(currentContext()); await pending; await Vue.nextTick();
      } else {
        h.activeSection.value = "open-items"; await Vue.nextTick();
        assert.equal(h.calls.at(-1), oldPage);
        h.activeSection.value = "activity"; await Vue.nextTick();
      }
      assert.equal(oldPage.args[2].aborted, true); assert.notEqual(h.calls.at(-1), oldPage);
      const resumed = h.calls.at(-1); assert.equal(resumed.args[4].cursor, "cursor-20");
      oldPage.resolve(response(20, 20)); await tick();
      assert.equal(h.vouchers.value.length, 20); assert.equal(h.sectionLoading.value.vouchers, true, "late finally cannot clear resumed loading");
      resumed.resolve(response(20, 20)); await tick();
      assert.strictEqual(h.vouchers.value, originalRows); assert.equal(h.vouchers.value.length, 40);
      assert.match(workbench.text(), /2 \/ 3/); assert.doesNotMatch(workbench.text(), /正在读取第/);
      assert.equal(workbench.cards().length, 20);
    } finally { workbench?.close(); h.close(); }
  }
});

test("refresh drops the independent list after snapshot change, failed gates, or company change", async () => {
  for (const failure of ["snapshot", "context", "main", "company"]) {
    const h = await readyParent(); let workbench;
    try {
      workbench = await mountWorkbench(h); await workbench.click("按凭证");
      const oldRows = h.vouchers.value, before = h.calls.length, pending = h.refreshCurrent(true);
      const fresh = response(0, 20);
      if (failure === "snapshot") fresh.snapshot_version = "new-snapshot";
      if (failure === "company") { h.route.query.company_id = "b"; await Vue.nextTick(); }
      if (failure === "main") h.calls.at(-1).reject(new Error("main failed")); else h.calls.at(-1).resolve(fresh);
      h.contextCalls.at(-1).resolve(failure === "context" ? { current_company: { company_id: "b" }, periods: [] } : currentContext());
      await pending; await Vue.nextTick();
      assert.equal(h.vouchersReady.value, false); assert.notStrictEqual(h.vouchers.value, oldRows);
      assert.equal(h.calls.length, before + 1, "invalidated or failed refresh cannot restart voucher reads");
      assert.equal(workbench.cards().length, 0);
      if (failure === "snapshot") assert.match(workbench.text(), /按业务/); else assert.equal(h.response.value, null);
    } finally { workbench?.close(); h.close(); }
  }
});

test("refreshing default business mode and precise focus never initializes a voucher list", async () => {
  for (const precise of [false, true]) {
    const h = await harness(currentContext()); let workbench;
    try {
      const main = h.loadData("2026-02"), first = response(precise ? 30 : 0, precise ? 10 : 20);
      h.calls.at(-1).resolve(first); await main; h.activeSection.value = "activity";
      if (precise) h.openVoucher("v35");
      workbench = await mountWorkbench(h); const before = h.calls.length;
      const pending = h.refreshCurrent(true), fresh = response(0, 20);
      h.calls.at(-1).resolve(fresh); h.contextCalls.at(-1).resolve(currentContext()); await pending; await Vue.nextTick();
      assert.equal(h.calls.length, before + 1); assert.equal(h.vouchersReady.value, false);
      assert.equal(workbench.cards().length, precise ? 1 : 0);
      if (precise) {
        assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v35");
        assert.equal(h.voucherPreviewIndex.value.has("v35"), true);
        assert.match(workbench.cards()[0].props.class, /is-open/); assert.match(workbench.text(), /凭证 36/);
      }
    } finally { workbench?.close(); h.close(); }
  }
});

test("same-version route focus preserves manual voucher mode while a fresh different target takes priority", async () => {
  for (const mode of ["paged", "all"]) for (const changedTarget of [false, true]) {
    const h = await harness(currentContext()); let workbench;
    try {
      h.route.query.voucher = "36"; await Vue.nextTick();
      const main = h.loadData("2026-02"), initial = response(30, 10);
      initial.data.focused_voucher = rows(35, 1)[0];
      h.calls.at(-1).resolve(initial); await main;
      const first = h.initializeVouchers(); h.calls.at(-1).resolve(response(0, 20)); await first;
      const all = h.loadAllVouchers(); h.calls.at(-1).resolve(response(20, 20)); await tick(); h.calls.at(-1).resolve(response(40, 5)); await all;
      h.activeSection.value = "activity"; workbench = await mountWorkbench(h);
      await workbench.click("按凭证");
      if (mode === "paged") await workbench.click("下一页");
      else await workbench.click("改为全部显示凭证");
      const originalFocus = h.response.value.data.focused_voucher, before = h.calls.length;
      const pending = h.refreshCurrent(true), fresh = response(30, 10);
      fresh.data.focused_voucher = rows(changedTarget ? 1 : 35, 1)[0];
      assert.notStrictEqual(fresh.data.focused_voucher, originalFocus);
      h.calls.at(-1).resolve(fresh); h.contextCalls.at(-1).resolve(currentContext()); await pending; await Vue.nextTick();
      assert.equal(h.calls.length, before + 1, "restoring route focus must not reread the independent list");
      if (changedTarget) {
        assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v1");
        const selected = workbench.cards().find(card => card.props.class.includes("is-open"));
        assert(selected); assert.match(workbench.text(selected), /凭证 2/);
        if (mode === "paged") assert.match(workbench.text(), /1 \/ 3/);
      } else {
        assert.strictEqual(h.response.value.data.focused_voucher, originalFocus);
        assert.equal(workbench.cards().length, mode === "paged" ? 5 : 45);
        if (mode === "paged") {
          assert.match(workbench.text(), /3 \/ 3/); assert.match(workbench.text(workbench.cards()[0]), /凭证 41/);
          assert.equal(workbench.cards().some(card => card.props.class.includes("is-open")), false);
        }
      }
      if (mode === "all") assert.equal(workbench.cards().length, 45);
    } finally { workbench?.close(); h.close(); }
  }
});
