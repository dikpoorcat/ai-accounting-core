import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

let sequence = 0;
const flush = async () => { for (let index = 0; index < 4; index++) { await Vue.nextTick(); } };
const page = { total_count: 50, filtered_count: 50, returned_count: 1, has_more: true, next_cursor: "next" };
function response(filter = "all", employee = "first") {
  return { snapshot_version: "version", selected_period: { key: "2026-09" }, data: {
    employee_filter: filter, employee_id: null, employees: { registered_count: 50 }, workforce_cost: { total_fen: "1234" },
    collections: { employees: { items: [{ employee_id: employee }], page: { ...page } },
      labor_sources: { items: [{ source_id: "labor" }], page: { ...page } } },
  } };
}
async function harness() {
  const calls = [], key = `employeeFilters${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-09" } });
  const context = Vue.ref({ current_company: { company_id: "company-a" }, periods: [{ key: "2026-09" }] });
  globalThis[key] = { Vue, route, context, calls, appendDashboardCollection };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  const source = readFileSync(new URL("../src/views/EmployeesView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const prelude = `const env = globalThis.${key};
    const { computed, nextTick, ref, watch } = env.Vue;
    const { appendDashboardCollection } = env;
    const useRoute = () => env.route;
    const useRouter = () => ({ push: async target => { env.route.query = target.query; }, replace: async target => { env.route.query = target.query; } });
    const useDashboardContext = () => ({ context: env.context, load: async () => env.context.value, refresh: async () => env.context.value });
    const useDashboardSections = () => ({ activeSection: ref('employees-overview'), focusSection() {} });
    const fetchEmployeesDashboard = (period, signal, query) => new Promise((resolve, reject) => env.calls.push({ period, signal, query, resolve, reject }));
    const onMounted = () => {}; const onBeforeUnmount = () => {};
    const dashboardErrorMessage = error => error.message; const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fen = BigInt; const formatFen = String;
  `;
  const { outputText } = ts.transpileModule(prelude + source + "\ninitialized = true; export { response, loading, filter, pageLoading, pageErrors, loadMore, loadPeriod };", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const scope = Vue.effectScope();
  const view = await scope.run(() => import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`));
  view.response.value = response();
  return { ...view, route, calls, close() { scope.stop(); delete globalThis[key]; } };
}

test("employee filters retain overview and labor while replacing only the server-filtered list", async () => {
  const h = await harness();
  try {
    const summary = h.response.value.data.employees, labor = h.response.value.data.collections.labor_sources;
    h.filter.value = "payroll"; await flush();
    assert.equal(h.loading.value, false);
    assert.equal(h.response.value.data.employees, summary);
    assert.equal(h.response.value.data.collections.labor_sources, labor);
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.calls[0].query, { section: "employees", employee_filter: "payroll", expected_version: "version" });
    const filtered = response("payroll", "matching"); filtered.data.collections.employees.page.filtered_count = 27;
    h.calls[0].resolve(filtered); await flush();
    assert.equal(h.response.value.data.collections.employees.page.filtered_count, 27);
    assert.equal(h.response.value.data.collections.employees.items[0].employee_id, "matching");
    assert.equal(h.response.value.data.employees, summary);
    assert.equal(h.response.value.data.collections.labor_sources, labor);
  } finally { h.close(); }
});

test("employee filter retries stay local and company changes discard all old results", async () => {
  const h = await harness();
  try {
    h.filter.value = "payroll"; await flush();
    h.filter.value = "ended"; await flush();
    assert.equal(h.calls[0].signal.aborted, true);
    h.calls[0].resolve(response("payroll", "old")); await flush();
    assert.deepEqual(h.response.value.data.collections.employees.items, []);
    h.calls[1].reject(new Error("名单暂不可用")); await flush();
    assert.equal(h.pageErrors.value["employees:"], "名单暂不可用");
    const retry = h.loadMore();
    assert.equal(h.calls[2].query.section, "employees");
    assert.equal(h.calls[2].query.cursor, undefined);
    h.calls[2].resolve(response("ended", "current")); await retry;
    h.filter.value = "unknown"; await flush();
    h.route.query.company_id = "company-b"; await flush();
    assert.equal(h.calls[3].signal.aborted, true);
    h.calls[3].resolve(response("unknown", "late")); await flush();
    assert.equal(h.response.value, null);
  } finally { h.close(); }
});

test("an expired employee-list snapshot refreshes the full page before continuing", async () => {
  const h = await harness();
  try {
    h.filter.value = "payroll"; await flush();
    h.calls[0].reject(Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" })); await flush();
    assert.equal(h.response.value, null);
    assert.equal(h.loading.value, true);
    assert.equal(h.calls[1].query.section, undefined);
    assert.equal(h.calls[1].query.employee_filter, "payroll");
    const fresh = response("payroll", "fresh"); fresh.snapshot_version = "fresh-version";
    h.calls[1].resolve(fresh); await flush();
    assert.equal(h.response.value.snapshot_version, "fresh-version");
    assert.equal(h.loading.value, false);
  } finally { h.close(); }
});

test("employee and labor continuations retain reactive arrays and discard late pages on filter changes", async () => {
  const h = await harness();
  try {
    for (const section of ["employees", "labor_sources"]) {
      const original = h.response.value.data.collections[section].items;
      const renderedCount = Vue.computed(() => h.response.value.data.collections[section].items.length);
      assert.equal(renderedCount.value, 1);
      const pending = h.loadMore(section);
      const next = response("all", "next-employee");
      next.data.collections.labor_sources.items = [{ source_id: "next-labor" }];
      next.data.collections[section].page.next_cursor = "following";
      h.calls.at(-1).resolve(next); await pending;
      assert.equal(h.response.value.data.collections[section].items, original);
      assert.equal(renderedCount.value, 2);
      assert.equal(h.response.value.data.collections[section].page.next_cursor, "following");
      const late = h.loadMore(section), request = h.calls.at(-1);
      h.filter.value = section === "employees" ? "payroll" : "ended"; await flush();
      assert.equal(request.signal.aborted, true);
      request.resolve(response("all", "late")); await late;
      assert.equal(original.length, 2);
      const filtered = response(h.filter.value, "filtered");
      h.calls.at(-1).resolve(filtered); await flush();
    }
  } finally { h.close(); }
});
