import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const { verifyVisibleVoucherRows, formatFen } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");

test("visible five-column entry rows preserve exact amounts and balance without requiring a totals footer", () => {
  const amount = "900719925474099301";
  const voucher = {
    number: "3", voucher_version_id: "version", subject_id: "subject", type: "销售", kind: "sale",
    summary: "销售", list_summary: "销售", business_amount_label: "业务金额", business_amount_fen: amount,
    amount_fen: amount, state: "已入账", date: null,
    recognition: { precision: "month", period: "2016-02", date: null, label: "按月确认" },
    asset: null, asset_members: [], lines: [
      { line_number: 1, code: "1002", account: "银行存款", debit_fen: amount, credit_fen: "0",
        party: "", source_label: "", party_state: "not_applicable", parties: [] },
      { line_number: 2, code: "6001", account: "收入", debit_fen: "0", credit_fen: amount,
        party: "", source_label: "", party_state: "not_applicable", parties: [] },
    ],
  };
  const rows = [{ code: "1002", account: "银行存款", debit: formatFen(amount), credit: "—" },
    { code: "6001", account: "收入", debit: "—", credit: formatFen(amount) }];
  verifyVisibleVoucherRows(rows, voucher);
  assert.throws(() => verifyVisibleVoucherRows(rows.slice(1), voucher), /entry lines missing/);
  assert.throws(() => verifyVisibleVoucherRows([...rows].reverse(), voucher), /account code differs/);
  assert.throws(() => verifyVisibleVoucherRows([{ ...rows[0], debit: formatFen("1") }, rows[1]], voucher), /debit amount differs/);
  assert.throws(() => verifyVisibleVoucherRows([rows[0], { ...rows[1], credit: "—" }], voucher), /credit amount differs/);
});
