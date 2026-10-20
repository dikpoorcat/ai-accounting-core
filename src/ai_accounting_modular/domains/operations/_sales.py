"""Revenue and contractual advances share one exact inclusive-tax calculation."""

import calendar
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import (
    ContractError,
    format_fen,
    parse_rate,
)

from ._common import (
    adopt_source,
    amount,
    base_plan,
    checked_amount,
    issue,
    line,
    obligation,
    reject,
)


def _policy_reference(fact, context, vat):
    policy = context.get_fact(vat["policy_id"])
    if policy is None:
        raise ContractError(
            "needs_information",
            "需要所引用的增值税政策资料。",
            fact_issues=[issue(fact, "data/vat/policy_id", "所引用VAT政策尚不存在。")],
        )
    if policy["kind"] != "taxes.vat_policy":
        problem = issue(fact, "data/vat/policy_id", "所引用业务不是增值税政策。")
        problem["code"] = "incompatible_policy_kind"
        raise ContractError("invalid_input", "VAT政策引用的业务类型不正确。", fact_issues=[problem])
    return policy


def _check_tax_day(fact, point):
    if "tax_on" in point and point["tax_on"][:7] != point["tax_period"]:
        problem = issue(fact, "data/tax_point/tax_on", "已给税点日不属于明确的税月。")
        problem["code"] = "inconsistent_fact"
        raise ContractError("invalid_input", "税点日与税月矛盾。", fact_issues=[problem])


def _policy(fact, context, vat, tax_point):
    if tax_point["type"] != "explicit":
        reject("详细税点须接入税费领域公开结果后办理。", "unsupported_business")
    policy = _policy_reference(fact, context, vat)
    _check_tax_day(fact, tax_point)
    month, actual = tax_point["tax_period"], tax_point.get("tax_on")
    identity = policy["data"]["policy"]
    last = f"{month}-{calendar.monthrange(int(month[:4]), int(month[5:]))[1]:02d}"
    start, end = (actual, actual) if actual else (month + "-01", last)
    if start < identity["effective_from"] or (
        identity.get("effective_to") and end > identity["effective_to"]
    ):
        if actual:
            problem = issue(fact, "data/vat/policy_id", "已给真实税点日不属于该VAT政策有效期间。")
            problem["code"] = "policy_not_effective"
            raise ContractError(
                "unsupported_policy", "所选VAT政策不适用于已给税点日。", fact_issues=[problem]
            )
        raise ContractError(
            "needs_information",
            "税点精度不足以选择有效政策。",
            fact_issues=[
                issue(
                    fact,
                    "data/tax_point/tax_on",
                    "需提供足以选择有效政策的真实税点日期。",
                    precision=("day",),
                )
            ],
        )
    params = policy["data"]
    if vat["invoice_kind"] == "special":
        for field in ("waive_exemption", "waive_rate_reduction"):
            if vat[field] and not params["special_invoice_may_" + field]:
                reject("该增值税政策不允许专用发票采用所提交的放弃优惠选项。", "unsupported_policy")
    if parse_rate(params["payable_rate"]) > parse_rate(params["tax_base_rate"]):
        reject("VAT实纳率不能超过计税基础率。", "unsupported_policy")
    return policy


