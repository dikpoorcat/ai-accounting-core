"""Direct operating relationships are shared by declaration and final planning."""

from ._cooperation import contract_id
from ._roles import object_roles

_USE_COMPONENTS = {
    "operations.reimbursement": "transfer",
    "operations.overpayment": "reclassify_excess",
    "operations.pass_through_return": "transfer",
    "operations.advance_fulfillment": "offset",
    "operations.advance_refund": "transfer",
}


def references(fact, context):
    data, kind = fact["data"], fact["kind"]
    if kind in {"operations.advance_fulfillment", "operations.advance_refund"}:
        refs = [{"business_id": data["advance_id"], "slot": "main"}]
        if kind == "operations.advance_fulfillment":
            original = context.get_fact(data["advance_id"])
            vat = data.get("vat") or (original or {}).get("data", {}).get("vat")
            if vat:
                refs.append({"business_id": vat["policy_id"], "slot": "main"})
        return refs
    if kind == "operations.expense_recovery":
        return [{"business_id": data["expense_id"], "slot": "main"}]
    if kind == "operations.sale_return":
        return [{"business_id": data["sale_id"], "slot": "main"}]
    if kind == "operations.overpayment":
        return [data["source"]]
    if kind == "operations.pass_through_return":
        return [{"business_id": data["pass_through_id"], "slot": "main"}]
    if kind == "operations.reimbursement":
        return [x["source"] for x in data.get("allocations", data.get("sources", ()))]
    if "vat" in data:
        refs = [{"business_id": data["vat"]["policy_id"], "slot": "main"}]
        if data.get("tax_point", {}).get("type") == "derived":
            refs.append({"business_id": data["tax_point"]["tax_point_id"], "slot": "main"})
        return refs
    return []


def resource_uses(fact):
    kind, data = fact["kind"], fact["data"]
    if kind == "operations.reimbursement":
        refs = [item["source"] for item in data.get("allocations", data.get("sources", ()))]
    elif kind == "operations.overpayment":
        if data["amount_fen"] == "0":
            return []
        refs = [data["source"]]
    elif kind == "operations.pass_through_return":
        refs = [{"business_id": data["pass_through_id"], "slot": "remittance"}]
    elif kind in {"operations.advance_fulfillment", "operations.advance_refund"}:
        refs = [{"business_id": data["advance_id"], "slot": "advance"}]
    else:
        return []
    result = []
    for ref in refs:
        edge = {
            "consumer_business_id": fact["business_id"],
            "consumer_result_slot": "main",
            "producer_kind": "business",
            "producer_id": ref["business_id"],
            "producer_public_slot": ref["slot"],
            "read_mode": "use",
            "relation_kind": "settlement",
            "resource_components": [_USE_COMPONENTS[kind]],
            "period_from": fact["period"],
            "period_to": fact["period"],
        }
        actual = kind in {"operations.overpayment", "operations.pass_through_return"} or (
            kind == "operations.reimbursement" and data["type"] == "personal_payment"
        )
        if actual and "effective_on" in fact:
            edge["effective_on"] = fact["effective_on"]
        result.append(edge)
    return result


def reference_edge(fact, ref):
    point = fact["data"].get("tax_point")
    policy_only = (
        fact["kind"] == "operations.advance"
        and fact["data"]["side"] == "customer"
        and (point is None or point["type"] == "owner_recognition_default")
    )
    money_sensitive = fact["kind"] in {
        "operations.pass_through_return",
        "operations.overpayment",
    }
    return {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": ref["business_id"],
        "producer_public_slot": "main",
        "read_mode": "fact" if policy_only else "result",
        "relation_kind": "settlement"
        if money_sensitive
        else "policy"
        if policy_only
        else "calculation",
    }


def direct_relations(fact, context):
    result = [
        {
            "consumer_business_id": fact["business_id"],
            "consumer_result_slot": "main",
            "producer_kind": "object",
            "producer_id": object_id,
            "producer_public_slot": "main",
            "read_mode": "fact",
            "relation_kind": "identity",
        }
        for object_id in dict.fromkeys(ref.object_id for ref in object_roles(fact))
    ]
    result.extend(reference_edge(fact, ref) for ref in references(fact, context))
    agreement_id = contract_id(fact, context)
    if agreement_id is not None:
        result.append(
            {
                "consumer_business_id": fact["business_id"],
                "consumer_result_slot": "main",
                "producer_kind": "business",
                "producer_id": agreement_id,
                "producer_public_slot": "main",
                "read_mode": "result",
                "relation_kind": "calculation",
            }
        )
    return result


def fact_relations(fact, context):
    """Keep explicit tax-point references even when another calculation path supplies the edge."""
    fields = ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
    relations = {tuple(edge[key] for key in fields) for edge in direct_relations(fact, context)}
    point = fact["data"].get("tax_point")
    if point is not None and point["type"] == "derived":
        relations.add((fact["business_id"], "business", point["tax_point_id"], "calculation"))
    return tuple(dict(zip(fields, value, strict=True)) for value in sorted(relations))
