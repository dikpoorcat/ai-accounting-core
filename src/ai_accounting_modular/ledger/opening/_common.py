"""Opening plans carry sourced balances, never synthetic historical journals."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    format_fen,
    sum_fen,
)

ACCOUNTS = {"bank_account": "1002", "platform_account": "1012", "cash_account": "1001"}
PARTIES = {"person", "organization"}


def fail(message, code="dependency_conflict", *, fact_issues=()):
    raise ContractError(code, message, fact_issues=fact_issues)


def issue(fact, field, message, *, code):
    return {
        "code": code,
        "business_id": fact["business_id"],
        "field": field,
        "meaning": "建账起点的真实余额及接续依据",
        "message": message,
        "allowed_precision": [],
        "reusable_sources": [{"source_id": source} for source in fact["evidence_ids"][:500]],
        "blocking_scope": "business",
    }


def checked(value):
    try:
        return checked_fen(value)
    except ValueError as error:
        raise ContractError("invalid_input", "期初金额超出有符号64位整数分范围。") from error


def total(values):
    try:
        return sum_fen(tuple(values))
    except ValueError as error:
        raise ContractError("invalid_input", "期初合计超出有符号64位整数分范围。") from error


def require_object(fact, context, object_id, kinds, field):
    obj = context.get_object(object_id)
    if obj is None:
        message = "期初所填对象编号未登记；先核对引用或登记已有来源中的真实对象。"
        fail(
            message,
            "invalid_input",
            fact_issues=[issue(fact, field, message, code="reference_not_found")],
        )
    if obj["kind"] not in kinds:
        fail("期初对象的种类与成员的业务角色不一致。", "invalid_input")
    return obj


def base_plan(fact, context):
    sources = set(fact["evidence_ids"])
    for member in fact["data"]["members"]:
        sources.update(member["evidence_ids"])
    sources = sorted(sources)
    locations = []
    for offset in range(0, len(sources), 500):
        locations.extend(context.resolve_sources(sources[offset : offset + 500]))
    accounting_input = deepcopy(fact["data"])
    for member in accounting_input["members"]:
        data = member["data"]
        if data["type"] == "loan" and data["principal_fen"] == "0":
            data.pop("interest_start_on", None)
    return {
        "business_id": fact["business_id"],
        "result_slot": "main",
        "accounting_period": fact["period"],
        "calculation_key": "opening.package",
        "calculation_revision": 1,
        "rule_refs": [
            {
                "key": "opening.package",
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [],
            }
        ],
        "target": {
            "kind": "opening.package",
            "result_slot": "main",
            "values": {
                "bookkeeping_start": fact["period"],
                "debit_fen": "0",
                "credit_fen": "0",
                "member_count": len(fact["data"]["members"]),
            },
        },
        "basis": {
            "kind": "opening.package",
            "result_slot": "main",
            "period": fact["period"],
            "input": accounting_input,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": locations,
        },
        **{
            name: []
            for name in (
                "lines",
                "obligations",
                "settlements",
                "obligation_reclassifications",
                "cost_lots",
                "cost_consumptions",
                "fund_movements",
                "agency_proofs",
                "tax_sales",
                "report_contributions",
            )
        },
    }


def balance(plan, member, code, *, debit=0, credit=0, party=None, object_id=None):
    if not debit and not credit:
        return
    value = {
        "type": "opening_balance",
        "member_id": member["member_id"],
        "account_code": code,
        "debit_fen": format_fen(checked(debit)),
        "credit_fen": format_fen(checked(credit)),
    }
    if party is not None:
        value["party_id"] = party
    if object_id is not None:
        value["object_id"] = object_id
    plan["basis"]["adopted_quantities"].append(value)


def obligation(
    plan, slot, party, direction, value, code, category, cashflow, *, period=None, due=None
):
    result = {
        "business_id": plan["business_id"],
        "slot": slot,
        "party_id": party,
        "direction": direction,
        "category": category,
        "amount_fen": format_fen(value),
        "account_code": code,
        "allowed_settlements": ["money", "offset", "transfer"],
        "business_period": period or plan["accounting_period"],
    }
    if cashflow is not None:
        result["cashflow_weights"] = [{"category": cashflow, "weight": "1"}]
    if due is not None:
        result["due_on"] = due
    plan["obligations"].append(result)
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "obligation",
            "source": {"business_id": plan["business_id"], "slot": slot},
            "account_code": code,
            "direction": direction,
            "amount_fen": format_fen(value),
            "party_id": party,
        }
    )
