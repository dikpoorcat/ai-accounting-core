import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { test } from "node:test";
import ts from "typescript";

import {
  validateDashboardAssetsDeliveryResponse,
  validateDashboardBriefDeliveryResponse,
  validateDashboardEmployeesDeliveryResponse,
  validateDashboardFundsDeliveryResponse,
  validateDashboardQuarterlyReportDeliveryResponse,
} from "../src/api/generated/dashboardValidators.js";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const deliveryContracts = [
  ["brief", "dashboard_brief", validateDashboardBriefDeliveryResponse, samples.deferred_brief.response],
  ["funds", "dashboard_funds", validateDashboardFundsDeliveryResponse, samples.deferred_funds.response],
  ["employees", "dashboard_employees", validateDashboardEmployeesDeliveryResponse, samples.employees.response],
  ["assets", "dashboard_assets", validateDashboardAssetsDeliveryResponse, samples.assets.response],
  ["quarterly report", "dashboard_quarterly_report", validateDashboardQuarterlyReportDeliveryResponse, samples.deferred_quarterly_report.response],
];

function createClient(request) {
  const source = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8")
    .replace(/^import .*;\r?\n/gm, "");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const context = {
    exports: module.exports,
    module,
    requestLocalJson: request,
    window: { location: { origin: "http://accounting.test", search: "?company_id=co-1" } },
    URL,
    URLSearchParams,
    AbortController,
    DOMException,
    setTimeout,
    clearTimeout,
    Promise,
  };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  return module.exports;
}

function readyDelivery(response, period, modelVersion) {
  return {
    schema_version: 6,
    company_id: response.read_context.company_id,
    period,
    model_version: modelVersion,
    state: "ready",
    response,
  };
}

test("all five generated delivery contracts accept ready/preparing/failed and reject data outside ready", () => {
  for (const [label, command, validate, response] of deliveryContracts) {
    const period = command === "dashboard_quarterly_report"
      ? `${response.period.year}-Q${response.period.quarter}`
      : response.selected_period?.key ?? null;
    const modelVersion = command === "dashboard_quarterly_report"
      ? response.read_context.read_version
      : response.snapshot_version;
    const ready = readyDelivery(response, period, modelVersion);
    assert.equal(validate(ready), true, `${label}: ready response must validate`);
    assert.equal(validate({
      schema_version: 6, company_id: ready.company_id, period, model_version: null,
      state: "preparing", retry_after_ms: 250,
    }), true, `${label}: preparing response must validate`);
    assert.equal(validate({
      schema_version: 6, company_id: ready.company_id, period, model_version: null,
      state: "failed", reason: "corrupt",
    }), true, `${label}: failed response must validate`);
    const malformedReady = structuredClone(ready);
    malformedReady.response.schema_version = -1;
    assert.equal(validate(malformedReady), false, `${label}: ready payload must use its existing strict frame contract`);
    assert.equal(validate({
      schema_version: 6, company_id: ready.company_id, period, model_version: null,
      state: "preparing", retry_after_ms: 250, response,
    }), false, `${label}: preparing must not carry business data`);
  }
});

test("generated dashboard request retries preparing reads, emits status, and unwraps only a matching ready response", async () => {
  const response = samples.deferred_brief.response;
  const period = response.selected_period.key;
  const ready = readyDelivery(response, period, response.snapshot_version);
  const pending = {
    schema_version: 6,
    company_id: ready.company_id,
    period,
    model_version: null,
    state: "preparing",
    retry_after_ms: 100,
  };
  const requests = [];
  const client = createClient(async (path, options) => {
    requests.push({ path, options });
    return requests.length === 1 ? pending : ready;
  });
  let preparingCount = 0;
  const result = await client.requestGeneratedDashboard(
    `/api/dashboard/brief?company_id=${ready.company_id}&period=${period}`,
    "/api/dashboard/brief",
    value => value?.state === "ready" || value?.state === "preparing" || value?.state === "failed",
    envelope => envelope.state === "ready"
      ? { state: "ready", response: envelope.response, modelVersion: envelope.model_version }
      : envelope.state === "preparing"
        ? { state: "preparing", retryAfterMs: envelope.retry_after_ms }
        : { state: "failed", reason: envelope.reason },
    (url, envelope) => url.searchParams.get("company_id") === envelope.company_id && url.searchParams.get("period") === envelope.period,
    value => value?.schema_version === response.schema_version,
    (url, value) => value.read_context.company_id === url.searchParams.get("company_id")
      && value.selected_period?.key === url.searchParams.get("period")
      && value.snapshot_version === ready.model_version,
    { onPreparing: () => { preparingCount += 1; } },
  );

  assert.equal(result, response);
  assert.equal(preparingCount, 1);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].path, requests[1].path);
  assert.equal(requests[0].options.method, undefined);
});

