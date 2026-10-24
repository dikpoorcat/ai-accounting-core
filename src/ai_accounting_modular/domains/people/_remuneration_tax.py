"""A complete tax target splits existing payables without re-accruing remuneration."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from ai_accounting_modular.contracts.public import canonical_json, format_fen, parse_fen

from ._common import line, payable, plan_for, referenced, reject, total, use_location, use_result
from ._remuneration import read_declaration_allocation

KIND = "people.remuneration_tax"


def references(fact):
    data = fact["data"]
    return [
        (data["remuneration_id"], "result", "calculation"),
        *dict.fromkeys(
            (item["declaration_id"], "fact", "reference")
            for item in data["components"]
            if "declaration_id" in item
        ),
    ]


def use_dependency(fact):
    return {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": fact["data"]["remuneration_id"],
        "producer_public_slot": "net_pay",
        "read_mode": "use",
        "resource_components": ["transfer"],
        "relation_kind": "settlement",
        "period_from": fact["period"],
        "period_to": fact["period"],
    }


def _components(fact, context, original):
    values = original.target["values"]
    identities = set()
    locations, amounts, inputs = [], [], []
    for index, component in enumerate(fact["data"]["components"]):
        if "tax_fen" in component:
            value, sources = parse_fen(component["tax_fen"]), (component["source"],)
            adopted = {"tax_fen": component["tax_fen"]}
        else:
            value, sources = read_declaration_allocation(
                fact,
                context,
                component,
                field=f"/data/components/{index}",
                tax_period=fact["data"]["tax_period"],
                remuneration_id=fact["data"]["remuneration_id"],
                person_id=values["person_id"],
                remuneration_type=values["remuneration_type"],
            )
            adopted = {**component, "tax_fen": format_fen(value)}
        # The row locator is the same identity even when supplied once directly
        # and once through an actual declaration allocation.
        identity = canonical_json(sources[-1])
        if identity in identities:
            reject("同一扣税来源或申报分配不能重复组成税额。", code="invalid_input")
        identities.add(identity)
        amounts.append(value)
        inputs.append(adopted)
        locations.extend(sources)
    unique_locations = {canonical_json(item): item for item in locations}
    return (
        total(amounts),
        sorted(inputs, key=canonical_json),
        [unique_locations[key] for key in sorted(unique_locations)],
    )


def remuneration_tax(fact, context):
    data = fact["data"]
    original = referenced(
        fact,
        context,
        data["remuneration_id"],
        "people.remuneration",
        field="/data/remuneration_id",
    )
    values, basis = original.target["values"], original.basis
    if fact["period"] < basis["period"]:
        reject("扣税组成不能早于其原报酬成立月份。")
    target, components, locations = _components(fact, context, original)
    initial = parse_fen(values["tax_fen"]) if values.get("tax_period") == data["tax_period"] else 0
    if target < initial:
        reject(
            "新税额目标低于原报酬已经采用的税款组成；需要核对原组成更正或真实退还事实，"
            "不能自动转回员工应付或假造退款。"
        )
    difference = target - initial
    plan = plan_for(
        fact,
        context,
        {
            "remuneration_id": data["remuneration_id"],
            "person_id": values["person_id"],
            "remuneration_type": values["remuneration_type"],
            "tax_period": data["tax_period"],
            "initial_tax_fen": format_fen(initial),
            "target_tax_fen": format_fen(target),
            "additional_tax_fen": format_fen(difference),
            "basis_type": data["basis_type"],
        },
        accounting_input={**data, "components": components},
    )
    if "effective_on" in fact:
        if fact["effective_on"][:7] != fact["period"]:
            reject("扣税组成实际日期必须属于其核算月份。", code="invalid_input")
        plan["basis"]["effective_on"] = fact["effective_on"]
    use_result(plan, original)
    quantity = {
        "type": "remuneration",
        "source": {"business_id": data["remuneration_id"], "slot": "main"},
        "period": basis["period"],
        "values": deepcopy(values),
    }
    destination = basis["input"].get("destination", {})
    if destination.get("type") == "project":
        quantity["project_id"] = destination["project_id"]
    else:
        quantity["expense_area"] = destination.get("area", basis["input"].get("expense_area"))
    plan["basis"]["adopted_quantities"].append(quantity)
    for location in locations:
        use_location(plan, context, location)
    if difference:
        line(
            plan,
            "224104" if values["remuneration_type"] == "labor" else "221101",
            difference,
            debit=True,
            party=values["person_id"],
        )
        payable(
            plan,
            "tax",
            "222103",
            difference,
            category="withholding",
            period=data["tax_period"],
            cashflow="tax_payments",
        )
        plan["obligation_reclassifications"].append(
            {
                "relation_id": str(uuid5(NAMESPACE_URL, KIND + ":" + fact["business_id"])),
                "owner_business_id": fact["business_id"],
                "source": {"business_id": data["remuneration_id"], "slot": "net_pay"},
                "target": {"business_id": fact["business_id"], "slot": "tax"},
                "amount_fen": format_fen(difference),
                "mode": "transfer",
            }
        )
    return plan
