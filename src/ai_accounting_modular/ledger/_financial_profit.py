"""Profit main rows, controlled details and the same selected pretax slices."""

import json

from ai_accounting_modular.contracts.public import parse_fen
from ai_accounting_modular.ledger.reporting.public import profit_totals

from ._balances import sum_amounts
from ._financial_cells import add, known, scaled, zeros
from ._profit import has_unmapped_line, pretax_values, profit_rows


def profit(selection, first, last, column):
    coefficients = selection.flow(first, last)
    heads = [selection.heads[key] for key in coefficients]
    journals = selection.lines(heads)
    details = selection.report_details(heads)
    result = selection.prior("profit", first, last)
    for head in heads:
        identity, sign = head["result_id"], coefficients[head["result_id"]]
        lines = journals[identity]
        main = profit_rows(lines)
        local = zeros(32)
        local.update((line, known(value)) for line, value in main.items())
        explicit = {value["line"] for value in details[identity]}
        for value in details[identity]:
            line = value["line"]
            cell = (
                known(parse_fen(value["amount_fen"]))
                if "amount_fen" in value
                else selection.issues.unknown(
                    f"/profit_statement/{line}/{column}",
                    value["issue"]["meaning"],
                    value["issue"]["message"],
                    code=value["issue"]["code"],
                )
            )
            local[line] = add(local[line], cell)
        if 19 not in explicit:
            interest = sum_amounts(
                parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])
                for line in lines
                if line["account_code"] == "560301"
            )
            target = json.loads(head["target_json"])
            if (
                target["kind"] == "operations.other_income"
                and target["values"].get("expense_or_income_class") == "bank_interest"
            ):
                interest = sum_amounts(
                    parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])
                    for line in lines
                    if line["account_code"] in ("5603", "560301")
                )
            local[19] = known(interest)
        local[29] = known(
            sum_amounts(
                parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])
                for line in lines
                if line["account_code"] == "571103"
            )
        )
        target = json.loads(head["target_json"])
        if target["kind"] == "taxes.assessment" and "payable_vat_fen" in target["values"]:
            income = sum_amounts(
                parse_fen(line["credit_fen"]) - parse_fen(line["debit_fen"])
                for line in lines
                if line["account_code"] in ("6301", "630101")
            )
            rounding = parse_fen(target["values"].get("rounding_adjustment_fen", "0"))
            local[23] = known(sum_amounts((income, -max(-rounding, 0))))
        if has_unmapped_line(lines):
            local[24] = selection.issues.unknown(
                f"/profit_statement/30/{column}",
                "未映射的非零科目",
                "正式发生中存在未建立报表含义的非零科目。",
            )
        result = {line: add(result[line], scaled(local[line], sign)) for line in result}
    result.update(profit_totals(result))
    return result


def pretax_slices(selection, first, last):
    slices = []
    coefficients = selection.flow(first, last)
    heads = [selection.heads[key] for key in coefficients]
    journals = selection.lines(heads)
    for head in heads:
        rows = profit_rows(journals[head["result_id"]])
        if any(value for line, value in rows.items() if line != 31):
            slices.append(
                {
                    "business_id": head["business_id"],
                    "result_slot": head["result_slot"],
                    "period": head["business_period"],
                    "sign": coefficients[head["result_id"]],
                    "values": list(pretax_values(rows)),
                }
            )
    return sorted(
        slices,
        key=lambda value: (
            value["business_id"],
            value["result_slot"],
            value["period"],
            value["sign"],
        ),
    )
