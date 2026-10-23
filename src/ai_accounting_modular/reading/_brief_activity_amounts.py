"""Display an explicitly defined business amount, never an arbitrary journal total."""

from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import format_fen, parse_fen


@dataclass(frozen=True)
class BusinessAmount:
    amount_fen: str | None
    label: str
    state: Literal["known", "not_applicable", "unknown"] = "known"


# These are actual business events; contracts, projections and evidence-only profiles are not.
EVENT_KINDS = frozenset(
    {
        "operations.service_sale",
        "operations.expense",
        "operations.other_income",
        "operations.advance",
        "operations.advance_fulfillment",
        "operations.sale_return",
        "operations.advance_refund",
        "operations.expense_recovery",
        "operations.pass_through",
        "operations.pass_through_return",
        "operations.deposit",
        "operations.reimbursement",
        "operations.overpayment",
        "people.remuneration",
        "assets.project_cost",
        "assets.project_release",
        "assets.acquisition",
        "assets.activation",
        "assets.disposal",
        "assets.consumption",
        "financing.owner_funding",
        "financing.loan_drawdown",
        "financing.interest_period",
        "investment.money_fund_purchase",
        "investment.money_fund_redemption",
        "investment.cooperation_funding",
        "investment.cooperation_collection",
        "investment.cooperation_payment",
        "investment.cooperation_operator_transfer",
        "investment.cooperation_distribution",
        "investment.cooperation_exit",
        "money.movement",
        "money.transfer",
        "taxes.assessment",
        "taxes.tax_credit",
    }
)


def amount(content):
    kind, values = content.target["kind"], content.target.get("values", {})
    match kind:
        case "operations.service_sale" | "operations.advance" | "operations.advance_fulfillment":
            value, label = values["gross_fen"], "业务金额"
        case "operations.sale_return" | "operations.advance_refund":
            value, label = values["gross_fen"], "退回金额"
        case "operations.expense" | "operations.other_income" | "operations.deposit":
            value, label = values["amount_fen"], "业务金额"
        case "operations.expense_recovery" | "operations.pass_through_return":
            value, label = values["amount_fen"], "退回金额"
        case "operations.tax_release":
            value, label = values["vat_fen"], "转入应交增值税金额"
        case "operations.pass_through" | "operations.reimbursement" | "operations.overpayment":
            value, label = values["amount_fen"], "业务金额"
        case "people.remuneration":
            value, label = values["gross_fen"], "应发金额"
        case "assets.project_cost" | "assets.project_release":
            value, label = values["amount_fen"], "成本金额"
        case "assets.acquisition":
            value, label = values["total_cost_fen"], "购置成本"
        case "assets.activation":
            value = format_fen(sum(parse_fen(item["cost_fen"]) for item in values["assets"]))
            label = "启用资产成本"
        case "assets.disposal":
            value, label = values["gross_proceeds_fen"], "处置价款"
        case "assets.consumption":
            value, label = values["total_amount_fen"], "折旧摊销金额"
        case "financing.owner_funding" | "investment.cooperation_funding":
            value, label = values["amount_fen"], "实际金额"
        case "financing.loan_drawdown":
            value, label = values["principal_fen"], "借款本金"
        case "financing.interest_period":
            value, label = values["interest_fen"], "利息金额"
        case "investment.money_fund_purchase":
            value, label = values["cost_fen"], "申购成本"
        case "investment.money_fund_redemption":
            value, label = values["net_proceeds_fen"], "赎回价款"
        case "investment.cooperation_collection" | "investment.cooperation_payment":
            value, label = values["company_amount_fen"], "代公司清偿金额"
        case "investment.cooperation_distribution":
            value, label = values["amount_fen"], "实际金额"
        case "investment.cooperation_operator_transfer":
            return BusinessAmount(None, "无单一公司金额", "not_applicable")
        case "investment.cooperation_position" | "investment.cooperation_exit" | "opening.package":
            return BusinessAmount(None, "无单一业务金额", "not_applicable")
        case "money.movement" | "money.transfer":
            value, label = values["amount_fen"], "实际金额"
        case "taxes.assessment":
            if values["type"] == "vat":
                value = format_fen(
                    parse_fen(values["payable_vat_fen"]) + parse_fen(values["surtax_fen"])
                )
            else:
                value = values["change_fen"]
            label = "确认税费"
        case "taxes.tax_credit":
            value = format_fen(
                parse_fen(values["vat_credit_fen"]) + parse_fen(values["surtax_credit_fen"])
            )
            label = "税费退抵金额"
        case _:
            return BusinessAmount(None, "业务金额", "unknown")
    return BusinessAmount(value, label)


def signed(value, sign):
    return None if value is None else format_fen(parse_fen(value) * sign)


def recognition(content):
    day = content.basis.get("effective_on")
    if content.target["kind"] in {
        "investment.cooperation_funding",
        "investment.cooperation_distribution",
    }:
        day = content.target["values"]["actual_on"]
    return {
        "precision": "day" if day else "month",
        "period": content.basis["period"],
        "date": day,
        "label": day or content.basis["period"] + " 按月确认",
    }
