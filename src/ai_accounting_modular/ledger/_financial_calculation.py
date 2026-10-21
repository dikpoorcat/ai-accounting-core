"""The single statement/summary assembly; selection and output identity remain separate."""

from copy import deepcopy
from functools import partial

from ai_accounting_modular.contracts.public import parse_fen, schema_document

from ._balance_cube import balance_cube
from ._balances import sum_amounts
from ._cashflow import cash
from ._financial_cells import add, known, scaled
from ._financial_positions import cash_before, positions, previous
from ._financial_profit import pretax_slices, profit
from ._profit import pretax_basis_digest

MAPPING_V1 = "small-enterprise-financial-1"

ACCOUNT_FIELDS = (
    ("bank", ("1002",), 1),
    ("cash", ("1001",), 1),
    ("payment_platform", ("1012",), 1),
    ("fixed_cost", ("1601",), 1),
    ("fixed_accumulated", ("1602",), -1),
    ("fixed_net", ("1601", "1602"), 1),
    ("intangible_cost", ("1701",), 1),
    ("intangible_accumulated", ("1702",), -1),
    ("intangible_net", ("1701", "1702"), 1),
    ("long_term_cost", ("1601", "1701", "1604", "189901", "4301"), 1),
    ("long_term_accumulated", ("1602", "1702"), -1),
    ("long_term_net", ("1601", "1602", "1701", "1702", "1604", "189901", "4301"), 1),
)


def amount(selection, cutoff, codes, *, object_id=None, sign=1, field):
    cube = balance_cube(selection, cutoff)
    if cube is None:
        return selection.issues.unknown(
            field, "正式期初及指定月末余额", "缺少该比较时点的正式期初，不能把余额推定为零。"
        )
    return known(
        sign
        * sum_amounts(
            amount
            for (code, _, obj), amount in cube.items()
            if code in codes and (object_id is None or obj == object_id)
        )
    )


class ClassicCalculation:
    """Bind the existing owner computations without choosing public Sources or versions."""

    def __init__(self, selection):
        self.positions = partial(positions, selection)
        self.profit = partial(profit, selection)
        self.cash = partial(cash, selection)
        self.cash_before = partial(cash_before, selection)
        self.pretax_slices = partial(pretax_slices, selection)
        self.amount = partial(amount, selection)


def _check(code, left, right):
    difference = add(left, scaled(right, -1))
    if difference["state"] == "unestablished":
        return {"code": code, "status": "unknown", "issue_indexes": difference["issue_indexes"]}
    amount = difference["amount_fen"]
    return {
        "code": code,
        "status": "pass" if amount == "0" else "fail",
        "difference_fen": amount,
        "issue_indexes": [],
    }


def _table(definition, columns):
    schema = schema_document("reporting.schema.json")["$defs"][definition]["properties"]
    return {
        key: {
            "name": row["properties"]["name"]["const"],
            **{column: values[int(key)] for column, values in columns.items()},
        }
        for key, row in schema.items()
    }


