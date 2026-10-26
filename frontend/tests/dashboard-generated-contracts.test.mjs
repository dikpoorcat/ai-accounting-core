import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import * as validators from "../src/api/generated/dashboardValidators.js";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const schemas = JSON.parse(readFileSync(new URL("../src/api/generated/dashboardResponseSchemas.json", import.meta.url), "utf8"));
const validatorNames = {
  dashboard_context: "validateDashboardContextResponse",
  dashboard_brief: "validateDashboardBriefResponse",
  dashboard_brief_group: "validateDashboardBriefGroupResponse",
  dashboard_funds: "validateDashboardFundsResponse",
  dashboard_employees: "validateDashboardEmployeesResponse",
  dashboard_assets: "validateDashboardAssetsResponse",
  dashboard_business_status: "validateDashboardBusinessStatusResponse",
  dashboard_quarterly_report: "validateDashboardQuarterlyReportResponse",
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
  for (const name of ["company_with_period", "brief", "frozen_funds", "employees_labor_sources", "assets", "quarterly_report"]) assert(samples[name], name);
  for (const [name, sample] of Object.entries(samples)) {
    const validate = validators[validatorNames[sample.command]];
    assert(validate, `${name}: missing generated validator`);
    assert.equal(validate(sample.response), true, `${name}: ${JSON.stringify(validate.errors)}`);
  }
});

