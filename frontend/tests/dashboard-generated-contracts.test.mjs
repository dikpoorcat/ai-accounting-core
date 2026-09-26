import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import * as validators from "../src/api/generated/dashboardValidators.js";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const schemas = JSON.parse(readFileSync(new URL("../src/api/generated/dashboardResponseSchemas.json", import.meta.url), "utf8"));
const validatorNames = {
  workflow: "validateWorkflowResponse",
  period_readiness: "validatePeriodReadinessResponse",
  dashboard_context: "validateDashboardContextResponse",
  dashboard_brief: "validateDashboardBriefResponse",
  dashboard_funds: "validateDashboardFundsResponse",
  dashboard_employees: "validateDashboardEmployeesResponse",
  dashboard_assets: "validateDashboardAssetsResponse",
  dashboard_business_status: "validateDashboardBusinessStatusResponse",
  dashboard_quarterly_report: "validateDashboardQuarterlyReportResponse",
  dashboard_period_preparation: "validateDashboardPeriodPreparationResponse",
};

function findField(value, predicate) {
  if (!value || typeof value !== "object") return null;
  for (const [key, field] of Object.entries(value)) {
    if (predicate(key, field)) return { owner: value, key };
    const nested = findField(field, predicate);
    if (nested) return nested;
  }
  return null;
}

test("generated validators accept all current synthetic backend response branches", () => {
  for (const name of ["empty_workflow", "open_workflow", "empty_readiness", "open_readiness", "frozen_readiness"]) assert(samples[name], name);
  for (const [name, sample] of Object.entries(samples)) {
    const validate = validators[validatorNames[sample.command]];
    assert(validate, `${name}: missing generated validator`);
    assert.equal(validate(sample.response), true, `${name}: ${JSON.stringify(validate.errors)}`);
  }
});

test("the explicit generated manifest covers every browser-visible response contract", () => {
  assert.deepEqual(Object.keys(schemas).sort(), [
    "browser_jobs", "browser_security_status", "dashboard_assets", "dashboard_brief",
    "dashboard_business_status", "dashboard_close_review", "dashboard_context",
    "dashboard_employees", "dashboard_funds", "dashboard_period_preparation",
    "dashboard_quarterly_report", "period_readiness", "report_export_receipt", "workflow",
  ]);
  const source = readFileSync(new URL("../scripts/generate-dashboard-contracts.mjs", import.meta.url), "utf8");
  for (const key of Object.keys(schemas)) assert(source.includes(`["${key}"`), key);
});

test("new page contracts keep detail rows only under collections", () => {
  for (const sample of Object.values(samples)) {
    const response = sample.response;
    if (!response.data) continue;
    if (sample.command === "dashboard_funds") {
      for (const alias of ["accounts", "movements", "movement_page", "products", "events"]) assert.equal(alias in response.data, false, alias);
      assert.equal("rows" in (response.data.bank_statement ?? {}), false);
      assert.equal("page" in (response.data.bank_statement ?? {}), false);
    }
    if (sample.command === "dashboard_brief") {
      assert.equal("vouchers" in response.data, false);
      assert.equal("voucher_page" in response.data, false);
    }
    if (sample.command === "dashboard_employees") assert.equal("items" in response.data.employees, false);
    if (sample.command === "dashboard_assets") {
      assert.equal("projects" in response.data, false);
      assert.equal("items" in response.data.fixed, false);
      assert.equal("items" in response.data.intangible, false);
    }
  }
});

test("all non-context dashboards carry required read context and current schema versions", () => {
  const versions = {
    workflow: 1, period_readiness: 1,
    dashboard_context: 2, dashboard_brief: 7, dashboard_funds: 7,
    dashboard_employees: 7, dashboard_assets: 7, dashboard_business_status: 5,
    dashboard_quarterly_report: 4, dashboard_period_preparation: 4,
  };
  for (const [name, sample] of Object.entries(samples)) {
    assert.equal(sample.response.schema_version, versions[sample.command], name);
    if (sample.command.startsWith("dashboard_") && sample.command !== "dashboard_context") {
      assert.equal(typeof sample.response.read_context.company_id, "string", name);
      assert.equal(typeof sample.response.read_context.database_id, "string", name);
      assert.equal(typeof sample.response.read_context.as_of, "string", name);
      assert.equal(typeof sample.response.read_context.read_version, "string", name);
    }
  }
});

test("generated money validation accepts canonical int64 strings and rejects numbers", () => {
  const original = samples.cash_funds.response;
  for (const value of ["9223372036854775807", "-9223372036854775808"]) {
    const changed = structuredClone(original);
    const field = findField(changed, (key, current) => key.endsWith("_fen") && typeof current === "string");
    assert(field); field.owner[field.key] = value;
    assert.equal(validators.validateDashboardFundsResponse(changed), true, value);
  }
  for (const value of ["9223372036854775808", "01", "-0", 1, 1.5, true]) {
    const changed = structuredClone(original);
    const field = findField(changed, (key, current) => key.endsWith("_fen") && typeof current === "string");
    assert(field); field.owner[field.key] = value;
    assert.equal(validators.validateDashboardFundsResponse(changed), false, String(value));
  }
});
