"""The single account-to-profit mapping for reports and income-tax inputs."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import parse_fen

from ._balances import sum_amounts

_ACCOUNTS = {
    "5001": (1, -1),
    **{key: (2, 1) for key in ("5401", "540101", "540102", "540103", "540104")},
    "5403": (3, 1),
    **{key: (11, 1) for key in ("5601", "560101", "560102", "560103", "560104")},
    **{key: (14, 1) for key in ("5602", "560201", "560202", "560203", "560204")},
    "5603": (18, 1),
    "560301": (18, 1),
    "5111": (20, -1),
    "6301": (22, -1),
    "630101": (22, -1),
    **{key: (24, 1) for key in ("571101", "571102", "571103", "571104", "571105")},
    "5801": (31, 1),
}


def profit_rows(lines):
    """Only controlled ledger lines supply main rows; detail projections add none."""
    rows = defaultdict(int)
    for line in lines:
        mapping = _ACCOUNTS.get(line["account_code"])
        if mapping is not None:
            row, sign = mapping
            rows[row] = sum_amounts(
                (rows[row], sign * (parse_fen(line["debit_fen"]) - parse_fen(line["credit_fen"])))
            )
    return dict(rows)


def pretax_values(rows):
    operating = sum_amounts(
        rows.get(row, 0) * sign
        for row, sign in ((1, 1), (2, -1), (3, -1), (11, -1), (14, -1), (18, -1), (20, 1))
    )
    return (
        rows.get(1, 0),
        rows.get(2, 0),
        sum_amounts((operating, rows.get(22, 0), -rows.get(24, 0))),
    )


def has_pretax_effect(plan):
    return any(amount for row, amount in profit_rows(plan["lines"]).items() if row != 31)
