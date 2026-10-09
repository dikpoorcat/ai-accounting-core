import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { validateDashboardEmployeesResponse } from "../src/api/generated/dashboardValidators.js";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

const contractSamples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));

let sequence = 0;
const views = {
  Employees: { load: "loadPeriod", response: "response", error: "error" },
  Assets: { load: "loadAssets", response: "response", error: "errorMessage" },
  Brief: { load: "loadData", response: "response", error: "error" },
  Reports: { load: "preview", response: "report", error: "errorMessage" },
  Funds: { load: "loadFunds", response: "funds", error: "requestError" },
};
async function harness(name, refreshContext = async () => {}, initialContext = null, initialQuery = {}) {
  const key = `dashboardV2Harness${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-01", ...initialQuery }, hash: "" });
  const calls = [], unmount = [], replaces = [];
  const dashboardContext = Vue.ref(initialContext);
  globalThis[key] = { Vue, route, refreshContext, dashboardContext, unmount, replaces, appendDashboardCollection, fetch: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) };
  globalThis.window = { removeEventListener() {}, addEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  const source = readFileSync(new URL(`../src/views/${name}View.vue`, import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "")
    .replace("let mounted = true", "let mounted = false");
  const prefix = `
    const environment = globalThis.${key};
    const { ref, shallowReactive, shallowRef, computed, nextTick, watch } = environment.Vue;
    const { appendDashboardCollection } = environment;
    const onMounted = () => {}; const onBeforeUnmount = callback => environment.unmount.push(callback);
    const useRoute = () => environment.route;
    const useRouter = () => ({ replace: async value => environment.replaces.push(value), push: async () => {} });
    const useDashboardContext = () => ({ context: environment.dashboardContext, load: environment.refreshContext, refresh: environment.refreshContext });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const fetchEmployeesDashboard = environment.fetch, fetchAssetsDashboard = environment.fetch, fetchCompleteBrief = environment.fetch, fetchDeferredBrief = environment.fetch, fetchDeferredQuarterlyReport = environment.fetch, fetchFundsDashboard = environment.fetch;
    const fetchPeriodPreparation = () => new Promise(() => {});
    const dashboardErrorMessage = error => error.message; const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fen = value => BigInt(value ?? 0), formatFen = String, formatPositiveFen = String;
    const rememberFundAccounts = () => {};
    const fundAccountLabel = () => null;
  `;
  const config = views[name];
  const recoveryExports = name === "Reports" ? "" : ", loadMore, refreshChanged, updateNotice";
  const suffix = `\nmounted = true; ${name === "Funds" ? 'selectedPeriod.value = "2026-01";' : ''} export { ${config.load} as load, ${config.response} as response, ${config.error} as error, loading, refresh ${recoveryExports} };`;
  const { outputText } = ts.transpileModule(prefix + source + suffix, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  return { ...await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`), route, calls, dashboardContext, unmount: () => unmount.forEach(callback => callback()), replaces };
}
function result(name, marker) {
  if (name === "Reports") return { marker, statements: [] };
  if (name === "Funds") {
    const collection = () => ({ items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null } });
    return { schema_version: 6, snapshot_version: "same", selected_period: { key: "2026-01", label: "一月" }, data: {
      marker, collections: { accounts: collection(), movements: collection(), statements: collection(), investment_products: collection(), investment_events: collection() },
      investments: {}, bank_statement: {},
    } };
  }
  const briefContract = contractSamples.deferred_brief.response;
  return { marker, ...(name === "Brief" ? {
    schema_version: briefContract.schema_version, snapshot_version: briefContract.snapshot_version,
    read_context: { ...briefContract.read_context, company_id: "company-a" },
  } : {}), selected_period: { key: "2026-01" }, data: { workforce_cost: { has_activity: false }, vouchers: [], activity_groups: [], voucher_page: { has_more: false }, collections: {} } };
}
function period(name, month) { return name === "Reports" ? `2026-Q${month}` : `2026-0${month}`; }

