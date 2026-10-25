"""The owner's receipt-month convention reads indexed real-money allocations."""

from ai_accounting_modular.contracts.public import fact_source_ids

from ._common import amount, plan_for, reject, total


def receipts(target_business_id, context):
    found = []
    for fact in context.related_facts(target_business_id, "settlement"):
        data = fact["data"]
        if (
            fact["kind"] != "money.movement"
            or data["type"] != "settlement"
            or data["direction"] != "inflow"
        ):
            continue
        allocations = [
            item
            for item in data["allocations"]
            if item["source"]["business_id"] == target_business_id
        ]
        if not allocations:
            continue
        if any(item["source"]["slot"] not in {"primary", "vat"} for item in allocations):
            reject("收款月默认口径只能采用原业务服务应收的真实清偿分配。", "invalid_input")
        if fact["effective_on"][:7] != fact["period"]:
            reject("实际收款日期必须属于真实收款月份。", "invalid_input")
        value = total(amount(item["amount_fen"]) for item in allocations)
        if value:
            found.append((fact, value))
    return tuple(sorted(found, key=lambda item: (item[0]["effective_on"], item[0]["business_id"])))


def receipt_point(fact, context, target, gross):
    cursor, segments = 0, []
    actuals = receipts(target["business_id"], context)
    for receipt, value in actuals:
        end = total((cursor, value))
        if end > gross:
            reject("分配到原业务的累计实际收款超过该业务含税总额。", "invalid_input")
        segments.append(
            {
                "state": "due",
                "ranges": [{"from_fen": str(cursor), "to_fen": str(end)}],
                "tax_period": receipt["period"],
                "tax_on": receipt["effective_on"],
                "trigger": "owner_receipt_default",
                "event_ids": [],
                "receipt_ids": [receipt["business_id"]],
            }
        )
        cursor = end
    if cursor < gross:
        segments.append(
            {"state": "not_due", "ranges": [{"from_fen": str(cursor), "to_fen": str(gross)}]}
        )
    plan = plan_for(
        fact,
        context,
        {
            "target_business_id": target["business_id"],
            "basis": "owner_receipt_default",
            "segments": segments,
        },
    )
    plan["rule_refs"][0].update(key="owner-receipt-vat-period", revision=1)
    source_ids = list(
        dict.fromkeys(source for receipt, _ in actuals for source in fact_source_ids(receipt))
    )
    locations = plan["basis"]["source_locations"]
    for offset in range(0, len(source_ids), 500):
        for location in context.resolve_sources(source_ids[offset : offset + 500]):
            if location not in locations:
                locations.append(location)
    return plan