def _split(gross, params, vat):
    base = parse_rate(params["tax_base_rate"])
    rate = base if vat["waive_rate_reduction"] else parse_rate(params["payable_rate"])
    if rate != base:
        reject("计税基础率与实纳率不同的减征须明确其正式差额处理规则。", "unsupported_policy")
    with localcontext() as context:
        context.prec = 80
        net = checked_amount(
            int((Decimal(gross) / (1 + base)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        )
        tax = gross - net
    return net, tax


def _adopt_policy(plan, policy, context):
    source = context.get_result(policy["business_id"], "main")
    if source is None:
        reject("增值税政策的正式采用结果尚未取得。")
    adopt_source(plan, source)
    plan["basis"]["policies"].append(
        {
            "policy_business_id": policy["business_id"],
            "policy_kind": "taxes.vat_policy",
            "parameters": deepcopy(policy["data"]),
        }
    )
    plan["rule_refs"][0]["policy_business_ids"] = [policy["business_id"]]
    plan["rule_refs"][0]["official_urls"] = [policy["data"]["policy"]["official_url"]]


def _tax_sale(plan, fact, policy, vat, net, tax, gross):
    plan["tax_sales"].append(
        tax_sale_values(
            fact["business_id"],
            fact["data"]["tax_point"]["tax_period"],
            policy["data"],
            vat,
            net,
            tax,
            gross,
        )
    )


def tax_sale_values(business_id, period, params, vat, net, tax, gross):
    return {
        "source_business_id": business_id,
        "tax_period": period,
        "net_sales_fen": format_fen(net),
        "vat_fen": format_fen(tax),
        "nominal_rate": params["nominal_rate"],
        "tax_base_rate": params["tax_base_rate"],
        "payable_rate": params["tax_base_rate"]
        if vat["waive_rate_reduction"]
        else params["payable_rate"],
        "invoice_kind": vat["invoice_kind"],
        "exemption_eligible": vat["exemption_eligible"] and not vat["waive_exemption"],
        "filing_bucket": "standard",
        "ranges": [{"from_fen": "0", "to_fen": format_fen(gross)}],
    }


def service_sale(fact, context):
    data, gross = fact["data"], amount(fact["data"], "gross_fen")
    if "cooperation_id" in data:
        reject("合作按份销售须接入合同份额核验后方可办理。", "unsupported_business")
    if data["tax_point"]["type"] == "explicit" and data["tax_point"]["tax_period"] > fact["period"]:
        reject("未来税点的待转税额和税销售接续尚未完整接入。", "unsupported_business")
    policy = _policy(fact, context, data["vat"], data["tax_point"])
    if data["tax_point"]["tax_period"] < fact["period"]:
        reject("税点早于收入确认必须承接原预收税务来源，不能重新确认一次。")
    net, tax = _split(gross, policy["data"], data["vat"])
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": data["gross_fen"],
            "revenue_fen": format_fen(net),
            "vat_fen": format_fen(tax),
            "current_tax_net_fen": format_fen(net),
            "current_tax_vat_fen": format_fen(tax),
            "deferred_vat_fen": "0",
        },
    )
    _adopt_policy(plan, policy, context)
    line(plan, "1122", debit=gross, party=data["customer_id"], summary="确认服务应收")
    line(plan, "5001", credit=net, summary="确认服务收入")
    line(plan, "222101", credit=tax, summary="确认已到税点增值税")
    obligation(
        plan,
        "primary",
        data["customer_id"],
        "receivable",
        gross,
        "1122",
        cashflow="customer_receipts",
    )
    _tax_sale(plan, fact, policy, data["vat"], net, tax, gross)
    return plan


def advance(fact, context):
    data, gross = fact["data"], amount(fact["data"])
    customer = data["side"] == "customer"
    check_advance_tax_status(fact)
    net, tax, policy = gross, 0, None
    if customer and data["tax_status"] == "not_due" and "vat" in data:
        _policy_reference(fact, context, data["vat"])
    if customer and data["tax_status"] == "due":
        policy = _policy(fact, context, data["vat"], data["tax_point"])
        net, tax = _split(gross, policy["data"], data["vat"])
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
        if policy:
            _adopt_policy(plan, policy, context)
            _tax_sale(plan, fact, policy, data["vat"], net, tax, gross)
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
        reject("详细税点须接入税费领域公开结果后办理。", "unsupported_business")
    period = point["tax_period"]
    _check_tax_day(fact, point)
    not_due = data["tax_status"] == "not_due"
    if (not_due and period <= fact["period"]) or (not not_due and period > fact["period"]):
        problem = issue(fact, "data/tax_status", "预收税务状态与已给明确税点月份矛盾。")
        problem["code"] = "inconsistent_fact"
        raise ContractError("invalid_input", "预收税务状态与明确税点矛盾。", fact_issues=[problem])
    if period != fact["period"]:
        reject("非当月税点的预收税务接续尚未完整接入。", "unsupported_business")
