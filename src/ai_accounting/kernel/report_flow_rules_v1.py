"""Frozen v1 report row classification used only by historical close verification."""

from collections import defaultdict

CASH_ACCOUNTS = {"1001", "1002", "1012"}
RECLASS = {
    "1122": (4, 34),
    "1123": (5, 33),
    "1221": (8, 39),
    "122101": (8, 39),
    "122105": (8, 39),
    "2202": (5, 33),
    "2203": (4, 34),
    "2241": (8, 39),
    "224101": (8, 39),
    "224102": (8, 39),
    "224103": (8, 39),
    "224104": (8, 39),
    "224105": (8, 39),
}
PROFIT_ACCOUNTS = {
    "5001": (1, -1),
    "5111": (20, -1),
    "5401": (2, 1),
    "540101": (2, 1),
    "540104": (2, 1),
    "540102": (2, 1),
    "540103": (2, 1),
    "5403": (3, 1),
    "5601": (11, 1),
    "560101": (11, 1),
    "560102": (11, 1),
    "560103": (11, 1),
    "560104": (11, 1),
    "5602": (14, 1),
    "560201": (14, 1),
    "560202": (14, 1),
    "560203": (14, 1),
    "560204": (14, 1),
    "5603": (18, 1),
    "560301": (18, 1),
    "6301": (22, -1),
    "630101": (22, -1),
    "571101": (24, 1),
    "571102": (24, 1),
    "571103": (24, 1),
    "571104": (24, 1),
    "5801": (31, 1),
}
DETAIL_LINES = {
    "management_startup": 15,
    "management_entertainment": 16,
    "management_research": 17,
    "sales_merchandise_repair": 12,
    "sales_advertising_promotion": 13,
    "finance_interest": 19,
}
CASH_CATEGORIES = {
    "managed_reserve_outflow": 6,
    "customer_receipts": 1,
    "other_operating_receipts": 2,
    "pass_through_receipts": 2,
    "tax_refunds": 2,
    "pass_through_payments": 6,
    "pass_through_refund": 6,
    "asset_acquisition": 12,
    "asset_disposal": 10,
    "investment_recovery": 8,
    "investment_acquisition": 11,
    "loan_receipts": 14,
    "loan_repayment": 16,
    "financing_repayment": 16,
    "interest_payments": 17,
    "tax_payments": 5,
    "labor": 3,
}
INFLOW_ROWS = {1, 2, 8, 9, 10, 14, 15}
PROFIT_NAMES = range(1, 33)
CASH_FLOW_NAMES = range(1, 23)


def issue(field, message, **data):
    return {"field": field, "message": message, "semantics": "accounting", **data}


def sum_fen(values):
    result = 0
    for value in values:
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise ValueError("invalid v1 report amount")
        result += value
        if not -(2**63) <= result < 2**63:
            raise ValueError("v1 report amount overflow")
    return result


def _profit_rows(rows, begin, end, problems):
    result = {line: 0 for line in PROFIT_NAMES}
    for row in rows:
        if not begin <= row["period"] <= end or row["account"] not in PROFIT_ACCOUNTS:
            continue
        main, sign = PROFIT_ACCOUNTS[row["account"]]
        result[main] += row["amount"] * sign
        account, kind, fact = row["account"], row["kind"], row["fact"]
        automatic = account == "5603" and (
            kind in {"loan_interest", "bank_income"}
            or (kind == "expense" and getattr(fact, "expense_class", None) == "bank_fee")
        )
        if account == "560301" or (
            account == "5603"
            and (kind == "loan_interest" or getattr(fact, "income_kind", None) == "bank_interest")
        ):
            result[19] += row["amount"]
        if account == "6301" and (
            kind == "tax_assessment" or getattr(fact, "income_kind", None) == "government_grant"
        ):
            result[23] -= row["amount"]
        details = (
            [x for x in row["classification"].profit_details if x.line_no == row["line_no"]]
            if row["classification"]
            else []
        )
        if account == "5403":
            direction = -1 if row["reverses_id"] else 1
            automatic_tax = (
                kind == "tax_assessment"
                and row["amount"] * direction > 0
                and abs(row["amount"]) == row["values"].get("surtax_fen")
            )
            if automatic_tax:
                result[6] += direction * row["values"]["urban_tax_fen"]
                result[10] += direction * (
                    row["values"]["education_tax_fen"] + row["values"]["local_education_tax_fen"]
                )
                if details:
                    problems.append(
                        issue(
                            "report_classification.profit_details",
                            "已由计税依据确定附加税明细，不应重复分类",
                        )
                    )
            elif sum(x.amount_fen for x in details) == abs(row["amount"]) and all(
                x.detail_code.startswith("tax_") for x in details
            ):
                for item in details:
                    result[6 if item.detail_code == "tax_urban" else 10] += item.amount_fen * (
                        1 if row["amount"] > 0 else -1
                    )
            else:
                problems.append(
                    issue(
                        "report_classification.profit_details",
                        "附加税退抵差额需有依据的城市维护税及教育附加明细",
                        voucher_version_id=row["reverses_id"] or row["version_id"],
                        line_no=row["line_no"],
                    )
                )
            continue
        if account in {"5601", "5602", "5603"} and not automatic:
            family = {"5601": "sales_", "5602": "management_", "5603": "finance_"}[account]
            if sum(x.amount_fen for x in details) != abs(row["amount"]) or any(
                not x.detail_code.startswith(family) for x in details
            ):
                problems.append(
                    issue(
                        "report_classification.profit_details",
                        "费用明细分类需完整覆盖原凭证金额",
                        voucher_version_id=row["reverses_id"] or row["version_id"],
                        line_no=row["line_no"],
                    )
                )
            else:
                for item in details:
                    if item.detail_code in DETAIL_LINES:
                        result[DETAIL_LINES[item.detail_code]] += item.amount_fen * (
                            1 if row["amount"] > 0 else -1
                        )
        elif details:
            problems.append(
                issue(
                    "report_classification.profit_details",
                    "该行已有确定性分类或不属于可分类的费用",
                    voucher_version_id=row["version_id"],
                    line_no=row["line_no"],
                )
            )
    result[21] = (
        result[1] - result[2] - result[3] - result[11] - result[14] - result[18] + result[20]
    )
    result[30] = result[21] + result[22] - result[24]
    result[32] = result[30] - result[31]
    return result


