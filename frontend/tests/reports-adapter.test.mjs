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
  for (const [, relative] of [...outputText.matchAll(/(?:from |import\()"(\.[^"]+)"/g)]) {
    const modulePath = /\.[cm]?[jt]s$/.test(relative) ? relative : `${relative}.ts`;
    outputText = outputText.replaceAll(`"${relative}"`, JSON.stringify(await moduleUrl(new URL(modulePath, url))));
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

function dashboardContext(companyId, periods = [["2026-01", "open"]]) {
  const company = { company_id: companyId, name: companyId, taxpayer_id: null, status: "active" };
  const months = periods.map(([key, status]) => {
    const [year, month] = key.split("-").map(Number);
    return { key, year, month, label: key, short_label: key, status, start_date: `${key}-01`, end_date: `${key}-28`, closed_at: null };
  });
  return {
    schema_version: 3, company: company.name, companies: [company], current_company: company,
    periods: months, quarters: [], default_period: months.at(-1)?.key ?? null, default_quarter: null,
  };
}

test("quarterly export follows the same company and exact preview without an internal source selector", async () => {
  globalThis.window = { location: { origin: "http://127.0.0.1:7000", search: "?company_id=company-a" } };
  const generatedSamples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  const report = structuredClone(generatedSamples.quarterly_report.response);
  report.read_context.company_id = "company-a";
  report.export = { ...report.export, available: true, preview_digest: "digest", epochs: { accounting: 1, material: 2, management: 3 } };
  globalThis.fetch = async (url, options) => {
    if (url.startsWith("/api/dashboard/")) {
      const query = new URL(url, window.location.origin).searchParams;
      assert.equal(query.get("company_id"), "company-a");
      assert.equal(query.has("carry_forward_fact_id"), false);
      return new Response(JSON.stringify(report));
    }
    assert.equal(url, "/api/local/report-export");
    assert.deepEqual(JSON.parse(options.body), {
      company_id: "company-a", year: report.period.year, quarter: report.period.quarter,
      preview_digest: "digest", epochs: report.export.epochs,
      request_id: "request-one",
    });
    return new Response(JSON.stringify({ status: "queued", job_id: "job", preview_digest: "digest" }));
  };
  const preview = await reports.fetchDeferredQuarterlyReport("company-a", report.period.year, report.period.quarter);
  assert.deepEqual(await reports.requestQuarterlyExport("company-a", preview, "request-one"), {
    status: "queued", job_id: "job", preview_digest: "digest",
  });
});

test("invalid report delivery retains the service reason and is not described as a completed file", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ code: "report_download_invalid", message: "报表文件校验未通过，请重新生成" }), { status: 409 });
  await assert.rejects(reports.fetchQuarterlyWorkbook("company-a", "job"), error => error.code === "report_download_invalid" && error.message.includes("重新生成"));

});

