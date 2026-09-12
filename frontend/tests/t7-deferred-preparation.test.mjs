import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import { parse, compileTemplate } from "@vue/compiler-sfc";
import ts from "typescript";

const source = path => readFileSync(new URL(path, import.meta.url), "utf8");
const withoutImports = text => text.replace(/import[\s\S]*?from "[^"]+";/g, "");
let sequence = 0;
async function compile(code, environment = {}) {
  const key = `t7Deferred${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};\n${code}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  try { return await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`); }
  finally { delete globalThis[key]; }
}
const flush = async () => { for (let index = 0; index < 8; index++) { await Promise.resolve(); await Vue.nextTick(); } };
const readContext = (company = "a", version = "v1") => ({ company_id: company, database_id: `db-${company}`, read_version: version, as_of: "2026-09-13" });
const items = (pending = false) => ["materials", "accounting", "close_requirements"].map(key => ({ key, label: key, text: key, state: pending ? "pending" : "pass" }));
const page = () => ({ items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null, collection_version: "jobs-v1" } });
function preparation(period = "2026-02", context = readContext()) {
  return { schema_version: 1, projection: "dashboard_period_preparation_result", read_context: context, period, data: {
    period_preparation: { company_id: context.company_id, database_id: context.database_id, period, as_of: context.as_of,
      as_of_semantics: "current_knowledge", projection: "dashboard_period_preparation", closure: { state: "open" }, frozen_readiness: null, readiness: { issues: [] }, read_semantics: {},
      current_followups: { knowledge: "current_knowledge", affects_frozen_readiness: false,
        materials: { status: "ready", issues: [], inventory_count: 0, coverage_digest: "material-v1" },
        accounting: { status: "ready", issues: [], pending_subject_id: null, unpublished_count: 0 },
        close_requirements: { status: "ready", issues: [] }, settlements: { status: "ready", issues: [], complete: true, obligation_count: 0, movement_count: 0, source_amount_fen: "0", paid_fen: "0", other_settled_fen: "0", remaining_fen: "0" },
        external: { status: "ready", obligation_count: 0, completion_status_counts: {} }, file_jobs: { total_count: 0, issue_count: 0, status_counts: {} } } },
    brief_checks: { material_completeness: { closed: false, satisfied: true, issues: [] }, issues: [], attention_count: 0, items: items() },
  } };
}
function brief(period = "2026-02", context = readContext()) {
  return { schema_version: 2, projection: "dashboard_brief_deferred", read_context: context, snapshot_version: "page-v1", selected_period: { key: period, status: "open" }, data: {
    period_preparation: null, material_completeness: null, collections: { vouchers: page() },
    validation: { state: "pending", title: "账务核对", summary: "准备检查尚未完成", integrity_valid: true, attention_count: 0, issues: [], items: [{ key: "balance", label: "平衡", state: "pass", text: "平衡" }, ...items(true)] },
    total_debit_fen: "120", total_credit_fen: "120", vouchers: [], activity_groups: [], workforce_cost: { has_activity: false },
  } };
}
function report(context = readContext()) {
  return { schema_version: 1, projection: "dashboard_quarterly_report_deferred", read_context: context, period_preparations: null,
    period: { year: 2026, quarter: 1, label: "第一季度", quarter_end: "2026-03" }, statements: [], readiness: [],
    status: "ready", close_state: "closed", readiness_state: "ready", draft: false,
    export: { available: true, preview_digest: "preview-v1", epochs: { accounting: 1, material: 1, management: 1 }, file_name: "report.xlsx" },
    carry_forward: { selected_fact_id: null, options: [] }, technical: { requirement_codes: [] },
  };
}
class ApiError extends Error { constructor(status, code, message) { super(message); this.status = status; this.code = code; } }
const stale = () => new ApiError(409, "dashboard_snapshot_changed", "expired");

