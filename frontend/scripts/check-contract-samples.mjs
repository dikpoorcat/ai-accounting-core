import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import * as generatedValidators from "../src/api/generated/dashboardValidators.js";

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
  dashboard_close_review: "validateDashboardCloseReviewResponse",
  browser_jobs: "validateBrowserJobsResponse",
  browser_security_status: "validateBrowserSecurityStatusResponse",
  report_export_receipt: "validateReportExportReceiptResponse",
};

const samplePath = process.argv[2];
assert(samplePath, "usage: node scripts/check-contract-samples.mjs <samples.json>");
const samples = JSON.parse(await readFile(resolve(samplePath), "utf8"));
assert(samples && typeof samples === "object" && !Array.isArray(samples), "contract samples must be an object");

for (const [name, sample] of Object.entries(samples)) {
  assert(sample && typeof sample === "object" && !Array.isArray(sample), `${name}: sample must be an object`);
  const validatorName = validatorNames[sample.command];
  const validator = validatorName && generatedValidators[validatorName];
  assert(validator, `${name}: unsupported command ${JSON.stringify(sample.command)}`);
  assert(validator(sample.response), `${name}: ${JSON.stringify(validator.errors)}`);
}
