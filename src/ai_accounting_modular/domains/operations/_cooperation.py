"""The operating owner exposes project shares through one public trade quantity."""

from decimal import Decimal

from ai_accounting_modular.contracts.public import ContractError, format_fen

from ._common import adopt_source, amount, issue, referenced_result, reject, sum_amounts
from ._cumulative import position
from ._overpayment_pool import claim_order, claims_for

COOPERATION_SETTLEMENTS = ("money", "offset", "transfer", "agency")


def contract_id(fact, context):
    """Only operating facts are followed; contract internals belong to investment."""
    seen = set()
    while fact is not None and fact["business_id"] not in seen:
        seen.add(fact["business_id"])
        data, kind = fact["data"], fact["kind"]
        if kind in {"operations.service_sale", "operations.expense"}:
            return data.get("cooperation_id")
        field = {
            "operations.sale_return": "sale_id",
            "operations.expense_recovery": "expense_id",
        }.get(kind)
        original_id = (
            data.get(field)
            if field
            else (data["source"]["business_id"] if kind == "operations.overpayment" else None)
        )
        fact = context.get_fact(original_id) if original_id is not None else None
    return None


def trade(result, slot=None):
    quantities = [
        item
        for item in result.basis["adopted_quantities"]
        if item["type"] == "cooperation_trade" and (slot is None or item["source"]["slot"] == slot)
    ]
    if len(quantities) > 1:
        reject("一项经营结果只能公开一项明确的合作经营份额。")
    return quantities[0] if quantities else None


def contract(fact, context, agreement_id):
    agreement = referenced_result(fact, context, {"business_id": agreement_id, "slot": "main"})
    if agreement.target["kind"] != "investment.cooperation_contract":
        reject("合作经营必须引用已采用的合作投资合同。", "invalid_input")
    if agreement.basis["input"]["profile"] != "direct_joint_operation_cash_labor_v1":
        reject("当前经营只承接明确的现金与独立经营劳务按份合作。", "unsupported_business")
    return agreement


def share_at(project, agreement):
    # Exact integer arithmetic also works at the largest permitted money amount.
    numerator, denominator = Decimal(
        agreement.target["values"]["company_revenue_expense_share"]
    ).as_integer_ratio()
    quotient, remainder = divmod(project * numerator, denominator)
    return quotient + (2 * remainder >= denominator)


def require_amount(fact, actual, expected, field):
    if actual != expected:
        problem = issue(fact, field, "公司金额必须等于合同固定份额对本次项目范围的核算结果。")
        problem["code"] = "inconsistent_fact"
        raise ContractError(
            "invalid_input", "公司金额与合作合同份额不一致。", fact_issues=[problem]
        )


def publish(plan, agreement, *, side, project, company, offset=0, slot="primary"):
    adopt_source(plan, agreement)
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "cooperation_trade",
            "contract_id": agreement.source["business_id"],
            "source": {"business_id": plan["business_id"], "slot": slot},
            "side": side,
            "project_amount_fen": format_fen(project),
            "company_net_amount_fen": format_fen(company),
            "project_range_from_fen": format_fen(offset),
        }
    )


def original_share(fact, context, actual, *, side):
    data = fact["data"]
    if "cooperation_id" not in data:
        return None
    agreement = contract(fact, context, data["cooperation_id"])
    project = amount(data, "project_total_fen")
    require_amount(
        fact,
        actual,
        share_at(project, agreement),
        "/data/gross_fen" if side == "revenue" else "/data/amount_fen",
    )
    return agreement


def returned_share(fact, context, original, members, actual, *, side):
    original_trade = trade(original)
    if original_trade is None:
        if "project_total_fen" in fact["data"]:
            reject("普通经营退回不能附加合作项目金额。", "invalid_input")
        if not actual:
            reject("普通经营退回必须有实际退回金额。", "invalid_input")
        return None
    agreement = contract(fact, context, original_trade["contract_id"])
    missing = [member for member in members if "project_total_fen" not in member["data"]]
    if missing:
        raise ContractError(
            "needs_information",
            "合作退回需要实际项目退回金额，不能由公司金额反推。",
            fact_issues=[
                issue(member, "/data/project_total_fen", "请提供实际项目退回基数。")
                for member in missing
            ],
        )
    span = position(
        fact, members, "project_total_fen", amount(original_trade, "project_amount_fen")
    )
    expected = share_at(span.after, agreement) - share_at(span.before, agreement)
    require_amount(
        fact, actual, expected, "/data/gross_fen" if side == "revenue" else "/data/amount_fen"
    )
    return agreement, span


def publish_return(plan, returned, *, side, company, slot="primary"):
    if returned is not None:
        agreement, span = returned
        publish(
            plan,
            agreement,
            side=side,
            project=-(span.after - span.before),
            company=-company,
            offset=span.before,
            slot=slot,
        )


def publish_overpayment(plan, fact, context, original, *, direction):
    """Recovery concerns old settlement; it does not reverse operating profit twice."""
    source_trade = trade(original, fact["data"]["source"]["slot"])
    if source_trade is None:
        if "project_total_fen" in fact["data"]:
            reject("普通往来追偿不能附加合作项目金额。", "invalid_input")
        return False
    data = fact["data"]
    claims = claims_for(fact, context)
    missing = [claim for claim in claims if "project_total_fen" not in claim["data"]]
    if missing:
        raise ContractError(
            "needs_information",
            "合作追偿需要实际项目追收基数。",
            fact_issues=[
                issue(claim, "/data/project_total_fen", "不能从公司追偿额反推项目金额。")
                for claim in missing
            ],
        )
    agreement = contract(fact, context, source_trade["contract_id"])
    before = sum_amounts(
        amount(claim["data"], "project_total_fen")
        for claim in claims
        if claim_order(claim) < claim_order(fact)
    )
    offset = sum_amounts(
        (
            amount(source_trade, "project_range_from_fen"),
            abs(amount(source_trade, "project_amount_fen")),
            before,
        )
    )
    project = amount(data, "project_total_fen")
    company = amount(data)
    require_amount(
        fact,
        company,
        share_at(offset + project, agreement) - share_at(offset, agreement),
        "/data/amount_fen",
    )
    source_positive = (source_trade["side"] == "revenue") == (direction == "receivable")
    sign = -1 if source_positive else 1
    publish(
        plan,
        agreement,
        side=source_trade["side"],
        project=sign * project,
        company=sign * company,
        offset=offset,
        slot="recovery",
    )
    return True