async function apiHarness() {
  const calls = [], window = { location: { origin: "http://offline.invalid", search: "?company_id=b" } };
  const contracts = await compile(source("../src/api/dashboardContracts.ts"));
  const environment = { ...contracts, window, LocalApiError: ApiError, verifyMoneyStrings() {},
    requestLocalJson(path, options) { return new Promise((resolve, reject) => calls.push({ path, ...options, resolve, reject })); } };
  const client = await compile(`const { window, LocalApiError, verifyMoneyStrings, requestLocalJson, validDashboardContract } = environment;\n${withoutImports(source("../src/api/client.ts"))}`, environment);
  Object.assign(environment, client);
  const api = await compile(`const { requestJson, DashboardApiError } = environment;\n${withoutImports(source("../src/api/periodPreparation.ts"))}`, environment);
  const briefApi = await compile(`const { requestJson, pageQuery } = environment;\n${withoutImports(source("../src/api/brief.ts"))}`, environment);
  const reportApi = await compile(`const { requestJson, DashboardApiError, requestLocalJson, LocalApiError } = environment;\n${withoutImports(source("../src/api/reports.ts"))}`, environment);
  return { ...api, ...briefApi, ...reportApi, ...contracts, calls, window };
}

async function viewHarness(kind) {
  const calls = { main: [], checks: [], exports: [], context: [] }, trace = [], unmount = [];
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-02", quarter: "2026-Q1" }, hash: "" });
  const capture = (list, args) => new Promise((resolve, reject) => list.push({ args, resolve, reject }));
  const context = Vue.ref(null);
  const environment = { Vue, route, trace, unmount,
    nextTick: async () => { await Vue.nextTick(); trace.push("paint"); },
    router: { push() {}, replace() {} },
    contextState: { context, load: async () => ({ periods: [{ key: "2026-02", year: 2026, month: 2 }], quarters: [{ key: "2026-Q1", year: 2026, quarter: 1 }] }), refresh: () => capture(calls.context, []) },
    fetchDeferredBrief: (...args) => { trace.push("main"); return capture(calls.main, args); },
    fetchDeferredQuarterlyReport: (...args) => { trace.push("main"); return capture(calls.main, args); },
    fetchPeriodPreparation: (...args) => { trace.push("checks"); return capture(calls.checks, args); },
    requestQuarterlyExport: (...args) => capture(calls.exports, args),
  };
  const script = withoutImports(parse(source(`../src/views/${kind}View.vue`)).descriptor.scriptSetup.content);
  const names = kind === "Brief"
    ? "response, data, loading, error, preparation, preparationStatus, preparationError, loadData, loadMore, loadPreparation, refresh, invalidateRequests"
    : "report, loading, errorMessage, monthChecks, monthlyPreparations, reportHeadline, preview, loadPreparations, refresh, exportReport, invalidateRequests";
  const module = await compile(`export function instantiate() {
    const { computed, ref, watch } = environment.Vue;
    const { nextTick, fetchDeferredBrief, fetchDeferredQuarterlyReport, fetchPeriodPreparation, requestQuarterlyExport } = environment;
    const onMounted = () => {}, onBeforeUnmount = callback => environment.unmount.push(callback);
    const useRoute = () => environment.route, useRouter = () => environment.router, useDashboardContext = () => environment.contextState;
    const useDashboardSections = () => ({ activeSection: ref("overview"), focusSection() {} });
    const dashboardErrorMessage = error => error.name === "AbortError" ? "" : error.message;
    const isDashboardSnapshotChanged = error => error.code === "dashboard_snapshot_changed";
    const businessStateLabel = state => state, formatFen = value => String(value), formatPositiveFen = formatFen, fen = value => BigInt(value ?? 0);
    const document = { getElementById: () => null }, DashboardApiError = environment.ApiError, LocalApiError = environment.ApiError;
    ${script}
    mounted = true;
    return { ${names} };
  }`, { ...environment, ApiError });
  const scope = Vue.effectScope(), view = scope.run(() => module.instantiate());
  return { ...view, calls, trace, route,
    navigate(query) { route.query = { ...query }; },
    close() { unmount.forEach(callback => callback()); scope.stop(); } };
}