def _cash_rows(rows, begin, end, problems, *, account_balances=None):
    result = {line: 0 for line in CASH_FLOW_NAMES}
    transfers = defaultdict(int)
    for row in rows:
        if (
            row.get("opening")
            or not begin <= row["period"] <= end
            or row["account"] not in CASH_ACCOUNTS
        ):
            continue
        fact, tag = row["cash_source"], row["cashflow"]
        detail = (
            [x for x in row["classification"].cash_details if x.line_no == row["line_no"]]
            if row["classification"]
            else []
        )
        category = CASH_CATEGORIES.get(tag)
        if tag == "financing_receipts":
            category = 15 if getattr(fact, "funding_kind", None) == "capital" else 14
        elif tag == "operating_payments":
            expense_class = getattr(fact, "expense_class", None)
            category = (
                3
                if expense_class == "service"
                else (6 if expense_class in {"administration", "sales", "bank_fee"} else None)
            )
        elif tag == "payroll":
            category = 5 if row.get("cash_obligation", {}).get("account") == "222103" else 4
        if (
            row["kind"] in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"}
            and tag != "managed_reserve_outflow"
        ):
            transfers[row["version_id"]] += row["amount"]
            continue
        if detail:
            if sum(item.amount_fen for item in detail) != abs(row["amount"]) or (
                category is not None and any(item.category != category for item in detail)
            ):
                problems.append(
                    issue(
                        "report_classification.cash_details",
                        "现金分类须完整覆盖金额并与明确业务来源一致",
                        voucher_version_id=row["version_id"],
                        line_no=row["line_no"],
                    )
                )
                continue
            for item in detail:
                result[item.category] += item.amount_fen * (
                    (1 if row["amount"] > 0 else -1) * (1 if item.category in INFLOW_ROWS else -1)
                )
            continue
        if category is None:
            problems.append(
                issue(
                    "report_classification.cash_details",
                    "非零现金流缺少确定分类",
                    voucher_version_id=row["reverses_id"] or row["version_id"],
                    line_no=row["line_no"],
                )
            )
            continue
        result[category] += row["amount"] * (1 if category in INFLOW_ROWS else -1)
    if any(transfers.values()):
        problems.append(issue("cash_transfer", "内部资金划转未完整抵销"))
    result[7] = result[1] + result[2] - result[3] - result[4] - result[5] - result[6]
    result[13] = result[8] + result[9] + result[10] - result[11] - result[12]
    result[19] = result[14] + result[15] - result[16] - result[17] - result[18]
    result[20] = result[7] + result[13] + result[19]
    if account_balances is None:
        result[21] = sum_fen(
            r["amount"] for r in rows if r["account"] in CASH_ACCOUNTS and r["period"] < begin
        )
    else:
        totals = account_balances[begin - 1]
        result[21] = (
            None if totals is None else sum_fen(totals.get(account, 0) for account in CASH_ACCOUNTS)
        )
    result[22] = None if result[21] is None else result[21] + result[20]
    return result