function restoreGlobalsAfterTest(t, keys) {
  const saved = keys.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
  t.after(() => {
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
}

const reportStatus = (changes = {}) => ({ schema_version: 1, company_id: "company-a", database_id: "database-a",
  job_id: "older/job ?", status: "running", attempts: 1, error_code: null, error_message: null, ...changes });

test("report status is read only, bound to the captured company and exact encoded job", async t => {
  restoreGlobalsAfterTest(t, ["window", "fetch"]);
  const controller = new AbortController();
  globalThis.window = { location: { origin: "http://offline.invalid", search: "?company_id=another-company" } };
  globalThis.fetch = async (path, options) => {
    assert.equal(path, "/api/local/report-export/older%2Fjob%20%3F/status?company_id=company-a");
    assert.equal(options.method, undefined);
    assert.equal(options.body, undefined);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.signal, controller.signal);
    return new Response(JSON.stringify(reportStatus()));
  };
  assert.deepEqual(await reports.fetchReportExportStatus("company-a", "older/job ?", controller.signal), reportStatus());
  for (const changed of [{ company_id: "company-b" }, { job_id: "different" }, { attempts: "1" }, { result: {} }, { status: "unknown" }]) {
    globalThis.fetch = async () => new Response(JSON.stringify(reportStatus(changed)));
    await assert.rejects(reports.fetchReportExportStatus("company-a", "older/job ?"), { code: "REPORT_STATUS_RESPONSE" });
  }
});

test("report download uses the company and job from its call and preserves expiration feedback", async t => {
  restoreGlobalsAfterTest(t, ["window", "fetch"]);
  let expired = 0;
  globalThis.window = { dispatchEvent(event) { assert.equal(event.type, "finance-session-expired"); expired++; } };
  globalThis.fetch = async (path, options) => {
    assert.equal(path, "/api/local/report-export/job%2Fa/download?company_id=company-a");
    assert.equal(options.credentials, "same-origin");
    return new Response("verified workbook");
  };
  assert.equal(await (await reports.fetchQuarterlyWorkbook("company-a", "job/a")).text(), "verified workbook");
  globalThis.fetch = async () => new Response(JSON.stringify({ code: "owner_session_required", message: "请重新登录" }), { status: 401 });
  await assert.rejects(reports.fetchQuarterlyWorkbook("company-a", "job/a"), { code: "owner_session_required" });
  assert.equal(expired, 1);
});

test("context refresh preserves the mounted company while replacing its periods", async () => {
  const state = useDashboardContext();
  state.cancel();
  const initial = dashboardContext("company-a");
  globalThis.fetch = async () => new Response(JSON.stringify(initial));
  await state.load();
  let finish;
  globalThis.fetch = () => new Promise(resolve => { finish = resolve; });
  const pending = state.refresh();
  assert.equal(state.context.value.current_company.company_id, "company-a");
  assert.equal(state.context.value.periods[0].status, "open");
  finish(new Response(JSON.stringify(dashboardContext("company-a", [["2026-01", "closed"], ["2026-02", "open"]]))));
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
  finish(new Response(JSON.stringify(dashboardContext("company-a"))));
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
    const { fetchReportExportStatus, fetchQuarterlyWorkbook, requestQuarterlyExport,
      DashboardApiError, LocalApiError, dashboardErrorMessage } = globalThis.${key};
    const useRoute = () => globalThis.${key}.route ?? ({ query: { company_id: 'company-a' } });
    const useRouter = () => ({ replace: async () => {}, push: async () => {} });
    const useDashboardContext = () => ({ context: ref(null), load: async () => ({}), refresh: async () => ({}) });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const formatFen = String;
  `;
  const { outputText } = ts.transpileModule(imports + source + "\nmounted = true; export { exportReport, invalidateRequests, report, needsRegeneration, exportNotice, visibleStatementRows, activeStatementKey, statementValue, taxTemplateMode, activeTemplateMeta, balanceTemplateRows, templateStatementValue, templateSectionLabel };", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const view = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  view.report.value = {
    period: { year: 2026, quarter: 3 },
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
  assert.deepEqual(view.visibleStatementRows.value.map((row) => row.line), [39, 47]);
  assert.equal(view.statementValue(view.visibleStatementRows.value[0].values.ending_fen), "—");
});

test("a terminal failure creates a new task on explicit regeneration", async () => {
  const requests = [];
  const view = await reportView({
    ...exportEnvironment(),
    requestQuarterlyExport: async (_company, _report, requestId) => { requests.push(requestId); return { job_id: `job-${requests.length}` }; },
    fetchReportExportStatus: async (_company, id) => ({ job_id: id, status: id === "job-1" ? "failed" : "succeeded", attempts: 3 }),
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
    fetchReportExportStatus: async (_company, id) => ({ job_id: id, status: "succeeded", attempts: 1 }),
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
    fetchReportExportStatus: async () => {
      if (++read === 1) throw new DOMException("Aborted", "AbortError");
      return { job_id: "accepted-job", status: "succeeded", attempts: 1 };
    },
    fetchQuarterlyWorkbook: async () => new Blob(["verified"]),
  });
  await view.exportReport();
  assert.equal(view.needsRegeneration.value, false);
  await view.exportReport();
  assert.equal(requested, 1);
});


test("switching company cancels status and prevents a late download from the previous company", async t => {
  restoreGlobalsAfterTest(t, ["document", "crypto"]);
  for (const heldStage of ["status", "download"]) {
    let finish, entered;
    const started = new Promise(resolve => { entered = resolve; });
    const held = new Promise(resolve => { finish = resolve; });
    const route = { query: { company_id: "company-a" } };
    let signal, downloads = 0, clicks = 0;
    const view = await reportView({
      ...exportEnvironment(), route,
      requestQuarterlyExport: async () => ({ job_id: "accepted-job" }),
      fetchReportExportStatus: async (_company, _job, requestSignal) => {
        if (heldStage === "status") { signal = requestSignal; entered(); return held; }
        return { status: "succeeded", attempts: 1 };
      },
      fetchQuarterlyWorkbook: async (_company, _job, requestSignal) => {
        downloads++; signal = requestSignal; entered(); return held;
      },
    });
    globalThis.document.createElement = () => ({ click() { clicks++; }, remove() {} });
    const pending = view.exportReport();
    await started;
    route.query.company_id = "company-b";
    view.invalidateRequests();
    assert.equal(signal.aborted, true);
    finish(heldStage === "status" ? { status: "succeeded", attempts: 1 } : new Blob(["old-company"]));
    await pending;
    assert.equal(downloads, heldStage === "status" ? 0 : 1);
    assert.equal(clicks, 0);
    assert.equal(view.exportNotice.value, "");
  }
});

test("tax template mode restores full rows, form identities and exact yuan amounts", async () => {
  const view = await reportView(exportEnvironment());
  const columns = [{ key: "ending_fen", label: "期末余额" }, { key: "beginning_fen", label: "年初余额" }];
  const rows = Array.from({ length: 53 }, (_, index) => ({ line: index + 1, name: `项目${index + 1}`, values: { ending_fen: "0", beginning_fen: "0" }, has_amount: false, is_total: false }));
  view.report.value.statements = [{ key: "balance_sheet", columns, rows }];
  view.activeStatementKey.value = "balance_sheet";
  assert.equal(view.visibleStatementRows.value.length, 0);
  view.taxTemplateMode.value = true;
  assert.equal(view.visibleStatementRows.value.length, 53);
  assert.equal(view.activeTemplateMeta.value.formCode, "会小企01表");
  assert.deepEqual(view.balanceTemplateRows.value.flatMap(pair => [pair.left, pair.right]).filter(cell => cell.kind === "row").map(cell => cell.row.line).sort((a, b) => a - b), rows.map(row => row.line));
  assert.equal(view.templateStatementValue("900719925474099301"), "9,007,199,254,740,993.01");
  assert.equal(view.templateStatementValue("-1"), "-0.01");
  assert.equal(view.templateStatementValue(null), "—");
  for (const [key, formCode] of [["profit_statement", "会小企02表"], ["cash_flow_statement", "会小企03表"]]) {
    view.report.value.statements.push({ key, rows, columns });
    view.activeStatementKey.value = key;
    assert.equal(view.activeTemplateMeta.value.formCode, formCode);
  }
  assert.equal(view.templateSectionLabel("cash_flow_statement", 14), "三、筹资活动产生的现金流量：");
  view.taxTemplateMode.value = false;
  assert.equal(view.visibleStatementRows.value.length, 0);
});
