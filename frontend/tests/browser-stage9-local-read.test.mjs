import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const { verifyLocalRead } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const selected = { id: "synthetic-company", period: "2016-02" };
const snapshot = "snapshot-a";
const module = { key: "brief", action: "brief" };
const query = section => new URL(`http://local/api/dashboard/brief?company_id=${selected.id}&period=${selected.period}&expected_version=${snapshot}&limit=20&section=${section}`);

test("first voucher display permits only its bounded snapshot-associated collection", () => {
  const url = query("vouchers");
  verifyLocalRead(url, module, selected, snapshot, "vouchers");
  assert.throws(() => verifyLocalRead(url, module, selected, snapshot), /whole page or unrelated collection/);
  for (const [field, value] of [["company_id", "other"], ["period", "2016-03"],
    ["expected_version", "snapshot-b"], ["limit", "200"]]) {
    const wrong = new URL(url); wrong.searchParams.set(field, value);
    assert.throws(() => verifyLocalRead(wrong, module, selected, snapshot, "vouchers"));
  }
  const full = new URL(url); full.searchParams.delete("section");
  assert.throws(() => verifyLocalRead(full, module, selected, snapshot, "vouchers"), /whole page/);
  const context = new URL(url); context.pathname = "/api/dashboard/context";
  assert.throws(() => verifyLocalRead(context, module, selected, snapshot, "vouchers"), /unrelated data/);
});

test("display changes retain approved continuation reads without permitting fresh unrelated collections", () => {
  for (const [key, section] of [["brief", "activity"], ["funds", "accounts"],
    ["employees", "labor_sources"], ["assets", "projects"]]) {
    const current = { key, action: key }, url = query(section);
    url.pathname = `/api/dashboard/${key}`;
    assert.throws(() => verifyLocalRead(url, current, selected, snapshot));
    url.searchParams.set("cursor", "bound-cursor");
    verifyLocalRead(url, current, selected, snapshot);
    url.searchParams.set("section", "unrelated");
    assert.throws(() => verifyLocalRead(url, current, selected, snapshot));
  }
  const report = query("statements"); report.pathname = "/api/dashboard/quarterly-report";
  report.searchParams.set("cursor", "cursor");
  assert.throws(() => verifyLocalRead(report, { key: "reports", action: "quarterly-report" }, selected, snapshot));
});
