"""Company acquisition is separate from supplier payment or personal buying day."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_fen

from ._common import (
    PENDING,
    assets_in,
    base_plan,
    cost_lot,
    line,
    obligation,
    reject,
    total,
    unique_items,
)
from ._projects import project_sources


def check_acquisition(fact):
    data, assets = fact["data"], assets_in(fact)
    unique_items(assets, "asset_id", "一份取得业务不能重复同一资产身份。")
    if data["type"] == "project_completion" and data["asset"]["asset_type"] != "intangible":
        reject("首交项目完工只承接已明确资本化的无形资产。", "invalid_input")
    if data["type"] == "reimbursed_batch":
        unique_items(data["creditors"], "person_id", "同一报销债权人须合并为其真实合计。")
        target = parse_fen(data["total_cost_fen"])
        if (
            total(parse_fen(item["cost_fen"]) for item in assets) != target
            or total(parse_fen(item["amount_fen"]) for item in data["creditors"]) != target
        ):
            reject("资产合计、债权人合计和批次总成本必须相等。", "invalid_input")


def acquisition(fact, context):
    data, assets = fact["data"], assets_in(fact)
    amount = total(parse_fen(item["cost_fen"]) for item in assets)
    creditors = (
        sorted(data["creditors"], key=lambda item: item["person_id"])
        if data["type"] == "reimbursed_batch"
        else []
    )
    plan = base_plan(
        fact,
        context,
        {
            "total_cost_fen": str(amount),
            "assets": deepcopy(assets),
            "creditors": deepcopy(creditors),
        },
    )
    if "effective_on" in fact:
        plan["basis"]["effective_on"] = fact["effective_on"]
    for card in assets:
        cost = parse_fen(card["cost_fen"])
        line(plan, PENDING[card["asset_type"]], cost, debit=True)
        cost_lot(plan, "cost:" + card["asset_id"], card["asset_id"], cost, category="asset")
    if data["type"] == "direct":
        obligation(plan, "primary", data["supplier_id"], amount)
    elif data["type"] == "project_completion":
        if project_sources(fact, context, plan) != amount:
            reject("完工消耗的项目成本合计必须等于资产原价。", "invalid_input")
    else:
        for creditor in sorted(creditors, key=lambda item: item["person_id"]):
            obligation(
                plan,
                "reimbursement:" + creditor["person_id"],
                creditor["person_id"],
                parse_fen(creditor["amount_fen"]),
                account="2241",
            )
    return plan
