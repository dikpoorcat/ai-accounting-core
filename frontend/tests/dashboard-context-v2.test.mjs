import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let sequence = 0;
const views = {
  Employees: { load: "loadPeriod", response: "response", error: "error" },
  Assets: { load: "loadAssets", response: "response", error: "errorMessage" },
  Brief: { load: "loadData", response: "response", error: "error" },
  Reports: { load: "preview", response: "report", error: "errorMessage" },
  Funds: { load: "loadFunds", response: "funds", error: "requestError" },
};
async function harness(name, refreshContext = async () => {}) {
  const key = `dashboardV2Harness${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-01" }, hash: "" });
  const calls = [], unmount = [], replaces = [];
  globalThis[key] = { Vue, route, refreshContext, unmount, replaces, fetch: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) };
  globalThis.window = { removeEventListener() {}, addEventListener() {} };
  globalThis.document = { getElementById: () => null };
  const source = readFileSync(new URL(`../src/views/${name}View.vue`, import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const prefix = `
    const environment = globalThis.${key};
    const { ref, computed, nextTick, watch } = environment.Vue;
    const onMounted = () => {}; const onBeforeUnmount = callback => environment.unmount.push(callback);
    const useRoute = () => environment.route;
    const useRouter = () => ({ replace: async value => environment.replaces.push(value), push: async () => {} });
    const useDashboardContext = () => ({ context: ref(null), load: environment.refreshContext, refresh: environment.refreshContext });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const fetchEmployeesDashboard = environment.fetch, fetchAssetsDashboard = environment.fetch, fetchBrief = environment.fetch, fetchQuarterlyReport = environment.fetch, fetchFundsDashboard = environment.fetch;
    const dashboardErrorMessage = error => error.message; const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fen = value => BigInt(value ?? 0), formatFen = String, formatPositiveFen = String;
  `;
  const config = views[name];
  const suffix = `\nmounted = true; ${name === "Funds" ? 'selectedPeriod.value = "2026-01";' : ''} export { ${config.load} as load, ${config.response} as response, ${config.error} as error, loading, refresh };`;
  const { outputText } = ts.transpileModule(prefix + source + suffix, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  return { ...await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`), route, calls, unmount: () => unmount.forEach(callback => callback()), replaces };
}
function result(name, marker) {
  if (name === "Reports") return { marker, statements: [] };
  if (name === "Funds") return { schema_version: 2, snapshot_version: "same", selected_period: { label: "一月" }, data: { marker, accounts: [], investments: { products: [] }, bank_statement: {} } };
  return { marker, selected_period: { key: "2026-01" }, data: { workforce_cost: { has_activity: false }, vouchers: [], activity_groups: [], voucher_page: { has_more: false }, collections: {} } };
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

test("Brief: a distant voucher deep link requests one exact projection without loading preceding pages", async () => {
  const view = await harness("Brief");
  view.route.query.voucher = "10009";
  const pending = view.load("2026-01");
  assert.equal(view.calls[0].args[4].voucher_number, 10009);
  view.calls[0].resolve({ ...result("Brief", "direct"), data: { workforce_cost: { has_activity: false }, vouchers: [], collections: {}, focused_voucher: { voucher_version_id: "exact" }, voucher_page: { has_more: true } } });
  await pending;
  assert.equal(view.calls.length, 1);
  assert.equal(view.response.value.data.focused_voucher.voucher_version_id, "exact");
  assert.deepEqual(view.response.value.data.vouchers, []);
  view.unmount();
});

const modules = new Map();
async function moduleUrl(url) {
  if (modules.has(url.href)) return modules.get(url.href);
  let { outputText } = ts.transpileModule(readFileSync(url, "utf8"), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  for (const [, relative] of [...outputText.matchAll(/from "(\.[^"]+)"/g)]) outputText = outputText.replaceAll(`"${relative}"`, JSON.stringify(await moduleUrl(new URL(relative + ".ts", url))));
  const value = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`; modules.set(url.href, value); return value;
}
test("actual API consumers reject stale schemas, missing page metadata and mismatched returned counts", async () => {
  const { requestJson } = await import(await moduleUrl(new URL("../src/api/client.ts", import.meta.url)));
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=a" } };
  const page = { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null };
  const preparation = JSON.parse(readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8")).employees.data.period_preparation;
  const valid = { schema_version: 2, data: { period_preparation: preparation, employees: { items: [] }, collections: { employees: { items: [], page } } } };
  const rejected = [
    { ...valid, schema_version: 1 },
    { ...valid, data: { ...valid.data, collections: { employees: { items: [], page: { has_more: false } } } } },
    { ...valid, data: { ...valid.data, collections: { employees: { items: [], page: { ...page, returned_count: 1 } } } } },
    { ...valid, data: { ...valid.data, period_preparation: null } },
  ];
  for (const payload of rejected) {
    globalThis.fetch = async () => new Response(JSON.stringify(payload));
    await assert.rejects(requestJson("/api/dashboard/employees?period=2026-01"), error => error.code === "DASHBOARD_SCHEMA_MISMATCH" && error.message.includes("刷新"));
  }
  globalThis.fetch = async () => new Response(JSON.stringify(valid));
  assert.equal((await requestJson("/api/dashboard/employees?period=2026-01")).schema_version, 2);
});

test("business history API carries the selected settlement view and version through continuation", async () => {
  const { fetchBusinessStatus } = await import(await moduleUrl(new URL("../src/api/businessStatus.ts", import.meta.url)));
  const response = JSON.parse(readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8"))["business-status"];
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=co" } };
  const calls = [];
  globalThis.fetch = async url => { calls.push(new URL(url, "http://localhost").searchParams); return new Response(JSON.stringify(response)); };
  await fetchBusinessStatus("2026-11", "exact-business", undefined, { settlement_view: "historical", expected_version: "snapshot-a" });
  await fetchBusinessStatus("2026-11", "exact-business", undefined, { settlement_view: "historical", section: "settlement_events", cursor: "historical-cursor", expected_version: "snapshot-a" });
  assert.equal(calls[0].get("settlement_view"), "historical");
  assert.equal(calls[1].get("settlement_view"), "historical");
  assert.equal(calls[1].get("cursor"), "historical-cursor");
  assert.equal(calls[1].get("subject_id"), "exact-business");
  assert.equal(calls[1].get("expected_version"), "snapshot-a");
});

test("embedded historical settlement pages require their shared scope and accurate returned counts", async () => {
  const { requestJson } = await import(await moduleUrl(new URL("../src/api/client.ts", import.meta.url)));
  const response = JSON.parse(readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8")).employees;
  globalThis.window = { location: { origin: "http://localhost", search: "?company_id=co" } };
  for (const mutate of [source => { delete source.movements_scope; }, source => { source.movements_page.returned_count += 1; }]) {
    const malformed = structuredClone(response);
    mutate(malformed.data.workforce_cost.personal_labor.items[0]);
    globalThis.fetch = async () => new Response(JSON.stringify(malformed));
    await assert.rejects(requestJson("/api/dashboard/employees?period=2026-11"), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
  }
  globalThis.fetch = async () => new Response(JSON.stringify(response));
  assert.equal((await requestJson("/api/dashboard/employees?period=2026-11")).schema_version, 2);
});
