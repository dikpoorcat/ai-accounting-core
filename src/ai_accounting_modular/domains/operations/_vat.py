"""Versioned VAT selection and exact inclusive-tax source values."""

import calendar
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import ContractError, format_fen, parse_rate

from ._common import adopt_source, checked_amount, issue, reject, resolve_vat_choices, sum_amounts


def _policy_reference(fact, context, vat):
    policy = context.get_fact(vat["policy_id"])
    if policy is None:
        problem = issue(
            fact, "data/vat/policy_id", "已填写的VAT政策编号尚未登记，请核对编号或登记实际政策。"
        )
        problem["code"] = "reference_not_found"
        raise ContractError(
            "invalid_input", "增值税政策引用的业务编号不存在。", fact_issues=[problem]
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


def covers_month(parameters, month):
    identity = parameters["policy"]
    last = f"{month}-{calendar.monthrange(int(month[:4]), int(month[5:]))[1]:02d}"
    return month + "-01" >= identity["effective_from"] and (
        not identity.get("effective_to") or last <= identity["effective_to"]
    )


def _policy(fact, context, vat, tax_point, *, day_field="data/tax_point/tax_on", day_fact=None):
    vat = resolve_vat_choices(vat)
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
                    day_fact if day_fact is not None else fact,
                    day_field,
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


def _rates(params, vat):
    vat = resolve_vat_choices(vat)
    if vat["waive_rate_reduction"]:
        return params["nominal_rate"], params["nominal_rate"]
    return params["tax_base_rate"], params["payable_rate"]


def _split(gross, params, vat):
    base_text, rate_text = _rates(params, vat)
    base, rate = parse_rate(base_text), parse_rate(rate_text)
    if rate != base:
        reject("计税基础率与实纳率不同的减征须明确其正式差额处理规则。", "unsupported_policy")
    with localcontext() as context:
        context.prec = 80
        net = checked_amount(
            int((Decimal(gross) / (1 + base)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        )
        tax = gross - net
    return net, tax


def split_ranges(values, params, vat):
    """Use the same cumulative rounding at every original-gross boundary."""
    net = sum_amounts(
        _split(end, params, vat)[0] - _split(start, params, vat)[0] for start, end in values
    )
    gross = sum_amounts(end - start for start, end in values)
    return net, gross - net


def _adopt_policy(plan, policy, context):
    source = context.get_result(policy["business_id"], "main")
    if source is None:
        reject("增值税政策的正式采用结果尚未取得。")
    adopt_source(plan, source)
    adopted = {
        "policy_business_id": policy["business_id"],
        "policy_kind": "taxes.vat_policy",
        "parameters": deepcopy(policy["data"]),
    }
    if adopted not in plan["basis"]["policies"]:
        plan["basis"]["policies"].append(adopted)
    refs = plan["rule_refs"][0]
    refs["policy_business_ids"] = sorted(set(refs["policy_business_ids"]) | {policy["business_id"]})
    refs["official_urls"] = sorted(
        set(refs["official_urls"]) | {policy["data"]["policy"]["official_url"]}
    )


def tax_sale_values(business_id, period, params, vat, net, tax, gross):
    vat = resolve_vat_choices(vat)
    base, payable = _rates(params, vat)
    return {
        "source_business_id": business_id,
        "tax_period": period,
        "net_sales_fen": format_fen(net),
        "vat_fen": format_fen(tax),
        "nominal_rate": params["nominal_rate"],
        "tax_base_rate": base,
        "payable_rate": payable,
        "invoice_kind": vat["invoice_kind"],
        "exemption_eligible": vat["exemption_eligible"] and not vat["waive_exemption"],
        "filing_bucket": "standard",
        "ranges": [{"from_fen": "0", "to_fen": format_fen(gross)}],
    }
