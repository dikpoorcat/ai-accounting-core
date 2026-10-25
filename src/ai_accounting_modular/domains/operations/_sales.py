"""Revenue and contractual advances share one exact inclusive-tax calculation."""

from ai_accounting_modular.contracts.public import ContractError, format_fen

from ._common import amount, base_plan, issue, line, obligation, reject
from ._cooperation import COOPERATION_SETTLEMENTS, original_share, publish
from ._current_tax import adjustments, adopt_adjustments, apply_current_sales
from ._tax_lifecycle import adopt_details, measure_resolution, resolution, tax_details, totals
from ._vat import _adopt_policy, _policy, _policy_reference


def service_sale(fact, context):
    data, gross = fact["data"], amount(fact["data"], "gross_fen")
    if not gross:
        return _zero_cooperation_sale(fact, context)
    resolved, source, details = tax_details(fact, context, data["vat"], data["tax_point"], gross)
    net, tax, current_net, current_tax = totals(details, fact["period"])
    agreement = original_share(fact, context, net, side="revenue")
    changes, observed = adjustments(fact, context, resolved, details, gross)
    for change in changes.values():
        current_net -= sum(end - start for start, end in change["ranges"]) - change["tax"]
        current_tax -= change["tax"]
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": data["gross_fen"],
            "revenue_fen": format_fen(net),
            "vat_fen": format_fen(tax),
            "current_tax_net_fen": format_fen(current_net),
            "current_tax_vat_fen": format_fen(current_tax),
            "deferred_vat_fen": format_fen(tax - current_tax),
        },
    )
    adopt_details(plan, context, resolved, source, details)
    adopt_adjustments(plan, observed, context)
    line(plan, "1122", debit=gross, party=data["customer_id"], summary="确认服务应收")
    line(plan, "5001", credit=net, summary="确认服务收入")
    line(plan, "222101", credit=current_tax, summary="确认已到税点增值税")
    line(plan, "222104", credit=tax - current_tax, summary="确认尚未到税点的待转VAT")
    obligation(
        plan,
        "primary",
        data["customer_id"],
        "receivable",
        net if agreement is not None else gross,
        "1122",
        cashflow="customer_receipts",
        methods=COOPERATION_SETTLEMENTS
        if agreement is not None
        else ("money", "offset", "transfer"),
    )
    if agreement is not None:
        obligation(
            plan,
            "vat",
            data["customer_id"],
            "receivable",
            tax,
            "1122",
            cashflow="customer_receipts",
        )
        publish(
            plan, agreement, side="revenue", project=amount(data, "project_total_fen"), company=net
        )
    apply_current_sales(plan, details, changes)
    return plan


def _zero_cooperation_sale(fact, context):
    data = fact["data"]
    agreement = original_share(fact, context, 0, side="revenue")
    if agreement is None:
        reject("普通服务销售必须有实际销售金额。", "invalid_input")
    point = data["tax_point"]
    resolved, source = resolution(fact, context, point, 0)
    policy = (
        _policy(fact, context, data["vat"], point)
        if point["type"] == "explicit"
        else (_policy_reference(fact, context, data["vat"]))
    )
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": "0",
            "revenue_fen": "0",
            "vat_fen": "0",
            "current_tax_net_fen": "0",
            "current_tax_vat_fen": "0",
            "deferred_vat_fen": "0",
        },
    )
    _adopt_policy(plan, policy, context)
    adopt_details(plan, context, resolved, source, [])
    obligation(
        plan,
        "primary",
        data["customer_id"],
        "receivable",
        0,
        "1122",
        methods=COOPERATION_SETTLEMENTS,
        cashflow="customer_receipts",
    )
    obligation(
        plan, "vat", data["customer_id"], "receivable", 0, "1122", cashflow="customer_receipts"
    )
    publish(plan, agreement, side="revenue", project=amount(data, "project_total_fen"), company=0)
    return plan


def advance(fact, context):
    data, gross = fact["data"], amount(fact["data"])
    customer = data["side"] == "customer"
    tax, details, resolved, source = 0, [], None, None
    tax_status = data.get("tax_status")
    if customer:
        if "tax_point" in data:
            resolved, source = resolution(fact, context, data["tax_point"], gross)
            current_due = any(
                segment["state"] == "due" and segment["tax_period"] <= fact["period"]
                for segment in resolved["segments"]
            )
            tax_status = "due" if current_due else "not_due"
            if current_due and "vat" not in data:
                raise ContractError(
                    "needs_information",
                    "已到税点的预收需要适用VAT资料。",
                    fact_issues=[
                        issue(fact, "/data/vat", "本笔预收已到计税月份，需明确适用增值税资料。")
                    ],
                )
            if "vat" in data:
                details = measure_resolution(
                    fact, context, data["vat"], resolved, gross, include_not_due=False
                )
                _, _, _, tax = totals(details, fact["period"])
        if "vat" in data and not details:
            _policy_reference(fact, context, data["vat"])
    changes, observed = (
        adjustments(fact, context, resolved, details, gross) if details else ({}, [])
    )
    tax -= sum(change["tax"] for change in changes.values())
    net = gross - tax
    plan = base_plan(
        fact,
        context,
        {
            "side": data["side"],
            "gross_fen": data["amount_fen"],
            "advance_balance_fen": data["amount_fen"],
            "net_fen": format_fen(net),
            "vat_fen": format_fen(tax),
        },
    )
    if customer:
        plan["basis"]["input"]["tax_status"] = tax_status
        if not details:
            plan["basis"]["input"].pop("vat", None)
    party = data["counterparty_id"]
    if customer:
        line(plan, "1122", debit=gross, party=party, summary="确认客户预收收款权")
        line(plan, "2203", credit=net, party=party, summary="确认客户预收履约责任")
        line(plan, "222101", credit=tax, summary="确认预收已到税点增值税")
        obligation(
            plan, "primary", party, "receivable", gross, "1122", cashflow="customer_receipts"
        )
        obligation(
            plan,
            "advance",
            party,
            "payable",
            gross,
            "2203",
            category="advance",
            methods=("offset", "transfer"),
            cashflow=None,
        )
        if resolved is not None:
            adopt_details(plan, context, resolved, source, details)
            adopt_adjustments(plan, observed, context)
            apply_current_sales(plan, details, changes)
    else:
        line(plan, "1123", debit=gross, party=party, summary="确认供应商预付款权利")
        line(plan, "2202", credit=gross, party=party, summary="确认合同付款责任")
        cashflow = (
            "materials_services_payments" if data["purpose"] == "operating" else "asset_acquisition"
        )
        obligation(
            plan,
            "advance",
            party,
            "receivable",
            gross,
            "1123",
            category="advance",
            methods=("offset", "transfer"),
            cashflow=None,
        )
        obligation(plan, "primary", party, "payable", gross, "2202", cashflow=cashflow)
    return plan