test("a failed delivery returns a stable owner-facing message without exposing technical details", async () => {
  const failed = {
    schema_version: 6, company_id: "co-1", period: "2026-01", model_version: null,
    state: "failed", reason: "production_failed",
  };
  const requests = [];
  const client = createClient(async (path, options) => { requests.push({ path, options }); return failed; });
  await assert.rejects(
    client.requestGeneratedDashboard(
      "/api/dashboard/brief?period=2026-01", "/api/dashboard/brief", value => value?.state === "failed",
      envelope => ({ state: "failed", reason: envelope.reason }),
      (url, envelope) => url.searchParams.get("company_id") === envelope.company_id && url.searchParams.get("period") === envelope.period,
      () => false, () => false,
    ),
    error => error.message === "看板暂时无法更新，请稍后重试。" && !error.message.includes("production_failed"),
  );
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.method, undefined);
});

test("an explicit refresh restarts the projection once and then polls with ordinary reads", async () => {
  const response = samples.deferred_funds.response;
  const ready = readyDelivery(response, response.selected_period.key, response.snapshot_version);
  const requests = [];
  const client = createClient(async (path, options) => {
    requests.push({ path, options });
    return requests.length === 1
      ? { state: "preparing", retry_after_ms: 1, company_id: ready.company_id, period: ready.period }
      : ready;
  });
  const path = `/api/dashboard/funds?company_id=${ready.company_id}&period=${ready.period}&limit=20&movement_account_selection=first`;
  const result = await client.requestGeneratedDashboard(
    path, "/api/dashboard/funds", () => true,
    envelope => envelope.state === "ready"
      ? { state: "ready", response: envelope.response, modelVersion: envelope.model_version }
      : { state: "preparing", retryAfterMs: envelope.retry_after_ms },
    (url, envelope) => envelope.company_id === url.searchParams.get("company_id") && envelope.period === url.searchParams.get("period"),
    () => true, (url, value) => value.read_context.company_id === url.searchParams.get("company_id"),
    { retry: true },
  );
  assert.equal(result, response);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].path, "/api/dashboard/funds/retry");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    company_id: ready.company_id, period: ready.period, limit: "20", movement_account_selection: "first",
  });
  assert.equal(requests[1].path, path);
  assert.equal(requests[1].options.method, undefined);
});

test("aborting a preparing read cancels its retry timer before another request", async () => {
  let requestCount = 0;
  const client = createClient(async () => {
    requestCount += 1;
    return {
      schema_version: 6, company_id: "co-1", period: "2026-01", model_version: null,
      state: "preparing", retry_after_ms: 30000,
    };
  });
  const controller = new AbortController();
  const pending = client.requestGeneratedDashboard(
    "/api/dashboard/brief?period=2026-01", "/api/dashboard/brief", value => value?.state === "preparing",
    envelope => ({ state: "preparing", retryAfterMs: envelope.retry_after_ms }),
    (url, envelope) => url.searchParams.get("company_id") === envelope.company_id && url.searchParams.get("period") === envelope.period,
    () => false, () => false, { signal: controller.signal },
  );
  await new Promise(resolve => setTimeout(resolve, 0));
  controller.abort();
  await assert.rejects(pending, error => error.name === "AbortError");
  assert.equal(requestCount, 1);
});
