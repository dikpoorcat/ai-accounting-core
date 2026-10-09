import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

let sequence = 0;
const flush = async () => { for (let index = 0; index < 4; index++) { await Vue.nextTick(); } };
const page = { total_count: 50, filtered_count: 50, returned_count: 1, has_more: true, next_cursor: "next" };
const employmentFilters = ["employment_active", "employment_unpaid_leave", "employment_departed", "employment_unknown"];
const employeeFilters = ["all", "in_period", "ended", "unknown", ...employmentFilters];
function response(filter = "employment_active", employee = "first") {
  return { snapshot_version: "version", selected_period: { key: "2026-09" }, data: {
    employee_filter: filter, employee_id: null, employees: { registered_count: 50 }, workforce_cost: { total_fen: "1234" },
    collections: { employees: { items: [{ employee_id: employee }], page: { ...page } },
      labor_sources: { items: [{ source_id: "labor" }], page: { ...page } } },
  } };
}
async function harness(query = {}) {
  const calls = [], key = `employeeFilters${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-09", ...query } });
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
  const { outputText } = ts.transpileModule(prelude + source + "\ninitialized = true; export { response, loading, filter, displayMode, pageLoading, pageErrors, loadMore, loadPeriod, selectPeriod, paginationScope };", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const scope = Vue.effectScope();
  const view = await scope.run(() => import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`));
  view.response.value = response(view.filter.value);
  return { ...view, route, calls, close() { scope.stop(); delete globalThis[key]; } };
}

test("employee filters default to currently employed while preserving valid explicit selections", async () => {
  const cases = [
    [{}, "employment_active"],
    [{ employee_filter: "invalid" }, "employment_active"],
    [{ employee_filter: "payroll" }, "employment_active"],
    [{ employee_filter: "no_payroll" }, "employment_active"],
    [{ employee_filter: "employment_inactive" }, "employment_active"],
    [{ employee_filter: "employment_regular" }, "employment_active"],
    ...employeeFilters.map(value => [{ employee_filter: value }, value]),
  ];
  for (const [query, expected] of cases) {
    const h = await harness(query);
    try {
      assert.equal(h.filter.value, expected);
      const pending = h.loadPeriod("2026-09");
      assert.equal(h.calls.length, 1);
      assert.equal(h.calls[0].query.employee_filter, expected);
      h.calls[0].resolve(response(expected));
      await pending;
    } finally { h.close(); }
  }
});

test("all and monthly roster filters stay explicit while currently employed restores the default", async () => {
  const h = await harness();
  try {
    h.filter.value = "all"; await flush();
    assert.equal(h.route.query.employee_filter, "all");
    assert.equal(h.filter.value, "all");
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].query.employee_filter, "all");
    h.calls[0].resolve(response("all")); await flush();
    h.filter.value = "in_period"; await flush();
    assert.equal(h.route.query.employee_filter, "in_period");
    assert.equal(h.filter.value, "in_period");
    assert.equal(h.calls.length, 2);
    assert.equal(h.calls[1].query.employee_filter, "in_period");
    h.calls[1].resolve(response("in_period")); await flush();
    h.filter.value = "employment_active"; await flush();
    assert.equal(h.route.query.employee_filter, undefined);
    assert.equal(h.filter.value, "employment_active");
    assert.equal(h.calls[2].query.employee_filter, "employment_active");
    h.calls[2].resolve(response()); await flush();
  } finally { h.close(); }
});

test("the employee month picker retains each filter and drops old exact targets and cursors", async () => {
  for (const selected of [undefined, ...employeeFilters]) {
    const h = await harness({ employee_filter: selected, employee_id: "old-target", cursor: "old-cursor", expected_version: "old-version" });
    try {
      h.displayMode.value = "list";
      h.selectPeriod("2026-10"); await flush();
      assert.equal(h.route.query.period, "2026-10");
      assert.equal(h.route.query.employee_filter, selected);
      assert.equal(h.filter.value, selected ?? "employment_active");
      for (const key of ["employee_id", "cursor", "expected_version"]) assert.equal(h.route.query[key], undefined, key);
      assert.equal(h.calls.length, 1, "month changes issue one new page request");
      assert.equal(h.calls[0].period, "2026-10");
      assert.equal(h.calls[0].query.employee_filter, selected ?? "employment_active");
      assert.equal(h.calls[0].query.employee_id, undefined);
      const next = response(selected ?? "employment_active", "new-month");
      next.selected_period.key = "2026-10";
      h.calls[0].resolve(next); await flush();
      assert.equal(h.response.value.data.collections.employees.items[0].employee_id, "new-month");
      assert.equal(h.displayMode.value, "list");
    } finally { h.close(); }
  }
});

test("an exact employee link requests all employees independently of the roster filter", async () => {
  for (const employeeFilter of [undefined, "ended", ...employmentFilters]) {
    const h = await harness({ employee_id: "target", employee_filter: employeeFilter });
    try {
      assert.equal(h.filter.value, employeeFilter ?? "employment_active");
      const pending = h.loadPeriod("2026-09");
      assert.equal(h.calls[0].query.employee_filter, "all");
      assert.equal(h.calls[0].query.employee_id, "target");
      const focused = response("all", "target"); focused.data.employee_id = "target";
      h.calls[0].resolve(focused); await pending;
      assert.equal(h.route.query.employee_id, "target");
      assert.equal(h.response.value.data.collections.employees.items[0].employee_id, "target");
    } finally { h.close(); }
  }
});

