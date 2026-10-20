"""Current related facts define a stable position in one original amount."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import cumulative_shares, format_fen

from ._common import amount, reject, sum_amounts


@dataclass(frozen=True)
class AmountRange:
    before: int
    after: int


@dataclass(frozen=True)
class Position(AmountRange):
    total: int


def related(fact, context, source_id, kinds, source_field):
    members = {
        item["business_id"]: item
        for item in context.related_facts(source_id, "calculation")
        if item["kind"] in kinds and item["data"][source_field] == source_id
    }
    members[fact["business_id"]] = fact
    return tuple(sorted(members.values(), key=lambda item: (item["period"], item["business_id"])))


def position(fact, members, field, capacity):
    total = sum_amounts(amount(item["data"], field) for item in members)
    if total > capacity:
        reject("同一原业务的累计退回或消耗不能超过原金额。")
    key = fact["period"], fact["business_id"]
    before = sum_amounts(
        amount(item["data"], field)
        for item in members
        if (item["period"], item["business_id"]) < key
    )
    return Position(before, sum_amounts((before, amount(fact["data"], field))), total)


def interval_shares(span, weights):
    before, after = cumulative_shares(span.before, weights), cumulative_shares(span.after, weights)
    return [new - old for old, new in zip(before, after, strict=True)]


def adopt_position(plan, source, span):
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "cumulative_range",
            "source": source,
            "from_fen": format_fen(span.before),
            "to_fen": format_fen(span.after),
        }
    )


def inherit_cashflow(source, target, spans):
    if "cashflow_weights" not in source:
        return
    parts = sorted(
        source["cashflow_weights"],
        key=lambda item: item["category"],
    )
    weights = [amount(item, "weight") for item in parts]
    shares = [interval_shares(span, weights) for span in spans]
    positive = {}
    for index, item in enumerate(parts):
        total = sum_amounts(row[index] for row in shares)
        if total:
            positive[item["category"]] = total
    if positive:
        target["cashflow_weights"] = classification_weights(positive)
    elif len(parts) == 1:
        target["cashflow_weights"] = [{"category": parts[0]["category"], "weight": "1"}]


def classification_weights(components):
    return [
        {"category": category, "weight": "1" if len(components) == 1 else format_fen(value)}
        for category, value in sorted(components.items())
    ]