for (const name of Object.keys(views)) {
  test(`${name}: A → B → A rejects success, catch and finally from the abandoned generation`, async () => {
    const view = await harness(name);
    const first = view.load(period(name, 1));
    view.calls[0].resolve(result(name, "first-a")); await first;
    view.route.query.period = "2026-02";
    const old = view.load(period(name, 2));
    view.route.query.period = "2026-01";
    assert.equal(view.response.value, null);
    const latest = view.load(period(name, 1));
    view.calls[1].reject(new Error("late-b-error")); await old;
    assert.equal(view.error.value, "");
    assert.equal(view.loading.value, true, "old finally must not clear current loading");
    view.calls[2].resolve(result(name, "latest-a")); await latest;
    assert.equal(view.response.value.marker, "latest-a");
    const abandoned = view.load(period(name, 1));
    view.route.query.period = "2026-02";
    const current = view.load(period(name, 2));
    view.calls[4].resolve(result(name, "current-b")); await current;
    view.calls[3].resolve(result(name, "late-a-success")); await abandoned;
    assert.equal(view.response.value.marker, "current-b");
    view.unmount();
  });
  test(`${name}: a refresh cannot revive a selection after A → B → A or unmount`, async () => {
    let release;
    const view = await harness(name, () => new Promise(resolve => { release = resolve; }));
    const pending = view.refresh();
    view.route.query.period = "2026-02";
    view.route.query.period = "2026-01";
    release({ periods: [] }); await pending;
    assert.equal(view.calls.length, 0);
    const request = view.load(period(name, 1));
    view.unmount();
    view.calls[0].resolve(result(name, "after-unmount")); await request;
    assert.equal(view.response.value, null);
    assert.equal(view.error.value, "");
  });
}

test("Brief: an exact business deep link requests one projection without preceding pages", async () => {
  const view = await harness("Brief", async () => {}, null, { voucher: "10009" });
  const pending = view.load("2026-01");
  assert.equal(view.calls[0].args[4].voucher_number, 10009);
  view.calls[0].resolve({ ...result("Brief", "direct"), data: { collections: {}, focused_activity: { subject_id: "business-10009" } } });
  await pending;
  assert.equal(view.calls.length, 1);
  assert.equal(view.response.value.data.focused_activity.subject_id, "business-10009");
  view.unmount();
});

test("hot refresh starts context and scoped main request together, then commits after scope validation", async () => {
  const current = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-01", year: 2026, month: 1 }], quarters: [{ key: "2026-Q1" }] };
  for (const name of Object.keys(views).filter((item) => item !== "Funds")) {
    let releaseContext;
    const view = await harness(name, () => new Promise(resolve => { releaseContext = resolve; }), current,
      name === "Reports" ? { quarter: "2026-Q1" } : {});
    const pending = view.refresh();
    assert.equal(view.calls.length, 1, `${name}: main request starts before context completes`);
    view.calls[0].resolve(result(name, "parallel"));
    await Promise.resolve();
    assert.equal(view.response.value, null, `${name}: main response stays hidden until context is checked`);
    view.dashboardContext.value = structuredClone(current);
    await Vue.nextTick();
    assert.equal(view.calls.length, 1, `${name}: same-scope context replacement cannot duplicate the main request`);
    releaseContext(current);
    await pending;
    assert.equal(view.response.value?.marker, "parallel", `${name}: validated response commits`);
    view.unmount();
  }
});

test("a failed context check cannot mark a concurrent Employees response complete", async () => {
  const current = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-01" }], quarters: [] };
  let rejectContext;
  const view = await harness("Employees", () => new Promise((_resolve, reject) => { rejectContext = reject; }), current);
  const pending = view.refresh();
  view.calls[0].resolve(result("Employees", "must-stay-hidden"));
  rejectContext(new Error("context failed"));
  await pending;
  assert.equal(view.response.value, null);
  assert.equal(view.error.value, "context failed");
  assert.equal(view.loading.value, false);
  view.unmount();
});