test("deferred request projections remain distinct from complete and unrelated page contracts", async () => {
  const { validDashboardContract: valid } = await apiHarness();
  const deferred = "/api/dashboard/brief?company_id=a&period=2026-02&preparation=deferred";
  const full = brief(); delete full.projection; delete full.read_context;
  full.data.period_preparation = preparation().data.period_preparation;
  assert(valid(deferred, brief())); assert(!valid("/api/dashboard/brief", brief())); assert(!valid(deferred, full)); assert(valid("/api/dashboard/brief", full));
  assert(!valid("/api/dashboard/brief", { ...full, projection: "unknown_projection" }));
  for (const value of [-1, NaN, 1.5, undefined]) { const malformed = brief(); malformed.data.validation.attention_count = value; assert(!valid(deferred, malformed)); }
  for (const value of [undefined, "true", 0]) { const malformed = brief(); malformed.data.validation.integrity_valid = value; assert(!valid(deferred, malformed)); }
  const missing = brief(); missing.data.validation.items = []; assert(!valid(deferred, missing));
  assert(valid(deferred, { schema_version: 2, projection: "dashboard_brief_deferred", data: null, read_context: null }));
  assert(!valid(deferred, { schema_version: 2, data: null, read_context: null }));
  assert(!valid("/api/dashboard/funds?preparation=deferred", { ...brief(), data: { collections: { movements: page() }, period_preparation: null } }));
  const quarterly = "/api/dashboard/quarterly-report?company_id=a&year=2026&quarter=1&preparation=deferred";
  assert(valid(quarterly, report())); assert(!valid(quarterly, { ...report(), period_preparations: [] }));
  assert(!valid("/api/dashboard/quarterly-report", report()));
  assert(!valid("/api/dashboard/quarterly-report", { ...report(), projection: "unknown_projection", period_preparations: [] }));
});

test("preparation API captures the company and rejects wrong month, version, date or database", async () => {
  const h = await apiHarness();
  const request = h.fetchPeriodPreparation(readContext(), "2026-02");
  const url = new URL(h.calls[0].path, "http://offline.invalid");
  assert.equal(url.searchParams.get("company_id"), "a", "global current company b cannot replace captured a");
  h.window.location.search = "?company_id=c";
  h.calls[0].resolve(preparation()); assert.equal((await request).read_context.company_id, "a");
  for (const [field, value] of [["period", "2026-03"], ["read_version", "v2"], ["as_of", "2026-09-14"], ["database_id", "db-replaced"]]) {
    const pending = h.fetchPeriodPreparation(readContext(), "2026-02");
    const result = preparation();
    if (field === "period") { result.period = value; result.data.period_preparation.period = value; }
    else { result.read_context[field] = value; if (field !== "read_version") result.data.period_preparation[field] = value; }
    h.calls.at(-1).resolve(result);
    await assert.rejects(pending, error => ["DASHBOARD_SCHEMA_MISMATCH", "dashboard_snapshot_changed"].includes(error.code));
  }
});

