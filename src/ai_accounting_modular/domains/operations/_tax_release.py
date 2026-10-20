"""A later complete monthly slot transfers VAT without recognizing income twice."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError

from ._advance_pool import consumption_pool
from ._common import adopt_source, amount, base_plan, issue, line, reject, sum_amounts
from ._tax_lifecycle import adopted_resolution, planned_sales
from ._tax_ranges import gross, intersect, ranges, subtract, wire
from ._vat import split_ranges


def _before_tax(member, segment):
    month = segment["tax_period"]
    return member["period"] < month or (
        member["period"] == month
        and "tax_on" in segment
        and "effective_on" in member
        and member["effective_on"] < segment["tax_on"]
    )


def _unpriced_remainder(plan, resolved, prior, capacity):
    """Only a proved pre-tax refund removes an unpriced future responsibility."""
    for segment in resolved["segments"]:
        if segment.get("tax_period") != plan["accounting_period"]:
            continue
        positions = ranges(segment["ranges"], capacity)
        removed = []
        for member, result in prior:
            if member["kind"] != "operations.advance_refund" or not _before_tax(member, segment):
                continue
            used = intersect(
                positions,
                sorted(
                    pair
                    for part in result.basis["input"]["tax_returns"]
                    for pair in ranges(part["ranges"], capacity)
                ),
            )
            if used:
                adopt_source(plan, result)
                removed.extend(used)
        if subtract(positions, sorted(removed)):
            return True
    return False


def releases(fact, month, context):
    main = context.get_result(fact["business_id"], "main")
    if main is None:
        reject("税额释放需要原经营主结果的完整采用依据。")
    resolved = adopted_resolution(main)
    selected = [
        sale
        for sale in planned_sales(main)
        if sale["tax_period"] == month and sale["source_business_id"] == fact["business_id"]
    ]
    plan = base_plan(fact, context, {})
    slot = "tax:" + month
    plan.update(result_slot=slot, accounting_period=month, calculation_key="operations.tax_release")
    plan["target"] = {
        "kind": "operations.tax_release",
        "result_slot": slot,
        "values": {"gross_fen": "0", "net_sales_fen": "0", "vat_fen": "0"},
    }
    plan["basis"] = {
        "kind": "operations.tax_release",
        "result_slot": slot,
        "period": month,
        "input": {"tax_period": month, "ranges": []},
        "source_results": [],
        "adopted_quantities": [{"type": "tax_point", "resolution": resolved}],
        "policies": deepcopy(main.basis["policies"]),
        "source_locations": deepcopy(main.basis["source_locations"]),
    }
    adopt_source(plan, main)
    plan["rule_refs"][0].update(
        key="operations.tax_release",
        policy_business_ids=[item["policy_business_id"] for item in main.basis["policies"]],
        official_urls=sorted(
            {item["parameters"]["policy"]["official_url"] for item in main.basis["policies"]}
        ),
    )
    capacity = amount(main.target["values"], "gross_fen")
    customer_advance = fact["kind"] == "operations.advance"
    kinds = (
        {"operations.advance_refund", "operations.advance_fulfillment"}
        if customer_advance
        else {"operations.sale_return"}
    )
    source_field = "advance_id" if customer_advance else "sale_id"
    prior = [
        member
        for member in context.related_facts(fact["business_id"], "calculation")
        if member["kind"] in kinds
        and member["data"][source_field] == fact["business_id"]
        and (
            member["period"] < month
            or (
                member["period"] == month
                and member["kind"] in {"operations.sale_return", "operations.advance_refund"}
            )
        )
    ]
    prior_results = []
    for member in prior:
        result = context.get_result(member["business_id"], "main")
        if result is None:
            reject("税额释放需要其此前真实退回或履约的完整正式结果。")
        prior_results.append((member, result))
    if (
        not selected
        and not main.basis["policies"]
        and _unpriced_remainder(plan, resolved, prior_results, capacity)
    ):
        raise ContractError(
            "needs_information",
            "已到税点需要明确适用VAT资料。",
            fact_issues=[issue(fact, "/data/vat", "本月存续税源的VAT资料尚未建立。")],
        )
    pool_sources, allocations = [], {}
    if customer_advance and selected:
        pool_sources, _, _, allocations = consumption_pool(main, context)
    deferred, advance_tax, values = 0, 0, []
    for sale in selected:
        original_ranges = ranges(sale["ranges"], capacity)
        segment = next(
            segment
            for segment in resolved["segments"]
            if segment.get("tax_period") == month
            and intersect(original_ranges, ranges(segment["ranges"], capacity)) == original_ranges
        )
        removed, removed_tax, fulfilled_tax = [], 0, 0
        for member, result in prior_results:
            if not _before_tax(member, segment):
                continue
            if member["kind"] == "operations.advance_fulfillment":
                spans = [
                    (amount(item, "from_fen"), amount(item, "to_fen"))
                    for item in result.basis["adopted_quantities"]
                    if item["type"] == "cumulative_range"
                    and item["source"] == {"business_id": fact["business_id"], "slot": "advance"}
                ]
                used = intersect(sorted(spans), original_ranges)
                if used:
                    adopt_source(plan, result)
                    index = next(
                        index for index, source in enumerate(pool_sources) if source["sale"] == sale
                    )
                    fulfilled_tax += allocations[member["business_id"], index]
                continue
            for part in result.basis["input"].get("tax_returns", ()):
                used = intersect(ranges(part["ranges"], capacity), original_ranges)
                if not used:
                    continue
                adopt_source(plan, result)
                if used != ranges(part["ranges"], capacity):
                    reject("一项退回税源不能跨越不同原计税来源。")
                removed.extend(used)
                if customer_advance:
                    policy = next(
                        item
                        for item in main.basis["policies"]
                        if item["policy_business_id"] == main.basis["input"]["vat"]["policy_id"]
                    )
                    removed_tax += split_ranges(
                        used, policy["parameters"], main.basis["input"]["vat"]
                    )[1]
                else:
                    removed_tax += amount(part, "vat_fen")
        remaining = subtract(original_ranges, sorted(removed))
        value, tax = gross(remaining), amount(sale, "vat_fen") - removed_tax
        if tax < 0 or tax > value or fulfilled_tax > tax:
            reject("税点前真实退回或履约与原采用VAT容量不一致。")
        plan["basis"]["adopted_quantities"].append({"type": "tax_sale", "sale": deepcopy(sale)})
        if value:
            plan["tax_sales"].append(
                {
                    **sale,
                    "net_sales_fen": str(value - tax),
                    "vat_fen": str(tax),
                    "ranges": wire(remaining),
                }
            )
            values.extend(remaining)
        if customer_advance:
            deferred += fulfilled_tax
            advance_tax += tax - fulfilled_tax
        else:
            deferred += tax
    net = sum_amounts(amount(sale, "net_sales_fen") for sale in plan["tax_sales"])
    tax = sum_amounts(amount(sale, "vat_fen") for sale in plan["tax_sales"])
    plan["target"]["values"] = {
        "gross_fen": str(net + tax),
        "net_sales_fen": str(net),
        "vat_fen": str(tax),
    }
    plan["basis"]["input"]["ranges"] = wire(sorted(values))
    line(plan, "222104", debit=deferred, summary="释放已确认收入的待转VAT")
    line(
        plan,
        "2203",
        debit=advance_tax,
        party=fact["data"].get("counterparty_id"),
        summary="释放预收已到税点VAT",
    )
    line(plan, "222101", credit=tax, summary="确认本月已到税点VAT")
    return plan
