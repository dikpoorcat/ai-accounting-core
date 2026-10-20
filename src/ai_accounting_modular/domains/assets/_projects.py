"""Capitalized project costs and explicit one-time transfers."""

from ai_accounting_modular.contracts.public import parse_fen

from ._common import (
    EXPENSE,
    base_plan,
    consume,
    cost_definition,
    cost_lot,
    line,
    obligation,
    project_account,
    reject,
    result_for,
    total,
)


def project_cost(fact, context):
    data, amount = fact["data"], parse_fen(fact["data"]["amount_fen"])
    plan = base_plan(
        fact,
        context,
        {
            "project_id": data["project_id"],
            "amount_fen": data["amount_fen"],
            "treatment": "capitalized",
        },
    )
    account = "4301" if data["project_nature"] == "internal_development" else "189901"
    line(plan, account, amount, debit=True, object_id=data["project_id"])
    obligation(plan, "primary", data["supplier_id"], amount)
    cost_lot(plan, "cost", data["project_id"], amount, category="project")
    return plan


def project_sources(fact, context, plan):
    data, seen = fact["data"], set()
    context.load_costs(item["source"] for item in data["sources"])
    amount = total(parse_fen(item["amount_fen"]) for item in data["sources"])
    for item in sorted(
        data["sources"], key=lambda item: (item["source"]["business_id"], item["source"]["slot"])
    ):
        ref, part = item["source"], parse_fen(item["amount_fen"])
        key = ref["business_id"], ref["slot"]
        if key in seen:
            reject("同一项目成本来源不能在同一业务中重复列出。", "invalid_input")
        seen.add(key)
        definition = cost_definition(
            fact, context, ref, object_id=data["project_id"], category="project"
        )
        if part > parse_fen(definition.value["amount_fen"]):
            reject("项目成本消耗超过来源确认金额。")
        original = result_for(context, ref["business_id"], definition.source["result_slot"])
        consume(plan, definition, part)
        line(plan, project_account(original), part, object_id=data["project_id"])
    return amount


def project_release(fact, context):
    data = fact["data"]
    amount = total(parse_fen(item["amount_fen"]) for item in data["sources"])
    plan = base_plan(
        fact,
        context,
        {
            "project_id": data["project_id"],
            "amount_fen": str(amount),
            "treatment": "released",
        },
    )
    line(plan, EXPENSE[data["expense_class"]], amount, debit=True)
    project_sources(fact, context, plan)
    return plan
