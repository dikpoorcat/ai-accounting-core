import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

let sequence = 0;
async function harness() {
  const source = readFileSync(new URL("../src/views/BriefView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-02" } });
  const environment = { Vue, route, appendDashboardCollection, calls: [], unmount: [] };
  const key = `briefVoucherLoading${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};
    export function instantiate() {
      const { computed, ref, shallowReactive, shallowRef, watch } = environment.Vue;
      const { appendDashboardCollection } = environment;
      const useRoute = () => environment.route;
      const useRouter = () => ({ replace() {}, push() {} });
      const useDashboardContext = () => ({ context: ref(null), load: async () => null, refresh: async () => null });
      const useDashboardSections = () => ({ activeSection: ref("overview"), focusSection() {} });
      const fetchDeferredBrief = (...args) => new Promise((resolve, reject) => environment.calls.push({ args, resolve, reject }));
      const onMounted = () => {};
      const onBeforeUnmount = callback => environment.unmount.push(callback);
      const dashboardErrorMessage = error => error.message;
      const isDashboardSnapshotChanged = () => false;
      ${source}
      return { response, loading, sectionLoading, sectionErrors, loadData, loadMore, loadAllVouchers, openVoucher, indexVouchers, voucherPreviewIndex, focusedVoucherSelection, invalidateRequests };
    }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  const scope = Vue.effectScope(), state = scope.run(() => module.instantiate());
  return { ...state, ...environment, close() { environment.unmount.forEach(callback => callback()); scope.stop(); } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const rows = (start, count) => Array.from({ length: count }, (_, index) => ({ voucher_version_id: `v${start + index}` }));
const response = (start, count, total = 45) => ({
  schema_version: 13, snapshot_version: "same-snapshot", read_context: { company_id: "a" },
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
