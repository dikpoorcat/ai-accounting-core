"""Causal cash ranges for one original obligation's current recovery claims."""

from ai_accounting_modular.contracts.public import ResourceCutoff

from ._common import amount, reject, sum_amounts
from ._cumulative import AmountRange


def claim_order(fact):
    return (
        fact["period"],
        "effective_on" not in fact,
        fact.get("effective_on", ""),
        fact["business_id"],
    )


def claims_for(fact, context):
    source = fact["data"]["source"]
    claims = {
        item["business_id"]: item
        for item in context.related_facts(source["business_id"], "settlement")
        if item["kind"] == "operations.overpayment" and item["data"]["source"] == source
    }
    claims[fact["business_id"]] = fact
    return tuple(sorted(claims.values(), key=claim_order))


def allocate_cash_ranges(entries):
    """Take newest still-free cash positions, without visiting individual fen."""
    free, allocated, previous_paid, claimed = [], {}, 0, 0
    for business_id, paid, value in entries:
        if business_id in allocated or paid < previous_paid:
            reject("追偿池的业务身份重复或真实现金累计倒退。", "invalid_input")
        claimed = sum_amounts((claimed, value))
        if value <= 0 or claimed > paid:
            reject("截至各笔追偿时，累计追偿不能超过已经发生的真实付款。")
        if paid > previous_paid:
            free.append((previous_paid, paid))
        previous_paid, needed, spans = paid, value, []
        while needed:
            start, end = free.pop()
            taken = min(end - start, needed)
            spans.append(AmountRange(end - taken, end))
            if start < end - taken:
                free.append((start, end - taken))
            needed -= taken
        allocated[business_id] = tuple(sorted(spans, key=lambda span: span.before))
    return allocated


def pool_ranges(fact, context):
    source = fact["data"]["source"]
    entries = [
        (
            claim["business_id"],
            context.actual_settled_fen(
                source, ResourceCutoff(claim["period"], claim.get("effective_on"))
            ),
            amount(claim["data"]),
        )
        for claim in claims_for(fact, context)
    ]
    return allocate_cash_ranges(entries)
