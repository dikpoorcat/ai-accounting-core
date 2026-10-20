"""Controlled plan construction and explicit fact issues."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    format_fen,
    parse_fen,
    sum_fen,
)

EXPENSE_ACCOUNTS = {
    "administration": "5602",
    "sales": "5601",
    "service": "5401",
    "bank_fee": "5603",
    "tax_late_fee": "571103",
    "social_contribution_late_fee": "571104",
}
PAYABLE_ACCOUNTS = {"supplier": "2202", "employee": "224101", "individual": "2241"}
REPORT_CODES = {
    "administration": {
        "management_startup",
        "management_entertainment",
        "management_research",
        "management_other",
    },
    "sales": {"sales_merchandise_repair", "sales_advertising_promotion", "sales_other"},
    "bank_fee": {"finance_interest", "finance_other"},
    "service": set(),
    "tax_late_fee": set(),
    "social_contribution_late_fee": set(),
}
REPORT_LINES = {
    "sales_merchandise_repair": 12,
    "sales_advertising_promotion": 13,
    "management_startup": 15,
    "management_entertainment": 16,
    "management_research": 17,
    "finance_interest": 19,
}


def reject(message, code="dependency_conflict"):
    raise ContractError(code, message)


def checked_amount(value):
    try:
        return checked_fen(value)
    except ValueError as error:
        raise ContractError("invalid_input", "经营核算金额超出有符号64位整数分范围。") from error


def sum_amounts(values):
    operands = tuple(values)
    try:
        return sum_fen(operands)
    except ValueError as error:
        raise ContractError("invalid_input", "经营核算合计超出有符号64位整数分范围。") from error


def issue(fact, field, message, *, precision=()):
    return {
        "code": "missing_fact",
        "business_id": fact["business_id"],
        "field": field if field.startswith("/") else "/" + field.replace(".", "/"),
        "meaning": "accounting",
        "message": message,
        "allowed_precision": list(precision),
        "reusable_sources": [{"source_id": x} for x in fact["evidence_ids"][:500]],
        "blocking_scope": "business",
    }


def line(plan, account, *, debit=0, credit=0, party=None, summary="经营业务"):
    if not debit and not credit:
        return
    value = {
        "line_no": len(plan["lines"]) + 1,
        "account_code": account,
        "debit_fen": format_fen(debit),
        "credit_fen": format_fen(credit),
        "summary": summary,
    }
    if party:
        value["party_id"] = party
    plan["lines"].append(value)


def obligation(
    plan,
    slot,
    party,
    direction,
    amount,
    account,
    *,
    category="trade",
    methods=("money", "offset", "transfer"),
    cashflow="other_operating_payments",
):
    value = {
        "business_id": plan["business_id"],
        "slot": slot,
        "direction": direction,
        "category": category,
        "amount_fen": format_fen(amount),
        "account_code": account,
        "allowed_settlements": list(methods),
        "business_period": plan["accounting_period"],
    }
    if party is not None:
        value["party_id"] = party
    if cashflow:
        value["cashflow_weights"] = [{"category": cashflow, "weight": "1"}]
    plan["obligations"].append(value)


def base_plan(fact, context, values, *, adopt_effective_on=False):
    kind = fact["kind"]
    accounting_input = deepcopy(fact["data"])
    if (
        kind == "operations.advance"
        and accounting_input["side"] == "customer"
        and accounting_input["tax_status"] == "not_due"
        and "tax_point" not in accounting_input
    ):
        accounting_input.pop("vat", None)
    tax_point = accounting_input.get("tax_point", {})
    tax_source = tax_point.pop("source", None)
    source_ids = list(fact["evidence_ids"])
    if tax_source is not None and tax_source["source_id"] not in source_ids:
        source_ids.append(tax_source["source_id"])
    sources = context.resolve_sources(source_ids)
    if tax_source is not None and tax_source not in sources:
        sources.append(tax_source)
    basis = {
        "kind": kind,
        "result_slot": "main",
        "period": fact["period"],
        "input": accounting_input,
        "source_results": [],
        "adopted_quantities": [],
        "policies": [],
        "source_locations": sources,
    }
    if adopt_effective_on and "effective_on" in fact:
        basis["effective_on"] = fact["effective_on"]
    plan = {
        "business_id": fact["business_id"],
        "result_slot": "main",
        "accounting_period": fact["period"],
        "calculation_key": kind,
        "calculation_revision": 1,
        "rule_refs": [{"key": kind, "revision": 1, "policy_business_ids": [], "official_urls": []}],
        "target": {"kind": kind, "result_slot": "main", "values": values},
        "basis": basis,
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


def referenced_result(fact, context, ref):
    result = context.get_result(ref["business_id"], "main")
    if result is None:
        reject("原业务当前正式核算结果尚未取得。")
    if result.basis["period"] > fact["period"]:
        reject("后续业务不能早于原业务核算月份。")
    return result


def adopt_source(plan, result):
    if result.source not in plan["basis"]["source_results"]:
        plan["basis"]["source_results"].append(deepcopy(result.source))


def amount(data, field="amount_fen"):
    return parse_fen(data[field])
