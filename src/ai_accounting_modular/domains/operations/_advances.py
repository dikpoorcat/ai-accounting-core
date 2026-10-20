"""Customer advances consume one gross capacity and adopt tax only once."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from ai_accounting_modular.contracts.public import ContractError

from ._advance_pool import adopt_refunds, consumption_pool, consumptions
from ._common import (
    adopt_source,
    amount,
    base_plan,
    issue,
    line,
    obligation,
    referenced_result,
    reject,
    sum_amounts,
)
from ._cumulative import AmountRange, adopt_position, position
from ._relationships import _adopt_obligation, _resource, _transfer
from ._tax_lifecycle import (
    adopt_details,
    adopted_resolution,
    day_used,
    measure_resolution,
    planned_sales,
    resolution,
    totals,
)
from ._tax_ranges import gross as range_gross
from ._tax_ranges import (
    intersect,
    local_ranges,
    ranges,
    return_ranges,
    validate_resolution,
    wire,
)
from ._tax_returns import adopt_return_sources, emit_negative_sales, sources
from ._vat import split_ranges


def _original(fact, context):
    original = referenced_result(
        fact, context, {"business_id": fact["data"]["advance_id"], "slot": "main"}
    )
    if (
        original.target["kind"] != "operations.advance"
        or original.basis["input"]["side"] != "customer"
    ):
        reject("履约和预收退款必须引用客户预收业务。")
    definition = _resource(context, {"business_id": fact["data"]["advance_id"], "slot": "advance"})
    return original, definition, consumptions(fact["data"]["advance_id"], context, fact)


def _adopt_original(plan, original, definition):
    adopt_source(plan, original)
    _adopt_obligation(plan, definition)


def _consume(plan, fact, definition):
    value = {
        "settlement_id": str(
            uuid5(NAMESPACE_URL, "operations:" + fact["business_id"] + ":advance")
        ),
        "source": deepcopy(definition.reference),
        "consumer": {"business_id": fact["business_id"], "slot": "main"},
        "period": fact["period"],
        "amount_fen": fact["data"]["gross_fen"],
        "method": "offset",
    }
    plan["settlements"].append(value)


def _parts(original, positions):
    source = original.basis["input"]
    policy = next(
        (
            item
            for item in original.basis["policies"]
            if item["policy_business_id"] == source.get("vat", {}).get("policy_id")
        ),
        None,
    )
    if policy is None:
        reject("原预收缺少本次采用的完整VAT资料。")
    result = []
    for item in sources(original):
        selected = intersect(positions, item["ranges"])
        if selected:
            net, tax = split_ranges(selected, policy["parameters"], source["vat"])
            result.append((item, selected, net, tax))
    return result


def _check_inherited_vat(fact, original):
    if "vat" in fact["data"] and fact["data"]["vat"] != original.basis["input"].get("vat"):
        reject("已采用预收税务资料不能另换税率。", "invalid_input")


def _inherit_resolution(fact, original, positions, unresolved, resolved):
    """New tax responsibility cannot replace the adopted tax on inherited gross."""
    capacity = amount(original.target["values"], "gross_fen")
    gross = amount(fact["data"], "gross_fen")
    strict = fact["data"]["tax_point"]["type"] == "derived" or not unresolved
    inherited = []
    for segment in adopted_resolution(original)["segments"]:
        if segment["state"] != "due":
            continue
        used = local_ranges(intersect(ranges(segment["ranges"], capacity), positions), positions)
        if not used:
            continue
        if strict:
            for stated in resolved["segments"]:
                if not intersect(used, ranges(stated["ranges"], gross)):
                    continue
                if (
                    stated["state"] != "due"
                    or stated["tax_period"] != segment["tax_period"]
                    or (
                        "tax_on" in stated
                        and "tax_on" in segment
                        and stated["tax_on"] != segment["tax_on"]
                    )
                ):
                    reject("履约重述的原税源月份或已知税点日与原采用依据不一致。", "invalid_input")
        inherited.append({**deepcopy(segment), "ranges": wire(used)})
    for segment in resolved["segments"]:
        used = intersect(ranges(segment["ranges"], gross), unresolved)
        if used:
            inherited.append({**deepcopy(segment), "ranges": wire(used)})
    value = {
        "target_business_id": fact["business_id"],
        "segments": sorted(inherited, key=lambda item: amount(item["ranges"][0], "from_fen")),
    }
    validate_resolution(value, fact["business_id"], gross)
    return value


def _untaxed_terms(fact, original_fact):
    data = fact["data"]
    vat = data.get("vat", original_fact["data"].get("vat"))
    point = data.get("tax_point")
    missing = []
    if vat is None:
        missing.append(issue(fact, "data/vat", "未计税预收履约需明确适用VAT资料。"))
    if point is None:
        missing.append(issue(fact, "data/tax_point", "未计税预收履约需明确本次真实税点。"))
    if missing:
        raise ContractError(
            "needs_information", "预收履约缺少会影响核算的税务事实。", fact_issues=missing
        )
    return vat, point


def _positions(fact, members, capacity):
    return ranges(return_ranges(members, capacity)[fact["business_id"]], capacity)


def _save_positions(plan, reference, positions):
    for start, end in positions:
        adopt_position(plan, reference, AmountRange(start, end))


def _new_tax_details(fact, context, original, positions, unresolved, *, rounding_offset=0):
    original_fact = context.get_fact(original.source["business_id"])
    vat, point = _untaxed_terms(fact, original_fact)
    gross = amount(fact["data"], "gross_fen")
    resolved, tax_source = resolution(fact, context, point, gross)
    responsibility = local_ranges(unresolved, positions) if unresolved is not None else None
    if responsibility is not None:
        resolved = _inherit_resolution(fact, original, positions, responsibility, resolved)
    details = measure_resolution(
        fact,
        context,
        vat,
        resolved,
        gross,
        rounding_offset=rounding_offset,
        responsibility=responsibility,
        coordinates=positions if unresolved is not None else None,
    )
    return vat, resolved, tax_source, details


def advance_fulfillment(fact, context):
    original, definition, members = _original(fact, context)
    original_fact = context.get_fact(fact["data"]["advance_id"])
    gross = amount(fact["data"], "gross_fen")
    positions = _positions(fact, members, amount(original.target["values"], "gross_fen"))
    inherited = bool(planned_sales(original))
    details, resolved, tax_source, vat, tax_span = [], None, None, None, None
    known, old_due, old_deferred, net, tax = [], 0, 0, 0, 0
    if inherited:
        pool_sources, _, pool_refunds, allocations = consumption_pool(original, context, fact)
        parts = _parts(original, positions)
        parts = [
            (
                item,
                selected,
                range_gross(selected) - allocations[fact["business_id"], pool_sources.index(item)],
                allocations[fact["business_id"], pool_sources.index(item)],
            )
            if item["segment"]["state"] == "due"
            else (item, selected, net, tax)
            for item, selected, net, tax in parts
        ]
        known = [row for row in parts if row[0]["segment"]["state"] == "due"]
        unresolved = [
            pair for row in parts if row[0]["segment"]["state"] == "not_due" for pair in row[1]
        ]
        net, tax = sum_amounts(row[2] for row in known), sum_amounts(row[3] for row in known)
        old_due = sum_amounts(
            row[3] for row in known if row[0]["segment"]["tax_period"] <= fact["period"]
        )
        old_deferred = tax - old_due
        if unresolved:
            vat, resolved, tax_source, details = _new_tax_details(
                fact, context, original, positions, sorted(unresolved)
            )
        else:
            _check_inherited_vat(fact, original)
            if "tax_point" in fact["data"]:
                resolved, tax_source = resolution(fact, context, fact["data"]["tax_point"], gross)
                resolved = _inherit_resolution(fact, original, positions, [], resolved)
    else:
        vat, _ = _untaxed_terms(fact, original_fact)
        same_basis = [
            member
            for member in members
            if member["kind"] == fact["kind"]
            and member["data"].get("vat", original_fact["data"].get("vat")) == vat
        ]
        tax_span = position(
            fact, same_basis, "gross_fen", amount(original.target["values"], "gross_fen")
        )
        vat, resolved, tax_source, details = _new_tax_details(
            fact, context, original, positions, None, rounding_offset=tax_span.before
        )
    new_net, new_tax, _, new_current = totals(details, fact["period"])
    net, tax = sum_amounts((net, new_net)), sum_amounts((tax, new_tax))
    deferred = sum_amounts((old_deferred, new_tax - new_current))
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": fact["data"]["gross_fen"],
            "revenue_fen": str(net),
            "vat_fen": str(tax),
            "advance_consumed_fen": fact["data"]["gross_fen"],
        },
    )
    _adopt_original(plan, original, definition)
    _save_positions(plan, definition.reference, positions)
    if inherited:
        adopt_refunds(
            plan,
            pool_refunds,
            members,
            context,
            {pool_sources.index(row[0]) for row in known},
            pool_sources,
        )
        original_sources = sources(original)
        adopted = [
            (index, {})
            for index, source in enumerate(original_sources)
            if intersect(positions, source["ranges"])
        ]
        adopt_return_sources(plan, original, original_sources, adopted)
        for month in sorted(
            {
                row[0]["segment"]["tax_period"]
                for row in known
                if original.basis["period"] < row[0]["segment"]["tax_period"] <= fact["period"]
            }
        ):
            release = context.get_result(fact["data"]["advance_id"], "tax:" + month)
            if release is None:
                reject("已计税履约需要原预收对应税月的正式结果。")
            adopt_source(plan, release)
    if resolved is not None:
        if vat is not None:
            plan["basis"]["input"]["vat"] = deepcopy(vat)
        adopt_details(plan, context, resolved, tax_source, details)
        if tax_span is not None:
            adopt_position(
                plan, {"business_id": fact["data"]["advance_id"], "slot": "main"}, tax_span
            )
        plan["tax_sales"].extend(
            item["sale"]
            for item in details
            if item["sale"] is not None and item["sale"]["tax_period"] == fact["period"]
        )
    line(
        plan,
        "2203",
        debit=gross - old_due,
        party=definition.value["party_id"],
        summary="释放已履约预收",
    )
    line(plan, "5001", credit=net, summary="确认预收履约收入")
    line(plan, "222101", credit=new_current, summary="确认本次履约新到税点VAT")
    line(plan, "222104", credit=deferred, summary="确认本次履约待转VAT")
    _consume(plan, fact, definition)
    return plan


def advance_refund(fact, context):
    original, definition, members = _original(fact, context)
    gross, party = amount(fact["data"], "gross_fen"), definition.value["party_id"]
    capacity = amount(original.target["values"], "gross_fen")
    positions = _positions(fact, members, capacity)
    resolved = [
        item["resolution"]
        for item in original.basis["adopted_quantities"]
        if item["type"] == "tax_point"
    ]
    parts, tax, selected = [], 0, None
    if planned_sales(original):
        original_sources, _, refunds, _ = consumption_pool(original, context, fact)
        selected = refunds[fact["business_id"]]
        parts = [part for _, part in selected]
        tax = sum_amounts(amount(part, "vat_fen") for part in parts)
    else:
        parts = [{"state": "not_due", "ranges": wire(positions), "vat_fen": "0"}]
        if "tax_returns" in fact["data"]:
            parts = deepcopy(fact["data"]["tax_returns"])
            if any(part["state"] != "not_due" or amount(part, "vat_fen") for part in parts):
                reject("未计税预收退款只能退还原毛额，不能虚构已税冲回。", "invalid_input")
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": fact["data"]["gross_fen"],
            "net_reversed_fen": str(gross - tax),
            "vat_reversed_fen": str(tax),
        },
        adopt_effective_on=any(
            day_used(segment, fact) and intersect(positions, ranges(segment["ranges"], capacity))
            for value in resolved
            for segment in value["segments"]
        ),
    )
    plan["basis"]["input"]["tax_returns"] = deepcopy(parts)
    _adopt_original(plan, original, definition)
    _save_positions(plan, definition.reference, positions)
    if planned_sales(original):
        adopt_return_sources(plan, original, original_sources, selected)
        emit_negative_sales(plan, original_sources, selected)
        for month in sorted(
            {
                part["original_tax_period"]
                for part in parts
                if part["state"] == "taxed"
                and original.basis["period"] < part["original_tax_period"] < fact["period"]
            }
        ):
            release = context.get_result(fact["data"]["advance_id"], "tax:" + month)
            if release is None:
                reject("预收退款需要原预收对应已计税月份的正式结果。")
            adopt_source(plan, release)
    line(plan, "2203", debit=gross - tax, party=party, summary="转出原未履约预收")
    line(plan, "222101", debit=tax, summary="冲减退款对应已采用VAT")
    line(plan, "2203", credit=gross, party=party, summary="确认预收退款责任")
    obligation(plan, "refund", party, "payable", gross, "2203", cashflow="customer_receipts")
    _transfer(
        plan, definition.reference, {"business_id": fact["business_id"], "slot": "refund"}, gross
    )
    return plan
