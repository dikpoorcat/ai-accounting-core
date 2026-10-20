"""The single account-to-profit mapping for reports and income-tax inputs."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    content_digest,
    format_fen,
    parse_fen,
    schema_document,
)
from ai_accounting_modular.ledger.reporting.public import profit_totals

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
_CONTROLLED_ACCOUNTS = frozenset(
    schema_document("reporting.schema.json")["$defs"]["AccountCode"]["enum"]
)


def has_unmapped_line(lines):
    """Unknown formal accounts cannot silently become zero profit or zero assets."""
    return any(
        line["account_code"] not in _CONTROLLED_ACCOUNTS
        and (parse_fen(line["debit_fen"]) or parse_fen(line["credit_fen"]))
        for line in lines
    )


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
    cells = {
        line: {"state": "known", "amount_fen": format_fen(rows.get(line, 0))}
        for line in (1, 2, 3, 11, 14, 18, 20, 22, 24, 31)
    }
    totals = profit_totals(cells)
    return (
        rows.get(1, 0),
        rows.get(2, 0),
        parse_fen(totals[30]["amount_fen"]),
    )


def pretax_basis_digest(first, last, start, slices, prior, source, kind, *, view="current"):
    """Hash only the actual PL1/2/30 slices and necessary boundary basis."""
    values = [
        {key: value for key, value in item.items() if key != "sign" or view != "current"}
        for item in slices
    ]
    payload = {
        "period_from": first,
        "period_to": last,
        "results": sorted(values, key=lambda item: (item["business_id"], item["result_slot"])),
        "prior": {
            "kind": kind,
            "values": list(prior) if prior is not None else None,
            "source": [source["business_id"], source["result_slot"]] if source else None,
        },
        "start_period": start if first < start else None,
    }
    if view != "current":
        payload["view"] = view
    return content_digest(payload)


def has_pretax_effect(plan):
    return any(amount for row, amount in profit_rows(plan["lines"]).items() if row != 31)


def is_profit_account(code):
    return code in _ACCOUNTS
