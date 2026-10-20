"""Real receivables, deposits, and personal debt transfers."""

from uuid import NAMESPACE_URL, uuid5

from ai_accounting_modular.contracts.public import ContractError, format_fen

from ._common import (
    adopt_source,
    amount,
    base_plan,
    issue,
    line,
    obligation,
    referenced_result,
    reject,
    sum_amounts,
)
from ._cumulative import Position, adopt_position, inherit_cashflow


def _resource(context, ref):
    items = context.resource_definitions([ref], "obligation")
    if len(items) != 1:
        reject("当前公开债权债务不存在。")
    return items[0]


def _adopt_obligation(plan, definition):
    ob = definition.value
    adopt_source(plan, definition)
    quantity = {
        "type": "obligation",
        "source": {"business_id": ob["business_id"], "slot": ob["slot"]},
        "account_code": ob["account_code"],
        "direction": ob["direction"],
        "amount_fen": ob["amount_fen"],
    }
    if "party_id" in ob:
        quantity["party_id"] = ob["party_id"]
    plan["basis"]["adopted_quantities"].append(quantity)


def _transfer(plan, source, target, total, *, mode="transfer"):
    plan["obligation_reclassifications"].append(
        {
            "relation_id": str(
                uuid5(
                    NAMESPACE_URL,
                    "operations:"
                    + plan["business_id"]
                    + ":"
                    + source["business_id"]
                    + ":"
                    + source["slot"]
                    + ":"
                    + target["slot"],
                )
            ),
            "owner_business_id": plan["business_id"],
            "source": source,
            "target": target,
            "amount_fen": format_fen(total),
            "mode": mode,
        }
    )


def pass_through(fact, context):
    data, total = fact["data"], amount(fact["data"])
    plan = base_plan(fact, context, {"amount_fen": data["amount_fen"], "treatment": "pass_through"})
    line(plan, "122105", debit=total, party=data["payer_id"], summary="确认代收债权")
    line(plan, "224105", credit=total, party=data["beneficiary_id"], summary="确认转付责任")
    obligation(
        plan,
        "collection",
        data["payer_id"],
        "receivable",
        total,
        "122105",
        category="pass_through",
        cashflow="other_operating_receipts",
    )
    obligation(
        plan,
        "remittance",
        data["beneficiary_id"],
        "payable",
        total,
        "224105",
        category="pass_through",
    )
    return plan


def deposit(fact, context):
    data, total = fact["data"], amount(fact["data"])
    reimbursed = data["type"] == "reimbursed"
    creditor = data["employee_id"] if reimbursed else data["counterparty_id"]
    account = "224101" if reimbursed else "2202"
    plan = base_plan(fact, context, {"amount_fen": data["amount_fen"], "treatment": data["type"]})
    line(plan, "1221", debit=total, party=data["counterparty_id"], summary="确认可退押金")
    line(plan, account, credit=total, party=creditor, summary="确认押金付款责任")
    obligation(
        plan,
        "deposit",
        data["counterparty_id"],
        "receivable",
        total,
        "1221",
        category="deposit",
        cashflow="other_operating_receipts",
    )
    obligation(
        plan,
        "reimbursement" if reimbursed else "primary",
        creditor,
        "payable",
        total,
        account,
        category="reimbursement" if reimbursed else "trade",
    )
    return plan


