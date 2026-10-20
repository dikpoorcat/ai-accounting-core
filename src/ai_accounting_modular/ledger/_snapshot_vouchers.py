"""Verify saved effective journals and exact frozen-baseline reversals."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import format_fen, validate_contract

from ._formal_output import target_voucher
from ._values import check_lines, fail


def verify_vouchers(records, heads, repo):
    vouchers = {row["voucher_id"]: row for row in records["voucher"]}
    lines, roles, numbers = defaultdict(list), defaultdict(set), defaultdict(set)
    for row in records["voucher_line"]:
        if row["voucher_id"] not in vouchers:
            fail("storage_corrupt", "正式分录行缺少所属凭证。")
        value = {
            key: row[key]
            for key in ("line_no", "account_code", "summary", "party_id", "object_id")
            if row[key] is not None
        }
        value.update(
            debit_fen=format_fen(row["debit_fen"]), credit_fen=format_fen(row["credit_fen"])
        )
        validate_contract("core.schema.json", "Line", value)
        lines[row["voucher_id"]].append(value)
    for voucher in vouchers.values():
        head = heads.get(voucher["result_id"])
        if head is None or voucher["period"] != head["posting_period"]:
            fail("storage_corrupt", "正式凭证缺少同一采用期的所属结果。")
        validate_contract("core.schema.json", "Id", voucher["voucher_id"])
        role, result_id = voucher["role"], voucher["result_id"]
        if role not in {"original", "replacement", "reversal"} or role in roles[result_id]:
            fail("storage_corrupt", "正式凭证角色不合法或重复。")
        roles[result_id].add(role)
        number = voucher["number"]
        if type(number) is not int or number < 1 or number in numbers[voucher["period"]]:
            fail("storage_corrupt", "正式凭证编号不合法或重复。")
        numbers[voucher["period"]].add(number)
        saved_lines = sorted(lines[voucher["voucher_id"]], key=lambda line: line["line_no"])
        if voucher["sealed"] != 1 or not saved_lines:
            fail("storage_corrupt", "正式凭证未封存或缺少分录。")
        check_lines(saved_lines)
        if voucher["summary"] != saved_lines[0]["summary"]:
            fail("storage_corrupt", "正式凭证摘要与封存分录不一致。")
        baseline_id = head["baseline_result_id"]
        if (role == "original") != (baseline_id is None):
            fail("storage_corrupt", "原凭证或更正凭证与冻结基线不一致。")
        if role == "reversal":
            original = target_voucher(repo, heads[baseline_id])
            if original is None or voucher["reverses_voucher_id"] != original["voucher_id"]:
                fail("storage_corrupt", "冲正凭证没有准确引用被冻结的有效原凭证。")
            expected = [
                dict(line, debit_fen=line["credit_fen"], credit_fen=line["debit_fen"])
                for line in original["lines"]
            ]
            if saved_lines != expected:
                fail("storage_corrupt", "冲正分录没有完整反向抵销冻结原凭证。")
        elif voucher["reverses_voucher_id"] is not None:
            fail("storage_corrupt", "非冲正凭证不能保留冲正关联。")
    for head in heads.values():
        own = roles[head["result_id"]]
        if head["target_state"] == "withdrawn" and own - {"reversal"}:
            fail("storage_corrupt", "退出结果不能保留新的有效凭证。")
        if "original" in own and own != {"original"}:
            fail("storage_corrupt", "原凭证不能混用更正角色。")
        baseline = heads.get(head["baseline_result_id"])
        if baseline is not None and (
            "replacement" in own
            or head["target_state"] == "withdrawn"
            or head["business_period"] != baseline["business_period"]
        ):
            original = target_voucher(repo, baseline)
            if original is not None and "reversal" not in own:
                fail("storage_corrupt", "替换、退出或移月没有冲正冻结的有效原凭证。")
    sequences = {row["period"]: row["next_number"] for row in records["voucher_sequence"]}
    for period, next_number in sequences.items():
        validate_contract("core.schema.json", "Month", period)
        if type(next_number) is not int or next_number <= max(numbers[period], default=0):
            fail("storage_corrupt", "凭证下一个编号没有超过已用编号。")
    if numbers.keys() - sequences.keys():
        fail("storage_corrupt", "正式凭证缺少所属月份的编号序列。")
    return {voucher["result_id"] for voucher in vouchers.values()}
