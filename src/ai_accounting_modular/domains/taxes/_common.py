"""Small, strict constructors shared only inside the tax domain."""

from calendar import monthrange
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    fact_source_paths,
    format_fen,
    parse_fen,
    parse_rate,
    sum_fen,
)


def reject(message, code="dependency_conflict"):
    raise ContractError(code, message)


def issue(fact, field, message, *, code="missing_fact", precision=(), blocking_scope="business"):
    return {
        "business_id": fact["business_id"],
        "code": code,
        "field": field,
        "meaning": "税务采用依据",
        "message": message,
        "allowed_precision": list(precision),
        "reusable_sources": [{"source_id": x} for x in fact["evidence_ids"][:500]],
        "blocking_scope": blocking_scope,
    }


def missing(fact, field, message, *, precision=()):
    raise ContractError(
        "needs_information",
        message,
        fact_issues=[issue(fact, field, message, precision=precision)],
    )


def amount(value):
    return parse_fen(value)


def checked(value):
    try:
        return checked_fen(value)
    except ValueError as error:
        raise ContractError("invalid_input", "税额超出有符号64位整数分范围。") from error


def total(values):
    try:
        return sum_fen(tuple(values))
    except ValueError as error:
        raise ContractError("invalid_input", "税务合计超出有符号64位整数分范围。") from error


def rounded(value):
    return checked(int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def multiply(value, *rates):
    with localcontext() as context:
        context.prec = 80
        result = Decimal(value)
        for rate in rates:
            result *= parse_rate(rate)
        return rounded(result)


def month_end(month):
    year, number = map(int, month.split("-"))
    return f"{month}-{monthrange(year, number)[1]:02d}"


def interval(start, end, *, same_year=False):
    if start > end or same_year and start[:4] != end[:4]:
        reject("税务期间必须按顺序并处于同一税年。", "invalid_input")


def accounting_input(fact):
    """Separate declared source locators from the exact accounting payload."""
    data = deepcopy(fact["data"])
    locators = []
    for paths in fact_source_paths(fact).values():
        for pointer in paths:
            if not pointer.startswith("/data/"):
                continue
            parts = [x.replace("~1", "/").replace("~0", "~") for x in pointer.split("/")[2:]]
            source = fact["data"]
            for part in parts[:-1]:
                source = source[int(part)] if isinstance(source, list) else source[part]
            if source not in locators:
                locators.append(deepcopy(source))
            parent = data
            for part in parts[:-2]:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            parent.pop(parts[-2], None)
    if fact["kind"] == "taxes.actual_declaration" and data["type"] == "payroll":
        data.pop("declared_on", None)
    return data, locators


def plan_for(fact, context, values):
    data, locators = accounting_input(fact)
    sources = context.resolve_sources(fact["evidence_ids"])
    for location in locators:
        context.resolve_sources([location["source_id"]])
        if location not in sources:
            sources.append(location)
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
            "input": data,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": sources,
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
    if "effective_on" in fact:
        if fact["effective_on"][:7] != fact["period"]:
            reject("税费核算实际日必须属于其明确经济月份。", "invalid_input")
        plan["basis"]["effective_on"] = fact["effective_on"]
    return plan


def adopt_source(plan, source):
    if source not in plan["basis"]["source_results"]:
        plan["basis"]["source_results"].append(deepcopy(source))


def result_for(fact, context, business_id, kind, field):
    result = context.get_result(business_id, "main")
    if result is None:
        missing(fact, field, "需要所引用的已采用税务或业务结果。")
    if result.target["kind"] != kind:
        reject("所引用的正式结果不属于所需业务类型。", "invalid_input")
    return result


def line(plan, account, value, *, debit=False, summary="税费采用"):
    if not value:
        return
    if value < 0:
        reject("受控分录金额不能为负。", "invalid_input")
    plan["lines"].append(
        {
            "line_no": len(plan["lines"]) + 1,
            "account_code": account,
            "debit_fen": format_fen(value if debit else 0),
            "credit_fen": format_fen(0 if debit else value),
            "summary": summary,
        }
    )


def obligation(
    plan, slot, account, value, *, receivable=False, methods=("money", "offset"), weights=None
):
    row = {
        "business_id": plan["business_id"],
        "slot": slot,
        "direction": "receivable" if receivable else "payable",
        "category": "tax",
        "amount_fen": format_fen(value),
        "account_code": account,
        "allowed_settlements": list(methods),
        "business_period": plan["accounting_period"],
    }
    if weights:
        row["cashflow_weights"] = deepcopy(weights)
    plan["obligations"].append(row)


def fact_for(fact, context, business_id, kind, field):
    source = context.get_fact(business_id)
    if source is None:
        missing(fact, field, "需要所引用的有据税务资料。")
    if source["kind"] != kind:
        reject("税务资料引用的业务类型不正确。", "invalid_input")
    return source
