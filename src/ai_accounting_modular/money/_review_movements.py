"""Reuse ledger's narrow formal directory and page only actual allocation bodies."""

from ai_accounting_modular.contracts.public import ReadPage

from ._read_activity import movement_views


def page(owner, selection, query, read):
    found = owner._ledger.review_movement_page(selection, query, read)
    scope = {
        "company_id": read.company_id,
        "period_from": selection.period_from,
        "period_to": selection.period_to,
        "view": "current",
    }
    return ReadPage(
        found.version,
        found.total_count,
        found.filtered_count,
        movement_views(owner, found.items, scope, read),
        found.next_cursor,
    )
