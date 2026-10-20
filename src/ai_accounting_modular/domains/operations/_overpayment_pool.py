"""Causal actual-settlement ranges for one original obligation's current recovery claims."""

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff

from ._common import amount, issue, reject, sum_amounts
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


def allocate_settlement_ranges(entries):
    """Take newest still-free actual-settlement positions, without visiting individual fen."""
    free, allocated, previous_paid, claimed = [], {}, 0, 0
    for business_id, paid, value in entries:
        if business_id in allocated or paid < previous_paid:
            reject("追偿池的业务身份重复或实际清偿累计倒退。", "invalid_input")
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
    claims = claims_for(fact, context)
    excluded = ((fact["business_id"], "main"),)
    entries, claimed = [], 0
    for claim in claims:
        value = amount(claim["data"])
        if not value:
            continue
        claimed = sum_amounts((claimed, value))
        paid = actual_cleared(
            context,
            source,
            ResourceCutoff(claim["period"], claim.get("effective_on")),
            excluded,
            claimed,
        )
        entries.append((claim["business_id"], paid, value))
    allocated = allocate_settlement_ranges(entries)
    return {claim["business_id"]: allocated.get(claim["business_id"], ()) for claim in claims}


def actual_cleared(context, source, cutoff, excluded, claimed):
    (window,) = context.resource_windows(
        [source],
        "obligation",
        cutoff,
        excluding_consumers=excluded,
        components=("money", "agency"),
    )
    paid = sum_amounts((window.certain.money_settled_fen, window.certain.agency_settled_fen))
    possible = paid + sum(amount(use.value) for use in window.uncertain_uses)
    if paid < claimed <= possible:
        problems = []
        for use in window.uncertain_uses:
            fact = context.get_fact(use.source["business_id"])
            if fact is None:
                reject("追偿采用的代理清偿缺少当前事实。")
            problem = issue(
                fact,
                "/effective_on",
                "同月代理清偿的先后影响追偿是否已经成立，需要真实清偿日。",
                precision=("day",),
            )
            problem["code"] = "resource_day_required"
            problems.append(problem)
        raise ContractError(
            "needs_information", "代理清偿日影响本笔追偿依据。", fact_issues=problems
        )
    return paid


def balance_dependency(fact):
    source = fact["data"]["source"]
    return {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": source["business_id"],
        "producer_public_slot": source["slot"],
        "read_mode": "balance",
        "relation_kind": "settlement",
        "resource_components": ["money", "agency"],
        "period_from": "0001-01",
        "period_to": "9999-12",
        "excluded_result_slots": [{"business_id": fact["business_id"], "result_slot": "main"}],
    }
