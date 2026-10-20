"""Strict plan construction; dates and absent facts are never guessed."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    format_fen,
    sum_fen,
)


def reject(message, *, code="dependency_conflict"):
    raise ContractError(code, message)


def problem(fact, field, message, *, code="missing_fact", precision=(), meaning="薪酬核算依据"):
    return {
        "code": code,
        "business_id": fact["business_id"],
        "field": field,
        "meaning": meaning,
        "message": message,
        "allowed_precision": list(precision),
        "reusable_sources": [{"source_id": x} for x in fact["evidence_ids"][:500]],
        "blocking_scope": "business",
    }


def missing(fact, field, message, *, precision=()):
    raise ContractError(
        "needs_information",
        message,
        fact_issues=[problem(fact, field, message, precision=precision)],
    )


def checked(value):
    try:
        return checked_fen(value)
    except ValueError as error:
        raise ContractError("invalid_input", "薪酬金额超出有符号64位整数分范围。") from error


def total(values):
    try:
        return sum_fen(tuple(values))
    except ValueError as error:
        raise ContractError("invalid_input", "薪酬合计超出有符号64位整数分范围。") from error


def plan_for(fact, context, values, *, accounting_input=None):
    kind = fact["kind"]
    plan = {
        "business_id": fact["business_id"],
        "result_slot": "main",
        "accounting_period": fact["period"],
        "calculation_key": kind,
        "calculation_revision": 1,
        "rule_refs": [{"key": kind, "revision": 1, "policy_business_ids": [], "official_urls": []}],
        "target": {"kind": kind, "result_slot": "main", "values": values},
        "basis": {
            "kind": kind,
            "result_slot": "main",
            "period": fact["period"],
            "input": deepcopy(fact["data"] if accounting_input is None else accounting_input),
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": context.resolve_sources(fact["evidence_ids"]),
        },
    }
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
    ):
        plan[name] = []
    return plan


def use_result(plan, result):
    if result.source not in plan["basis"]["source_results"]:
        plan["basis"]["source_results"].append(deepcopy(result.source))


def use_location(plan, context, locator):
    context.resolve_sources([locator["source_id"]])
    if locator not in plan["basis"]["source_locations"]:
        plan["basis"]["source_locations"].append(deepcopy(locator))


def line(plan, account, amount, *, debit=False, party=None, object_id=None):
    if not amount:
        return
    value = {
        "line_no": len(plan["lines"]) + 1,
        "account_code": account,
        "debit_fen": format_fen(amount if debit else 0),
        "credit_fen": format_fen(0 if debit else amount),
        "summary": "职工薪酬" if plan["basis"]["input"].get("type") != "labor" else "个人劳务",
    }
    if party is not None:
        value["party_id"] = party
    if object_id is not None:
        value["object_id"] = object_id
    plan["lines"].append(value)


def payable(plan, slot, account, amount, *, category, party=None, period=None, cashflow=None):
    value = {
        "business_id": plan["business_id"],
        "slot": slot,
        "direction": "payable",
        "category": category,
        "amount_fen": format_fen(amount),
        "account_code": account,
        "allowed_settlements": ["money", "offset", "transfer"],
        "business_period": period or plan["accounting_period"],
    }
    if party is not None:
        value["party_id"] = party
    if cashflow is not None:
        value["cashflow_weights"] = [{"category": cashflow, "weight": "1"}]
    plan["obligations"].append(value)
    line(plan, account, amount, party=party)


def referenced(fact, context, business_id, kind, *, field):
    result = context.get_result(business_id, "main")
    if result is None:
        missing(fact, field, "引用的薪酬核算来源尚不存在。")
    if result.target["kind"] != kind:
        reject("引用的业务不是所需的薪酬来源类型。")
    return result
