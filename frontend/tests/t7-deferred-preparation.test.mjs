import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import { parse, compileTemplate } from "@vue/compiler-sfc";
import ts from "typescript";
import {
  validateDashboardBriefResponse,
  validateDashboardFundsResponse,
  validateDashboardQuarterlyReportResponse,
} from "../src/api/generated/dashboardValidators.js";

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
  return { schema_version: 3, projection: "dashboard_period_preparation_result", read_context: context, period, data: {
    period_preparation: { company_id: context.company_id, database_id: context.database_id, period, as_of: context.as_of,
      as_of_semantics: "current_knowledge", projection: "dashboard_period_preparation", closure: { state: "open" }, frozen_readiness: null, readiness: { issues: [] }, read_semantics: {},
      current_followups: { knowledge: "current_knowledge", affects_frozen_readiness: false,
        materials: { status: "ready", issues: [], inventory_count: 0, coverage_digest: "material-v1" },
        accounting: { status: "ready", issues: [], pending_subject_id: null, unpublished_count: 0 },
        close_requirements: { status: "ready", issues: [] }, settlements: { status: "ready", issues: [], complete: true, obligation_count: 0, movement_count: 0, source_amount_fen: "0", paid_fen: "0", other_settled_fen: "0", remaining_fen: "0" },
        tax_import_mapping: { status: "not_applicable", blocking_scope: "tax_import_file", mapping_fact_ids: [], calculation_ids: [], issues: [] }, external: { status: "ready", obligation_count: 0, completion_status_counts: {} }, file_jobs: { total_count: 0, issue_count: 0, status_counts: {} } } },
    brief_checks: { material_completeness: { closed: false, satisfied: true, issues: [] }, issues: [], attention_count: 0, items: items() },
  } };
}
function brief(period = "2026-02", context = readContext()) {
  return { schema_version: 5, projection: "dashboard_brief_deferred", read_context: context, snapshot_version: "page-v1", selected_period: { key: period, status: "open" }, data: {
    period_preparation: null, material_completeness: null, collections: { vouchers: page() },
    validation: { state: "pending", title: "账务核对", summary: "准备检查尚未完成", integrity_valid: true, attention_count: 0, issues: [], items: [{ key: "balance", label: "平衡", state: "pass", text: "平衡" }, ...items(true)] },
    total_debit_fen: "120", total_credit_fen: "120", vouchers: [], activity_groups: [], workforce_cost: { has_activity: false },
  } };
}
function report(context = readContext()) {
  return { schema_version: 3, projection: "dashboard_quarterly_report_deferred", read_context: context, period_preparations: null,
    period: { year: 2026, quarter: 1, label: "第一季度", quarter_end: "2026-03" }, statements: [], readiness: [],
    status: "ready", close_state: "closed", readiness_state: "ready", draft: false,
    export: { available: true, preview_digest: "preview-v1", epochs: { accounting: 1, material: 1, management: 1 }, file_name: "report.xlsx" },
    carry_forward: { selected_fact_id: null, options: [] }, technical: { requirement_codes: [] },
  };
}
class ApiError extends Error { constructor(status, code, message) { super(message); this.status = status; this.code = code; } }
const stale = () => new ApiError(409, "dashboard_snapshot_changed", "expired");