test("same-selection refresh retains hidden data until both new response and context succeed", async () => {
  const current = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-01", year: 2026, month: 1 }], quarters: [{ key: "2026-Q1" }] };
  for (const name of Object.keys(views)) {
    let releaseContext;
    const view = await harness(name, () => new Promise(resolve => { releaseContext = resolve; }), current,
      name === "Reports" ? { quarter: "2026-Q1" } : {});
    const first = view.load(period(name, 1));
    view.calls[0].resolve(result(name, "before")); await first;
    const before = view.response.value;
    assert(before, name);
    const pending = view.refresh();
    assert.equal(view.loading.value, true, name);
    assert.equal(view.response.value, before, `${name}: preserve the mounted projection while hidden`);
    view.calls[1].resolve(result(name, "after"));
    await Vue.nextTick();
    assert.equal(view.response.value, before, `${name}: an unchecked context cannot expose a new projection`);
    releaseContext(current); await pending;
    assert.equal(view.response.value.marker, "after", name);
    assert.equal(view.loading.value, false, name);
    view.unmount();
  }
});

test("retained refresh data is cleared on errors and immediate selection changes", async () => {
  const current = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-01", year: 2026, month: 1 }], quarters: [{ key: "2026-Q1" }] };
  for (const name of Object.keys(views)) {
    const view = await harness(name, async () => current, current, name === "Reports" ? { quarter: "2026-Q1" } : {});
    const seed = view.load(period(name, 1));view.calls[0].resolve(result(name, "before"));await seed;
    const failure = view.refresh();view.calls[1].reject(new Error("read failed"));await failure;
    assert.equal(view.response.value, null, `${name}: old amounts cannot reappear after failure`);
    assert.equal(view.loading.value, false, name);
    const reseed = view.load(period(name, 1));view.calls[2].resolve(result(name, "before"));await reseed;
    const abandoned = view.refresh();
    view.route.query.company_id = "company-b";
    assert.equal(view.response.value, null, `${name}: scope changes clear immediately`);
    view.calls[3].resolve(result(name, "late"));await abandoned;
    assert.equal(view.response.value, null, `${name}: late same-selection refresh cannot restore old data`);
    view.unmount();
  }
});

test("a removed period in refreshed context clears retained amounts instead of exposing the old result", async () => {
  const current = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-01", year: 2026, month: 1 }], quarters: [{ key: "2026-Q1" }] };
  for (const name of Object.keys(views)) {
    const view = await harness(name, async () => ({ ...current, periods: [] }), current, name === "Reports" ? { quarter: "2026-Q1" } : {});
    const seed = view.load(period(name, 1));view.calls[0].resolve(result(name, "before"));await seed;
    const pending = view.refresh();view.calls[1].resolve(result(name, "unchecked"));await pending;
    assert.equal(view.response.value, null, name);
    assert.equal(view.loading.value, false, name);
    assert.match(view.error.value, /期间已变化/, name);
    view.unmount();
  }
});

const recoveryContext = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-01", year: 2026, month: 1 }] };
const recoveryCollection = () => ({ items: [], page: { total_count: 2, filtered_count: 2, returned_count: 0, has_more: true, next_cursor: "next" } });
function recoveryResult(name, marker) {
  const value = result(name, marker);
  value.snapshot_version = "v1";
  value.read_context = { company_id: "company-a" };
  for (const section of ["activity", "open_items", "movements", "statements", "investment_events", "accounts", "investment_products", "employees", "labor_sources", "assets", "projects"]) value.data.collections[section] = recoveryCollection();
  return value;
}
const recoveryPaths = [
  ["Brief", "activity"], ["Brief", "open_items"],
  ["Funds", "book"], ["Funds", "bank"], ["Funds", "investment"], ["Funds", "accounts"], ["Funds", "investment_products"],
  ["Employees", "employees"], ["Employees", "labor_sources"],
  ["Assets", "assets"], ["Assets", "projects"],
];
function continueRecovery(view, section) {
  return view.loadMore(section);
}

