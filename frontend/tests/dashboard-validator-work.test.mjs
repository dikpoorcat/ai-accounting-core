import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { test } from "node:test";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));

// Execute the actual standalone output, recording real function invocations and
// BigInt conversions. No production instrumentation or wall-clock SLA assertion.
function measuredValidator(module, exportName, watched, ignoreMoneyPattern = false) {
  const calls = [], amounts = [];
  const source = readFileSync(new URL(`../src/api/generated/${module}.js`, import.meta.url), "utf8")
    .replace(/export const /g, "const ")
    .replace(/function (validate\d+)\(data, ([\s\S]*?)\)\{/g,
      (match, name) => `${match}if (watched.has(data)) calls.push(["${name}", data]);`);
  class SchemaRegExp extends RegExp {
    constructor(pattern, flags) {
      super(pattern, flags);
      if (ignoreMoneyPattern && pattern === "^(0|-?[1-9][0-9]*)$") this.test = () => true;
    }
  }
  const context = { watched: new Set(watched), calls, RegExp: SchemaRegExp,
    BigInt(value) { amounts.push(value); return BigInt(value); } };
  runInNewContext(`${source}\nglobalThis.validate = ${exportName};`, context);
  return { validate: context.validate, calls, amounts };
}
function moneyCount(value) {
  if (!value || typeof value !== "object") return 0;
  return Object.entries(value).reduce((count, [key, item]) => count
    + (key.endsWith("_fen") && typeof item === "string" ? 1 : moneyCount(item)), 0);
}

test("every brief scope invokes only its matching response branch", () => {
  for (const scope of ["overview", "activity", "open_items", "vouchers"]) {
    const response = structuredClone(samples[scope === "overview" ? "brief" : `brief_${scope}`].response);
    const measured = measuredValidator("dashboardBrief", "validateDashboardBriefResponse", [response]);
    assert(measured.validate(response), JSON.stringify(measured.validate.errors));
    assert.equal(measured.calls.length, 2, scope); // root plus one response branch
    assert.equal(new Set(measured.calls.map(([name]) => name)).size, 2, scope);
  }
});

test("500 and 985 voucher rows are each validated once and each amount converted once", () => {
  for (const size of [20, 500, 985]) {
    const response = structuredClone(samples.brief_vouchers.response);
    const collection = response.data.collections.vouchers;
    const original = collection.items[0];
    collection.items = Array.from({ length: size }, (_, index) => ({ ...structuredClone(original), number: String(index + 1), voucher_version_id: `work-${index}` }));
    Object.assign(collection.page, { total_count: size, filtered_count: size, returned_count: size, has_more: false, next_cursor: null });
    const measured = measuredValidator("dashboardBrief", "validateDashboardBriefResponse", collection.items);
    assert(measured.validate(response), JSON.stringify(measured.validate.errors));
    assert.equal(measured.calls.length, size);
    for (const row of collection.items) assert.equal(measured.calls.filter(([, item]) => item === row).length, 1);
    assert.equal(measured.amounts.length, moneyCount(response), `voucher ${size}`);
  }
});

test("funds pages convert each amount once without changing account or movement content", () => {
  for (const size of [20, 500]) {
    const response = structuredClone(samples.cash_funds.response), collection = response.data.collections.movements;
    const original = collection.items[0];
    collection.items = Array.from({ length: size }, (_, index) => ({ ...original, id: `movement-${index}` }));
    Object.assign(collection.page, { total_count: size, filtered_count: size, returned_count: size, has_more: false, next_cursor: null });
    const before = structuredClone(response);
    const measured = measuredValidator("dashboardFunds", "validateDashboardFundsResponse", collection.items);
    assert(measured.validate(response), JSON.stringify(measured.validate.errors));
    assert.equal(measured.amounts.length, moneyCount(response));
    assert.deepEqual(response, before);
  }
});

test("money keyword alone rejects noncanonical strings and preserves both int64 endpoints", () => {
  for (const value of ["0", "1", "-1", "9223372036854775807", "-9223372036854775808",
    "9223372036854775808", "-9223372036854775809", "-0", "00", "01", "-01", "+1", "1.0", "1e2", "", " 1", "1 ", 1, 1.5, true, false]) {
    const response = structuredClone(samples.cash_funds.response);
    response.data.opening_fen = value;
    const measured = measuredValidator("dashboardFunds", "validateDashboardFundsResponse", [], true);
    const accepted = ["0", "1", "-1", "9223372036854775807", "-9223372036854775808"].includes(value);
    assert.equal(measured.validate(response), accepted, String(value));
  }
});

test("scope dispatch retains strict version, required fields, unknown fields and nested checks", () => {
  const mutations = [
    response => { delete response.scope; },
    response => { response.scope = "unknown"; },
    response => { response.scope = null; },
    response => { response.scope = 1; },
    response => { response.scope = "activity"; },
    response => { response.schema_version = 18; },
    response => { response.extra = true; },
    response => { delete response.read_context; },
    response => { response.data.collections.vouchers.items[0].extra = true; },
    response => { delete response.data.collections.vouchers.items[0].summary; },
    response => { response.data.collections.vouchers.items[0].lines[0].debit_fen = "-0"; },
  ];
  for (const mutate of mutations) {
    const response = structuredClone(samples.brief_vouchers.response); mutate(response);
    const measured = measuredValidator("dashboardBrief", "validateDashboardBriefResponse", []);
    assert.equal(measured.validate(response), false, mutate.toString());
  }
  for (const response of [null, [], 1, "vouchers", true]) {
    assert.equal(measuredValidator("dashboardBrief", "validateDashboardBriefResponse", []).validate(response), false);
  }
});
