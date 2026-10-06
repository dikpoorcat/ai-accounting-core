import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import { parse, compileTemplate } from "@vue/compiler-sfc";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";
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
const readContext = (company = "a", version = "v1") => ({ company_id: company, database_id: `db-${company}`, read_version: version, as_of: "2026-09-13" });
const samples = JSON.parse(source("./fixtures/dashboard-contracts.json"));
function brief(period = "2026-02", context = readContext()) {
  const value = structuredClone(samples.deferred_brief.response);
  value.selected_period.key = period; value.read_context = context;
  return value;
}
function report(context = readContext()) {
  const value = structuredClone(samples.deferred_quarterly_report.response);
  value.read_context = context;
  value.export = { ...value.export, available: true, preview_digest: "preview-v1", epochs: { accounting: 1, material: 1, management: 1 }, file_name: "report.xlsx" };
  return value;
}
class ApiError extends Error { constructor(status, code, message) { super(message); this.status = status; this.code = code; } }

async function viewHarness(kind) {
  const calls = { main: [], checks: [], exports: [], context: [], review: [] }, trace = [], unmount = [];
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-02", quarter: "2026-Q1" }, hash: "" });
  const capture = (list, args) => new Promise((resolve, reject) => list.push({ args, resolve, reject }));
  const context = Vue.ref(null);
  const environment = { Vue, route, trace, unmount, appendDashboardCollection,
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
    ? "response, data, loading, error, reviewRequest, needsMonthlyReview, showMonthlyReview, loadData, loadMore, refresh, invalidateRequests"
    : "report, loading, errorMessage, reportHeadline, preview, refresh, exportReport, invalidateRequests";
  const module = await compile(`export function instantiate() {
    const { computed, ref, shallowReactive, shallowRef, watch } = environment.Vue;
    const { appendDashboardCollection } = environment;
    const { nextTick, fetchDeferredBrief, fetchDeferredQuarterlyReport, fetchPeriodPreparation, requestQuarterlyExport } = environment;
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

test("owner page contracts reject full preparation and technical projections", () => {
  for (const [validator, response] of [
    [validateDashboardBriefResponse, samples.deferred_brief.response],
    [validateDashboardFundsResponse, samples.deferred_funds.response],
    [validateDashboardQuarterlyReportResponse, samples.deferred_quarterly_report.response],
  ]) {
    assert(validator(response), JSON.stringify(validator.errors));
    assert.equal("period_preparation" in (response.data ?? response), false);
    assert.equal("period_preparations" in response, false);
    assert.equal(validator({ ...response, projection: "dashboard_period_preparation_result" }), false);
    const changed = structuredClone(response);
    if (changed.data) changed.data.period_preparation = {};
    else changed.period_preparations = [];
    assert.equal(validator(changed), false);
  }
});

test("Brief exposes its main data without requesting full preparation", async () => {
  const h = await viewHarness("Brief");
  try {
    const pending = h.loadData("2026-02");
    h.calls.main[0].resolve(brief()); await pending;
    assert(h.data.value);
    assert.equal(h.calls.checks.length, 0);
    assert.equal(h.loading.value, false);
  } finally { h.close(); }
});

test("Brief refresh only loads main and context, closing the old on-demand review", async () => {
  const h = await viewHarness("Brief");
  try {
    const first = h.loadData("2026-02");
    assert.equal(h.calls.main.length, 1);
    assert.equal(h.calls.review.length, 0);
    assert.equal(h.response.value, null);
    const complete = brief();
    complete.data.month_state = "open";
    complete.data.owner_review_request = { preview_digest: "a".repeat(64) };
    h.calls.main[0].resolve(structuredClone(complete)); await first;
    assert.equal(h.needsMonthlyReview.value, true);
    assert.equal(h.showMonthlyReview.value, false);
    h.showMonthlyReview.value = true;
    h.context.value = { current_company: readContext(), periods: [{ key: "2026-02" }] };
    const refreshed = h.refresh();
    assert.equal(h.calls.context.length, 1);
    assert.equal(h.calls.main.length, 2);
    assert.equal(h.calls.review.length, 0, "main refresh must not read review details");
    assert.equal(h.showMonthlyReview.value, false);
    assert.equal(h.reviewRequest.value, null, "old digest stops being available immediately");
    assert(h.calls.main[0].args[2].aborted);
    h.calls.context[0].resolve(h.context.value);
    const next = structuredClone(complete);
    next.data.owner_review_request.preview_digest = "b".repeat(64);
    h.calls.main[1].resolve(next); await refreshed;
    assert.equal(h.reviewRequest.value.preview_digest, "b".repeat(64));
    assert.equal(h.showMonthlyReview.value, false, "new digest does not reopen details");
    assert.equal(h.calls.review.length, 0);
  } finally { h.close(); }
});

test("Brief rejects old company scope and clears review state after main failure", async () => {
  const h = await viewHarness("Brief");
  try {
    const old = h.loadData("2026-02");
    h.showMonthlyReview.value = true;
    h.navigate({ company_id: "b", period: "2026-03" });
    assert.equal(h.showMonthlyReview.value, false);
    assert.equal(h.reviewRequest.value, null);
    assert(h.calls.main[0].args[2].aborted);
    const current = h.loadData("2026-03");
    h.calls.main[0].resolve(brief()); await old;
    assert.equal(h.response.value, null);
    const complete = brief("2026-03", readContext("b"));
    complete.data.month_state = "open";
    complete.data.owner_review_request = { preview_digest: "b".repeat(64) };
    h.calls.main[1].resolve(complete); await current;
    assert.equal(h.response.value.read_context.company_id, "b");
    assert.equal(h.reviewRequest.value.preview_digest, "b".repeat(64));
    h.showMonthlyReview.value = true;
    const failed = h.loadData("2026-03");
    assert.equal(h.showMonthlyReview.value, false);
    h.calls.main[2].reject(new ApiError(502, "content_integrity_failed", "主数据损坏"));
    await failed;
    assert.equal(h.calls.main.length, 3, "broken main response is not retried");
    assert.equal(h.calls.review.length, 0);
    assert.equal(h.reviewRequest.value, null);
    assert.equal(h.response.value, null);
    assert.equal(h.error.value, "主数据损坏");
  } finally { h.close(); }
});

test("Brief context failure aborts main request and hides its successful response and locator", async () => {
  const h = await viewHarness("Brief");
  try {
    let rejectContext;
    const gate = new Promise((_resolve, reject) => { rejectContext = reject; });
    const pending = h.loadData("2026-02", gate);
    const complete = brief();
    complete.data.month_state = "open";
    complete.data.owner_review_request = { preview_digest: "a".repeat(64) };
    h.calls.main[0].resolve(complete);
    rejectContext(new Error("公司范围已变化"));
    await pending;
    assert.equal(h.response.value, null);
    assert.equal(h.reviewRequest.value, null);
    assert.equal(h.showMonthlyReview.value, false);
    assert.equal(h.calls.review.length, 0);
    assert(h.calls.main[0].args[2].aborted);
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

test("all owner page templates omit full period preparation", () => {
  for (const name of ["Brief", "Funds", "Employees", "Assets", "Reports"]) {
    const { descriptor } = parse(source(`../src/views/${name}View.vue`));
    const compiled = compileTemplate({ source: descriptor.template.content, filename: `${name}View.vue`, id: `test-${name}` });
    assert.deepEqual(compiled.errors, []);
    assert.doesNotMatch(descriptor.template.content, /<PeriodPreparation|<pre[\s>]/);
    assert.doesNotMatch(descriptor.scriptSetup.content, /fetchPeriodPreparation/);
  }
});
