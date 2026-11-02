"""Sparse integer contributions retain exact missing-meaning and overflow rules."""

from ai_accounting_modular.contracts.public import parse_fen, validate_contract
from ai_accounting_modular.ledger.reporting.public import profit_totals

from ._balances import sum_amounts
from ._financial_cells import known
from ._journal_amounts import journal_amount
from ._profit import has_unmapped_line, pretax_values
from ._values import fail

# Unknown contributions carry issue indexes, never an inferred zero amount.
type _Amount = int | tuple[int, ...]
_TARGET_VALUES = frozenset(("operations.other_income", "taxes.assessment"))


def _new_vat(head, target):
    return (
        target["kind"] == "taxes.assessment"
        and "payable_vat_fen" in target["values"]
        and head["calculation_key"] == "taxes.assessment"
        and head["calculation_revision"] >= 2
    )


def load_profit_inputs(selection, heads):
    """Batch only selected VAT revisions that adopted the ordinary accounting split."""
    targets = [head for head in heads if selection.kind(head) in _TARGET_VALUES]
    selection.load_targets(targets)
    selection.load_bases([head for head in targets if _new_vat(head, selection.target(head))])


def _vat_exemption(selection, head, lines):
    quantities = [
        value
        for value in selection.basis(head)["adopted_quantities"]
        if value["type"] == "ordinary_vat_accounting"
    ]
    if len(quantities) != 1:
        fail("storage_corrupt", "新版VAT正式结果必须有唯一的普通免税及舍入核算组成。")
    quantity = quantities[0]
    validate_contract("business-facts.schema.json", "OrdinaryVatAccountingQuantity", quantity)
    exemption = parse_fen(quantity["exemption_income_fen"])
    rounding = parse_fen(quantity["rounding_adjustment_fen"])
    income = sum_amounts(
        journal_amount(line["credit_fen"]) - journal_amount(line["debit_fen"])
        for line in lines
        if line["account_code"] in ("6301", "630101")
    )
    expense = sum_amounts(
        journal_amount(line["debit_fen"]) - journal_amount(line["credit_fen"])
        for line in lines
        if line["account_code"] == "571105"
    )
    if income != sum_amounts((exemption, -min(rounding, 0))) or expense != max(rounding, 0):
        fail("storage_corrupt", "普通VAT正式免税及舍入组成与同源分录不能对平。")
    return exemption


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
    target = selection.target(head) if selection.kind(head) in _TARGET_VALUES else None
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
            journal_amount(line["debit_fen"]) - journal_amount(line["credit_fen"])
            for line in lines
            if line["account_code"] == "560301"
        )
        if (
            target is not None
            and target["kind"] == "operations.other_income"
            and target["values"].get("expense_or_income_class") == "bank_interest"
        ):
            interest = sum_amounts(
                journal_amount(line["debit_fen"]) - journal_amount(line["credit_fen"])
                for line in lines
                if line["account_code"] in ("5603", "560301")
            )
        local[19] = interest
    local[29] = sum_amounts(
        journal_amount(line["debit_fen"]) - journal_amount(line["credit_fen"])
        for line in lines
        if line["account_code"] == "571103"
    )
    for detail, account in ((26, "571106"), (27, "571107")):
        local[detail] = sum_amounts(
            journal_amount(line["debit_fen"]) - journal_amount(line["credit_fen"])
            for line in lines
            if line["account_code"] == account
        )
    if (
        target is not None
        and target["kind"] == "taxes.assessment"
        and "payable_vat_fen" in target["values"]
    ):
        if _new_vat(head, target):
            local[23] = _vat_exemption(selection, head, lines)
        else:
            # Revision 1 adopted no ordinary-component detail. Preserve that
            # historical rule for its exact head, including frozen predecessors.
            income = sum_amounts(
                journal_amount(line["credit_fen"]) - journal_amount(line["debit_fen"])
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
    load_profit_inputs(selection, heads)
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
