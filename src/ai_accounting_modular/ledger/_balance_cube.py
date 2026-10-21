"""One selected debit-balance cube retains party and object dimensions."""

from collections import defaultdict
from itertools import chain

from ._balances import sum_amounts
from ._journal_amounts import journal_amount


def previous(period):
    year, month = int(period[:4]), int(period[5:])
    return f"{year if month > 1 else year - 1:04d}-{month - 1 if month > 1 else 12:02d}"


def balance_cube(selection, cutoff):
    if cutoff not in selection._balance_cubes:
        opening = selection.opening_rows()
        if opening is None or cutoff < previous(selection.start):
            selection._balance_cubes[cutoff] = None
        else:
            heads = list(selection.stock(cutoff).values()) if cutoff >= selection.start else []
            journals = selection.lines(heads)
            values = defaultdict(int)
            for row in chain(opening, chain.from_iterable(journals.values())):
                key = row["account_code"], row.get("party_id"), row.get("object_id")
                values[key] = sum_amounts(
                    (
                        values[key],
                        journal_amount(row["debit_fen"]),
                        -journal_amount(row["credit_fen"]),
                    )
                )
            selection._balance_cubes[cutoff] = dict(values)
    return selection._balance_cubes[cutoff]