test("all continuation recovery paths clear invalid projections and wait for the new main response after context", async () => {
  for (const [name, section] of recoveryPaths) {
    const label = `${name}/${section}`;
    let releaseContext;
    const view = await harness(name, () => new Promise(resolve => { releaseContext = resolve; }), recoveryContext);
    const seed = view.load("2026-01"); view.calls[0].resolve(recoveryResult(name, "before")); await seed;
    const pending = continueRecovery(view, section);
    assert.equal(view.calls.length, 2, label);
    view.calls[1].reject(Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" }));
    await Vue.nextTick();
    assert.equal(view.response.value, null, `${label}: known-invalid data clears immediately`);
    assert.equal(view.loading.value, true, label);
    assert.equal(view.calls.length, 3, `${label}: exactly one replacement main request`);
    releaseContext(recoveryContext); await Vue.nextTick();
    assert.equal(view.response.value, null, `${label}: fresh context alone cannot restore amounts`);
    assert.equal(view.loading.value, true, label);
    assert.equal(view.updateNotice.value, "资料已更新，正在重新读取。", label);
    view.calls[2].resolve(recoveryResult(name, "after")); await pending;
    assert.equal(view.response.value.marker, "after", label);
    assert.equal(view.loading.value, false, label);
    view.unmount();
  }
});

test("late continuation failures from A-B-A cannot trigger recovery or replace the new projection", async () => {
  for (const [name, section] of recoveryPaths) {
    const view = await harness(name, async () => recoveryContext, recoveryContext);
    const seed = view.load("2026-01"); view.calls[0].resolve(recoveryResult(name, "old")); await seed;
    const late = continueRecovery(view, section);
    view.route.query.company_id = "company-b"; view.route.query.company_id = "company-a";
    const latest = view.load("2026-01"); view.calls[2].resolve(recoveryResult(name, "latest")); await latest;
    view.calls[1].reject(Object.assign(new Error("late changed"), { code: "dashboard_snapshot_changed" })); await late;
    assert.equal(view.response.value.marker, "latest", `${name}/${section}`);
    assert.equal(view.calls.length, 3, "abandoned continuation cannot initiate another request");
    assert.equal(view.loading.value, false);
    view.unmount();
  }
});

test("detail version-change notifications and explicit mismatched continuation versions clear before refreshing", async () => {
  for (const name of ["Brief", "Funds", "Employees", "Assets"]) {
    const view = await harness(name, async () => recoveryContext, recoveryContext);
    const seed = view.load("2026-01"); view.calls[0].resolve(recoveryResult(name, "before")); await seed;
    const pending = view.refreshChanged();
    assert.equal(view.response.value, null, name);
    assert.equal(view.loading.value, true, name);
    view.calls[1].resolve(recoveryResult(name, "after")); await pending;
    assert.equal(view.response.value.marker, "after", name);
    view.unmount();
  }
  for (const [name, section] of [["Employees", "employees"], ["Assets", "assets"]]) {
    const view = await harness(name, async () => recoveryContext, recoveryContext);
    const seed = view.load("2026-01"); view.calls[0].resolve(recoveryResult(name, "before")); await seed;
    const pending = view.loadMore(section);
    view.calls[1].resolve({ ...recoveryResult(name, "wrong"), snapshot_version: "v2" }); await Vue.nextTick();
    assert.equal(view.response.value, null, name);
    assert.equal(view.loading.value, true, name);
    view.calls[2].resolve(recoveryResult(name, "after")); await pending;
    assert.equal(view.response.value.marker, "after", name);
    view.unmount();
  }
});

const modules = new Map();
async function moduleUrl(url) {
  if (modules.has(url.href)) return modules.get(url.href);
  let { outputText } = ts.transpileModule(readFileSync(url, "utf8"), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  for (const [, relative] of [...outputText.matchAll(/from "(\.[^"]+)"/g)]) {
    const modulePath = /\.[cm]?[jt]s$/.test(relative) ? relative : `${relative}.ts`;
    outputText = outputText.replaceAll(`"${relative}"`, JSON.stringify(await moduleUrl(new URL(modulePath, url))));
  }
  const value = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`; modules.set(url.href, value); return value;
}
test("actual API consumers reject stale schemas, missing page metadata and mismatched returned counts", async () => {
  const valid = structuredClone(contractSamples.employees.response);
  assert(validateDashboardEmployeesResponse(valid));
  for (const mutate of [
    value => { value.schema_version = 5; },
    value => { delete value.data.collections.employees.page; },
    value => { value.data.employees.items = []; },
  ]) {
    const malformed = structuredClone(valid);
    mutate(malformed);
    assert.equal(validateDashboardEmployeesResponse(malformed), false);
  }
});

test("business history API carries the selected settlement view and version through continuation", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  const { fetchBusinessStatus } = await server.ssrLoadModule("/src/api/businessStatus.ts");
  const response = structuredClone(contractSamples.business_status.response);
  response.data.settlement_view = "historical";
  const companyId = response.read_context.company_id;
  const subjectId = response.data.identity.subject_id;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${companyId}` } };
  const calls = [];
  globalThis.fetch = async url => { calls.push(new URL(url, "http://localhost").searchParams); return new Response(JSON.stringify(response)); };
  try {
    await fetchBusinessStatus(response.selected_period.key, subjectId, undefined, { settlement_view: "historical" });
    await fetchBusinessStatus(response.selected_period.key, subjectId, undefined, { settlement_view: "historical", section: "settlement_events" });
    assert.equal(calls[0].get("settlement_view"), "historical");
    assert.equal(calls[1].get("settlement_view"), "historical");
    assert.equal(calls[1].get("subject_id"), subjectId);
    assert.equal(calls[1].get("company_id"), companyId);
    response.data.detail_scope = { category: "payroll", voucher_version_id: "exact-voucher", amount_fen: "123", amount_label: "实际付款" };
    const scope = { settlement_view: "historical", detail_scope_category: "payroll", voucher_version_id: "exact-voucher", expected_version: response.snapshot_version };
    await fetchBusinessStatus(response.selected_period.key, subjectId, undefined, scope);
    await fetchBusinessStatus(response.selected_period.key, subjectId, undefined, { ...scope, section: "settlement_events" });
    assert.equal(calls[2].get("detail_scope_category"), "payroll"); assert.equal(calls[3].get("voucher_version_id"), "exact-voucher");
    for (const wrong of [
      { ...scope, detail_scope_category: "expense_supplier" },
      { ...scope, voucher_version_id: "different-voucher" },
      { ...scope, settlement_view: "current" },
      { ...scope, expected_version: "different-snapshot" },
      { settlement_view: "historical" },
    ]) await assert.rejects(fetchBusinessStatus(response.selected_period.key, subjectId, undefined, wrong), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
  } finally { await server.close(); }
});

test("embedded historical settlement pages require their shared scope and accurate returned counts", async () => {
  const response = contractSamples.employees.response;
  assert.equal("items" in response.data.employees, false);
  assert(validateDashboardEmployeesResponse(response));
  const collection = response.data.collections.employees;
  assert.equal(collection.page.returned_count, collection.items.length);
  const malformed = structuredClone(response);
  malformed.data.collections.employees.page.returned_count += 1;
  assert.notEqual(malformed.data.collections.employees.page.returned_count, malformed.data.collections.employees.items.length);
});
