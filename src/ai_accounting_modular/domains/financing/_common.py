"""Exact controlled plans and the financing owner's public source quantities."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    format_fen,
    parse_fen,
    sum_fen,
)

STANDARD_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
APPENDIX_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf"
ACCOUNTS = {"bank_account": "1002", "cash_account": "1001", "platform_account": "1012"}
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


def fail(message, code="dependency_conflict"):
    raise ContractError(code, message)


def issue(fact, field, message, *, code="missing_fact", precision=()):
    return {
        "code": code,
        "business_id": fact["business_id"],
        "field": field,
        "meaning": "影响融资本金、利息或投资成本的核算事实",
        "message": message,
        "allowed_precision": list(precision),
        "reusable_sources": [{"source_id": x} for x in fact["evidence_ids"][:500]],
        "blocking_scope": "business",
    }


def missing(fact, field, message, *, precision=(), code="missing_fact"):
    raise ContractError(
        "needs_information",
        message,
        fact_issues=[issue(fact, field, message, precision=precision, code=code)],
    )


def checked(value):
    try:
        return checked_fen(value)
    except ValueError as error:
        raise ContractError("invalid_input", "融资投资金额超出有符号64位整数分范围。") from error


def total(values):
    try:
        return sum_fen(values)
    except ValueError as error:
        raise ContractError("invalid_input", "融资投资合计超出有符号64位整数分范围。") from error


def amount(value):
    return parse_fen(value)


def base_plan(fact, context, values, *, accounting_input=None):
    basis = {
        "kind": fact["kind"],
        "result_slot": "main",
        "period": fact["period"],
        "input": deepcopy(fact["data"] if accounting_input is None else accounting_input),
        "source_results": [],
        "adopted_quantities": [],
        "policies": [],
        "source_locations": context.resolve_sources(fact["evidence_ids"]),
    }
    if "effective_on" in fact:
        basis["effective_on"] = fact["effective_on"]
    return {
        "business_id": fact["business_id"],
        "result_slot": "main",
        "accounting_period": fact["period"],
        "calculation_key": fact["kind"],
        "calculation_revision": 1,
        "rule_refs": [
            {
                "key": fact["kind"],
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [STANDARD_URL, APPENDIX_URL],
            }
        ],
        "target": {"kind": fact["kind"], "result_slot": "main", "values": values},
        "basis": basis,
        **{key: [] for key in PLAN_ARRAYS},
    }


def line(plan, account, *, debit=0, credit=0, party=None, object_id=None, summary):
    if not debit and not credit:
        return
    value = {
        "line_no": len(plan["lines"]) + 1,
        "account_code": account,
        "debit_fen": format_fen(checked(debit)),
        "credit_fen": format_fen(checked(credit)),
        "summary": summary,
    }
    for key, item in (("party_id", party), ("object_id", object_id)):
        if item is not None:
            value[key] = item
    plan["lines"].append(value)


def obligation(plan, slot, party, direction, value, account, category, cashflow, *, due_on=None):
    result = {
        "business_id": plan["business_id"],
        "slot": slot,
        "party_id": party,
        "direction": direction,
        "category": category,
        "amount_fen": format_fen(value),
        "account_code": account,
        "allowed_settlements": ["money", "transfer"],
        "business_period": plan["accounting_period"],
        "cashflow_weights": [{"category": cashflow, "weight": "1"}],
    }
    if due_on is not None:
        result["due_on"] = due_on
    plan["obligations"].append(result)


def adopt(plan, source):
    if source not in plan["basis"]["source_results"]:
        plan["basis"]["source_results"].append(deepcopy(source))


def adopt_obligation(plan, definition):
    value = definition.value
    adopt(plan, definition.source)
    quantity = {
        "type": "obligation",
        "source": deepcopy(definition.reference),
        **{key: value[key] for key in ("account_code", "direction", "amount_fen")},
    }
    if "party_id" in value:
        quantity["party_id"] = value["party_id"]
    plan["basis"]["adopted_quantities"].append(quantity)


def require_object(fact, context, object_id, kinds, field):
    value = context.get_object(object_id)
    if value is None:
        missing(fact, field, "所引用的实际对象尚未在本公司登记。", precision=("identity",))
    if value["kind"] not in kinds:
        raise ContractError(
            "invalid_input",
            "融资投资引用对象的种类与业务角色不符。",
            fact_issues=[
                issue(
                    fact,
                    field,
                    "不能用账户、人员或产品冒充其他业务角色。",
                    code="incompatible_object_kind",
                )
            ],
        )
    return value


def add_funds(plan, fact, context, party, value, category):
    account_id = fact["data"]["funds_account_id"]
    account = require_object(fact, context, account_id, ACCOUNTS, "/data/funds_account_id")
    plan["fund_movements"].append(
        {
            "movement_id": context.movement_id(fact["business_id"], "funds"),
            "public_slot": "funds",
            "funds_account_id": account_id,
            "actual_on": fact["effective_on"],
            "direction": "inflow",
            "amount_fen": format_fen(value),
            "party_id": party,
            "cashflow_parts": [{"category": category, "amount_fen": format_fen(value)}],
        }
    )
    for source in context.fund_sources(fact, plan["fund_movements"]):
        if source not in plan["basis"]["source_locations"]:
            plan["basis"]["source_locations"].append(deepcopy(source))
    line(
        plan,
        ACCOUNTS[account["kind"]],
        debit=value,
        object_id=account_id,
        summary="收到真实投入资金",
    )


def definitions(context, refs, kind):
    result = []
    for offset in range(0, len(refs), 500):
        result.extend(context.resource_definitions(refs[offset : offset + 500], kind))
    return result
