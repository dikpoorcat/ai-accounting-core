"""Selected-month confirmations preserve frozen correction differences."""

from collections import defaultdict

from ._common import amount
from ._investment_events import EVENT_KINDS
from ._read_funds import _contents, _pages
from ._read_types import InvestmentEvent


def flows(ledger, scope, read, cache, profits):
    views = list(
        _pages(
            lambda page: ledger.query_adopted_values(scope, tuple(sorted(EVENT_KINDS)), page, read)
        )
    )
    refs = [
        ref
        for view in views
        for ref in (view.reference, view.baseline_reference)
        if ref is not None
    ]
    bodies = _contents(ledger, refs, read, cache)
    events, by_terms = [], defaultdict(list)
    for view in views:
        if view.target == view.baseline_target:
            continue
        for sign, target, reference in (
            (-1, view.baseline_target, view.baseline_reference),
            (1, view.target, view.reference),
        ):
            if target is None or target["kind"] not in EVENT_KINDS:
                continue
            body, values = bodies[reference["result_id"]], target["values"]
            kind = target["kind"].removeprefix("investment.")
            management = kind in {"market_estimate", "holding_change"}
            event = InvestmentEvent(
                reference["result_id"] + (":before" if sign < 0 else ":after"),
                reference["business_id"],
                values["product_id"],
                kind,
                view.reference["posting_period"]
                if scope["view"] == "as_posted"
                else reference["business_period"],
                body.basis["period"],
                body.basis.get("effective_on"),
                body.source,
                reference,
                view.baseline_target is not None,
                None if management else sign * amount(values["cost_fen"]),
                None if management else sign * amount(values["amount_fen"]),
                None if management else sign * profits.get(reference["result_id"], (0, 0))[0],
                None,
                None,
                values["terms_id"],
                sign * profits.get(reference["result_id"], (0, 0))[1],
            )
            events.append(event)
            by_terms[values["terms_id"]].append(event)
    return events, by_terms
