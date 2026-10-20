"""Monotone cumulative cash classification, with exact integer priorities."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import cumulative_shares, validate_contract

from ._common import allocation_refs, amount, format_fen, total


def _cash_allocations(source, context):
    for fact in context.related_facts(source["business_id"], "settlement"):
        if fact["kind"] != "money.movement" or fact["data"]["type"] not in {
            "settlement",
            "payroll_reserve",
        }:
            continue
        for allocation in allocation_refs(fact):
            if allocation["source"] == source:
                yield fact, amount(allocation["amount_fen"])


def actual_settled_fen(source, cutoff, context):
    validate_contract("core.schema.json", "BusinessRef", source)
    return total(
        value
        for fact, value in _cash_allocations(source, context)
        if fact["period"] <= cutoff.period
        and (cutoff.on is None or fact["effective_on"] <= cutoff.on)
    )


def payment_parts(fact, allocation, obligation, context):
    classification = obligation.get("cashflow_weights")
    if classification is None:
        return None
    current = amount(allocation["amount_fen"])
    categories = defaultdict(int)
    for part in classification:
        categories[part["category"]] += amount(part["weight"])
    ordered = sorted(categories)
    weights = [categories[key] for key in ordered]
    source = allocation["source"]
    order = fact["effective_on"], fact["business_id"]
    before = total(
        value
        for other, value in _cash_allocations(source, context)
        if other["business_id"] != fact["business_id"]
        and (other["effective_on"], other["business_id"]) < order
    )
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