test("shared employee and labor display mode preserves loaded data, filter and pending pages without requests", async () => {
  const h = await harness();
  try {
    assert.equal(h.displayMode.value, "cards");
    const current = h.response.value, collections = current.data.collections, scope = h.paginationScope();
    const filter = h.filter.value, query = { ...h.route.query };
    const employeeItems = collections.employees.items, laborItems = collections.labor_sources.items;
    const employeePage = h.loadMore("employees"), laborPage = h.loadMore("labor_sources");
    const requests = [...h.calls];
    assert.equal(requests.length, 2);
    h.displayMode.value = "list"; await flush();
    assert.equal(h.displayMode.value, "list");
    h.displayMode.value = "cards"; await flush();
    assert.equal(h.displayMode.value, "cards");
    assert.equal(h.filter.value, filter);
    assert.deepEqual(h.route.query, query);
    assert.equal(h.calls.length, 2, "display switches must not issue requests");
    assert.equal(h.response.value, current);
    assert.equal(h.response.value.data.collections, collections);
    assert.equal(h.paginationScope(), scope);
    for (const request of requests) {
      assert.equal(request.signal.aborted, false);
      assert.equal(request.query.cursor, "next");
      assert.equal(request.query.expected_version, "version");
      assert.equal(request.query.employee_filter, "employment_active");
      const next = response("employment_active", "next-employee");
      next.data.collections.labor_sources.items = [{ source_id: "next-labor" }];
      next.data.collections[request.query.section].page.next_cursor = "following";
      request.resolve(next);
    }
    await Promise.all([employeePage, laborPage]);
    assert.equal(h.response.value.data.collections.employees.items, employeeItems);
    assert.equal(h.response.value.data.collections.labor_sources.items, laborItems);
    assert.equal(employeeItems.length, 2);
    assert.equal(laborItems.length, 2);
    assert.equal(h.response.value.data.collections.employees.page.next_cursor, "following");
    assert.equal(h.response.value.data.collections.labor_sources.page.next_cursor, "following");
    assert.equal(h.paginationScope(), scope);
  } finally { h.close(); }
});

for (const selectedFilter of employmentFilters) {
test(`employee filter ${selectedFilter} retains overview and labor while replacing only the server-filtered list`, async () => {
  const h = await harness({ employee_filter: "all" });
  try {
    const summary = h.response.value.data.employees, labor = h.response.value.data.collections.labor_sources;
    h.filter.value = selectedFilter; await flush();
    assert.equal(h.loading.value, false);
    assert.equal(h.response.value.data.employees, summary);
    assert.equal(h.response.value.data.collections.labor_sources, labor);
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.calls[0].query, { section: "employees", employee_filter: selectedFilter, expected_version: "version" });
    const filtered = response(selectedFilter, "matching"); filtered.data.collections.employees.page.filtered_count = 27;
    h.calls[0].resolve(filtered); await flush();
    assert.equal(h.response.value.data.collections.employees.page.filtered_count, 27);
    assert.equal(h.response.value.data.collections.employees.items[0].employee_id, "matching");
    assert.equal(h.response.value.data.employees, summary);
    assert.equal(h.response.value.data.collections.labor_sources, labor);
  } finally { h.close(); }
});
}

test("employee filter retries stay local and company changes discard all old results", async () => {
  const h = await harness();
  try {
    h.filter.value = "employment_unpaid_leave"; await flush();
    h.filter.value = "ended"; await flush();
    assert.equal(h.calls[0].signal.aborted, true);
    h.calls[0].resolve(response("employment_unpaid_leave", "old")); await flush();
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
    h.filter.value = "employment_unpaid_leave"; await flush();
    h.calls[0].reject(Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" })); await flush();
    assert.equal(h.response.value, null);
    assert.equal(h.loading.value, true);
    assert.equal(h.calls[1].query.section, undefined);
    assert.equal(h.calls[1].query.employee_filter, "employment_unpaid_leave");
    const fresh = response("employment_unpaid_leave", "fresh"); fresh.snapshot_version = "fresh-version";
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
      const next = response(h.filter.value, "next-employee");
      next.data.collections.labor_sources.items = [{ source_id: "next-labor" }];
      next.data.collections[section].page.next_cursor = "following";
      h.calls.at(-1).resolve(next); await pending;
      assert.equal(h.response.value.data.collections[section].items, original);
      assert.equal(renderedCount.value, 2);
      assert.equal(h.response.value.data.collections[section].page.next_cursor, "following");
      const late = h.loadMore(section), request = h.calls.at(-1);
      h.filter.value = section === "employees" ? "employment_unpaid_leave" : "employment_departed"; await flush();
      assert.equal(request.signal.aborted, true);
      request.resolve(response(request.query.employee_filter, "late")); await late;
      assert.equal(original.length, 2);
      const filtered = response(h.filter.value, "filtered");
      h.calls.at(-1).resolve(filtered); await flush();
    }
  } finally { h.close(); }
});