def reimbursement(fact, context):
    data = fact["data"]
    allocations = data["allocations"] if data["type"] == "personal_payment" else data["sources"]
    total = sum_amounts(amount(x) for x in allocations)
    if data["type"] == "personal_payment":
        if fact["effective_on"][:7] != fact["period"]:
            reject("个人实际垫付日必须属于核算月。", "invalid_input")
        if total != amount(data):
            reject("原债分配合计必须等于个人实付金额。", "invalid_input")
    keys = [(x["source"]["business_id"], x["source"]["slot"]) for x in allocations]
    if len(keys) != len(set(keys)):
        reject("同一原债不能在个人垫付中重复分配。", "invalid_input")
    plan = base_plan(
        fact,
        context,
        {"amount_fen": format_fen(total), "treatment": data["type"]},
        adopt_effective_on=data["type"] == "personal_payment",
    )
    parts = []
    for index, allocation in enumerate(allocations):
        ref = allocation["source"]
        definition = _resource(context, ref)
        ob = definition.value
        if ob["direction"] != "payable" or "money" not in ob["allowed_settlements"]:
            reject("个人垫付只承接允许实际付款的公司原应付。")
        if "recipient_id" not in allocation:
            field = "allocations" if data["type"] == "personal_payment" else "sources"
            raise ContractError(
                "needs_information",
                "需要明确原债实际收款人。",
                fact_issues=[
                    issue(
                        fact,
                        f"data/{field}/{index}/recipient_id",
                        "原债实际收款人不能用垫付人代替。",
                    )
                ],
            )
        if allocation["recipient_id"] != ob.get("party_id"):
            reject("原债实际收款人必须与原债权人一致。")
        if ob["business_period"] > fact["period"]:
            reject("转债月份不能早于原公司债务成立月。")
        _adopt_obligation(plan, definition)
        allocated = amount(allocation)
        line(
            plan,
            ob["account_code"],
            debit=allocated,
            party=ob.get("party_id"),
            summary="原公司债务转由个人垫付",
        )
        _transfer(
            plan, ref, {"business_id": fact["business_id"], "slot": "reimbursement"}, allocated
        )
        if "cashflow_parts" in ob and len(ob["cashflow_parts"]) == 1:
            parts.append({"category": ob["cashflow_parts"][0]["category"], "amount_fen": allocated})
    line(plan, "2241", credit=total, party=data["payer_id"], summary="确认对垫付人的偿还责任")
    obligation(
        plan,
        "reimbursement",
        data["payer_id"],
        "payable",
        total,
        "2241",
        category="reimbursement",
        cashflow=None,
    )
    if len(parts) == len(allocations):
        grouped = {}
        for part in parts:
            grouped[part["category"]] = sum_amounts(
                (grouped.get(part["category"], 0), part["amount_fen"])
            )
        plan["obligations"][0]["cashflow_parts"] = [
            {"category": category, "amount_fen": format_fen(value)}
            for category, value in sorted(grouped.items())
        ]
    return plan


def pass_through_return(fact, context):
    data, total = fact["data"], amount(fact["data"])
    source_id = data["pass_through_id"]
    original = referenced_result(fact, context, {"business_id": source_id, "slot": "main"})
    if original.target["kind"] != "operations.pass_through":
        reject("代收退款必须引用原代收代付业务。")
    collection = _resource(context, {"business_id": source_id, "slot": "collection"})
    remittance = _resource(context, {"business_id": source_id, "slot": "remittance"})
    plan = base_plan(
        fact,
        context,
        {"amount_fen": data["amount_fen"], "treatment": "pass_through_return"},
        adopt_effective_on="effective_on" in fact,
    )
    _adopt_obligation(plan, collection)
    _adopt_obligation(plan, remittance)
    payer, beneficiary = collection.value["party_id"], remittance.value["party_id"]
    line(plan, "224105", debit=total, party=beneficiary, summary="转出未转付责任")
    line(plan, "224105", credit=total, party=payer, summary="确认对原付款人的退款责任")
    obligation(plan, "refund", payer, "payable", total, "224105", category="pass_through")
    _transfer(
        plan, remittance.reference, {"business_id": fact["business_id"], "slot": "refund"}, total
    )
    return plan


def overpayment(fact, context):
    data, total = fact["data"], amount(fact["data"])
    original = _resource(context, data["source"])
    ob = original.value
    if (
        ob["direction"] != "payable"
        or ob.get("party_id") != data["counterparty_id"]
        or ob["business_period"] > fact["period"]
    ):
        reject("超付追收必须引用不晚于本月成立、属于同一交易方的原应付。")
    plan = base_plan(
        fact,
        context,
        {"amount_fen": data["amount_fen"], "treatment": "overpayment_recovery"},
        adopt_effective_on="effective_on" in fact,
    )
    _adopt_obligation(plan, original)
    line(plan, "1221", debit=total, party=data["counterparty_id"], summary="确认实际超付追收")
    line(
        plan,
        ob["account_code"],
        credit=total,
        party=data["counterparty_id"],
        summary="重分类原债超付",
    )
    obligation(
        plan,
        "recovery",
        data["counterparty_id"],
        "receivable",
        total,
        "1221",
        category="other",
        cashflow=None,
    )
    if ob.get("cashflow_parts"):
        original_amount = amount(ob)
        end = sum_amounts((original_amount, total))
        span = Position(original_amount, end, end)
        inherit_cashflow(ob, plan["obligations"][0], span)
        adopt_position(plan, data["source"], span)
    _transfer(
        plan,
        data["source"],
        {"business_id": fact["business_id"], "slot": "recovery"},
        total,
        mode="reclassify_excess",
    )
    return plan
