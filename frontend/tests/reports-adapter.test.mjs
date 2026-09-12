import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { randomUUID } from "node:crypto";
import * as Vue from "vue";
import ts from "typescript";

const modules = new Map();
globalThis.dashboardTestVue = Vue;
async function moduleUrl(url) {
  if (modules.has(url.href)) return modules.get(url.href);
  let { outputText } = ts.transpileModule(readFileSync(url, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  for (const [, relative] of [...outputText.matchAll(/from "(\.[^"]+)"/g)]) {
    outputText = outputText.replaceAll(`"${relative}"`, JSON.stringify(await moduleUrl(new URL(relative + ".ts", url))));
  }
  outputText = outputText.replace('from "vue"', 'from "data:text/javascript,export const ref=globalThis.dashboardTestVue.ref;export const readonly=globalThis.dashboardTestVue.readonly"');
  const value = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
  modules.set(url.href, value);
  return value;
}
const reports = await import(await moduleUrl(new URL("../src/api/reports.ts", import.meta.url)));
const local = await import(await moduleUrl(new URL("../src/api/localKernel.ts", import.meta.url)));
const { useDashboardContext } = await import(await moduleUrl(new URL("../src/composables/useDashboardContext.ts", import.meta.url)));
const client = await import(await moduleUrl(new URL("../src/api/client.ts", import.meta.url)));

test("chosen immutable continuation source follows the company through preview and export", async () => {
  globalThis.window = { location: { origin: "http://127.0.0.1:7000", search: "?company_id=company-a" } };
  const report = {
    schema_version: 1, period_preparations: [], period: { year: 2026, quarter: 3 },
    export: { available: true, preview_digest: "digest", epochs: { accounting: 1, material: 2, management: 3 } },
    carry_forward: { selected_fact_id: "immutable-source", options: [] },
  };
  globalThis.fetch = async (url, options) => {
    if (url.startsWith("/api/dashboard/")) {
      const query = new URL(url, window.location.origin).searchParams;
      assert.equal(query.get("company_id"), "company-a");
      assert.equal(query.get("carry_forward_fact_id"), "immutable-source");
      return new Response(JSON.stringify(report));
    }
    assert.equal(url, "/api/local/report-export");
    assert.deepEqual(JSON.parse(options.body), {
      company_id: "company-a", year: 2026, quarter: 3,
      preview_digest: "digest", epochs: report.export.epochs,
      request_id: "request-one", carry_forward_fact_id: "immutable-source",
    });
    return new Response(JSON.stringify({ job_id: "job" }));
  };
  const preview = await reports.fetchQuarterlyReport(2026, 3, undefined, "immutable-source");
  assert.deepEqual(await reports.requestQuarterlyExport("company-a", preview, "request-one"), { job_id: "job" });
});

test("invalid report delivery retains the service reason and is not described as a completed file", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ code: "report_download_invalid", message: "报表文件校验未通过，请重新生成" }), { status: 409 });
  await assert.rejects(reports.fetchQuarterlyWorkbook("company-a", "job"), error => error.code === "report_download_invalid" && error.message.includes("重新生成"));
  const job = { kind: "report_export", status: "succeeded", delivery_status: "invalid", delivery_message: "文件缺失，请重新生成", download_available: false };
  assert.equal(local.localJobDownloadAvailable(job), false);
  assert.equal(local.localJobMessage(job), "文件缺失，请重新生成");
});

test("context refresh preserves the mounted company while replacing its periods", async () => {
  const state = useDashboardContext();
  state.cancel();
  const initial = { schema_version: 2, current_company: { company_id: "company-a" }, periods: [{ key: "2026-01", status: "open" }] };
  globalThis.fetch = async () => new Response(JSON.stringify(initial));
  await state.load();
  let finish;
  globalThis.fetch = () => new Promise(resolve => { finish = resolve; });
  const pending = state.refresh();
  assert.equal(state.context.value.current_company.company_id, "company-a");
  assert.equal(state.context.value.periods[0].status, "open");
  finish(new Response(JSON.stringify({ ...initial, periods: [{ key: "2026-01", status: "closed" }, { key: "2026-02", status: "open" }] })));
  await pending;
  assert.equal(state.context.value.periods[0].status, "closed");
  assert.equal(state.context.value.periods.length, 2);
  state.cancel();
});

test("a late context response cannot restore the company cleared during a switch", async () => {
  const state = useDashboardContext();
  state.cancel();
  let finish;
  globalThis.fetch = () => new Promise(resolve => { finish = resolve; });
  const pending = state.load();
  state.cancel();
  finish(new Response(JSON.stringify({ schema_version: 2, current_company: { company_id: "old-company" } })));
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(state.context.value, null);
});

