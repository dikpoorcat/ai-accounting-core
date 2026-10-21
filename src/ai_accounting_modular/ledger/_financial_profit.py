"""Sparse integer contributions retain exact missing-meaning and overflow rules."""

from ai_accounting_modular.contracts.public import parse_fen
from ai_accounting_modular.ledger.reporting.public import profit_totals

from ._balances import sum_amounts
from ._financial_cells import known
from ._profit import has_unmapped_line, pretax_values

# Unknown contributions carry issue indexes, never an inferred zero amount.
type _Amount = int | tuple[int, ...]


def _from_cell(cell) -> _Amount:
    return (
        parse_fen(cell["amount_fen"]) if cell["state"] == "known" else tuple(cell["issue_indexes"])
    )


def _add(left: _Amount, right: _Amount) -> _Amount:
    if isinstance(left, tuple) or isinstance(right, tuple):
        return tuple(
            sorted(
                set(
                    (left if isinstance(left, tuple) else ())
                    + (right if isinstance(right, tuple) else ())
                )
            )
        )
    return sum_amounts((left, right))


def _contribution(selection, head, lines, details, column):
    local = dict(selection.profit_rows(head, lines))
    target = selection.target(head)
    explicit = {value["line"] for value in details}
    for value in details:
        line = value["line"]
        amount = (
            parse_fen(value["amount_fen"])
            if "amount_fen" in value
            else _from_cell(
                selection.issues.unknown(
                    f"/profit_statement/{line}/{column}",
                    value["issue"]["meaning"],
                    value["issue"]["message"],
                    code=value["issue"]["code"],
                )
            )
        )
        local[line] = _add(local.get(line, 0), amount)
    if 19 not in explicit:
        interest = sum_amounts(
            parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])
            for line in lines
            if line["account_code"] == "560301"
        )
        if (
            target["kind"] == "operations.other_income"
            and target["values"].get("expense_or_income_class") == "bank_interest"
        ):
            interest = sum_amounts(
                parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])
                for line in lines
                if line["account_code"] in ("5603", "560301")
            )
        local[19] = interest
    local[29] = sum_amounts(
        parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])
        for line in lines
        if line["account_code"] == "571103"
    )
    if target["kind"] == "taxes.assessment" and "payable_vat_fen" in target["values"]:
        income = sum_amounts(
            parse_fen(line["credit_fen"]) - parse_fen(line["debit_fen"])
            for line in lines
            if line["account_code"] in ("6301", "630101")
        )
        rounding = parse_fen(target["values"].get("rounding_adjustment_fen", "0"))
        local[23] = sum_amounts((income, -max(-rounding, 0)))
    if has_unmapped_line(lines):
        local[24] = _from_cell(
            selection.issues.unknown(
                f"/profit_statement/30/{column}",
                "未映射的非零科目",
                "正式发生中存在未建立报表含义的非零科目。",
            )
        )
    return local


def profit(selection, first, last, column):
    coefficients = selection.flow(first, last)
    heads = [selection.heads[key] for key in coefficients]
    journals, details = selection.lines(heads), selection.report_details(heads)
    result = {
        line: _from_cell(cell) for line, cell in selection.prior("profit", first, last).items()
    }
    for head in heads:
        identity, sign = head["result_id"], coefficients[head["result_id"]]
        local = _contribution(selection, head, journals[identity], details[identity], column)
        for line in sorted(local):
            amount = local[line]
            # Scaling is checked even if the accumulated line is already unknown.
            scaled = amount if isinstance(amount, tuple) else sum_amounts((sign * amount,))
            result[line] = _add(result[line], scaled)
    cells = {
        line: {"state": "unestablished", "issue_indexes": list(amount)}
        if isinstance(amount, tuple)
        else known(amount)
        for line, amount in result.items()
    }
    cells.update(profit_totals(cells))
    return cells


def pretax_slices(selection, first, last):
    slices = []
    coefficients = selection.flow(first, last)
    heads = [selection.heads[key] for key in coefficients]
    journals = selection.lines(heads)
    for head in heads:
        rows = selection.profit_rows(head, journals[head["result_id"]])
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
