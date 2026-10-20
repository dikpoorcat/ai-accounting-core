"""Ledger balances remain primary; resource positions only classify and reconcile them."""

import calendar
import json
from collections import defaultdict
from datetime import date

from ai_accounting_modular.contracts.public import parse_fen
from ai_accounting_modular.ledger.opening.public import is_zero_origin
from ai_accounting_modular.ledger.reporting.public import balance_totals

from ._balance_cube import balance_cube, previous
from ._balances import obligation_remaining, sum_amounts
from ._financial_cells import known, zeros
from ._profit import is_profit_account

_PAIRS = {
    "1122": (4, 34),
    "2203": (4, 34),
    "1123": (5, 33),
    "2202": (5, 33),
    **{
        code: (8, 39)
        for code in (
            "1221",
            "122105",
            "122106",
            "122107",
            "2241",
            "224101",
            "224102",
            "224103",
            "224104",
            "224105",
            "224106",
        )
    },
}
_DIRECT = {
    "1001": (1, 1),
    "1002": (1, 1),
    "1012": (1, 1),
    "1101": (2, 1),
    "1601": (18, 1),
    "1602": (19, -1),
    "1604": (21, 1),
    "1701": (25, 1),
    "1702": (25, 1),
    "4301": (26, 1),
    "189901": (28, 1),
    "2001": (31, -1),
    "221101": (35, -1),
    "221102": (35, -1),
    "221103": (35, -1),
    **{code: (36, -1) for code in ("222101", "222102", "222103", "222104", "222106")},
    "2231": (37, -1),
    "3001": (48, -1),
    "3002": (49, -1),
    "3101": (50, -1),
    "3104": (51, -1),
}


def _resource_positions(selection, heads):
    obligations = selection.children("obligation", heads)
    owners, values, totals = {}, {}, defaultdict(lambda: defaultdict(int))
    for head in heads:
        for value in obligations[head["result_id"]]:
            key = head["business_id"], value["public_slot"]
            owners[key], values[key] = head, value
    for head_rows in selection.children("settlement", heads).values():
        for row in head_rows:
            key = row["source_business_id"], row["source_public_slot"]
            totals[key][row["method"]] = sum_amounts(
                (totals[key][row["method"]], row["amount_fen"])
            )
    for head_rows in selection.children("obligation_reclassification", heads).values():
        for row in head_rows:
            key = row["source_business_id"], row["source_public_slot"]
            totals[key][row["mode"]] = sum_amounts((totals[key][row["mode"]], row["amount_fen"]))
    return [
        (key, owners[key], value, obligation_remaining(value["amount_fen"], totals[key]))
        for key, value in values.items()
    ]


def _loan_balances(selection, cutoff, resources, loan_total):
    """Classify only a GL-reconciled loan principal by its adopted contract terms."""
    loans = [item for item in resources if item[2]["account_code"] == "2501"]
    selection.load_bases(
        list({head["result_id"]: head for _, head, _, remaining in loans if remaining}.values())
    )
    report_day = date(
        int(cutoff[:4]), int(cutoff[5:]), calendar.monthrange(int(cutoff[:4]), int(cutoff[5:]))[1]
    )
    edge = (
        date(
            report_day.year + 1,
            report_day.month,
            min(report_day.day, calendar.monthrange(report_day.year + 1, report_day.month)[1]),
        )
        if report_day.year < 9999
        else date.max
    )
    classified, short, valid = 0, 0, True
    for key, owner, value, remaining in loans:
        classified = sum_amounts((classified, remaining))
        if not remaining:
            continue
        selection.source(owner)
        quantities = selection.basis(owner).get("adopted_quantities", [])
        principal = [
            q
            for q in quantities
            if q["type"] == "loan_principal"
            and (q["source"]["business_id"], q["source"]["slot"]) == key
        ]
        terms = [
            q
            for q in quantities
            if q["type"] == "loan_terms"
            and principal
            and q["contract_id"] == principal[0]["contract_id"]
        ]
        if (
            value["direction"] != "payable"
            or value["category"] != "loan_principal"
            or len(principal) != 1
            or len(terms) != 1
            or remaining < 0
            or terms[0]["repayment"] != "bullet"
            or terms[0]["lender_id"] != value["party_id"]
            or value["due_on"] not in (None, terms[0]["matures_on"])
        ):
            valid = False
        elif date.fromisoformat(terms[0]["matures_on"]) <= edge:
            short = sum_amounts((short, remaining))
    return (
        (short, sum_amounts((loan_total, -short))) if valid and classified == loan_total else None
    )


