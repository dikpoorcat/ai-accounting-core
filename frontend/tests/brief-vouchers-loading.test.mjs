import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let sequence = 0;
async function harness() {
  const source = readFileSync(new URL("../src/views/BriefView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-02" } });
  const environment = { Vue, route, calls: [], unmount: [] };
  const key = `briefVoucherLoading${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};
    export function instantiate() {
      const { computed, ref, shallowRef, watch } = environment.Vue;
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
      return { response, loading, sectionLoading, sectionErrors, loadData, loadMore, loadAllVouchers, openVoucher };
    }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  const scope = Vue.effectScope(), state = scope.run(() => module.instantiate());
  return { ...state, ...environment, close() { environment.unmount.forEach(callback => callback()); scope.stop(); } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const rows = (start, count) => Array.from({ length: count }, (_, index) => ({ voucher_version_id: `v${start + index}` }));
const response = (start, count, total = 45) => ({
  schema_version: 11, snapshot_version: "same-snapshot", read_context: { company_id: "a" },
  selected_period: { key: "2026-02" }, data: {
    month_state: "closed", owner_review_request: null,
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
  } finally { h.close(); }
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

test("opening a voucher outside the first page only replaces the focused record", async () => {
  const h = await harness();
  try {
    h.response.value = response(0, 20);
    const firstCollection = h.response.value.data.collections.vouchers;
    const target = h.openVoucher("v44");
    assert.equal(h.loading.value, false);
    assert.equal(h.response.value.data.collections.vouchers, firstCollection);
    assert.deepEqual(h.calls[0].args[4], { section: "vouchers", voucher_version_id: "v44" });
    const next = response(0, 20); next.data.focused_voucher = { voucher_version_id: "v44" };
    h.calls[0].resolve(next); await target;
    assert.equal(h.response.value.data.focused_voucher.voucher_version_id, "v44");
    assert.equal(h.response.value.data.collections.vouchers, firstCollection);
    assert.equal(h.route.query.voucher, undefined, "opening a local record must not trigger whole-page route loading");
  } finally { h.close(); }
});
