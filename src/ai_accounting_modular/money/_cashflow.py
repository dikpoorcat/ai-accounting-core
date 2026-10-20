"""Monotone cumulative cash classification, with exact integer priorities."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import cumulative_shares

from ._common import allocation_refs, amount, format_fen, total


def payment_parts(fact, allocation, obligation, context):
    parts = obligation.get("cashflow_parts")
    if parts is None:
        return None
    current = amount(allocation["amount_fen"])
    categories = defaultdict(int)
    for part in parts:
        categories[part["category"]] += amount(part["amount_fen"])
    ordered = sorted(categories)
    weights = [categories[key] for key in ordered]
    if not weights and amount(obligation["amount_fen"]) == 0:
        # A fully corrected zero obligation may still have a real old payment
        # and an explicit excess-recovery relationship. No positive original
        # classification weights remain, so keep the payment and expose the
        # classification as unknown rather than inventing zero cash or a class.
        return None
    if not weights or total(weights) != amount(obligation["amount_fen"]):
        raise ValueError("formal obligation classification does not equal its amount")
    source = allocation["source"]
    order = fact["effective_on"], fact["business_id"]
    previous = []
    for other in context.related_facts(source["business_id"], "settlement"):
        if (
            other["business_id"] == fact["business_id"]
            or other["kind"] != "money.movement"
            or other["data"]["type"] not in {"settlement", "payroll_reserve"}
            or (other["effective_on"], other["business_id"]) >= order
        ):
            continue
        previous.extend(
            amount(item["amount_fen"])
            for item in allocation_refs(other)
            if item["source"] == source
        )
    before = total(previous)
    after = total((before, current))
    previous_shares = cumulative_shares(before, weights)
    next_shares = cumulative_shares(after, weights)
    return [
        {"category": key, "amount_fen": format_fen(next_value - old_value)}
        for key, old_value, next_value in zip(ordered, previous_shares, next_shares, strict=True)
        if next_value != old_value
    ]


def combine_parts(groups):
    if any(parts is None for parts in groups):
        return None
    totals = defaultdict(list)
    for parts in groups:
        for part in parts:
            totals[part["category"]].append(amount(part["amount_fen"]))
    return [
        {"category": key, "amount_fen": format_fen(total(totals[key]))}
        for key in sorted(totals)
        if total(totals[key])
    ]
