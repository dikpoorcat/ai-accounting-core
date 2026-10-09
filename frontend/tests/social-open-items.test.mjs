import assert from "node:assert/strict";
import { test } from "node:test";
import { groupContributionMembers, contributionProgressRows } from "./helpers/briefOpenItemGrouping.mjs";

const item = (id, changes = {}) => ({ id, category_key: "payroll_payables", party: "同名员工", description: "社保公积金", status: "open", current_status: "open", source_amount_fen: "10000", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "10000", current_outstanding_fen: "10000", subject_id: `subject-${id}`, contribution_group_key: "employee-a", contribution_component: "employee_social", payroll_period: "2026-09", ...changes });
const progress = items => contributionProgressRows(groupContributionMembers(items)[0]);

test("contribution details separate formal employee identities and payroll months", () => {
  const raw = [item("a"), item("b", { contribution_group_key: "employee-b" }), item("earlier", { payroll_period: "2026-08" }), item("salary", { contribution_component: null })];
  const before = structuredClone(raw), groups = groupContributionMembers(raw);
  assert.equal(groups.length, 3); assert.deepEqual(raw, before, "detail grouping never rewrites formal source records");
  assert.equal(groups[0].contributionMembers[0], raw[0]); assert.equal(groups[2].payroll_period, "2026-08");
});
test("two fixed components per matter leave missing original and paid amounts unknown", () => {
  const rows = progress([item("only")]); assert.equal(rows.length, 2);
  assert.equal(rows[0].present, true); assert.equal(rows[0].sourceAmountFen, "10000");
  for (const row of rows.filter(row => !row.present)) {
    assert.equal(row.sourceAmountFen, null); assert.equal(row.paidFen, null); assert.equal(row.otherSettledFen, null); assert.equal(row.outstandingFen, null); assert.equal(row.currentOutstandingFen, null); assert.equal(row.changed, false);
  }
});
test("unknown fields propagate independently and integer totals preserve cents", () => {
  const row = progress([item("huge", { source_amount_fen: "9007199254740993", paid_fen: "9007199254740993", other_settled_fen: "9007199254740993", outstanding_fen: "9007199254740993", current_outstanding_fen: "9007199254740993" }), item("extra", { source_amount_fen: "1", paid_fen: "1", other_settled_fen: null, outstanding_fen: "1", current_outstanding_fen: null })])[0];
  assert.equal(row.sourceAmountFen, "9007199254740994"); assert.equal(row.paidFen, "9007199254740994"); assert.equal(row.otherSettledFen, null); assert.equal(row.outstandingFen, "9007199254740994"); assert.equal(row.currentOutstandingFen, null);
});
test("excess and unknown obligations cannot advertise normal settlement", () => {
  assert.equal(progress([item("excess", { status: "over_settled", current_status: "over_settled", outstanding_fen: "-10000" }), item("open")])[0].status, "over_settled");
  for (const status of ["checking", "unestablished"]) assert.equal(progress([item("unknown", { status, current_status: status, outstanding_fen: "0", current_outstanding_fen: "0" }), item("settled", { status: "settled", current_status: "settled", outstanding_fen: "0", current_outstanding_fen: "0" })])[0].status, "checking");
});
test("corrected and withdrawn members retain notices alongside pending contributions", () => {
  for (const status of ["reversed", "withdrawn"]) {
    const row = progress([item("active"), item("terminal", { status, current_status: status, outstanding_fen: "0", current_outstanding_fen: "0" })])[0];
    assert.equal(row.status, "open"); assert.deepEqual(row.notices, [status === "reversed" ? "含已更正款项" : "含已撤回款项"]);
  }
});
test("latest progress detects each obligation's change even when component totals match", () => {
  const rows = progress([item("one", { current_outstanding_fen: "5000", current_status: "partial" }), item("two", { current_outstanding_fen: "15000", current_status: "over_settled" }), item("unchanged", { contribution_component: "employer_social" })]);
  const changed = rows.filter(row => row.changed); assert.equal(changed.length, 1); assert.equal(changed[0].outstandingFen, changed[0].currentOutstandingFen); assert.equal(changed[0].currentStatus, "over_settled");
  assert.equal(rows.find(row => row.component === "employer_social").changed, false);
  assert.equal(progress([item("missing-current", { current_status: null, current_outstanding_fen: null })])[0].changed, false);
});


test("social and housing stay separate for the same employee and payroll month", () => {
  const groups = groupContributionMembers([item("social"), item("housing", { contribution_component: "employee_housing" })]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(group => group.contributionMatter), ["social", "housing"]);
  assert.deepEqual(contributionProgressRows(groups[0]).map(row => row.component), ["employee_social", "employer_social"]);
  assert.deepEqual(contributionProgressRows(groups[1]).map(row => row.component), ["employee_housing", "employer_housing"]);
  assert.notEqual(groups[0].id, groups[1].id);
});