test("Brief displays main data before checking and combines only returned display checks", async () => {
  const h = await viewHarness("Brief");
  try {
    const pending = h.loadData("2026-02"); assert.equal(h.calls.checks.length, 0);
    const main = brief(); main.data.validation.attention_count = 2;
    h.calls.main[0].resolve(main); await pending;
    assert.equal(h.loading.value, false); assert.equal(h.data.value.total_debit_fen, "120");
    assert.equal(h.preparationStatus.value, "loading"); assert.equal(h.data.value.validation.state, "pending");
    assert.deepEqual(h.trace, ["main", "paint", "checks"]);
    const result = preparation(); result.data.brief_checks.attention_count = 3;
    result.data.brief_checks.items[0].state = "pending";
    result.data.brief_checks.issues = [{ message: "待核对" }];
    h.calls.checks[0].resolve(result); await flush();
    assert.equal(h.preparationStatus.value, "ready"); assert.equal(h.data.value.validation.attention_count, 5);
    assert.equal(h.data.value.validation.state, "attention"); assert.equal(h.data.value.validation.items[0].key, "balance");
    assert.equal(h.response.value.data.period_preparation, null, "the main response remains a deferred projection");
    for (const status of ["loading", "error", "stale"]) {
      h.preparationStatus.value = status;
      assert.equal(h.data.value.period_preparation, null, "a retained result is displayable only in ready state");
      assert.equal(h.data.value.validation.attention_count, 2);
    }
    h.preparationStatus.value = "ready";
    const paging = h.loadMore("file_jobs");
    assert.equal(h.calls.main[1].args[0], "a"); assert.equal(h.calls.main[1].args[3], "page-v1");
    const next = brief(); next.data.collections = { file_jobs: page() };
    h.calls.main[1].resolve(next); await paging;
    assert.equal(h.preparationStatus.value, "ready"); assert.equal(h.data.value.validation.attention_count, 5);
    assert.equal(h.data.value.period_preparation.period, "2026-02");
  } finally { h.close(); }
});

test("Brief local failure, retry and stale version preserve main data without refresh loops", async () => {
  const h = await viewHarness("Brief");
  try {
    const load = h.loadData("2026-02"); h.calls.main[0].resolve(brief()); await load;
    h.calls.checks[0].reject(new Error("检查失败")); await flush();
    assert.equal(h.preparationStatus.value, "error"); assert.equal(h.error.value, ""); assert(h.data.value);
    const retry = h.loadPreparation(); assert.equal(h.calls.main.length, 1);
    h.calls.checks[1].reject(stale()); await retry;
    assert.equal(h.preparationStatus.value, "stale"); assert.equal(h.data.value.validation.state, "pending");
    await h.loadPreparation(); assert.equal(h.calls.checks.length, 2, "stale results require a manual main refresh");
  } finally { h.close(); }
});

test("Brief switching company/month rejects late check success, failure and finally", async t => {
  for (const outcome of ["success", "error"]) await t.test(outcome, async () => {
    const h = await viewHarness("Brief");
    try {
      const first = h.loadData("2026-02"); h.calls.main[0].resolve(brief()); await first;
      const old = h.calls.checks[0];
      h.navigate({ company_id: "b", period: "2026-03" });
      assert(old.args[2].aborted); assert.equal(h.data.value, null);
      const next = h.loadData("2026-03"); h.calls.main[1].resolve(brief("2026-03", readContext("b"))); await next;
      if (outcome === "success") old.resolve(preparation()); else old.reject(new Error("late failure"));
      await flush(); assert.equal(h.preparationStatus.value, "loading"); assert.equal(h.preparationError.value, "");
      h.calls.checks[1].resolve(preparation("2026-03", readContext("b"))); await flush();
      assert.equal(h.data.value.period_preparation.company_id, "b");
    } finally { h.close(); }
  });
});

test("Brief empty main and main failures do not start preparation requests", async () => {
  const h = await viewHarness("Brief");
  try {
    const first = h.loadData(null); h.calls.main[0].resolve({ ...brief(), data: null, read_context: null, selected_period: null }); await first;
    assert.equal(h.calls.checks.length, 0); assert.equal(h.loading.value, false);
    const second = h.loadData("2026-02"); h.calls.main[1].reject(new Error("主数据失败")); await second;
    assert.equal(h.error.value, "主数据失败"); assert.equal(h.calls.checks.length, 0);
  } finally { h.close(); }
});

