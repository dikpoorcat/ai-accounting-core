import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { validateDashboardBriefResponse } from "../src/api/generated/dashboardBrief.js";
import { validateDashboardBriefGroupResponse } from "../src/api/generated/dashboardBriefGroup.js";

const require = createRequire(import.meta.url);
const { briefForRequest } = require("./browser-brief-groups.cjs");
const { verifyMainPayload } = require("./browser-stage9-hot-refresh.cjs");
const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const selected = { id: samples.brief.response.read_context.company_id, period: samples.brief.response.selected_period.key };
const query = section => {
  const url = new URL("http://offline.invalid/api/dashboard/brief");
  url.searchParams.set("company_id", selected.id);
  url.searchParams.set("period", selected.period);
  if (section) {
    url.searchParams.set("section", section);
    url.searchParams.set("expected_version", samples.brief.response.snapshot_version);
  }
  return url;
};

test("group browser factory returns the actual four brief contracts without mutating its overview", () => {
  const overview = structuredClone(samples.brief.response), before = structuredClone(overview);
  assert.equal(briefForRequest(overview, query()), overview);
  assert(validateDashboardBriefResponse(overview), JSON.stringify(validateDashboardBriefResponse.errors));
  for (const section of ["activity", "open_items", "vouchers"]) {
    const source = structuredClone(overview);
    if (section === "vouchers") source.data.collections.vouchers = structuredClone(samples.brief_vouchers.response.data.collections.vouchers);
    const original = structuredClone(source), response = briefForRequest(source, query(section));
    assert(validateDashboardBriefResponse(response), JSON.stringify(validateDashboardBriefResponse.errors));
    assert.equal(response.scope, section);
    assert.deepEqual(response, samples[`brief_${section}`].response);
    assert.deepEqual(Object.keys(response.data), ["collections"]);
    assert.deepEqual(Object.keys(response.data.collections), [section]);
    assert.deepEqual(source, original);
  }
  assert.deepEqual(overview, before);
});

test("main browser verifier accepts actual overview and isolated sections while retaining per-voucher checks", () => {
  verifyMainPayload(samples.brief.response, "brief", selected, query());
  for (const section of ["activity", "open_items", "vouchers"]) {
    const response = samples[`brief_${section}`].response, target = query(section);
    verifyMainPayload(response, "brief", selected, target);
    const wrongScope = { ...response, scope: "overview" };
    assert.throws(() => verifyMainPayload(wrongScope, "brief", selected, target), /scope/);
    const repeated = structuredClone(response);
    repeated.data.owner_tasks = samples.brief.response.data.owner_tasks;
    assert.throws(() => verifyMainPayload(repeated, "brief", selected, target), /repeated overview/);
  }
  const vouchers = structuredClone(samples.brief_vouchers.response);
  vouchers.data.collections.vouchers.items[0].lines[0].debit_fen = "1";
  assert.throws(() => verifyMainPayload(vouchers, "brief", selected, query("vouchers")), /balance|total/);
});

test("browser factory keeps precise focus on overview and rejects section plus locator", () => {
  const overview = structuredClone(samples.brief.response);
  overview.data.focused_voucher = structuredClone(samples.brief_vouchers.response.data.collections.vouchers.items[0]);
  const target = query(); target.searchParams.set("voucher_version_id", overview.data.focused_voucher.voucher_version_id);
  assert.equal(briefForRequest(overview, target).data.focused_voucher, overview.data.focused_voucher);
  for (const locator of ["voucher_version_id", "voucher_number"]) {
    const invalid = query("activity"); invalid.searchParams.set(locator, "1");
    assert.throws(() => briefForRequest(overview, invalid), /precise voucher locators/);
  }
  assert.throws(() => briefForRequest(overview, query("other")), /unknown brief section/);
  assert.throws(() => briefForRequest(overview, query("vouchers")), /no vouchers page/);
});

test("browser factory preserves empty-data semantics for all scopes", () => {
  const empty = { ...structuredClone(samples.brief.response), data: null, selected_period: null, snapshot_version: null };
  for (const section of [null, "activity", "open_items", "vouchers"]) {
    const response = briefForRequest(empty, query(section));
    assert.equal(response.scope, section ?? "overview");
    assert.equal(response.data, null);
    assert(validateDashboardBriefResponse(response), JSON.stringify(validateDashboardBriefResponse.errors));
  }
});

test("group browser member seeds retain group3 and exact members plus vouchers", () => {
  const memberSamples = Object.values(samples).filter(sample => sample.command === "dashboard_brief_group");
  assert(memberSamples.length > 0);
  for (const { response } of memberSamples) {
    assert.equal(response.schema_version, 3);
    assert(validateDashboardBriefGroupResponse(response), JSON.stringify(validateDashboardBriefGroupResponse.errors));
    assert.deepEqual(Object.keys(response.data.collections).sort(), ["members", "vouchers"]);
  }
});
