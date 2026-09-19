import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  validateDashboardContextResponse,
  validateDashboardFundsResponse,
} from "../src/api/generated/dashboardValidators.js";

const samplePath = process.argv[2];
assert(samplePath, "usage: node scripts/check-contract-samples.mjs <samples.json>");

const validators = {
  dashboard_context: validateDashboardContextResponse,
  dashboard_funds: validateDashboardFundsResponse,
};
const samples = JSON.parse(await readFile(resolve(samplePath), "utf8"));
assert(samples && typeof samples === "object" && !Array.isArray(samples), "contract samples must be an object");

for (const [name, sample] of Object.entries(samples)) {
  assert(sample && typeof sample === "object" && !Array.isArray(sample), `${name}: sample must be an object`);
  const validator = validators[sample.command];
  assert(validator, `${name}: unsupported command ${JSON.stringify(sample.command)}`);
  assert(validator(sample.response), `${name}: ${JSON.stringify(validator.errors)}`);
}