test("Reports checks the focused month first and serializes all three months independently", async () => {
  const h = await viewHarness("Reports");
  try {
    const pending = h.preview("2026-Q1"); h.calls.main[0].resolve(report()); await pending;
    assert.deepEqual(h.trace, ["main", "paint", "checks"]); assert.equal(h.loading.value, false);
    assert.equal(h.calls.checks.length, 1); assert.equal(h.calls.checks[0].args[1], "2026-02");
    assert.deepEqual(h.monthChecks.value.map(month => month.status), ["loading", "pending", "pending"]);
    h.calls.checks[0].reject(new Error("二月失败")); await flush();
    assert.equal(h.calls.checks.length, 2); assert.equal(h.calls.checks[1].args[1], "2026-01");
    h.calls.checks[1].resolve(preparation("2026-01")); await flush();
    assert.equal(h.calls.checks[2].args[1], "2026-03"); h.calls.checks[2].resolve(preparation("2026-03")); await flush();
    assert.deepEqual(h.monthChecks.value.map(month => month.status), ["error", "ready", "ready"]);
    assert.equal(h.errorMessage.value, ""); assert.equal(h.reportHeadline.value, "本季度报表已准备好");
    void h.exportReport(); assert.equal(h.calls.exports.length, 1, "monthly followup failure cannot change export eligibility");
    const retry = h.loadPreparations("2026-02"); assert.equal(h.calls.main.length, 1);
    h.calls.checks[3].resolve(preparation()); await retry;
    assert(h.monthChecks.value.every(month => month.status === "ready"));
  } finally { h.close(); }
});

test("Reports stale version stops the queue and requires a manual refresh", async () => {
  const h = await viewHarness("Reports");
  try {
    const pending = h.preview("2026-Q1"); h.calls.main[0].resolve(report()); await pending;
    h.calls.checks[0].reject(stale()); await flush();
    assert(h.monthChecks.value.every(month => month.status === "stale"));
    await h.loadPreparations(); assert.equal(h.calls.checks.length, 1); assert.equal(h.calls.main.length, 1);
    assert.equal(h.report.value.export.available, true); assert.equal(h.reportHeadline.value, "本季度报表已准备好");
  } finally { h.close(); }
});

test("Reports navigation and unmount abort old queues including ignored abort responses", async () => {
  const h = await viewHarness("Reports");
  try {
    const first = h.preview("2026-Q1"); h.calls.main[0].resolve(report()); await first;
    const old = h.calls.checks[0];
    h.navigate({ company_id: "b", period: "2026-02", quarter: "2026-Q1", carry_forward_fact_id: "source-b" });
    assert(old.args[2].aborted); assert.equal(h.monthChecks.value.length, 0);
    const second = h.preview("2026-Q1"); h.calls.main[1].resolve(report(readContext("b"))); await second;
    old.resolve(preparation()); await flush();
    assert.equal(h.calls.checks.length, 2, "the old queue does not request its next month");
    assert.equal(h.calls.main[1].args[4], "source-b");
    h.close(); assert(h.calls.checks[1].args[2].aborted);
    h.calls.checks[1].resolve(preparation("2026-02", readContext("b"))); await flush();
    assert.equal(h.calls.checks.length, 2); assert.equal(h.monthChecks.value.length, 0);
  } finally { h.close(); }
});

test("both view templates compile and require ready preparation before rendering its details", () => {
  for (const name of ["Brief", "Reports"]) {
    const { descriptor } = parse(source(`../src/views/${name}View.vue`));
    const compiled = compileTemplate({ source: descriptor.template.content, filename: `${name}View.vue`, id: `test-${name}` });
    assert.deepEqual(compiled.errors, []);
    assert.match(descriptor.template.content, /<PeriodPreparation v-if="[^"]*=== 'ready'/);
  }
  const text = source("../src/views/BriefView.vue");
  assert.match(text, /preparationStatus === 'ready' && data.validation.items.length > 0 && data.validation.items.every/);
});
