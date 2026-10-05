import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const { captureRefreshResources } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const selection = { action: "brief", companyId: "synthetic", period: "2026-09", quarter: "2026-Q3" };
const resource = (action, query, overrides = {}) => ({
  name: `https://private-host.invalid/api/dashboard/${action}?company_id=synthetic${query}`,
  startTime: 110, fetchStart: 111, requestStart: 115, responseStart: 150,
  responseEnd: 180, duration: 70, transferSize: 1000, encodedBodySize: 800,
  decodedBodySize: 1500, initiatorType: "fetch", nextHopProtocol: "http/1.1",
  ...overrides,
});
const pair = () => [resource("context", ""), resource("brief", "&period=2026-09")];
const capture = (entries, selected = selection) => captureRefreshResources(entries, selected, 100, 220, 1700000000000);

test("captures raw relative network timestamps and the tail after both responses without host data", () => {
  const result = capture(pair());
  assert.deepEqual(result.diagnostics, []);
  assert.equal(result.render_tail_ms, 40);
  assert.equal(result.clickStart, 100);
  assert.equal(result.renderAt, 220);
  assert.equal(result.timeOrigin, 1700000000000);
  assert.equal(result.resources.brief[0].startTime, 110);
  assert.equal(result.resources.brief[0].requestStart, 115);
  assert.equal(result.resources.brief[0].duration, 70);
  assert.deepEqual(result.resources.brief[0].query, { company_id: "synthetic", period: "2026-09" });
  assert.equal(result.resources.brief[0].path, "/api/dashboard/brief");
  assert.equal(result.resources.brief[0].responseStatus, null);
  assert.ok(!JSON.stringify(result).includes("private-host"));
  assert.match(result.attribution, /not source CPU attribution/);
});

test("associates only this click, company, action and month", () => {
  const unrelated = [
    resource("brief", "&period=2026-08"),
    resource("brief", "&period=2026-09", { startTime: 99 }),
    resource("brief", "&period=2026-09", { name: "https://host.invalid/api/dashboard/brief?company_id=other&period=2026-09" }),
    resource("funds", "&period=2026-09"),
    resource("context", "", { name: "invalid URL" }),
  ];
  assert.deepEqual(capture([...pair(), ...unrelated]), capture(pair()));
});

test("quarterly report requires exact year and quarter", () => {
  const selected = { ...selection, action: "quarterly-report" };
  const result = capture([
    resource("context", ""),
    resource("quarterly-report", "&year=2026&quarter=3"),
    resource("quarterly-report", "&year=2025&quarter=3"),
    resource("quarterly-report", "&year=2026&quarter=2"),
  ], selected);
  assert.deepEqual(result.diagnostics, []);
  assert.equal(result.resources["quarterly-report"].length, 1);
  assert.deepEqual(result.resources["quarterly-report"][0].query,
    { company_id: "synthetic", year: "2026", quarter: "3" });
});

test("missing resource is explicit and cannot fabricate a tail", () => {
  const result = capture([resource("context", "")]);
  assert.deepEqual(result.diagnostics, ["brief:missing_resource"]);
  assert.deepEqual(result.resources.brief, []);
  assert.equal(result.render_tail_ms, null);
});

test("duplicate associated requests retain both entries and cannot pick a convenient delay", () => {
  const result = capture([...pair(), resource("brief", "&period=2026-09", { responseEnd: 200 })]);
  assert.deepEqual(result.diagnostics, ["brief:duplicate_resources"]);
  assert.equal(result.resources.brief.length, 2);
  assert.equal(result.render_tail_ms, null);
});

test("incomplete or inconsistent resource timestamps are diagnostic, never negative tails", () => {
  for (const overrides of [{ responseEnd: 0 }, { responseEnd: 221 }, { responseEnd: undefined }, { duration: -1 }]) {
    const result = capture([resource("context", ""), resource("brief", "&period=2026-09", overrides)]);
    assert.deepEqual(result.diagnostics, ["brief:invalid_resource_timestamps"]);
    assert.equal(result.render_tail_ms, null);
  }
  const invalid = captureRefreshResources(pair(), selection, 100, 99, 1700000000000);
  assert.ok(invalid.diagnostics.includes("invalid_measurement_timestamps"));
  assert.equal(invalid.render_tail_ms, null);
});