let harnessNumber = 0;
async function reportView(stubs) {
  const key = `reportViewHarness${++harnessNumber}`;
  globalThis[key] = stubs;
  const source = readFileSync(new URL("../src/views/ReportsView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const imports = `
    const { computed, nextTick, ref, watch } = globalThis.dashboardTestVue;
    const onMounted = () => {}; const onBeforeUnmount = () => {};
    const { fetchLocalJob, fetchQuarterlyWorkbook, requestQuarterlyExport,
      DashboardApiError, LocalApiError, dashboardErrorMessage } = globalThis.${key};
    const useRoute = () => ({ query: { company_id: 'company-a' } });
    const useRouter = () => ({ replace: async () => {}, push: async () => {} });
    const useDashboardContext = () => ({ context: ref(null), load: async () => ({}), refresh: async () => ({}) });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const formatFen = String;
  `;
  const { outputText } = ts.transpileModule(imports + source + "\nmounted = true; export { exportReport, report, needsRegeneration, exportNotice, visibleStatementRows, activeStatementKey, taxTemplateMode, statementValue };", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const view = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  view.report.value = {
    period: { year: 2026, quarter: 3 }, carry_forward: { selected_fact_id: null, options: [] },
    export: { available: true, preview_digest: "same-preview", file_name: "report.xlsx", epochs: {} },
  };
  return view;
}

function exportEnvironment() {
  globalThis.crypto ??= { randomUUID };
  globalThis.document = { body: { append() {} }, createElement() { return { click() {}, remove() {} }; } };
  return { DashboardApiError: client.DashboardApiError, LocalApiError: local.LocalApiError, dashboardErrorMessage: client.dashboardErrorMessage };
}

test("the default report view retains unknown amounts while hiding proved zero rows", async () => {
  const view = await reportView(exportEnvironment());
  const unknown = { line: 39, name: "其他应付款", values: { ending_fen: null, beginning_fen: "0" }, has_amount: false, is_total: false };
  const zero = { line: 38, name: "应付利润", values: { ending_fen: "0", beginning_fen: "0" }, has_amount: false, is_total: false };
  const total = { line: 47, name: "负债合计", values: { ending_fen: null, beginning_fen: "0" }, has_amount: false, is_total: true };
  view.report.value.statements = [{ key: "balance_sheet", rows: [zero, unknown, total] }];
  view.activeStatementKey.value = "balance_sheet";
  assert.equal(view.taxTemplateMode.value, false);
  assert.deepEqual(view.visibleStatementRows.value.map((row) => row.line), [39, 47]);
  assert.equal(view.statementValue(view.visibleStatementRows.value[0].values.ending_fen), "—");
  view.taxTemplateMode.value = true;
  assert.deepEqual(view.visibleStatementRows.value.map((row) => row.line), [38, 39, 47]);
});

test("a terminal failure creates a new task on explicit regeneration", async () => {
  const requests = [];
  const view = await reportView({
    ...exportEnvironment(),
    requestQuarterlyExport: async (_company, _report, requestId) => { requests.push(requestId); return { job_id: `job-${requests.length}` }; },
    fetchLocalJob: async (_company, id) => [{ id, status: id === "job-1" ? "failed" : "succeeded", attempts: 3 }],
    fetchQuarterlyWorkbook: async () => new Blob(["verified"]),
  });
  await view.exportReport();
  assert.equal(view.needsRegeneration.value, true);
  await view.exportReport();
  assert.equal(requests.length, 2);
  assert.notEqual(requests[0], requests[1]);
  assert.equal(view.needsRegeneration.value, false);
});

test("damaged report output does not trap regeneration on the old job", async () => {
  const requests = [];
  const view = await reportView({
    ...exportEnvironment(),
    requestQuarterlyExport: async (_company, _report, requestId) => { requests.push(requestId); return { job_id: `job-${requests.length}` }; },
    fetchLocalJob: async (_company, id) => [{ id, status: "succeeded", attempts: 1 }],
    fetchQuarterlyWorkbook: async (_company, id) => {
      if (id === "job-1") throw new local.LocalApiError(409, "report_download_invalid", "文件校验失败");
      return new Blob(["verified"]);
    },
  });
  await view.exportReport();
  assert.equal(view.needsRegeneration.value, true);
  await view.exportReport();
  assert.equal(new Set(requests).size, 2);
});

test("an interrupted wait resumes the accepted task without creating a duplicate", async () => {
  let requested = 0;
  let read = 0;
  const view = await reportView({
    ...exportEnvironment(),
    requestQuarterlyExport: async () => { requested += 1; return { job_id: "accepted-job" }; },
    fetchLocalJob: async () => {
      if (++read === 1) throw new DOMException("Aborted", "AbortError");
      return [{ id: "accepted-job", status: "succeeded", attempts: 1 }];
    },
    fetchQuarterlyWorkbook: async () => new Blob(["verified"]),
  });
  await view.exportReport();
  assert.equal(view.needsRegeneration.value, false);
  await view.exportReport();
  assert.equal(requested, 1);
});
