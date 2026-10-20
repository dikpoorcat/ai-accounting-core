"""Controlled money plans and actual account roles."""

from copy import deepcopy
from uuid import UUID, uuid5

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    format_fen,
    parse_fen,
    sum_fen,
    validate_contract,
)

ACCOUNT_CODES = {"bank_account": "1002", "cash_account": "1001", "platform_account": "1012"}
PARTY_KINDS = {"person", "organization"}
PLAN_ARRAYS = (
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


def stable_id(business_id, name):
    return str(uuid5(UUID(business_id), "ai-accounting-modular/1/" + name))


def occurrence_id(business_id, public_slot):
    validate_contract("core.schema.json", "Id", business_id)
    validate_contract("core.schema.json", "Slot", public_slot)
    return stable_id(business_id, "funds/" + public_slot)


def fail(message, code="dependency_conflict"):
    raise ContractError(code, message)


def total(values):
    try:
        return sum_fen(tuple(values))
    except ValueError as error:
        raise ContractError("invalid_input", "资金合计超出有符号64位整数分范围。") from error


def amount(value):
    return parse_fen(value)


def issue(fact, field, message, code="missing_fact", precision=()):
    return {
        "code": code,
        "business_id": fact["business_id"],
        "field": field,
        "meaning": "决定实际资金或清偿含义的业务事实",
        "message": message,
        "allowed_precision": list(precision),
        "reusable_sources": [{"source_id": x} for x in fact["evidence_ids"]],
        "blocking_scope": "business",
    }


def require_object(fact, context, object_id, kinds, field):
    obj = context.get_object(object_id)
    if obj is None or obj["kind"] not in kinds:
        raise ContractError(
            "needs_information" if obj is None else "invalid_input",
            "资金业务引用的对象缺失或种类不符。",
            fact_issues=[
                issue(
                    fact,
                    field,
                    "请提供实际对象身份。" if obj is None else "对象种类与此业务角色不符。",
                    "object_not_found" if obj is None else "incompatible_object_kind",
                    ("identity",),
                )
            ],
        )
    return obj


def account(fact, context, object_id, field):
    obj = require_object(fact, context, object_id, ACCOUNT_CODES, field)
    return ACCOUNT_CODES[obj["kind"]], obj["kind"]


def line(plan, code, *, debit=0, credit=0, party=None, object_id=None, summary="实际资金业务"):
    if not debit and not credit:
        return
    checked_fen(debit)
    checked_fen(credit)
    value = {
        "line_no": len(plan["lines"]) + 1,
        "account_code": code,
        "debit_fen": format_fen(debit),
        "credit_fen": format_fen(credit),
        "summary": summary,
    }
    if party is not None:
        value["party_id"] = party
    if object_id is not None:
        value["object_id"] = object_id
    plan["lines"].append(value)


def source_locations(fact):
    values = [{"source_id": sid} for sid in fact["evidence_ids"]]
    data = fact["data"]
    for row in data.get("rows", ()):
        if row["source"] not in values:
            values.append(deepcopy(row["source"]))
    for field in ("return_source", "boundary_source"):
        if field in data and data[field] not in values:
            values.append(deepcopy(data[field]))
    return values


def accounting_input(fact):
    """The same accounting projection is checked at adoption and period review."""
    data = deepcopy(fact["data"])
    for field in ("return_source", "boundary_source"):
        data.pop(field, None)
    for row in data.get("rows", ()):
        for field in ("source", "reference", "counterparty_text", "memo"):
            row.pop(field, None)
    return data


def base_plan(fact, context, values, *, actual=False):
    locations = source_locations(fact)
    ids = list(dict.fromkeys(x["source_id"] for x in locations))
    # The records boundary confirms that every referenced source belongs here.
    context.resolve_sources(ids)
    basis = {
        "kind": fact["kind"],
        "result_slot": "main",
        "period": fact["period"],
        "input": accounting_input(fact),
        "source_results": [],
        "adopted_quantities": [],
        "policies": [],
        "source_locations": locations,
    }
    if actual:
        basis["effective_on"] = fact["effective_on"]
    plan = {
        "business_id": fact["business_id"],
        "result_slot": "main",
        "accounting_period": fact["period"],
        "calculation_key": fact["kind"],
        "calculation_revision": 1,
        "rule_refs": [
            {"key": fact["kind"], "revision": 1, "policy_business_ids": [], "official_urls": []}
        ],
        "target": {"kind": fact["kind"], "result_slot": "main", "values": values},
        "basis": basis,
        **{key: [] for key in PLAN_ARRAYS},
    }
    return plan


def adopt_source(plan, source):
    if source not in plan["basis"]["source_results"]:
        plan["basis"]["source_results"].append(deepcopy(source))


def movement(plan, fact, slot, account_id, direction, value, *, party=None, parts=None, group=None):
    result = {
        "movement_id": occurrence_id(fact["business_id"], slot),
        "public_slot": slot,
        "funds_account_id": account_id,
        "actual_on": fact["effective_on"],
        "direction": direction,
        "amount_fen": format_fen(value),
    }
    for key, item in (("party_id", party), ("cashflow_parts", parts), ("transfer_group_id", group)):
        if item is not None:
            result[key] = item
    plan["fund_movements"].append(result)
    return result


def allocation_refs(fact):
    data = fact["data"]
    if fact["kind"] == "money.movement":
        return data.get("allocations", ())
    if fact["kind"] == "money.transfer" and data["type"] == "offset":
        return (data["first"], data["second"])
    return ()


def object_roles(fact):
    data, roles = fact["data"], []
    for field in ("funds_account_id", "from_account_id", "to_account_id"):
        if field in data:
            roles.append((data[field], ACCOUNT_CODES, "/data/" + field))
    if "platform_account_id" in data:
        roles.append(
            (data["platform_account_id"], {"platform_account"}, "/data/platform_account_id")
        )
    if fact["kind"] == "money.bank_statement":
        roles = [(data["funds_account_id"], {"bank_account"}, "/data/funds_account_id")]
    if "counterparty_id" in data:
        roles.append((data["counterparty_id"], PARTY_KINDS, "/data/counterparty_id"))
    for index, allocation in enumerate(allocation_refs(fact)):
        if "recipient_id" in allocation:
            roles.append(
                (allocation["recipient_id"], PARTY_KINDS, f"/data/allocations/{index}/recipient_id")
            )
    return roles
