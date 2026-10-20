"""Fixed-share arithmetic and capital rights, without storage or invented cash."""

from dataclasses import dataclass, fields, replace
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import parse_rate

from ._common import checked, fail, total


@dataclass(frozen=True)
class Totals:
    company_funded_fen: int = 0
    company_received_fen: int = 0
    company_profit_received_fen: int = 0
    company_capital_returned_fen: int = 0
    operator_funded_fen: int = 0
    operator_withdrawn_fen: int = 0
    operator_profit_received_fen: int = 0
    project_revenue_fen: int = 0
    project_expense_fen: int = 0
    company_revenue_fen: int = 0
    company_expense_fen: int = 0
    project_collected_fen: int = 0
    project_paid_fen: int = 0
    company_collected_fen: int = 0
    company_paid_fen: int = 0
    company_outside_settled_out_fen: int = 0
    company_outside_settled_in_fen: int = 0

    def plus(self, **changes):
        unknown = set(changes) - {field.name for field in fields(self)}
        if unknown:
            raise ValueError(f"unknown cooperation totals: {sorted(unknown)}")
        return replace(
            self, **{key: total((getattr(self, key), value)) for key, value in changes.items()}
        )


def share(value, rate):
    """The contract's half-up curve; it is not a capacity or a money movement."""
    if type(value) is not int or value < 0:
        fail("合作分配基数必须是非负整数分。", "invalid_input")
    parsed = parse_rate(rate)
    if not Decimal(0) < parsed < Decimal(1):
        fail("合作份额必须严格大于0且小于1。", "invalid_input")
    with localcontext() as context:
        context.prec = 80
        return checked(int((Decimal(value) * parsed).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def interval_share(before, after, rate, *, offset=0):
    if min(before, after, offset) < 0 or after < before:
        fail("合作累计分配区间不合法。", "invalid_input")
    return checked(share(total((offset, after)), rate) - share(total((offset, before)), rate))


def position(values: Totals):
    funded = total((values.company_funded_fen, values.company_outside_settled_out_fen))
    received = total((values.company_received_fen, values.company_outside_settled_in_fen))
    company_cash = total(
        (funded, values.company_collected_fen, -values.company_paid_fen, -received)
    )
    operator_cash = total(
        (
            values.operator_funded_fen,
            values.project_collected_fen,
            -values.company_collected_fen,
            -values.project_paid_fen,
            values.company_paid_fen,
            -values.operator_withdrawn_fen,
        )
    )
    company_profit = checked(values.company_revenue_fen - values.company_expense_fen)
    operator_profit = total(
        (
            values.project_revenue_fen,
            -values.company_revenue_fen,
            -values.project_expense_fen,
            values.company_expense_fen,
        )
    )
    operator_due, company_due = max(-operator_cash, 0), max(-company_cash, 0)
    return {
        "company_cash_right_fen": company_cash,
        "operator_cash_right_fen": operator_cash,
        "company_capital_fen": total((funded, company_profit, -received)),
        "operator_capital_fen": total(
            (values.operator_funded_fen, operator_profit, -values.operator_withdrawn_fen)
        ),
        "company_profit_fen": company_profit,
        "operator_profit_fen": operator_profit,
        "pool_fen": total((company_cash, operator_cash)),
        "held_fen": checked(max(company_cash, 0) - operator_due),
        "operator_due_fen": operator_due,
        "company_due_fen": company_due,
    }
