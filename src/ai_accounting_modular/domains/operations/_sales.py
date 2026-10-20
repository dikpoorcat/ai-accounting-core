"""Revenue and contractual advances share one exact inclusive-tax calculation."""

from ai_accounting_modular.contracts.public import ContractError, format_fen

from ._common import amount, base_plan, issue, line, obligation, reject
from ._current_tax import adjustments, adopt_adjustments, apply_current_sales
from ._tax_lifecycle import adopt_details, resolution, tax_details, totals
from ._vat import _check_tax_day, _policy_reference


def service_sale(fact, context):
    data, gross = fact["data"], amount(fact["data"], "gross_fen")
    if "cooperation_id" in data:
        reject("合作按份销售须接入合同份额核验后方可办理。", "unsupported_business")
    resolved, source, details = tax_details(fact, context, data["vat"], data["tax_point"], gross)
    net, tax, current_net, current_tax = totals(details, fact["period"])
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
        gross,
        "1122",
        cashflow="customer_receipts",
    )
    apply_current_sales(plan, details, changes)
    return plan


def advance(fact, context):
    data, gross = fact["data"], amount(fact["data"])
    customer = data["side"] == "customer"
    check_advance_tax_status(fact)
    net, tax, details, resolved, source = gross, 0, [], None, None
    if customer:
        if "tax_point" in data:
            resolved, source = resolution(fact, context, data["tax_point"], gross)
            current_due = any(
                segment["state"] == "due" and segment["tax_period"] <= fact["period"]
                for segment in resolved["segments"]
            )
            if current_due != (data["tax_status"] == "due"):
                problem = issue(fact, "/data/tax_status", "预收状态与正式税点范围矛盾。")
                problem["code"] = "inconsistent_fact"
                raise ContractError(
                    "invalid_input", "预收税务状态与税点矛盾。", fact_issues=[problem]
                )
            if "vat" in data:
                resolved, source, details = tax_details(
                    fact, context, data["vat"], data["tax_point"], gross, include_not_due=False
                )
                _, _, _, tax = totals(details, fact["period"])
                net = gross - tax
        elif "vat" in data:
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


def check_advance_tax_status(fact):
    data = fact["data"]
    if data["side"] != "customer" or "tax_point" not in data:
        return
    point = data["tax_point"]
    if point["type"] == "derived":
        return
    period = point["tax_period"]
    _check_tax_day(fact, point)
    not_due = data["tax_status"] == "not_due"
    if (not_due and period <= fact["period"]) or (not not_due and period > fact["period"]):
        problem = issue(fact, "data/tax_status", "预收税务状态与已给明确税点月份矛盾。")
        problem["code"] = "inconsistent_fact"
        raise ContractError("invalid_input", "预收税务状态与明确税点矛盾。", fact_issues=[problem])
