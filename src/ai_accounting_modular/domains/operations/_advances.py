"""Customer advances consume one gross capacity and adopt tax only once."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from ai_accounting_modular.contracts.public import ContractError, format_fen

from ._common import (
    adopt_source,
    amount,
    base_plan,
    issue,
    line,
    obligation,
    referenced_result,
    reject,
)
from ._cumulative import adopt_position, position, related
from ._relationships import _adopt_obligation, _resource, _transfer
from ._returns import adopt_tax, negative_tax
from ._sales import _adopt_policy, _policy, _split, tax_sale_values

_KINDS = {"operations.advance_fulfillment", "operations.advance_refund"}


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
    members = related(fact, context, fact["data"]["advance_id"], _KINDS, "advance_id")
    span = position(fact, members, "gross_fen", amount(original.target["values"], "gross_fen"))
    return original, definition, members, span


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


def _inherited_split(original, span):
    params = original.basis["policies"][0]["parameters"]
    vat = original.basis["input"]["vat"]
    before, _ = _split(span.before, params, vat)
    after, _ = _split(span.after, params, vat)
    if span.after == amount(original.target["values"], "gross_fen"):
        after = amount(original.target["values"], "net_fen")
    net = after - before
    return net, span.after - span.before - net


def _check_inherited_terms(fact, original):
    for field in ("vat", "tax_point"):
        if field not in fact["data"]:
            continue
        value = deepcopy(fact["data"][field])
        if field == "tax_point":
            value.pop("source", None)
        if value != original.basis["input"][field]:
            reject("已计税预收履约必须沿用原采用税务资料，不能另换税率或税点。", "invalid_input")


def _untaxed_terms(fact, context, original_fact):
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
    if point["type"] != "explicit" or point["tax_period"] != fact["period"]:
        reject("非本次履约月的税点接续尚未完整实现。", "unsupported_business")
    return vat, point, _policy(fact, context, vat, point)


def advance_fulfillment(fact, context):
    original, definition, members, span = _original(fact, context)
    gross = amount(fact["data"], "gross_fen")
    due = original.basis["input"]["tax_status"] == "due"
    policy = vat = point = None
    if due:
        _check_inherited_terms(fact, original)
        net, tax = _inherited_split(original, span)
    else:
        original_fact = context.get_fact(fact["data"]["advance_id"])
        vat, point, policy = _untaxed_terms(fact, context, original_fact)
        same_basis = [
            member
            for member in members
            if member["kind"] == fact["kind"]
            and member["data"].get("vat", original_fact["data"].get("vat")) == vat
        ]
        tax_span = position(
            fact, same_basis, "gross_fen", amount(original.target["values"], "gross_fen")
        )
        before, _ = _split(tax_span.before, policy["data"], vat)
        after, _ = _split(tax_span.after, policy["data"], vat)
        net, tax = after - before, gross - (after - before)
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": fact["data"]["gross_fen"],
            "revenue_fen": format_fen(net),
            "vat_fen": format_fen(tax),
            "advance_consumed_fen": fact["data"]["gross_fen"],
        },
    )
    _adopt_original(plan, original, definition)
    adopt_position(plan, definition.reference, span if due else tax_span)
    if due:
        adopt_tax(plan, original)
    else:
        plan["basis"]["input"]["vat"] = deepcopy(vat)
        _adopt_policy(plan, policy, context)
        plan["tax_sales"].append(
            tax_sale_values(
                fact["business_id"],
                point["tax_period"],
                policy["data"],
                vat,
                net,
                tax,
                gross,
            )
        )
    line(
        plan,
        "2203",
        debit=net if due else gross,
        party=definition.value["party_id"],
        summary="释放已履约预收",
    )
    line(plan, "5001", credit=net, summary="确认预收履约收入")
    if not due:
        line(plan, "222101", credit=tax, summary="确认本次履约新到税点VAT")
    _consume(plan, fact, definition)
    return plan


def advance_refund(fact, context):
    original, definition, _, span = _original(fact, context)
    gross, party = amount(fact["data"], "gross_fen"), definition.value["party_id"]
    due = original.basis["input"]["tax_status"] == "due"
    net, tax = _inherited_split(original, span) if due else (gross, 0)
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": fact["data"]["gross_fen"],
            "net_reversed_fen": format_fen(net),
            "vat_reversed_fen": format_fen(tax),
        },
    )
    _adopt_original(plan, original, definition)
    if due:
        adopt_position(plan, definition.reference, span)
    line(plan, "2203", debit=net, party=party, summary="转出原未履约预收")
    line(plan, "222101", debit=tax, summary="冲减退款对应已采用VAT")
    line(plan, "2203", credit=gross, party=party, summary="确认预收退款责任")
    obligation(plan, "refund", party, "payable", gross, "2203", cashflow="customer_receipts")
    _transfer(
        plan, definition.reference, {"business_id": fact["business_id"], "slot": "refund"}, gross
    )
    if due:
        sale = adopt_tax(plan, original)
        negative_tax(
            plan,
            sale,
            net,
            tax,
            [{"from_fen": format_fen(span.before), "to_fen": format_fen(span.after)}],
        )
    return plan
