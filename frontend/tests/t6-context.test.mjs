import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let sequence = 0;
const source = path => readFileSync(new URL(path, import.meta.url), "utf8");
const withoutImports = text => text.replace(/import[\s\S]*?from "[^"]+";/g, "");
async function compile(code, environment) {
  const key = `t6Context${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};\n${code}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  return module;
}
function queryString(query) {
  return `?${new URLSearchParams(Object.entries(query).filter(([, value]) => value !== undefined))}`;
}
function context(company, periods, defaultPeriod = periods[0] ?? null) {
  const companies = ["a", "b"].map(company_id => ({ company_id, name: `公司${company_id}`, status: "active" }));
  return {
    schema_version: 2, company: `公司${company}`, companies,
    current_company: companies.find(item => item.company_id === company), default_period: defaultPeriod,
    periods: periods.map(key => ({ key, year: Number(key.slice(0, 4)), month: Number(key.slice(5)), status: "open" })),
    quarters: [...new Set(periods.map(key => `${key.slice(0, 4)}-Q${Math.ceil(Number(key.slice(5)) / 3)}`))].map(key => ({ key })),
    default_quarter: null,
  };
}
async function flush() { for (let index = 0; index < 6; index++) { await Promise.resolve(); await Vue.nextTick(); } }

async function harness(query = { company_id: "a", period: "2026-01" }, name = "employees") {
  const calls = [], replacements = [], pushes = [], unmount = [], notices = [];
  const route = Vue.reactive({ query: { ...query }, name, hash: "" });
  const window = { location: { search: queryString(query) }, addEventListener() {}, removeEventListener() {} };
  const environment = {
    Vue, window,
    fetchDashboardContext(signal) {
      return new Promise((resolve, reject) => calls.push({ resolve, reject, signal, query: window.location.search }));
    },
  };
  const shared = await compile(`
    const { ref, readonly } = environment.Vue;
    const { window, fetchDashboardContext } = environment;
    const dashboardErrorMessage = error => error?.name === "AbortError" ? "" : error.message;
    ${withoutImports(source("../src/composables/useDashboardContext.ts"))}
  `, environment);
  const state = shared.useDashboardContext();
  function navigate(target) {
    window.location.search = queryString(target.query);
    route.query = Object.fromEntries(Object.entries(target.query).filter(([, value]) => value !== undefined));
    route.hash = target.hash ?? route.hash;
  }
  const router = {
    replace: async target => { replacements.push(target); navigate(target); },
    push: async target => { pushes.push(target); navigate(target); },
  };
  const storage = new Map();
  Object.assign(environment, {
    route, router, unmount,
    contextState: { ...state, setSelectionNotice: message => { notices.push(message); state.setSelectionNotice(message); } },
    storage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
  });
  const script = withoutImports(source("../src/App.vue").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]);
  const app = await compile(`
    export function instantiate() {
      const { computed, ref, watch, nextTick } = environment.Vue;
      const { window, storage: localStorage } = environment;
      const document = { documentElement: { dataset: {} } };
      const onMounted = () => {}, onBeforeUnmount = callback => environment.unmount.push(callback);
      const defineProps = () => ({}), __APP_VERSION__ = "test";
      const useRoute = () => environment.route, useRouter = () => environment.router;
      const useDashboardContext = () => environment.contextState;
      ${script}
      return { setAuthenticated, loadCompanyContext, contextError, selectCompany, selectPeriod };
    }
  `, environment);
  const scope = Vue.effectScope();
  const instance = scope.run(() => app.instantiate());
  return { ...instance, state, calls, route, router, window, replacements, pushes, notices, environment, navigate,
    close() { unmount.forEach(callback => callback()); scope.stop(); state.cancel(); } };
}

test("T6 shared context: A → B → A rejects stale success, error and finally", async () => {
  const h = await harness();
  try {
    const first = h.state.load().catch(error => error);
    h.window.location.search = "?company_id=b";
    const middle = h.state.load().catch(error => error);
    h.window.location.search = "?company_id=a";
    const latest = h.state.load();
    assert(h.calls[0].signal.aborted); assert(h.calls[1].signal.aborted);
    h.calls[1].reject(new Error("late B failure")); await middle;
    h.calls[0].resolve(context("a", ["2025-12"]));
    assert.equal((await first).name, "AbortError");
    assert.equal(h.state.context.value, null);
    assert.equal(h.state.error.value, "");
    assert.equal(h.state.loading.value, true, "abandoned finally cannot stop the newest request");
    h.calls[2].resolve(context("a", ["2026-02"])); await latest;
    assert.equal(h.state.context.value.default_period, "2026-02");
    assert.equal(h.state.loading.value, false);
  } finally { h.close(); }
});

test("T6 new company context keeps a supported month, falls back explicitly, and permits an empty period", async t => {
  for (const [label, periods, expected] of [
    ["same month", ["2026-03", "2026-01"], "2026-01"],
    ["default month", ["2026-03"], "2026-03"],
    ["no months", [], undefined],
  ]) await t.test(label, async () => {
    const h = await harness({ company_id: "b", period: "2026-01", employee_filter: "payroll" });
    try {
      h.setAuthenticated(true);
      h.calls.at(-1).resolve(context("b", periods)); await flush();
      assert.equal(h.route.query.company_id, "b");
      assert.equal(h.route.query.period, expected);
      assert.equal(h.route.query.employee_filter, "payroll", "explicit route selection is not page data or a cursor");
      if (label === "default month") assert.match(h.state.selectionNotice.value, /2026-03/);
      if (label === "no months") assert.match(h.state.selectionNotice.value, /没有可查看的月份/);
    } finally { h.close(); }
  });
});

test("T6 App ignores a former company's late context and fallback after navigation", async () => {
  const h = await harness();
  try {
    h.setAuthenticated(true);
    const abandoned = h.calls[0];
    h.navigate({ query: { company_id: "b", period: "2026-02" } });
    const current = h.calls.at(-1);
    assert.notEqual(current, abandoned);
    current.resolve(context("b", ["2026-02"])); await flush();
    const replacementCount = h.replacements.length;
    abandoned.resolve(context("a", ["2025-12"])); await flush();
    assert.equal(h.route.query.company_id, "b");
    assert.equal(h.route.query.period, "2026-02");
    assert.equal(h.replacements.length, replacementCount);
    assert.equal(h.contextError.value, "");
    // Settle any superseded B fetch produced by simultaneous company/month watchers.
    for (const call of h.calls) if (call !== current && call !== abandoned) call.resolve(context("b", ["2026-02"]));
    await flush();
  } finally { h.close(); }
});

test("T6 refresh replaces month options and safely reconciles an unavailable current month", async () => {
  const h = await harness();
  try {
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("a", ["2026-01"])); await flush();
    const refreshed = h.state.refresh();
    assert.equal(h.state.context.value.current_company.company_id, "a", "refresh keeps the mounted company while loading");
    h.calls.at(-1).resolve(context("a", ["2026-02"])); await refreshed; await flush();
    assert.equal(h.state.context.value.periods[0].key, "2026-02");
    assert.equal(h.route.query.period, "2026-02", "the URL must no longer point to a removed month");
  } finally { h.close(); }
});

test("T6 month changes while refresh waits cannot be overwritten by an older selection", async () => {
  const h = await harness();
  try {
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("a", ["2026-01", "2026-02"])); await flush();
    const pending = h.state.refresh();
    h.navigate({ query: { company_id: "a", period: "2026-02", employee_filter: "payroll" } });
    h.calls.at(-1).resolve(context("a", ["2026-01", "2026-02"])); await pending; await flush();
    assert.equal(h.route.query.period, "2026-02");
    assert.equal(h.route.query.employee_filter, "payroll");
  } finally { h.close(); }
});

test("T6 report fallback uses the latest available quarter month and names the actual fallback", async () => {
  const h = await harness({ company_id: "b", period: "2025-12", quarter: "2026-Q1" }, "reports");
  try {
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("b", ["2026-01", "2026-03", "2026-02", "2026-06"], "2026-06")); await flush();
    assert.equal(h.route.query.period, "2026-03");
    assert.equal(h.route.query.quarter, "2026-Q1");
    assert.match(h.state.selectionNotice.value, /2026-03/);
    assert.doesNotMatch(h.state.selectionNotice.value, /2026-06/);
  } finally { h.close(); }
});

test("T6 an initial URL without company still restores the saved company before selecting its month", async () => {
  const h = await harness({ period: "2026-01" });
  try {
    h.environment.storage.setItem("finance-dashboard-company-id", "b");
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("a", ["2026-01"])); await flush();
    assert.equal(h.route.query.company_id, "b");
    h.calls.at(-1).resolve(context("b", ["2026-03"])); await flush();
    assert.equal(h.route.query.period, "2026-03");
    assert.equal(h.state.context.value.current_company.company_id, "b");
  } finally { h.close(); }
});

test("T6 a refresh to an empty company keeps no invented month or quarter", async () => {
  const h = await harness({ company_id: "a", period: "2026-01", quarter: "2026-Q1" }, "reports");
  try {
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("a", ["2026-01"])); await flush();
    const refreshed = h.state.refresh();
    h.calls.at(-1).resolve(context("a", [])); await refreshed; await flush();
    assert.equal(h.route.query.period, undefined);
    assert.equal(h.route.query.quarter, undefined);
    assert.equal(h.calls.length, 2, "normalizing loaded context must not fetch it again");
  } finally { h.close(); }
});

test("sidebar calendar month changes clear page state and align the report quarter", async () => {
  const h = await harness({
    company_id: "a",
    period: "2026-01",
    employee_filter: "payroll",
    voucher: "7",
  });
  try {
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("a", ["2025-12", "2026-01", "2026-04"]));
    await flush();
    h.route.hash = "#old-detail";
    await h.selectPeriod("2026-04");
    assert.deepEqual(h.pushes.at(-1), {
      query: { company_id: "a", period: "2026-04", quarter: undefined },
      hash: "",
    });
    assert.deepEqual(h.route.query, { company_id: "a", period: "2026-04" });

    h.route.name = "reports";
    h.navigate({
      query: {
        company_id: "a",
        period: "2026-04",
        quarter: "2026-Q2",
        carry_forward_fact_id: "old-source",
      },
      hash: "#old-report-detail",
    });
    await h.selectPeriod("2025-12");
    assert.deepEqual(h.pushes.at(-1), {
      query: { company_id: "a", period: "2025-12", quarter: "2025-Q4" },
      hash: "",
    });
    assert.deepEqual(h.route.query, {
      company_id: "a",
      period: "2025-12",
      quarter: "2025-Q4",
    });
  } finally { h.close(); }
});

test("T6 report company switch validates the shared month before choosing the new company default", async t => {
  for (const [label, periods, expected] of [
    ["missing month uses new default despite old quarter still existing", ["2026-06", "2026-03"], "2026-06"],
    ["same month is retained when available", ["2026-06", "2026-03", "2026-01"], "2026-01"],
    ["empty company stays empty", [], undefined],
  ]) await t.test(label, async () => {
    const h = await harness({ company_id: "a", period: "2026-01", quarter: "2026-Q1", carry_forward_fact_id: "old-company-source" }, "reports");
    try {
      h.setAuthenticated(true);
      h.calls.at(-1).resolve(context("a", ["2026-01"])); await flush();
      await h.selectCompany("b");
      h.calls.at(-1).resolve(context("b", periods, periods[0] ?? null)); await flush();
      assert.equal(h.route.query.period, expected);
      assert.equal(h.route.query.company_id, "b");
      assert.equal(h.route.query.quarter, undefined, "Reports must derive the new quarter from the validated shared month");
      assert.equal(h.route.query.carry_forward_fact_id, undefined);
      if (expected === "2026-06") assert.match(h.state.selectionNotice.value, /2026-06/);
      if (expected === undefined) assert.match(h.state.selectionNotice.value, /没有可查看的月份/);
    } finally { h.close(); }
  });
});

test("T6 sidebar changes company with shared selection only and clears page-specific state", async () => {
  const h = await harness({ company_id: "a", period: "2026-01", employee_filter: "payroll", cursor: "never-transfer", voucher: "7" });
  try {
    await h.selectCompany("b");
    assert.deepEqual(h.pushes[0], { query: { company_id: "b", period: "2026-01" }, hash: "" });
    assert.equal(h.state.context.value, null);
    h.route.name = "reports";
    h.navigate({ query: { company_id: "b", period: "2026-01", quarter: "2026-Q1", carry_forward_fact_id: "do-not-transfer" } });
    await h.selectCompany("a");
    assert.deepEqual(h.pushes[1], { query: { company_id: "a", period: "2026-01" }, hash: "" });
  } finally { h.close(); }
});
