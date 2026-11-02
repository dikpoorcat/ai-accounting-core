"""Explain the producer's real 5111 movement without creating another money source."""

from copy import deepcopy

from ._common import amount, reject, total
from ._investment_tax_math import event_key


def pool_effect(plan, component, tax_change):
    if tax_change:
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "investment_income_adjustment",
                "component": component,
                "attribution": "company_pool",
                "amount_fen": str(-tax_change),
            }
        )


def interest_effects(plan, views, allocations):
    events = {event_key(view.source, view.event_index): view.event for view in views}
    for allocation in allocations:
        value = amount(allocation["amount_fen"])
        if not value:
            continue
        key = event_key(allocation["source"], allocation["event_index"])
        event = events.get(key)
        if event is None:
            reject("投资利息税额缺少精确正式收益来源，不能猜产品归属。")
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "investment_income_adjustment",
                "component": "interest_vat",
                "attribution": "event",
                "amount_fen": str(-value),
                "source": deepcopy(allocation["source"]),
                "event_index": allocation["event_index"],
                "terms_id": event["terms_id"],
                "product_id": event["product_id"],
            }
        )


def check_effects(plan):
    posted = total(
        amount(row["credit_fen"]) - amount(row["debit_fen"])
        for row in plan["lines"]
        if row["account_code"] == "5111"
    )
    explained = total(
        amount(row["amount_fen"])
        for row in plan["basis"]["adopted_quantities"]
        if row["type"] == "investment_income_adjustment"
    )
    if explained != posted:
        reject("投资税费的正式归属量与该结果实际5111变动不一致。")