test("the generated manifest contains exactly the current browser response contracts", () => {
  assert.deepEqual(Object.keys(schemas).sort(), [
    "browser_security_status", "dashboard_assets", "dashboard_assets_delivery", "dashboard_brief", "dashboard_brief_delivery",
    "dashboard_brief_group", "dashboard_business_status", "dashboard_close_review", "dashboard_context",
    "dashboard_employees", "dashboard_employees_delivery", "dashboard_funds", "dashboard_funds_delivery",
    "dashboard_quarterly_report", "dashboard_quarterly_report_delivery", "report_export_receipt", "report_export_status",
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
    dashboard_context: 3, dashboard_brief: 19, dashboard_brief_group: 3, dashboard_funds: 10,
    dashboard_employees: 13, dashboard_assets: 10, dashboard_business_status: 10,
    dashboard_quarterly_report: 5,
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

test("labor people require exact aggregate money and a nonnegative source count", () => {
  const original = samples.employees_labor_people.response;
  const workers = original.data.collections.labor_workers.items;
  const zero = workers.find(worker => worker.source_count === 0);
  const paid = workers.find(worker => worker.source_count > 0);
  assert(zero && paid, "backend samples include a zero-record person and a person with remuneration");
  assert.equal("labor_sources" in original.data.collections, false, "person cards do not pre-read source details");
  for (const field of ["gross_fen", "booked_tax_fen", "net_fen", "personal_labor_fen", "capitalized_labor_fen"]) {
    assert.equal(zero[field], "0", field);
    for (const value of [null, 0, "01", "9223372036854775808"]) {
      const malformed = structuredClone(original);
      malformed.data.collections.labor_workers.items[0][field] = value;
      assert.equal(validators.validateDashboardEmployeesResponse(malformed), false, `${field}: ${value}`);
    }
  }
  for (const value of [-1, "0", true]) {
    const malformed = structuredClone(original);
    malformed.data.collections.labor_workers.items[0].source_count = value;
    assert.equal(validators.validateDashboardEmployeesResponse(malformed), false, `source_count: ${value}`);
  }
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

test("asset acquisition and activation preserve month precision in pages and delivery envelopes", () => {
  const sample = Object.values(samples).find(item => item.command === "dashboard_assets"
    && findField(item.response, key => key === "in_service_date"));
  assert(sample);
  const original = findField(sample.response, key => key === "in_service_date").owner;
  function response(kind) {
    const result = structuredClone(samples.assets.response);
    const card = structuredClone(original);
    Object.assign(card, { asset_type: kind, category: kind, status: "active" });
    delete card.in_service_date;
    delete card.available_for_use_date;
    delete card.disposal;
    delete card.retirement;
    card[kind === "fixed" ? "in_service_date" : "available_for_use_date"] = "2026-01";
    card[kind === "fixed" ? "disposal" : "retirement"] = null;
    result.data.collections.assets = {
      items: [card],
      page: { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null },
    };
    return result;
  }
  for (const kind of ["fixed", "intangible"]) {
    for (const key of ["acquisition_date", kind === "fixed" ? "in_service_date" : "available_for_use_date"]) {
      for (const value of ["2026-01", "2026-01-15", null]) {
        const changed = response(kind);
        changed.data.collections.assets.items[0][key] = value;
        assert.equal(validators.validateDashboardAssetsResponse(changed), true, JSON.stringify(validators.validateDashboardAssetsResponse.errors));
        const envelope = {
          schema_version: 6, company_id: changed.read_context.company_id,
          period: changed.selected_period.key, model_version: changed.snapshot_version,
          state: "ready", response: changed,
        };
        assert.equal(validators.validateDashboardAssetsDeliveryResponse(envelope), true, JSON.stringify(validators.validateDashboardAssetsDeliveryResponse.errors));
      }
      for (const value of ["2026-00", "2026-13", "2026-1", "2026-01-00", "", 202601]) {
        const changed = response(kind);
        changed.data.collections.assets.items[0][key] = value;
        assert.equal(validators.validateDashboardAssetsResponse(changed), false, `${kind}.${key}: ${value}`);
      }
    }
  }
  const exit = response("fixed");
  Object.assign(exit.data.collections.assets.items[0], {
    status: "disposed", disposal: {
      date: "2026-01", kind: "sale", book_value_fen: "0", gross_proceeds_fen: "0",
      gain_fen: "0", loss_fen: "0", party: "合成购买方",
    },
  });
  assert.equal(validators.validateDashboardAssetsResponse(exit), false, "actual disposal still requires its real day");
});

test("owner employee responses reject provenance and conflict diagnostics", () => {
  const original = samples.employees_corrected_roster.response;
  const validate = validators.validateDashboardEmployeesResponse;
  assert.equal(validate(original), true, JSON.stringify(validate.errors));
  for (const key of ["field_sources", "field_conflicts", "accounting_state", "source_history"]) {
    const changed = structuredClone(original);
    changed.data.collections.employees.items[0][key] = [];
    assert.equal(validate(changed), false, key);
  }
});

test("employee contracts distinguish all six workforce states and monthly relationship segments", () => {
  const validate = validators.validateDashboardEmployeesResponse;
  for (const [filter, state] of [
    ["employment_active", "regular"],
    ["employment_unpaid_leave", "unpaid_leave"],
    ["employment_departed", "departed"],
    ["employment_unknown", "unknown"],
    ["all", "labor"],
    ["all", "labor_ended"],
  ]) {
    const response = structuredClone(samples.employees_focused.response);
    response.data.employee_filter = filter;
    response.data.collections.employees.items[0].employment_state = state;
    response.data.collections.employees.items[0].period_states = [state];
    response.data.collections.employees.items[0].possible_period_states = [state];
    response.data.collections.employees.items[0].relationship_segments = [{ state, start: "2026-01", end: "2026-02-01" }];
    assert(validate(response), JSON.stringify(validate.errors));
  }
  for (const key of ["period_states", "possible_period_states", "relationship_segments"]) {
    const response = structuredClone(samples.employees_focused.response);
    delete response.data.collections.employees.items[0][key];
    assert.equal(validate(response), false, key);
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

test("labor worker contracts validate a zero-fee roster independently of financial sources", () => {
  const validate = validators.validateDashboardEmployeesResponse;
  const response = structuredClone(samples.employees_focused.response);
  response.data.collections.labor_workers = {
    items: [{ person_id: "synthetic-labor", name: "合成劳务", employment_state: "labor",
      period_states: ["labor"], possible_period_states: ["labor"],
      relationship_segments: [{ state: "labor", start: "2026-01", end: null }],
      in_period: true, has_labor_activity: false, source_count: 0,
      gross_fen: "0", booked_tax_fen: "0", net_fen: "0", personal_labor_fen: "0", capitalized_labor_fen: "0" }],
    page: { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null },
  };
  assert(validate(response), JSON.stringify(validate.errors));
  for (const [key, value] of [["employment_state", "active"], ["period_states", ["employee"]], ["in_period", "yes"], ["has_labor_activity", 0]]) {
    const changed = structuredClone(response);
    changed.data.collections.labor_workers.items[0][key] = value;
    assert.equal(validate(changed), false, key);
  }
  const changed = structuredClone(response);
  changed.data.collections.labor_workers.items[0].relationship_segments[0].event_id = "internal-event";
  assert.equal(validate(changed), false, "event identity is not a dashboard field");
});

test("actual worker registration produces an independent roster entry without remuneration", () => {
  const response = samples.employees_labor_roster_zero_fee.response;
  const validate = validators.validateDashboardEmployeesResponse;
  assert(validate(response), JSON.stringify(validate.errors));
  assert(response.data.collections.labor_workers.items.length > 0);
  assert(response.data.employees.labor_worker_count > 0);
  for (const worker of response.data.collections.labor_workers.items) {
    assert.equal(worker.has_labor_activity, false);
    assert(worker.period_states.includes("labor"));
    assert(worker.relationship_segments.some(segment => segment.state === "labor"));
  }
});