def positions(selection, cutoff, column):
    if cutoff < selection.start:
        if selection.continuation and cutoff == f"{int(selection.start[:4]) - 1:04d}-12":
            selection.source(selection.continuation)
            return {
                item["line"]: known(parse_fen(item["beginning_fen"]))
                for item in selection.basis(selection.continuation)["input"][
                    "year_beginning_balance"
                ]
            }
        if (
            selection.opening
            and not selection.continuation
            and cutoff == f"{int(selection.start[:4]) - 1:04d}-12"
            and is_zero_origin(
                json.loads(selection.opening["target_json"]), selection.basis(selection.opening)
            )
        ):
            selection.source(selection.opening)
            return zeros(53)
        if cutoff != previous(selection.start) or selection.start[5:] != "01":
            # Only cash can be established at an interior pre-cutover boundary.
            return {
                line: selection.issues.unknown(
                    f"/balance_sheet/{line}/{column}",
                    "接账前资产负债表",
                    "接账期初不是年初余额，缺少该比较日期的正式接续。",
                )
                for line in range(1, 54)
            }
    cube = balance_cube(selection, cutoff)
    if cube is None:
        return {
            line: selection.issues.unknown(
                f"/balance_sheet/{line}/{column}",
                "正式建账起点",
                "缺少正式采用的期初余额，不能把此前余额推定为零。",
            )
            for line in range(1, 54)
        }
    heads = list(selection.stock(cutoff).values()) if cutoff >= selection.start else []
    grouped = defaultdict(int)
    for (code, party, _), amount in cube.items():
        grouped[code, party] = sum_amounts((grouped[code, party], amount))
    resource_heads = [*heads, selection.opening]
    resources = _resource_positions(selection, resource_heads)
    totals = defaultdict(int)
    unknown = set()
    for (code, party), amount in grouped.items():
        if code in _DIRECT:
            line, sign = _DIRECT[code]
            totals[line] = sum_amounts((totals[line], sign * amount))
        elif code in _PAIRS:
            asset, liability = _PAIRS[code]
            if party is None and amount and code in ("224102", "224103"):
                controlled = [
                    (value, remaining)
                    for _, _, value, remaining in resources
                    if value["account_code"] == code and value["party_id"] is None
                ]
                proven = controlled and all(
                    value["direction"] == "payable"
                    and value["category"] in ("social", "housing")
                    and remaining >= 0
                    for value, remaining in controlled
                )
                if proven and sum_amounts(remaining for _, remaining in controlled) == -amount:
                    totals[liability] = sum_amounts((totals[liability], -amount))
                else:
                    unknown.update((asset, liability))
            elif party is None and amount:
                unknown.update((asset, liability))
            elif amount:
                line = asset if amount > 0 else liability
                totals[line] = sum_amounts((totals[line], abs(amount)))
        elif code != "2501":
            if is_profit_account(code):
                # Undistributed profit contains the credit balance of all P&L accounts once.
                totals[51] = sum_amounts((totals[51], -amount))
            elif amount:
                unknown.update((30, 53))
    result = zeros(53)
    result.update((line, known(amount)) for line, amount in totals.items())
    loan_total = -sum_amounts(amount for (code, _), amount in grouped.items() if code == "2501")
    loans = _loan_balances(selection, cutoff, resources, loan_total)
    if loans is not None:
        result[40], result[42] = (known(amount) for amount in loans)
    else:
        unknown.update((40, 42))
    for line in unknown:
        result[line] = selection.issues.unknown(
            f"/balance_sheet/{line}/{column}",
            "余额的明确归属或采用条款",
            "非零余额缺少可对平的对象归属、逐笔贷款条款或受控报表映射。",
        )
    result.update(balance_totals(result))
    # An unknown unmapped balance must not be overwritten by a known subtotal.
    for line in unknown & {30, 53}:
        result[line] = selection.issues.unknown(
            f"/balance_sheet/{line}/{column}",
            "未映射非零余额",
            "存在尚未建立报表含义的正式非零余额。",
        )
    return result


def cash_before(selection, first, last, column):
    cutoff = previous(first)
    if cutoff >= selection.start or first == selection.start:
        rows = selection.opening_rows()
        if rows is not None:
            heads = list(selection.stock(cutoff).values()) if cutoff >= selection.start else []
            journals = selection.lines(heads)
            return known(
                sum_amounts(
                    parse_fen(row["debit_fen"]) - parse_fen(row["credit_fen"])
                    for row in [*rows, *(line for lines in journals.values() for line in lines)]
                    if row["account_code"] in ("1001", "1002", "1012")
                )
            )
        return selection.issues.unknown(
            f"/cash_flow_statement/21/{column}",
            "正式建账资金起点",
            "没有正式采用的期初资金，不能推定现金起点为零。",
        )
    return selection.prior("cash", first, last)[21]