async function viewHarness(kind) {
  const calls = { main: [], checks: [], exports: [], context: [], review: [] }, trace = [], unmount = [];
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
    prefetchCloseReview: (companyId, period, signal) => ({
      companyId, period,
      result: capture(calls.review, [companyId, period, signal]).then(
        value => ({ status: "fulfilled", value }), reason => ({ status: "rejected", reason }),
      ),
    }),
    requestQuarterlyExport: (...args) => capture(calls.exports, args),
  };
  const script = withoutImports(parse(source(`../src/views/${kind}View.vue`)).descriptor.scriptSetup.content);
  const names = kind === "Brief"
    ? "response, data, loading, error, preparation, preparationStatus, preparationError, closeReviewPrefetch, closeReviewRefreshKey, loadData, loadMore, loadPreparation, refresh, invalidateRequests"
    : "report, loading, errorMessage, reportHeadline, preview, refresh, exportReport, invalidateRequests";
  const module = await compile(`export function instantiate() {
    const { computed, ref, shallowRef, watch } = environment.Vue;
    const { nextTick, fetchDeferredBrief, fetchDeferredQuarterlyReport, fetchPeriodPreparation, prefetchCloseReview, requestQuarterlyExport } = environment;
    const fetchCompleteBrief = fetchDeferredBrief;
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
  return { ...view, calls, trace, route, context,
    navigate(query) { route.query = { ...query }; },
    close() { unmount.forEach(callback => callback()); scope.stop(); } };
}

test("deferred request projections remain distinct from complete and unrelated page contracts", async () => {
  const samples = JSON.parse(source("./fixtures/dashboard-contracts.json"));
  assert(validateDashboardBriefResponse(samples.deferred_brief.response));
  assert(validateDashboardFundsResponse(samples.deferred_funds.response));
  assert(validateDashboardQuarterlyReportResponse(samples.deferred_quarterly_report.response));
  for (const [validator, response] of [
    [validateDashboardBriefResponse, samples.deferred_brief.response],
    [validateDashboardFundsResponse, samples.deferred_funds.response],
    [validateDashboardQuarterlyReportResponse, samples.deferred_quarterly_report.response],
  ]) {
    assert.equal(validator({ ...response, schema_version: 5 }), false);
    assert.equal(validator({ ...response, projection: "retired_projection" }), false);
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
    assert.deepEqual(h.data.value.collections.file_jobs.items, [], "the paged collection becomes visible after a snapshot replacement");
    assert.equal(h.preparationStatus.value, "ready"); assert.equal(h.data.value.validation.attention_count, 5);
    assert.equal(h.data.value.period_preparation.period, "2026-02");
  } finally { h.close(); }
});

test("Brief complete response shows the full preparation without a second request", async () => {
  const h = await viewHarness("Brief");
  try {
    const pending = h.loadData("2026-02");
    const full = brief();
    full.data.period_preparation = preparation().data.period_preparation;
    h.calls.main[0].resolve(full);
    await pending;
    assert.equal(h.preparationStatus.value, "ready");
    assert.equal(h.calls.checks.length, 0);
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
    const second = h.loadData("2026-02"); h.calls.main[1].reject(new Error("完整读取失败"));
    await flush();
    h.calls.main[2].reject(new Error("主数据失败")); await second;
    assert.equal(h.error.value, "主数据失败"); assert.equal(h.calls.checks.length, 0);
  } finally { h.close(); }
});

test("Brief does not mask a broken complete response with a deferred retry", async () => {
  for (const code of ["DASHBOARD_SCHEMA_MISMATCH", "response_contract_mismatch", "content_integrity_failed"]) {
    const h = await viewHarness("Brief");
    try {
      const pending = h.loadData("2026-02");
      h.calls.main[0].reject(new ApiError(502, code, "主响应合同或来源损坏"));
      await pending;
      assert.equal(h.calls.main.length, 1, `${code}: no fallback request`);
      assert.equal(h.calls.checks.length, 0);
      assert.equal(h.response.value, null);
      assert.equal(h.error.value, "主响应合同或来源损坏");
    } finally { h.close(); }
  }
});

test("Brief starts one scoped review summary with the initial and refreshed main requests", async () => {
  const h = await viewHarness("Brief");
  try {
    const first = h.loadData("2026-02");
    assert.equal(h.calls.main.length, 1);
    assert.equal(h.calls.review.length, 1, "review starts before the main response resolves");
    assert.deepEqual(h.calls.review[0].args.slice(0, 2), ["a", "2026-02"]);
    assert.equal(h.response.value, null, "review cannot display before main and context succeed");
    const complete = brief(); complete.data.period_preparation = preparation().data.period_preparation;
    h.calls.main[0].resolve(complete); await first;
    assert.equal(h.closeReviewPrefetch.value.companyId, "a");
    assert.equal(h.closeReviewRefreshKey.value, 1);
    h.context.value = { current_company: readContext(), periods: [{ key: "2026-02" }] };
    const refreshed = h.refresh();
    assert.equal(h.calls.context.length, 1);
    assert.equal(h.calls.main.length, 2);
    assert.equal(h.calls.review.length, 2, "one review summary per main refresh");
    assert(h.calls.review[0].args[2].aborted);
    h.calls.context[0].resolve(h.context.value);
    h.calls.main[1].resolve(complete); await refreshed;
    assert.equal(h.closeReviewRefreshKey.value, 2);
    assert.equal(h.calls.review.length, 2);
  } finally { h.close(); }
});

test("Brief rejects old review scope and clears prefetch after main failure", async () => {
  const h = await viewHarness("Brief");
  try {
    const old = h.loadData("2026-02");
    const oldPrefetch = h.closeReviewPrefetch.value;
    h.navigate({ company_id: "b", period: "2026-03" });
    assert.equal(h.closeReviewPrefetch.value, null);
    assert(h.calls.review[0].args[2].aborted);
    const current = h.loadData("2026-03");
    h.calls.review[0].resolve({ company_id: "a", period: "2026-02" });
    assert.equal((await oldPrefetch.result).status, "fulfilled");
    h.calls.main[0].resolve(brief()); await old;
    assert.equal(h.response.value, null);
    const complete = brief("2026-03", readContext("b"));
    complete.data.period_preparation = preparation("2026-03", readContext("b")).data.period_preparation;
    h.calls.main[1].resolve(complete); await current;
    assert.equal(h.response.value.read_context.company_id, "b");
    assert.equal(h.closeReviewPrefetch.value.companyId, "b");

    const failed = h.loadData("2026-03");
    h.calls.review[2].reject(new Error("核对失败"));
    h.calls.main[2].reject(new ApiError(502, "content_integrity_failed", "主数据损坏"));
    await failed;
    assert.equal(h.calls.main.length, 3, "broken main response is not retried");
    assert.equal(h.closeReviewPrefetch.value, null);
    assert.equal(h.response.value, null);
    assert.equal(h.error.value, "主数据损坏");
  } finally { h.close(); }
});

test("Brief context failure aborts the prefetched review and hides a successful main response", async () => {
  const h = await viewHarness("Brief");
  try {
    let rejectContext;
    const gate = new Promise((_resolve, reject) => { rejectContext = reject; });
    const pending = h.loadData("2026-02", gate);
    const complete = brief(); complete.data.period_preparation = preparation().data.period_preparation;
    h.calls.main[0].resolve(complete);
    rejectContext(new Error("公司范围已变化"));
    await pending;
    assert.equal(h.response.value, null);
    assert.equal(h.closeReviewPrefetch.value, null);
    assert(h.calls.review[0].args[2].aborted);
    assert.equal(h.error.value, "公司范围已变化");
  } finally { h.close(); }
});

test("Reports uses its own readiness without loading company-wide month followups", async () => {
  const h = await viewHarness("Reports");
  try {
    const pending = h.preview("2026-Q1");
    h.calls.main[0].resolve(report());
    await pending;
    assert.deepEqual(h.trace, ["main", "paint"]);
    assert.equal(h.calls.checks.length, 0);
    assert.equal(h.loading.value, false);
    assert.equal(h.reportHeadline.value, "已就绪");
    void h.exportReport();
    assert.equal(h.calls.exports.length, 1);
  } finally { h.close(); }
});

test("only Brief renders the shared company-wide period preparation", () => {
  for (const name of ["Brief", "Funds", "Employees", "Assets", "Reports"]) {
    const { descriptor } = parse(source(`../src/views/${name}View.vue`));
    const compiled = compileTemplate({ source: descriptor.template.content, filename: `${name}View.vue`, id: `test-${name}` });
    assert.deepEqual(compiled.errors, []);
    if (name === "Brief") assert.match(descriptor.template.content, /<PeriodPreparation\s+v-if="[^"]*=== 'ready'/);
    else assert.doesNotMatch(descriptor.template.content, /<PeriodPreparation/);
  }
  const text = source("../src/views/BriefView.vue");
  assert.match(text, /preparationStatus === 'ready' && data.validation.items.length > 0 && data.validation.items.every/);
});
