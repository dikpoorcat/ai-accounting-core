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
  dashboard_brief_group: "validateDashboardBriefGroupResponse",
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
  for (const name of ["empty_workflow", "open_workflow", "empty_readiness", "open_readiness", "frozen_readiness", "employees_labor_sources"]) assert(samples[name], name);
  for (const [name, sample] of Object.entries(samples)) {
    const validate = validators[validatorNames[sample.command]];
    assert(validate, `${name}: missing generated validator`);
    assert.equal(validate(sample.response), true, `${name}: ${JSON.stringify(validate.errors)}`);
  }
});

test("the explicit generated manifest covers every browser-visible response contract", () => {
  assert.deepEqual(Object.keys(schemas).sort(), [
    "browser_security_status", "dashboard_assets", "dashboard_brief", "dashboard_brief_group",
    "dashboard_business_status", "dashboard_close_review", "dashboard_context",
    "dashboard_employees", "dashboard_funds", "dashboard_period_preparation",
    "dashboard_quarterly_report", "delete_work_draft", "list_work_drafts", "period_readiness",
    "read_work_draft", "report_export_receipt", "report_export_status", "save_work_draft", "workflow",
  ]);
  const source = readFileSync(new URL("../scripts/generate-dashboard-contracts.mjs", import.meta.url), "utf8");
  const serviceOnly = new Set(["delete_work_draft", "list_work_drafts", "read_work_draft", "save_work_draft"]);
  for (const key of Object.keys(schemas)) {
    if (!serviceOnly.has(key)) assert(source.includes(`["${key}"`), key);
  }
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
      for (const item of response.data.collections.open_items?.items ?? []) {
        assert.equal("source_period" in item, false);
      }
    }
    if (sample.command === "dashboard_employees") {
      assert.equal("items" in response.data.employees, false);
      for (const key of ["payroll_sources", "settlement_events"]) {
        assert.equal(key in response.data.collections, false, key);
      }
    }
    if (sample.command === "dashboard_assets") {
      assert.equal("projects" in response.data, false);
      assert.equal("items" in response.data.fixed, false);
      assert.equal("items" in response.data.intangible, false);
    }
  }
});

test("all non-context dashboards carry required read context and current schema versions", () => {
  const versions = {
    workflow: 2, period_readiness: 1,
    dashboard_context: 3, dashboard_brief: 18, dashboard_brief_group: 3, dashboard_funds: 9,
    dashboard_employees: 11, dashboard_assets: 10, dashboard_business_status: 10,
    dashboard_quarterly_report: 5, dashboard_period_preparation: 4,
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

test("workflow references use version 2 and nonnegative integers", () => {
  const original = samples.open_workflow.response;
  assert(original.issues.length > 0);
  for (const value of [-1, "0", true]) {
    const changed = structuredClone(original);
    changed.fact_issue_refs = [value];
    assert.equal(validators.validateWorkflowResponse(changed), false, String(value));
  }
  const changed = structuredClone(original);
  changed.schema_version = 1;
  assert.equal(validators.validateWorkflowResponse(changed), false);
});

test("personnel date validators preserve month and day precision and reject malformed dates", () => {
  const scenarios = [
    ["employees_month_dates", validators.validateDashboardEmployeesResponse, ["employment_start_date", "employment_end_date"]],
    ["business_month_dates", validators.validateDashboardBusinessStatusResponse, ["employment_start", "employment_end"]],
  ];
  for (const [name, validate, fields] of scenarios) {
    assert(samples[name], name);
    for (const key of fields) {
      for (const value of ["2025-12", "2025-12-15", null]) {
        const changed = structuredClone(samples[name].response);
        const field = findField(changed, (name, value) => name === key && (typeof value === "string" || value === null));
        assert(field, key); field.owner[field.key] = value;
        assert.equal(validate(changed), true, `${name}.${key}: ${JSON.stringify(validate.errors)}`);
      }
      for (const value of ["2025-00", "2025-13", "2025-1", "2025-12-00", "", 202512, true]) {
        const changed = structuredClone(samples[name].response);
        const field = findField(changed, (name, value) => name === key && (typeof value === "string" || value === null));
        assert(field, key); field.owner[field.key] = value;
        assert.equal(validate(changed), false, `${name}.${key}: ${String(value)}`);
      }
    }
  }
});

test("owner employee responses reject provenance and conflict diagnostics", () => {
  const original = samples.employees_date_conflict.response;
  const validate = validators.validateDashboardEmployeesResponse;
  assert.equal(validate(original), true, JSON.stringify(validate.errors));
  for (const key of ["field_sources", "field_conflicts", "accounting_state", "source_history"]) {
    const changed = structuredClone(original);
    changed.data.collections.employees.items[0][key] = [];
    assert.equal(validate(changed), false, key);
  }
});

test("employee contracts distinguish the four employment states", () => {
  const validate = validators.validateDashboardEmployeesResponse;
  for (const [filter, state] of [
    ["employment_active", "regular"],
    ["employment_unpaid_leave", "unpaid_leave"],
    ["employment_departed", "departed"],
    ["employment_unknown", "unknown"],
  ]) {
    const response = structuredClone(samples.employees_focused.response);
    response.data.employee_filter = filter;
    response.data.collections.employees.items[0].employment_state = state;
    assert(validate(response), JSON.stringify(validate.errors));
  }
  for (const state of [undefined, null, "in_period", true]) {
    const response = structuredClone(samples.employees_focused.response);
    if (state === undefined) delete response.data.collections.employees.items[0].employment_state;
    else response.data.collections.employees.items[0].employment_state = state;
    assert.equal(validate(response), false, String(state));
  }
  for (const removedFilter of ["payroll", "no_payroll", "employment_inactive", "employment_regular"]) {
    const response = structuredClone(samples.employees_focused.response);
    response.data.employee_filter = removedFilter;
    assert.equal(validate(response), false, removedFilter);
  }
});

test("employee contract rejects removed historical wage and payment collections", () => {
  const validate = validators.validateDashboardEmployeesResponse;
  const original = samples.employees_focused.response;
  assert(validate(original), JSON.stringify(validate.errors));
  assert.equal(original.data.collections.employees.items.length, 1);
  for (const key of ["payroll_sources", "settlement_events"]) {
    const changed = structuredClone(original);
    changed.data.collections[key] = {
      items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null },
    };
    assert.equal(validate(changed), false, key);
  }
});
