"""Current advance consumption conserves sourced VAT without proportional red VAT."""

from copy import deepcopy
from itertools import groupby

from ._common import amount, reject
from ._tax_lifecycle import day_used, taxed_at
from ._tax_ranges import gross, intersect, ranges, return_ranges, wire
from ._tax_returns import selected_parts, sources
from ._vat import split_ranges

KINDS = {"operations.advance_fulfillment", "operations.advance_refund"}


def consumptions(business_id, context, current=None):
    found = {
        item["business_id"]: item
        for item in context.related_facts(business_id, "calculation")
        if item["kind"] in KINDS and item["data"]["advance_id"] == business_id
    }
    if current is not None:
        found[current["business_id"]] = current
    return tuple(sorted(found.values(), key=lambda item: (item["period"], item["business_id"])))


def conserve_vat(baseline, consumed, remaining_gross, remaining_vat):
    if not 0 <= remaining_vat <= remaining_gross or not 0 <= consumed <= remaining_gross:
        reject("真实退款消耗的净额或VAT超过原税源容量。")
    return min(
        min(consumed, remaining_vat), max(baseline, 0, remaining_vat - (remaining_gross - consumed))
    )


def refund_parts(member, original_sources, positions, params, vat):
    if "tax_returns" in member["data"]:
        selected = selected_parts(member, original_sources, positions)
    else:
        selected = []
        for index, source in enumerate(original_sources):
            selected_ranges = intersect(positions, source["ranges"])
            if not selected_ranges:
                continue
            due = taxed_at(source["segment"], member)
            tax = split_ranges(selected_ranges, params, vat)[1] if due else 0
            part = {
                "state": "taxed" if due else "not_due",
                "ranges": wire(selected_ranges),
                "vat_fen": str(tax),
            }
            if due:
                part["original_tax_period"] = source["segment"]["tax_period"]
            selected.append((index, part))
    if any(part["state"] == "not_due" and amount(part, "vat_fen") for _, part in selected):
        reject("未计税预收退款不能冲减尚未确认的VAT。", "invalid_input")
    return selected


def consumption_pool(original, context, current=None):
    source_id = original.source["business_id"]
    members = consumptions(source_id, context, current)
    capacity = amount(original.target["values"], "gross_fen")
    assigned = return_ranges(members, capacity)
    original_sources = sources(original)
    vat = original.basis["input"].get("vat")
    policy = next(
        (
            item
            for item in original.basis["policies"]
            if vat is not None and item["policy_business_id"] == vat["policy_id"]
        ),
        None,
    )
    if policy is None:
        reject("预收消费池缺少原实际采用的VAT政策。")
    params = policy["parameters"]
    refunds = {
        member["business_id"]: refund_parts(
            member, original_sources, ranges(assigned[member["business_id"]], capacity), params, vat
        )
        for member in members
        if member["kind"] == "operations.advance_refund"
    }
    allocations = {}
    monthly_groups = tuple(tuple(group) for _, group in groupby(members, key=lambda x: x["period"]))
    for index, source in enumerate(original_sources):
        if source["segment"]["state"] != "due":
            continue
        remaining_gross, remaining_vat = gross(source["ranges"]), source["tax"]
        for monthly in monthly_groups:
            # A monthly complete target consumes actual red figures together;
            # strictly later economic months cannot rewrite this prefix.
            for member in monthly:
                for owner, part in refunds.get(member["business_id"], ()):
                    if owner != index:
                        continue
                    positions = ranges(part["ranges"], capacity)
                    value = gross(positions)
                    tax = (
                        amount(part, "vat_fen")
                        if part["state"] == "taxed"
                        else split_ranges(positions, params, vat)[1]
                    )
                    remaining_gross -= value
                    remaining_vat -= tax
            conserve_vat(0, 0, remaining_gross, remaining_vat)
            for member in monthly:
                if member["kind"] != "operations.advance_fulfillment":
                    continue
                positions = intersect(
                    ranges(assigned[member["business_id"]], capacity), source["ranges"]
                )
                value = gross(positions)
                tax = conserve_vat(
                    split_ranges(positions, params, vat)[1], value, remaining_gross, remaining_vat
                )
                allocations[member["business_id"], index] = tax
                remaining_gross -= value
                remaining_vat -= tax
    return original_sources, assigned, refunds, allocations


def adopt_refunds(plan, refunds, members, context, source_indices, original_sources):
    period = plan["accounting_period"]
    for member in members:
        selected = [
            part
            for owner, part in refunds.get(member["business_id"], ())
            if owner in source_indices
        ]
        if selected and member["period"] <= period:
            plan["basis"]["adopted_quantities"].append(
                {
                    "type": "tax_return",
                    "business_id": member["business_id"],
                    "period": member["period"],
                    "tax_returns": deepcopy(selected),
                }
            )
            if any(
                day_used(original_sources[index]["segment"], member)
                for index, _ in refunds[member["business_id"]]
                if index in source_indices
            ):
                plan["basis"]["adopted_quantities"][-1]["effective_on"] = member["effective_on"]
            for location in context.resolve_sources(member["evidence_ids"]):
                if location not in plan["basis"]["source_locations"]:
                    plan["basis"]["source_locations"].append(location)