def calculate_snapshot(normalized, context, selection, *, calculation=None):
    """Build statement values, issues and PreTax without a Source/version finalizer."""
    calculation = ClassicCalculation(selection) if calculation is None else calculation
    first, last = normalized["period_from"], normalized["period_to"]
    year = last[:4] + "-01"
    begin = calculation.positions(previous(year), "beginning")
    ending = calculation.positions(last, "ending")
    current = calculation.profit(first, last, "current")
    annual = current if first == year else calculation.profit(year, last, "year_to_date")
    current_cash = calculation.cash(first, last, "current")
    annual_cash = (
        deepcopy(current_cash) if first == year else calculation.cash(year, last, "year_to_date")
    )
    current_cash[21] = calculation.cash_before(first, last, "current_cash_begin")
    annual_cash[21] = begin[1]
    current_cash[22] = annual_cash[22] = ending[1]
    checks = [
        _check("balance_beginning", begin[30], begin[53]),
        _check("balance_ending", ending[30], ending[53]),
        _check("profit_current", current[32], add(current[30], scaled(current[31], -1))),
        _check("profit_ytd", annual[32], add(annual[30], scaled(annual[31], -1))),
        _check("cash_current", current_cash[22], add(current_cash[21], current_cash[20])),
        _check("cash_ytd", annual_cash[22], add(annual_cash[21], annual_cash[20])),
        _check("cash_to_balance_current", current_cash[22], ending[1]),
        _check("cash_to_balance_ytd", annual_cash[22], ending[1]),
        _check(
            "cash_to_postings_current",
            current_cash[20],
            add(ending[1], scaled(current_cash[21], -1)),
        ),
        _check(
            "cash_to_postings_ytd", annual_cash[20], add(ending[1], scaled(annual_cash[21], -1))
        ),
    ]
    prior = selection.prior("profit", year, last)
    proven = all(prior[line]["state"] == "known" for line in (1, 2, 30))
    prior_source = (
        (selection.continuation or selection.opening)
        if proven and year < context.start_period
        else None
    )
    prior_kind = (
        ("year_to_date_fen" if selection.continuation else "new_company_zero")
        if prior_source
        else None
    )
    pretax_digest = pretax_basis_digest(
        year,
        last,
        context.start_period,
        calculation.pretax_slices(year, last),
        tuple(parse_fen(prior[line]["amount_fen"]) for line in (1, 2, 30)) if proven else None,
        prior_source,
        prior_kind,
        view=selection.view,
    )
    pre_tax = {
        "period_from": year,
        "period_to": last,
        "basis_digest": pretax_digest,
        **{
            name: annual[line]
            for name, line in (
                ("operating_revenue", 1),
                ("operating_cost", 2),
                ("profit_before_tax", 30),
            )
        },
    }
    result = {
        "format": "ai-accounting-financial-snapshot/1",
        "company_id": context.company_id,
        "database_id": context.database_id,
        "head": context.head,
        "scope": normalized,
        "accounting_standard": "small_enterprise",
        "mapping_version": MAPPING_V1,
        "status": "partial"
        if selection.issues.values or any(check["status"] != "pass" for check in checks)
        else "ready",
        "period_bounds": {
            "current_from": first,
            "current_to": last,
            "year_from": year,
            "year_to": last,
            "current_cash_begin_before": previous(first),
            "balance_begin_before": previous(year),
        },
        "balance_sheet": _table("BalanceSheet", {"beginning": begin, "ending": ending}),
        "profit_statement": _table("ProfitStatement", {"current": current, "year_to_date": annual}),
        "cash_flow_statement": _table(
            "CashFlowStatement", {"current": current_cash, "year_to_date": annual_cash}
        ),
        "pre_tax": pre_tax,
        "checks": checks,
        "issues": selection.issues.values,
    }
    return result


def summary_values(normalized, selection, *, saved=None, calculation=None):
    """Compute the public summary fields; saved closed tables retain their old authority."""
    calculation = ClassicCalculation(selection) if calculation is None else calculation
    first, last = normalized["period_from"], normalized["period_to"]
    if saved is not None:
        ending = {int(key): row["ending"] for key, row in saved["balance_sheet"].items()}
        current = {int(key): row["current"] for key, row in saved["profit_statement"].items()}
        selection.issues.values = list(saved["issues"])
    else:
        ending = calculation.positions(last, "ending")
        current = calculation.profit(first, last, "current")
    values = {
        "revenue": add(*(current[n] for n in (1, 20, 22))),
        "expense": add(*(current[n] for n in (2, 3, 11, 14, 18, 24, 31))),
        "net_profit": current[32],
        **{
            name: ending[number]
            for name, number in (
                ("assets", 30),
                ("liabilities", 47),
                ("current_liabilities", 41),
                ("non_current_liabilities", 46),
                ("equity", 52),
                ("capital", 48),
                ("retained_profit", 51),
            )
        },
    }
    for name, codes, sign in ACCOUNT_FIELDS:
        values[name] = calculation.amount(last, codes, sign=sign, field="/" + name)
    return values
