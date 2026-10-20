"""Known same-month returns are part of one complete source tax target."""

from copy import deepcopy

from ._advance_pool import consumptions, refund_parts
from ._common import amount, reject
from ._tax_lifecycle import day_used, taxed_at
from ._tax_ranges import gross, ranges, return_ranges, subtract, wire
from ._tax_returns import selected_parts
from ._vat import split_ranges


def adjustments(fact, context, resolved, details, capacity):
    eligible = [
        index
        for index, item in enumerate(details)
        if item["segment"].get("tax_period") == fact["period"] and "tax_on" in item["segment"]
    ]
    if not eligible:
        return {}, []
    advance = fact["kind"] == "operations.advance"
    if advance:
        members = consumptions(fact["business_id"], context)
    else:
        members = tuple(
            sorted(
                (
                    member
                    for member in context.related_facts(fact["business_id"], "calculation")
                    if member["kind"] == "operations.sale_return"
                    and member["data"]["sale_id"] == fact["business_id"]
                ),
                key=lambda item: (item["period"], item["business_id"]),
            )
        )
    candidates = [
        member
        for member in members
        if member["kind"] != "operations.advance_fulfillment"
        and member["period"] == fact["period"]
        and "effective_on" in member
    ]
    if not candidates:
        return {}, []
    models = []
    for segment in resolved["segments"]:
        positions = ranges(segment["ranges"], capacity)
        detail = next((item for item in details if item["segment"] == segment), None)
        models.append(
            {
                "segment": segment,
                "ranges": positions,
                "net": detail["net"] if detail else gross(positions),
                "tax": detail["tax"] if detail else 0,
                "sale": detail["sale"] if detail else None,
            }
        )
    assigned = return_ranges(members, capacity)
    changed, observed = {}, []
    for member in candidates:
        positions = ranges(assigned[member["business_id"]], capacity)
        if advance:
            params, vat = details[0]["policy"]["data"], fact["data"]["vat"]
            selected = refund_parts(member, models, positions, params, vat)
        else:
            selected = selected_parts(
                member,
                models,
                positions,
                expected_tax=amount(member["data"], "credit_note_vat_fen"),
            )
        adopted = []
        for index, part in selected:
            segment = models[index]["segment"]
            if not day_used(segment, member) or taxed_at(segment, member):
                continue
            detail_index = next(
                index for index, item in enumerate(details) if item["segment"] == segment
            )
            used = ranges(part["ranges"], capacity)
            tax = (
                split_ranges(used, details[detail_index]["policy"]["data"], fact["data"]["vat"])[1]
                if advance
                else amount(part, "vat_fen")
            )
            current = changed.setdefault(detail_index, {"ranges": [], "tax": 0})
            current["ranges"].extend(used)
            current["tax"] += tax
            adopted.append(deepcopy(part))
        if adopted:
            observed.append(
                {
                    "type": "tax_return",
                    "business_id": member["business_id"],
                    "period": member["period"],
                    "effective_on": member["effective_on"],
                    "tax_returns": adopted,
                }
            )
    return changed, observed


def apply_current_sales(plan, details, changes):
    for index, item in enumerate(details):
        sale = item["sale"]
        if sale is None or sale["tax_period"] != plan["accounting_period"]:
            continue
        change = changes.get(index, {"ranges": [], "tax": 0})
        remaining = subtract(ranges(sale["ranges"], 2**63 - 1), sorted(change["ranges"]))
        value, tax = gross(remaining), amount(sale, "vat_fen") - change["tax"]
        if not 0 <= tax <= value:
            reject("同月税点前退回与原税源剩余净额、VAT不一致。")
        if value:
            plan["tax_sales"].append(
                {
                    **deepcopy(sale),
                    "ranges": wire(remaining),
                    "net_sales_fen": str(value - tax),
                    "vat_fen": str(tax),
                }
            )


def adopt_adjustments(plan, observed, context):
    plan["basis"]["adopted_quantities"].extend(observed)
    for quantity in observed:
        current = context.get_fact(quantity["business_id"])
        for location in context.resolve_sources(current["evidence_ids"]):
            if location not in plan["basis"]["source_locations"]:
                plan["basis"]["source_locations"].append(location)
