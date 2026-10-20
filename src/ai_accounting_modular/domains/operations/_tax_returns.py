"""Preserve sourced red VAT, original tax months and exact gross coverage."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError

from ._common import adopt_source, amount, issue, reject, sum_amounts
from ._tax_lifecycle import adopted_resolution, planned_sales, taxed_at
from ._tax_ranges import gross, intersect, ranges, wire
from ._vat import split_ranges


def sources(original):
    capacity = amount(original.target["values"], "gross_fen")
    vat = original.basis["input"].get("vat")
    policies = original.basis["policies"]
    sales = planned_sales(original)
    result = []
    for segment in adopted_resolution(original)["segments"]:
        positions = ranges(segment["ranges"], capacity)
        sale = next(
            (
                item
                for item in sales
                if ranges(item["ranges"], capacity) == positions
                and item["tax_period"] == segment.get("tax_period")
            ),
            None,
        )
        if sale is not None:
            net, tax = amount(sale, "net_sales_fen"), amount(sale, "vat_fen")
        elif original.target["kind"] == "operations.service_sale":
            policy = next(
                (
                    item
                    for item in policies
                    if vat is not None and item["policy_business_id"] == vat["policy_id"]
                ),
                None,
            )
            if policy is None:
                reject("原待转税源缺少实际采用的VAT政策。")
            net, tax = split_ranges(positions, policy["parameters"], vat)
        else:
            net, tax = gross(positions), 0
        result.append(
            {
                "segment": deepcopy(segment),
                "ranges": positions,
                "net": net,
                "tax": tax,
                "sale": sale,
            }
        )
    return result


def _state(source, fact):
    return "taxed" if taxed_at(source["segment"], fact) else "not_due"


def _part(source, fact, positions, tax):
    result = {"state": _state(source, fact), "ranges": wire(positions), "vat_fen": str(tax)}
    if result["state"] == "taxed":
        result["original_tax_period"] = source["segment"]["tax_period"]
    return result


def selected_parts(fact, original_sources, positions, *, expected_tax=None):
    """Multiple partial sources require actual components, never a proportional guess."""
    supplied = fact["data"].get("tax_returns")
    found = []
    if supplied is not None:
        for part in supplied:
            current = ranges(part["ranges"], max(end for _, end in positions))
            if amount(part, "vat_fen") > gross(current):
                reject("逐源红字VAT不能超过该项真实退回毛额。", "invalid_input")
            if intersect(current, positions) != current:
                reject("逐税源红字范围必须属于本次真实退回范围。", "invalid_input")
            matches = [
                index
                for index, source in enumerate(original_sources)
                if intersect(current, source["ranges"]) == current
            ]
            if len(matches) != 1:
                reject("每项红字税源范围必须明确归属于唯一原税源。", "invalid_input")
            index = matches[0]
            expected = _part(original_sources[index], fact, current, amount(part, "vat_fen"))
            if part != expected:
                reject("红字税期或未到税点状态与原正式范围不一致。", "invalid_input")
            found.append((index, deepcopy(part)))
    else:
        incomplete, known = [], 0
        for index, source in enumerate(original_sources):
            selected = intersect(positions, source["ranges"])
            if not selected:
                continue
            if selected == source["ranges"]:
                value = source["tax"]
                known = sum_amounts((known, value))
                found.append((index, _part(source, fact, selected, value)))
            else:
                incomplete.append((index, selected))
        if len(incomplete) > 1:
            raise ContractError(
                "needs_information",
                "多税源部分退回需要明确各自红字税额。",
                fact_issues=[
                    issue(
                        fact,
                        "/data/tax_returns",
                        "请按原税源范围分别提供真实红字VAT。",
                        precision=("fen", "source"),
                    )
                ],
            )
        if incomplete:
            index, selected = incomplete[0]
            value = expected_tax - known if expected_tax is not None else 0
            if value < 0:
                reject("本次红字VAT不足以覆盖完整退回的原税源。", "invalid_input")
            found.append((index, _part(original_sources[index], fact, selected, value)))
    if gross([pair for _, part in found for pair in ranges(part["ranges"], 2**63 - 1)]) != gross(
        positions
    ):
        reject("逐税源红字范围必须完整覆盖本次退回毛额。", "invalid_input")
    total_tax = sum_amounts(amount(part, "vat_fen") for _, part in found)
    if expected_tax is not None and total_tax != expected_tax:
        reject("逐税源VAT合计与本次明确红字VAT不一致。", "invalid_input")
    given_month = fact["data"].get("original_tax_period")
    if given_month is not None and any(
        part.get("original_tax_period") != given_month for _, part in found
    ):
        reject("单一原税月与逐源红字税期不一致。", "invalid_input")
    return sorted(found, key=lambda row: (amount(row[1]["ranges"][0], "from_fen"), row[0]))


def validate_capacities(original_sources, assignments):
    for index, source in enumerate(original_sources):
        selected = [part for found in assignments for owner, part in found if owner == index]
        used = sum_amounts(gross(ranges(part["ranges"], 2**63 - 1)) for part in selected)
        vat = sum_amounts(amount(part, "vat_fen") for part in selected)
        capacity = gross(source["ranges"])
        if vat > source["tax"] or used - vat > source["net"]:
            reject("累计红字VAT和净收入均不能超过其各自原正式税源。")
        if used == capacity and vat != source["tax"]:
            reject("完整退回某原税源必须包含该税源全部采用VAT。")


def adopt_return_sources(plan, original, original_sources, selected):
    adopt_source(plan, original)
    plan["basis"]["adopted_quantities"].append(
        {"type": "tax_point", "resolution": adopted_resolution(original)}
    )
    for index in sorted({index for index, _ in selected}):
        sale = original_sources[index]["sale"]
        if sale is not None:
            plan["basis"]["adopted_quantities"].append({"type": "tax_sale", "sale": deepcopy(sale)})
    plan["basis"]["policies"] = deepcopy(original.basis["policies"])
    plan["rule_refs"][0]["policy_business_ids"] = [
        item["policy_business_id"] for item in original.basis["policies"]
    ]
    plan["rule_refs"][0]["official_urls"] = sorted(
        {item["parameters"]["policy"]["official_url"] for item in original.basis["policies"]}
    )


def emit_negative_sales(plan, original_sources, selected):
    for index, part in selected:
        if part["state"] != "taxed":
            continue
        source = original_sources[index]["sale"]
        if source is None:
            reject("已计税退回缺少原正式税销售来源。")
        value = gross(ranges(part["ranges"], 2**63 - 1))
        vat = amount(part, "vat_fen")
        plan["tax_sales"].append(
            {
                **deepcopy(source),
                "source_business_id": plan["business_id"],
                "tax_period": plan["accounting_period"],
                "net_sales_fen": str(-(value - vat)),
                "vat_fen": str(-vat),
                "ranges": deepcopy(part["ranges"]),
            }
        )
