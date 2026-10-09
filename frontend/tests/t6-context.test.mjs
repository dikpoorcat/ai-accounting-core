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
      const { computed, defineAsyncComponent, ref, watch, nextTick } = environment.Vue;
      const { window, storage: localStorage } = environment;
      const document = { documentElement: { dataset: {} } };
      const onMounted = () => {}, onBeforeUnmount = callback => environment.unmount.push(callback);
      const defineProps = () => ({}), __APP_VERSION__ = "test";
      const useRoute = () => environment.route, useRouter = () => environment.router;
      const useDashboardContext = () => environment.contextState;
      ${script}
      return { authenticated, currentCompany, routeSelectionReady, setAuthenticated, loadCompanyContext, contextError, selectCompany, selectPeriod };
    }
  `, environment);
  const scope = Vue.effectScope();
  const instance = scope.run(() => app.instantiate());
  return { ...instance, state, calls, route, router, window, replacements, pushes, notices, environment, navigate,
    close() { unmount.forEach(callback => callback()); scope.stop(); state.cancel(); } };
}

function mountPageRequests(h, pageNames = ["brief", "funds", "employees", "assets", "reports"]) {
  // Evaluate App's actual RouterView condition inside Vue's renderer, so a
  // mount represents the page request made before route selection settles.
  const template = source("../src/App.vue").match(/<template>([\s\S]*?)<\/template>/)[1];
  const condition = template.match(/<template v-if="([^"]+)"/)[1];
  const canMount = new Function("authenticated", "currentCompany", "routeSelectionReady", `return ${condition};`);
  const requests = [];
  const mounts = [];
  const Page = Vue.defineComponent({
    setup() {
      mounts.push({ company_id: h.route.query.company_id, period: h.route.query.period });
      Vue.watch(() => [h.state.context.value?.current_company?.company_id, h.route.query.company_id,
        h.route.query.period, h.route.query.quarter], ([loadedCompany, companyId, period, quarter]) => {
        if (loadedCompany !== companyId || !period) return;
        for (const name of pageNames) requests.push({ name, company_id: companyId, period, quarter });
      }, { immediate: true });
      return () => Vue.h("page");
    },
  });
  const remove = node => {
    if (!node.parent) return;
    const index = node.parent.children.indexOf(node);
    if (index >= 0) node.parent.children.splice(index, 1);
    node.parent = null;
  };
  const renderer = Vue.createRenderer({
    createElement: type => ({ type, children: [], parent: null }),
    createText: text => ({ type: "text", text, parent: null }),
    createComment: text => ({ type: "comment", text, parent: null }),
    setText: (node, text) => { node.text = text; },
    setElementText: (node, text) => { node.text = text; },
    insert(node, parent, anchor) {
      if (node.parent) remove(node);
      const index = anchor ? parent.children.indexOf(anchor) : -1;
      if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node);
      node.parent = parent;
    },
    remove,
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp: () => {},
  });
  const Shell = Vue.defineComponent({
    render() {
      return canMount(h.authenticated.value, h.currentCompany.value, h.routeSelectionReady.value)
        ? Vue.h(Page, { key: h.currentCompany.value.company_id }) : null;
    },
  });
  const root = { children: [] };
  renderer.createApp(Shell).mount(root);
  return { requests, mounts, close: () => renderer.render(null, root) };
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

test("T6 RouterView never mounts a new company's pages with its former company's month", async () => {
  const h = await harness({ company_id: "a", period: "2026-01" });
  const page = mountPageRequests(h);
  try {
    h.setAuthenticated(true);
    h.calls[0].resolve(context("a", ["2026-01"])); await flush();
    assert.equal(page.requests.length, 5);
    await h.selectCompany("b");
    assert.equal(h.route.query.period, "2026-01", "company selection preserves a potentially shared month");
    h.calls[1].resolve(context("b", ["2026-03"])); await flush();
    assert.equal(h.route.query.period, "2026-03");
    assert.deepEqual(page.requests.filter(request => request.company_id === "b").map(request => request.period),
      Array(5).fill("2026-03"), "all pages must mount only after the new month is normalized");
  } finally { page.close(); h.close(); }
});

test("T6 RouterView waits for initial month and report-quarter normalization", async t => {
  for (const [label, query, name, periods, expectedPeriod, expectedQuarter] of [
    ["invalid month", { company_id: "a", period: "2025-12" }, "brief", ["2026-01"], "2026-01", undefined],
    ["invalid report quarter", { company_id: "a", period: "2026-01", quarter: "2025-Q4" }, "reports", ["2026-01"], "2026-01", undefined],
    ["empty company", { company_id: "a", period: "2025-12" }, "brief", [], undefined, undefined],
  ]) await t.test(label, async () => {
    const h = await harness(query, name);
    const page = mountPageRequests(h, [name]);
    try {
      h.setAuthenticated(true);
      h.calls[0].resolve(context("a", periods)); await flush();
      assert.equal(h.route.query.period, expectedPeriod);
      assert.equal(h.route.query.quarter, expectedQuarter);
      assert.deepEqual(page.requests.map(item => item.period), expectedPeriod ? [expectedPeriod] : []);
      assert.equal(page.mounts.length, 1, "the final selection mounts exactly once");
      if (name === "reports") assert.equal(page.requests[0].quarter, undefined);
    } finally { page.close(); h.close(); }
  });
});

test("T6 RouterView keeps a same-company refresh mounted until an actual fallback is needed", async () => {
  const h = await harness({ company_id: "a", period: "2026-01" });
  const page = mountPageRequests(h, ["brief"]);
  try {
    h.setAuthenticated(true);
    h.calls[0].resolve(context("a", ["2026-01"])); await flush();
    const sameMonth = h.state.refresh();
    assert.equal(page.mounts.length, 1, "refresh does not unmount the current page while waiting");
    h.calls[1].resolve(context("a", ["2026-01", "2026-02"])); await sameMonth; await flush();
    assert.equal(page.mounts.length, 1, "same-month refresh keeps its existing RouterView");
    const requestsBeforeFallback = page.requests.length;
    const fallback = h.state.refresh();
    h.calls[2].resolve(context("a", ["2026-02"])); await fallback; await flush();
    assert.equal(h.route.query.period, "2026-02");
    assert.deepEqual(page.requests.slice(requestsBeforeFallback).map(item => item.period), ["2026-02"],
      "the refreshed context must not request its now unavailable former month");
  } finally { page.close(); h.close(); }
});

test("T6 RouterView rapid A → B → A mounts only the winning company's selected month", async () => {
  const h = await harness({ company_id: "a", period: "2026-01" });
  const page = mountPageRequests(h, ["brief"]);
  try {
    h.setAuthenticated(true);
    h.calls[0].resolve(context("a", ["2026-01"])); await flush();
    await h.selectCompany("b");
    const abandoned = h.calls[1];
    await h.selectCompany("a");
    assert.equal(abandoned.signal.aborted, true);
    h.calls[2].resolve(context("a", ["2026-02"])); await flush();
    abandoned.resolve(context("b", ["2026-01"])); await flush();
    assert.deepEqual(page.requests.map(item => [item.company_id, item.period]),
      [["a", "2026-01"], ["a", "2026-02"]]);
  } finally { page.close(); h.close(); }
});

test("T6 an invalid same-company deep link is normalized without mounting its invalid month", async () => {
  const h = await harness({ company_id: "a", period: "2026-01" });
  const page = mountPageRequests(h, ["brief"]);
  try {
    h.setAuthenticated(true);
    h.calls[0].resolve(context("a", ["2026-01", "2026-02"])); await flush();
    h.navigate({ query: { company_id: "a", period: "2026-99" } }); await flush();
    assert.equal(h.route.query.period, "2026-01");
    assert.equal(page.requests.some(item => item.period === "2026-99"), false);
  } finally { page.close(); h.close(); }
});

test("T6 new company month changes during context adoption still settle and mount", async () => {
  const h = await harness({ company_id: "a", period: "2026-01" });
  const page = mountPageRequests(h, ["brief"]);
  try {
    h.setAuthenticated(true);
    h.calls[0].resolve(context("a", ["2026-01"])); await flush();
    await h.selectCompany("b");
    h.calls[1].resolve(context("b", ["2026-02", "2026-03"]));
    for (let i = 0; i < 5 && h.state.context.value?.current_company?.company_id !== "b"; i++) await Promise.resolve();
    assert.equal(h.state.context.value?.current_company?.company_id, "b");
    assert.equal(h.routeSelectionReady.value, false);
    h.navigate({ query: { company_id: "b", period: "2026-03" } });
    await flush();
    assert.equal(h.route.query.period, "2026-03");
    assert.equal(h.routeSelectionReady.value, true, "superseded context adoption must not leave RouterView closed");
    assert.deepEqual(page.requests.filter(item => item.company_id === "b").map(item => item.period), ["2026-03"]);
  } finally { page.close(); h.close(); }
});

test("T6 changing a new company's month before context returns restarts its request", async () => {
  const h = await harness({ company_id: "a", period: "2026-01" });
  const page = mountPageRequests(h, ["brief"]);
  try {
    h.setAuthenticated(true);
    h.calls[0].resolve(context("a", ["2026-01"])); await flush();
    h.navigate({ query: { company_id: "b", period: "2026-01" } });
    assert.equal(h.state.context.value, null, "company change cancels the old-company context synchronously");
    const abandoned = h.calls[1];
    h.navigate({ query: { company_id: "b", period: "2026-02" } });
    assert.equal(abandoned.signal.aborted, true);
    assert.equal(h.calls.length, 3);
    abandoned.resolve(context("b", ["2026-01"])); await flush();
    assert.equal(h.state.context.value, null);
    h.calls[2].resolve(context("b", ["2026-02"])); await flush();
    assert.equal(h.routeSelectionReady.value, true);
    assert.deepEqual(page.requests.filter(item => item.company_id === "b").map(item => item.period), ["2026-02"]);
  } finally { page.close(); h.close(); }
});

test("T6 new company context keeps a supported month, falls back explicitly, and permits an empty period", async t => {
  for (const [label, periods, expected] of [
    ["same month", ["2026-03", "2026-01"], "2026-01"],
    ["default month", ["2026-03"], "2026-03"],
    ["no months", [], undefined],
  ]) await t.test(label, async () => {
    const h = await harness({ company_id: "b", period: "2026-01", employee_filter: "employment_active" });
    try {
      h.setAuthenticated(true);
      h.calls.at(-1).resolve(context("b", periods)); await flush();
      assert.equal(h.route.query.company_id, "b");
      assert.equal(h.route.query.period, expected);
      assert.equal(h.route.query.employee_filter, "employment_active", "explicit route selection is not page data or a cursor");
      if (label === "default month") assert.match(h.state.selectionNotice.value, /2026-03/);
      if (label === "no months") assert.match(h.state.selectionNotice.value, /没有可查看的月份/);
    } finally { h.close(); }
  });
});

test("T6 App ignores a former company's late context and fallback after navigation", async t => {
  for (const [label, period] of [
    ["company and period change in one navigation", "2026-02"],
    ["company changes with unchanged period", "2026-01"],
  ]) await t.test(label, async () => {
    const h = await harness();
    try {
      h.setAuthenticated(true);
      assert.equal(h.calls.length, 1);
      const abandoned = h.calls[0];
      h.navigate({ query: { company_id: "b", period } });
      assert.equal(h.calls.length, 2, "one atomic navigation starts exactly one context request");
      const current = h.calls[1];
      assert.equal(abandoned.signal.aborted, true);
      assert.equal(current.signal.aborted, false, "the new request survives the same route commit");
      const latestQuery = { company_id: "b", period, voucher: "7", section: "activity" };
      h.navigate({ query: latestQuery, hash: "#activity" });
      assert.equal(h.calls.length, 2, "deep-link-only changes do not start another context request");
      assert.equal(current.signal.aborted, false, "deep-link-only changes do not cancel context");
      current.resolve(context("b", [period])); await flush();
      assert.equal(h.calls.length, 2, "adopting the loaded context does not refetch it");
      assert.equal(h.state.context.value.current_company.company_id, "b");
      assert.equal(h.state.loading.value, false);
      const replacementCount = h.replacements.length;
      abandoned.resolve(context("a", ["2025-12"])); await flush();
      assert.deepEqual(h.route.query, latestQuery);
      assert.equal(h.route.hash, "#activity");
      assert.equal(h.state.context.value.current_company.company_id, "b");
      assert.equal(h.state.loading.value, false);
      assert.equal(h.replacements.length, replacementCount);
      assert.equal(h.contextError.value, "");
      assert.equal(h.state.error.value, "");
      assert.equal(h.calls.length, 2);
    } finally { h.close(); }
  });
});

test("T6 refresh replaces month options and safely reconciles an unavailable current month", async () => {
  const h = await harness();
  try {
    h.setAuthenticated(true);
    assert.equal(h.calls.length, 1);
    h.calls.at(-1).resolve(context("a", ["2026-01"])); await flush();
    assert.equal(h.calls.length, 1);
    const refreshed = h.state.refresh();
    assert.equal(h.calls.length, 2, "an explicit refresh adds one context request");
    assert.equal(h.state.context.value.current_company.company_id, "a", "refresh keeps the mounted company while loading");
    h.calls[1].resolve(context("a", ["2026-02"])); await refreshed; await flush();
    assert.equal(h.state.context.value.periods[0].key, "2026-02");
    assert.equal(h.route.query.period, "2026-02", "the URL must no longer point to a removed month");
    assert.equal(h.calls.length, 2, "normalizing the refreshed period does not fetch context again");
  } finally { h.close(); }
});

test("T6 month changes while refresh waits cannot be overwritten by an older selection", async () => {
  const h = await harness();
  try {
    h.setAuthenticated(true);
    assert.equal(h.calls.length, 1);
    h.calls.at(-1).resolve(context("a", ["2026-01", "2026-02"])); await flush();
    assert.equal(h.calls.length, 1);
    const pending = h.state.refresh();
    assert.equal(h.calls.length, 2);
    const refreshed = h.calls[1];
    h.navigate({ query: { company_id: "a", period: "2026-02", employee_filter: "employment_active" } });
    assert.equal(h.calls.length, 2, "a month change with existing context does not start another refresh");
    assert.equal(refreshed.signal.aborted, false);
    refreshed.resolve(context("a", ["2026-01", "2026-02"])); await pending; await flush();
    assert.equal(h.route.query.period, "2026-02");
    assert.equal(h.route.query.employee_filter, "employment_active");
    assert.equal(h.calls.length, 2);
  } finally { h.close(); }
});

test("T6 report fallback uses the latest available quarter month and names the actual fallback", async () => {
  const h = await harness({ company_id: "b", period: "2025-12", quarter: "2026-Q1" }, "reports");
  try {
    h.setAuthenticated(true);
    assert.equal(h.calls.length, 1);
    h.calls[0].resolve(context("b", ["2026-01", "2026-03", "2026-02", "2026-06"], "2026-06")); await flush();
    assert.equal(h.route.query.period, "2026-03");
    assert.equal(h.route.query.quarter, "2026-Q1");
    assert.match(h.state.selectionNotice.value, /2026-03/);
    assert.doesNotMatch(h.state.selectionNotice.value, /2026-06/);
    assert.equal(h.calls.length, 1, "the explicit quarter fallback uses the already loaded context");
  } finally { h.close(); }
});

test("T6 an initial URL without company still restores the saved company before selecting its month", async () => {
  const h = await harness({ period: "2026-01" });
  try {
    h.environment.storage.setItem("finance-dashboard-company-id", "b");
    h.setAuthenticated(true);
    assert.equal(h.calls.length, 1);
    h.calls[0].resolve(context("a", ["2026-01"])); await flush();
    assert.equal(h.route.query.company_id, "b");
    assert.equal(h.calls.length, 2, "restoring a saved company is a distinct legitimate context request");
    assert.equal(new URLSearchParams(h.calls[1].query).get("company_id"), "b");
    h.calls[1].resolve(context("b", ["2026-03"])); await flush();
    assert.equal(h.route.query.period, "2026-03");
    assert.equal(h.state.context.value.current_company.company_id, "b");
    assert.equal(h.calls.length, 2, "selecting the saved company's month does not add a third request");
  } finally { h.close(); }
});

test("T7 an initial pending context restarts once when only the period changes", async () => {
  const h = await harness();
  try {
    h.setAuthenticated(true);
    assert.equal(h.calls.length, 1);
    const abandoned = h.calls[0];
    h.navigate({ query: { company_id: "a", period: "2026-02" } });
    assert.equal(h.calls.length, 2, "a new period without loaded context starts one replacement request");
    const current = h.calls[1];
    assert.equal(abandoned.signal.aborted, true);
    assert.equal(current.signal.aborted, false);
    abandoned.resolve(context("a", ["2026-01"])); await flush();
    assert.equal(h.route.query.period, "2026-02");
    assert.equal(h.state.context.value, null);
    assert.equal(h.state.loading.value, true, "the abandoned finally cannot stop the replacement request");
    assert.equal(h.contextError.value, "");
    current.resolve(context("a", ["2026-01", "2026-02"])); await flush();
    assert.equal(h.route.query.period, "2026-02");
    assert.equal(h.state.context.value.current_company.company_id, "a");
    assert.equal(h.state.loading.value, false);
    assert.equal(h.contextError.value, "");
    assert.equal(h.state.error.value, "");
    assert.equal(current.signal.aborted, false);
    assert.equal(h.calls.length, 2);
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

test("sidebar calendar month changes retain employee filters, clear detail targets and align report quarters", async () => {
  const h = await harness({
    company_id: "a",
    period: "2026-01",
    employee_filter: "employment_active",
    employee_id: "old-employee",
    cursor: "old-cursor",
    voucher: "7",
  });
  try {
    h.setAuthenticated(true);
    h.calls.at(-1).resolve(context("a", ["2025-12", "2026-01", "2026-04"]));
    await flush();
    h.route.hash = "#old-detail";
    await h.selectPeriod("2026-04");
    assert.deepEqual(h.pushes.at(-1), {
      query: { company_id: "a", period: "2026-04", quarter: undefined, employee_filter: "employment_active" },
      hash: "",
    });
    assert.deepEqual(h.route.query, { company_id: "a", period: "2026-04", employee_filter: "employment_active" });

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

test("the sidebar month picker preserves all employee selections including the default", async () => {
  for (const selected of [undefined, "all", "in_period", "ended", "unknown", "employment_active", "employment_unpaid_leave", "employment_departed", "employment_unknown"]) {
    const h = await harness({ company_id: "a", period: "2026-01", employee_filter: selected });
    try {
      h.setAuthenticated(true);
      h.calls.at(-1).resolve(context("a", ["2026-01", "2026-04"])); await flush();
      await h.selectPeriod("2026-04");
      assert.equal(h.route.query.period, "2026-04");
      assert.equal(h.route.query.employee_filter, selected);
    } finally { h.close(); }
  }
});

test("the sidebar month picker preserves asset filters and clears asset and project navigation state", async () => {
  for (const selected of [undefined, "active", "all", "fixed", "intangible", "pending", "exited"]) {
    const h = await harness({ company_id: "a", period: "2026-01", asset_filter: selected,
      asset_id: "old-asset", project_id: "old-project", cursor: "old-cursor", voucher: "7" }, "assets");
    try {
      h.setAuthenticated(true);
      h.calls.at(-1).resolve(context("a", ["2026-01", "2026-04"])); await flush();
      h.route.hash = "#asset-card-target";
      await h.selectPeriod("2026-04");
      assert.deepEqual(h.route.query, { company_id: "a", period: "2026-04", ...(selected === undefined ? {} : { asset_filter: selected }) });
      assert.equal(h.route.hash, "");
      assert.equal(h.calls.length, 1, "the same-company calendar selection reuses context");
    } finally { h.close(); }
  }
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
      assert.equal(h.calls.length, 1);
      h.calls[0].resolve(context("a", ["2026-01"])); await flush();
      assert.equal(h.calls.length, 1);
      await h.selectCompany("b");
      assert.equal(h.calls.length, 2, "the company and cleared quarter commit starts one request");
      const current = h.calls[1];
      assert.equal(current.signal.aborted, false);
      current.resolve(context("b", periods, periods[0] ?? null)); await flush();
      assert.equal(h.route.query.period, expected);
      assert.equal(h.route.query.company_id, "b");
      assert.equal(h.route.query.quarter, undefined, "Reports must derive the new quarter from the validated shared month");
      assert.equal(h.route.query.carry_forward_fact_id, undefined);
      if (expected === "2026-06") assert.match(h.state.selectionNotice.value, /2026-06/);
      if (expected === undefined) assert.match(h.state.selectionNotice.value, /没有可查看的月份/);
      assert.equal(current.signal.aborted, false);
      assert.equal(h.calls.length, 2, "normalizing the company month does not fetch context again");
    } finally { h.close(); }
  });
});

test("T6 sidebar changes company with shared selection only and clears page-specific state", async () => {
  const h = await harness({ company_id: "a", period: "2026-01", employee_filter: "employment_active", cursor: "never-transfer", voucher: "7" });
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
