"""Exact asset amounts, month arithmetic, and controlled posting construction."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    YearMonth,
    checked_fen,
    format_fen,
    sum_fen,
)

PENDING = {"fixed": "1604", "intangible": "189901"}
CAPITAL = {"fixed": "1601", "intangible": "1701"}
ACCUMULATED = {"fixed": "1602", "intangible": "1702"}
EXPENSE = {"administration": "5602", "sales": "5601", "service": "5401"}
CONSUMPTION = {
    "fixed": {"administration": "560202", "sales": "560102", "service": "540102"},
    "intangible": {"administration": "560203", "sales": "560103", "service": "540103"},
}


def reject(message, code="dependency_conflict", *, fact_issues=()):
    raise ContractError(code, message, fact_issues=fact_issues)


def issue(fact, field, message, *, precision=(), code="missing_fact"):
    return {
        "code": code,
        "business_id": fact["business_id"],
        "field": field,
        "meaning": "资产核算依据",
        "message": message,
        "allowed_precision": list(precision),
        "reusable_sources": [{"source_id": x} for x in fact["evidence_ids"][:500]],
        "blocking_scope": "business",
    }


def missing(fact, field, message, *, precision=()):
    raise ContractError(
        "needs_information",
        message,
        fact_issues=[issue(fact, field, message, precision=precision)],
    )


def checked(value):
    try:
        return checked_fen(value)
    except ValueError as error:
        raise ContractError("invalid_input", "资产金额超出有符号64位整数分范围。") from error


def total(values):
    try:
        return sum_fen(tuple(values))
    except ValueError as error:
        raise ContractError("invalid_input", "资产合计超出有符号64位整数分范围。") from error


def month_number(month):
    try:
        return YearMonth(month).ordinal
    except ValueError as error:
        raise ContractError("invalid_input", "资产月份必须是有效的YYYY-MM。") from error


def month_at(number):
    try:
        return str(YearMonth.from_ordinal(number))
    except ValueError as error:
        raise ContractError("invalid_input", "摊折月份超出合同允许的年份。") from error


def add_months(month, count):
    return month_at(month_number(month) + count)


def base_plan(fact, context, values):
    kind = fact["kind"]
    accounting_input = deepcopy(fact["data"])
    for field in ("assets", "items"):
        if field in accounting_input:
            accounting_input[field].sort(key=lambda item: item["asset_id"])
    if "creditors" in accounting_input:
        accounting_input["creditors"].sort(key=lambda item: item["person_id"])
    if "sources" in accounting_input:
        accounting_input["sources"].sort(
            key=lambda item: (
                item["source"]["business_id"],
                item["source"]["slot"],
            )
        )
    plan = new_plan(fact["business_id"], kind, fact["period"])
    plan.update(
        target={"kind": kind, "result_slot": "main", "values": values},
        basis={
            "kind": kind,
            "result_slot": "main",
            "period": fact["period"],
            "input": accounting_input,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": context.resolve_sources(fact["evidence_ids"]),
        },
    )
    return plan


def new_plan(business_id, kind, period, slot="main"):
    plan = {
        "business_id": business_id,
        "result_slot": slot,
        "accounting_period": period,
        "calculation_key": kind,
        "calculation_revision": 1,
        "rule_refs": [{"key": kind, "revision": 1, "policy_business_ids": [], "official_urls": []}],
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


def source(plan, result):
    plan["basis"]["source_results"].append(deepcopy(result.source))


def line(plan, account, amount, *, debit=False, party=None, object_id=None):
    if not amount:
        return
    row = {
        "line_no": len(plan["lines"]) + 1,
        "account_code": account,
        "debit_fen": format_fen(amount if debit else 0),
        "credit_fen": format_fen(0 if debit else amount),
        "summary": "资产及项目核算",
    }
    if party is not None:
        row["party_id"] = party
    if object_id is not None:
        row["object_id"] = object_id
    plan["lines"].append(row)


def obligation(plan, slot, party, amount, *, account="2202", direction="payable"):
    plan["obligations"].append(
        {
            "business_id": plan["business_id"],
            "slot": slot,
            "party_id": party,
            "direction": direction,
            "category": "trade",
            "amount_fen": format_fen(amount),
            "account_code": account,
            "allowed_settlements": ["money", "offset", "transfer"],
            "business_period": plan["accounting_period"],
            "cashflow_weights": [
                {
                    "category": "asset_acquisition" if direction == "payable" else "asset_disposal",
                    "weight": "1",
                }
            ],
        }
    )
    line(plan, account, amount, debit=direction == "receivable", party=party)


def cost_lot(plan, slot, object_id, amount, *, category):
    plan["cost_lots"].append(
        {
            "business_id": plan["business_id"],
            "slot": slot,
            "object_id": object_id,
            "category": category,
            "amount_fen": format_fen(amount),
            "business_period": plan["accounting_period"],
        }
    )


def consume(plan, definition, amount, *, adopt_quantity=True):
    plan["cost_consumptions"].append(
        {
            "source": deepcopy(definition.reference),
            "consumer": {"business_id": plan["business_id"], "slot": plan["result_slot"]},
            "amount_fen": format_fen(amount),
            "period": plan["accounting_period"],
        }
    )
    plan["basis"]["source_results"].append(deepcopy(definition.source))
    if adopt_quantity:
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "cost",
                "source": deepcopy(definition.reference),
                "object_id": definition.value["object_id"],
                "amount_fen": format_fen(amount),
            }
        )


def finalize_plan(plan):
    """Deduplicate source bindings and aggregate equal journal dimensions once."""
    refs = {tuple(sorted(ref.items())): ref for ref in plan["basis"]["source_results"]}
    plan["basis"]["source_results"] = list(refs.values())
    grouped = {}
    for row in plan["lines"]:
        side = "debit_fen" if row["debit_fen"] != "0" else "credit_fen"
        key = row["account_code"], row.get("party_id"), row.get("object_id"), side
        if key not in grouped:
            grouped[key] = deepcopy(row)
        else:
            grouped[key][side] = format_fen(
                total(
                    (
                        int(grouped[key][side]),
                        int(row[side]),
                    )
                )
            )
    plan["lines"] = [dict(row, line_no=index) for index, row in enumerate(grouped.values(), 1)]


def cost_definition(fact, context, ref, *, object_id, category):
    definitions = context.resource_definitions([ref], "cost")
    if len(definitions) != 1 or definitions[0].reference != ref:
        reject("所引用的公开成本不存在。")
    definition = definitions[0]
    if definition.value["object_id"] != object_id or definition.value["category"] != category:
        reject("成本来源的项目、资产或性质不符合本项业务。")
    return definition


def result_for(context, business_id, slot="main"):
    result = context.get_result(business_id, slot)
    if result is None:
        reject("资产核算缺少已声明的正式来源结果。")
    return result


def assets_in(fact):
    if fact["kind"] == "opening.package":
        return sorted(
            (
                member["data"]
                for member in fact["data"]["members"]
                if member["data"]["type"] == "asset"
            ),
            key=lambda item: item["asset_id"],
        )
    if fact["kind"] == "assets.acquisition":
        data = fact["data"]
        return (
            sorted(data["assets"], key=lambda item: item["asset_id"])
            if data["type"] == "reimbursed_batch"
            else [data["asset"]]
        )
    if fact["kind"] == "assets.activation":
        return sorted(fact["data"]["items"], key=lambda item: item["asset_id"])
    if fact["kind"] == "assets.disposal":
        return [fact["data"]]
    return []


def unique_items(items, key, message):
    ids = [item[key] for item in items]
    if len(ids) != len(set(ids)):
        reject(message, "invalid_input")


def project_account(result):
    if result.target["kind"] == "assets.project_cost":
        return (
            "4301"
            if result.basis["input"]["project_nature"] == "internal_development"
            else "189901"
        )
    if (
        result.target["kind"] == "people.remuneration"
        and result.basis["input"].get("destination", {}).get("type") == "project"
    ):
        return "189901"
    reject("项目消耗只接受已明确资本化的项目成本来源。")
