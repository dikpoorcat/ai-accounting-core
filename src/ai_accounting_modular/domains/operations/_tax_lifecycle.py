"""One sourced resolution feeds revenue, advances and later monthly VAT release."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.domains.taxes.public import tax_point_periods

from ._common import adopt_source, issue, reject, sum_amounts
from ._tax_ranges import gross, intersect, original_ranges, ranges, validate_resolution, wire
from ._vat import (
    _adopt_policy,
    _check_tax_day,
    _policy,
    _policy_reference,
    covers_month,
    split_ranges,
    tax_sale_values,
)


def taxed_at(segment, fact):
    if segment["state"] != "due":
        return False
    if segment["tax_period"] != fact["period"]:
        return segment["tax_period"] < fact["period"]
    if "tax_on" in segment and "effective_on" in fact:
        return segment["tax_on"] <= fact["effective_on"]
    # Month precision is sufficient for a same-period positive/negative tax
    # contribution. It does not assert a fictitious order within that month.
    return True


def day_used(segment, fact):
    return (
        segment.get("tax_period") == fact["period"]
        and "tax_on" in segment
        and "effective_on" in fact
    )


def resolution(fact, context, point, capacity):
    source = None
    if point["type"] == "owner_recognition_default":
        segment = {"state": "not_due", "ranges": wire([(0, capacity)])}
        if fact["kind"] != "operations.advance":
            segment.update(
                state="due",
                tax_period=fact["period"],
                trigger="owner_recognition_default",
                event_ids=[],
            )
        value = {
            "target_business_id": fact["business_id"],
            "basis": "owner_recognition_default",
            "segments": [segment] if capacity else [],
        }
    elif point["type"] == "explicit":
        _check_tax_day(fact, point)
        segment = {
            "state": "due",
            "ranges": wire([(0, capacity)]),
            "tax_period": point["tax_period"],
            "trigger": "explicit",
            "event_ids": [],
        }
        if "tax_on" in point:
            segment["tax_on"] = point["tax_on"]
        value = {
            "target_business_id": fact["business_id"],
            "segments": [segment] if capacity else [],
        }
    else:
        source = context.get_result(point["tax_point_id"], "main")
        if source is None:
            problem = issue(
                fact,
                "/data/tax_point/tax_point_id",
                "所引用的详细税点业务尚无正式推导结果，请检查上游核算。",
            )
            problem["code"] = "result_not_ready"
            raise ContractError(
                "dependency_conflict",
                "详细税点需要对应的正式推导结果。",
                fact_issues=[problem],
            )
        if source.target["kind"] != "taxes.tax_point":
            reject("详细税点必须引用税点推导业务。", "invalid_input")
        value = deepcopy(source.target["values"])
    validate_contract("business-facts.schema.json", "TaxPointResolution", value)
    validate_resolution(value, fact["business_id"], capacity)
    return value, source


def tax_details(fact, context, vat, point, capacity, *, include_not_due=True):
    resolved, source = resolution(fact, context, point, capacity)
    details = measure_resolution(
        fact, context, vat, resolved, capacity, include_not_due=include_not_due
    )
    return resolved, source, details


def measure_resolution(
    fact,
    context,
    vat,
    resolved,
    capacity,
    *,
    include_not_due=True,
    rounding_offset=0,
    responsibility=None,
    coordinates=None,
):
    """Measure only the new responsibility of an already validated full resolution."""
    point = fact["data"]["tax_point"]
    details = []
    for segment in resolved["segments"]:
        positions = ranges(segment["ranges"], capacity)
        if responsibility is not None:
            positions = intersect(positions, responsibility)
        if not positions:
            continue
        if segment["state"] == "not_due" and not include_not_due:
            continue
        if segment["state"] == "due":
            if segment["tax_period"] < fact["period"]:
                problem = issue(
                    fact,
                    "/data/tax_point",
                    "已明确的税点早于本次业务所属月，本入口不能替代此前税源的接续。"
                    "请先核查已登记的客户预收及其履约，或接账税源；"
                    "只有确有该业务时才沿对应入口接续，不能自动创建预收或改动已知月份。",
                    precision=("source",),
                )
                problem["code"] = "tax_source_required"
                raise ContractError(
                    "dependency_conflict",
                    "更早税点缺少对应的正式税源接续。",
                    fact_issues=[problem],
                )
            selected = {"type": "explicit", "tax_period": segment["tax_period"]}
            if "tax_on" in segment:
                selected["tax_on"] = segment["tax_on"]
        else:
            selected = {"type": "explicit", "tax_period": fact["period"]}
        valuation_day_used = False
        uses_business_day = segment["state"] == "not_due" or (
            segment.get("trigger") == "owner_recognition_default"
        )
        if uses_business_day:
            reference = _policy_reference(fact, context, vat)
            if not covers_month(reference["data"], fact["period"]) and "effective_on" in fact:
                if fact["effective_on"][:7] != fact["period"]:
                    problem = issue(
                        fact, "/effective_on", "用于选择收入计量政策的真实履约日必须属于核算月。"
                    )
                    problem["code"] = "inconsistent_fact"
                    raise ContractError(
                        "invalid_input", "真实履约日与政策核算月份不一致。", fact_issues=[problem]
                    )
                selected["tax_on"] = fact["effective_on"]
                valuation_day_used = True
        day_fact, day_field = fact, "/data/tax_point/tax_on"
        if uses_business_day:
            day_field = "/effective_on"
        elif point["type"] == "derived":
            day_fact = context.get_fact(point["tax_point_id"])
            day_field = next(
                (
                    f"/data/events/{index}/on"
                    for index, event in enumerate(
                        (day_fact or {}).get("data", {}).get("events", ())
                    )
                    if event["event_id"] in segment["event_ids"] and "on" not in event
                ),
                "/data/events",
            )
        policy = _policy(
            fact,
            context,
            vat,
            selected,
            day_field=day_field,
            day_fact=day_fact,
        )
        net, tax = split_ranges(
            original_ranges(positions, coordinates)
            if coordinates is not None
            else [(start + rounding_offset, end + rounding_offset) for start, end in positions],
            policy["data"],
            vat,
        )
        sale = None
        if segment["state"] == "due":
            sale = tax_sale_values(
                fact["business_id"],
                segment["tax_period"],
                policy["data"],
                vat,
                net,
                tax,
                gross(positions),
            )
            sale["ranges"] = wire(positions)
        details.append(
            {
                "segment": segment,
                "policy": policy,
                "net": net,
                "tax": tax,
                "sale": sale,
                "valuation_day_used": valuation_day_used,
            }
        )
    return details


def adopt_details(plan, context, resolved, source, details):
    plan["basis"]["adopted_quantities"].append(
        {"type": "tax_point", "resolution": deepcopy(resolved)}
    )
    if source is not None:
        adopt_source(plan, source)
    adopted = set()
    for item in details:
        policy = item["policy"]
        if policy["business_id"] not in adopted:
            _adopt_policy(plan, policy, context)
            adopted.add(policy["business_id"])
        if item["sale"] is not None:
            plan["basis"]["adopted_quantities"].append(
                {"type": "tax_sale", "sale": deepcopy(item["sale"])}
            )
        if item["valuation_day_used"]:
            current = context.get_fact(plan["business_id"])
            plan["basis"]["effective_on"] = current["effective_on"]


def totals(details, period):
    net = sum_amounts(item["net"] for item in details)
    tax = sum_amounts(item["tax"] for item in details)
    current = [
        item
        for item in details
        if item["sale"] is not None and item["sale"]["tax_period"] == period
    ]
    current_net = sum_amounts(item["net"] for item in current)
    current_tax = sum_amounts(item["tax"] for item in current)
    return net, tax, current_net, current_tax


def planned_sales(original):
    return [
        deepcopy(item["sale"])
        for item in original.basis["adopted_quantities"]
        if item["type"] == "tax_sale"
    ]


def adopted_resolution(original):
    found = [
        item["resolution"]
        for item in original.basis["adopted_quantities"]
        if item["type"] == "tax_point"
    ]
    own = [item for item in found if item["target_business_id"] == original.source["business_id"]]
    found = own or found
    if len(found) != 1:
        reject("原经营结果缺少唯一完整采用的税点范围。")
    return deepcopy(found[0])


def future_months(fact, context):
    point = fact["data"].get("tax_point")
    if not point:
        return []
    if point["type"] == "owner_recognition_default":
        return []
    if point["type"] == "explicit":
        months = [point["tax_period"]]
    else:
        source = context.get_fact(point["tax_point_id"])
        months = tax_point_periods(source, context) if source is not None else ()
    return sorted({month for month in months if month > fact["period"]})


def result_slots(fact, context):
    limit = context.evaluation_period_to
    return ["main", *("tax:" + month for month in future_months(fact, context) if month <= limit)]
