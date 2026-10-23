"""Shared display meanings from exact formal contents, without reads or accounting math."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import ResourceDefinition
from ai_accounting_modular.ledger.public import (
    AdoptedContent,
    AdoptedOriginProjection,
    ObligationDirectoryItem,
)


@dataclass(frozen=True)
class BusinessLabel:
    code: str
    title: str


@dataclass(frozen=True)
class ObligationFields:
    """Only the actual resource fields used to describe an obligation."""

    reference: dict
    direction: str
    category: str
    business_period: str
    party_id: str | None


def obligation_fields(resource):
    if isinstance(resource, ObligationFields):
        return resource
    if isinstance(resource, ObligationDirectoryItem):
        return ObligationFields(
            resource.reference,
            resource.direction,
            resource.category,
            resource.business_period,
            resource.party_id,
        )
    return ObligationFields(
        resource.reference,
        resource.value["direction"],
        resource.value["category"],
        resource.value["business_period"],
        resource.value.get("party_id"),
    )


def origin_fields(content):
    """Finite meaning readers accept projections; amount/date readers still require full bodies."""
    if isinstance(content, AdoptedOriginProjection):
        return content.target_fields, content.basis_fields
    return content.target, content.basis


def business_fund_accounts(content):
    """Exact accounts displayed by whole-business progress, from its adopted input."""
    target, basis = origin_fields(content)
    kind, data = target["kind"], basis.get("input", {})
    if kind in {
        "money.movement",
        "financing.owner_funding",
        "financing.loan_drawdown",
        "investment.cooperation_funding",
        "investment.cooperation_distribution",
    }:
        return {data["funds_account_id"]} if "funds_account_id" in data else set()
    if kind == "money.transfer" and data["type"] == "physical":
        return {data["from_account_id"], data["to_account_id"]}
    return set()


def business_matter(
    content: AdoptedContent | AdoptedOriginProjection,
    resource: ResourceDefinition | ObligationDirectoryItem | ObligationFields | None = None,
) -> dict:
    """A typed product, project or contract identity; never a display-name grouping rule."""
    target, basis = origin_fields(content)
    kind, data = target["kind"], basis.get("input", {})
    values = target.get("values", {})
    if kind in {"investment.money_fund_purchase", "investment.money_fund_redemption"}:
        return {"fund_id": values["fund_id"]}
    if kind.startswith("investment.cooperation_") and "contract_id" in values:
        return {"contract_id": values["contract_id"]}
    if kind == "financing.loan_drawdown":
        return {"contract_id": values["contract_id"]}
    if kind == "financing.interest_period":
        contracts = {
            item["contract_id"]
            for item in content.basis["adopted_quantities"]
            if item["type"] == "loan_terms"
        }
        return {"contract_id": next(iter(contracts))} if len(contracts) == 1 else {}
    if kind in {"assets.project_cost", "assets.project_release"}:
        return {"project_id": values["project_id"]}
    if kind == "assets.disposal":
        return {"asset_id": values["asset_id"]}
    if kind == "assets.acquisition":
        return {"acquisition_id": content.reference["business_id"]}
    if kind.startswith("operations.") and "cooperation_id" in data:
        return {"contract_id": data["cooperation_id"]}
    if kind in {"operations.advance_fulfillment", "operations.advance_refund"}:
        return {"advance_id": data["advance_id"]}
    if kind == "operations.sale_return":
        return {"sale_id": data["sale_id"]}
    if kind == "operations.expense_recovery":
        return {"expense_id": data["expense_id"]}
    if kind == "operations.pass_through_return":
        return {"pass_through_id": data["pass_through_id"]}
    if kind == "operations.overpayment":
        return {
            "source_business_id": data["source"]["business_id"],
            "source_slot": data["source"]["slot"],
        }
    if kind == "opening.package" and resource is not None:
        return {
            "opening_business_id": resource.reference["business_id"],
            "opening_slot": resource.reference["slot"],
        }
    return {}


_KINDS = {
    "operations.service_sale": ("service_sale", "服务业务款"),
    "operations.sale_return": ("sale_return", "销售退款"),
    "operations.expense_recovery": ("expense_recovery", "费用退回款"),
    "operations.deposit": ("deposit", "押金"),
    "operations.advance_fulfillment": ("advance_fulfillment", "预收预付款履约"),
    "operations.advance_refund": ("advance_refund", "预收预付款退回"),
    "operations.pass_through": ("pass_through", "代收代付款"),
    "operations.pass_through_return": ("pass_through_return", "代收代付款返还"),
    "operations.tax_release": ("tax_release", "确认本月已到税点税额"),
    "financing.loan_contract": ("loan_contract", "借款合同"),
    "financing.loan_drawdown": ("loan_drawdown", "借款到账"),
    "financing.interest_period": ("interest", "借款利息"),
    "investment.money_fund_purchase": ("money_fund_purchase", "货币基金申购"),
    "investment.money_fund_redemption": ("money_fund_redemption", "货币基金赎回"),
    "investment.cooperation_contract": ("cooperation_contract", "合作投资合同"),
    "investment.cooperation_funding": ("cooperation_funding", "合作投资出资"),
    "investment.cooperation_collection": ("cooperation_collection", "合作项目代收款"),
    "investment.cooperation_payment": ("cooperation_payment", "合作项目代付款"),
    "investment.cooperation_operator_transfer": ("cooperation_operator_transfer", "合作经营者转款"),
    "investment.cooperation_distribution": ("cooperation_distribution", "合作投资分回"),
    "investment.cooperation_exit": ("cooperation_exit", "合作投资退出"),
    "assets.acquisition": ("asset_acquisition", "资产购置"),
    "assets.activation": ("asset_activation", "资产启用"),
    "assets.project_cost": ("project_cost", "项目成本"),
    "assets.project_release": ("project_release", "项目成本转出"),
    "assets.disposal": ("asset_disposal", "资产处置"),
    "assets.consumption": ("asset_consumption", "资产折旧与摊销"),
    "investment.cooperation_position": ("cooperation_position", "合作投资位置调整"),
    "taxes.tax_credit": ("tax_credit", "税费退抵款"),
}
_RESOURCE = {
    "social": ("social", "社保费"),
    "housing": ("housing", "住房公积金"),
    "withholding": ("withholding", "代扣个人所得税"),
    "loan_principal": ("loan_principal", "借款本金"),
    "interest": ("interest", "借款利息"),
}
_EXPENSES = {
    "administration": ("administration_expense", "管理费用"),
    "sales": ("sales_expense", "销售费用"),
    "service": ("service_expense", "服务业务成本"),
    "bank_fee": ("bank_fee", "银行手续费"),
    "tax_late_fee": ("tax_late_fee", "税收滞纳金"),
    "social_contribution_late_fee": ("social_late_fee", "社保滞纳金"),
}
_REMUNERATION = {
    "wage": ("wage", "员工工资"),
    "bonus": ("bonus", "员工奖金"),
    "labor": ("labor", "劳务报酬"),
}


def business_label(
    content: AdoptedContent | AdoptedOriginProjection,
    resource: ResourceDefinition | ObligationDirectoryItem | ObligationFields | None = None,
) -> BusinessLabel:
    """A resource may refine a multi-purpose source, such as wages containing social dues."""
    target, basis = origin_fields(content)
    kind, data = target["kind"], basis.get("input", {})
    light = isinstance(resource, ObligationFields | ObligationDirectoryItem)
    category = (resource.category if light else resource.value["category"]) if resource else None
    if category in _RESOURCE:
        return BusinessLabel(*_RESOURCE[category])
    if kind == "operations.overpayment":
        if resource is not None:
            return (
                BusinessLabel("overpayment_recovery", "多付款追收")
                if (resource.direction if light else resource.value["direction"]) == "receivable"
                else BusinessLabel("overreceipt_refund", "多收款退回")
            )
        return BusinessLabel(
            *{
                "overpayment_recovery": ("overpayment_recovery", "多付款追收"),
                "overreceipt_refund": ("overreceipt_refund", "多收款退回"),
            }.get(
                target.get("values", {}).get("treatment"),
                ("excess_settlement", "超额收付款处理"),
            )
        )
    if kind == "financing.owner_funding":
        return BusinessLabel(
            *{
                "loan": ("owner_loan", "股东借入款"),
                "capital": ("owner_capital", "股东实缴出资"),
            }.get(data.get("funding_kind"), ("unknown", "股东资金用途未明确"))
        )
    if kind == "people.remuneration":
        return BusinessLabel(*_REMUNERATION.get(data.get("type"), ("remuneration", "人员报酬")))
    if kind == "operations.expense":
        code, title = _EXPENSES.get(data.get("expense_class"), ("expense", "经营费用"))
        return BusinessLabel(code, title + ("报销" if category == "reimbursement" else ""))
    if kind == "operations.advance":
        return BusinessLabel(
            *{
                "customer": ("customer_advance", "客户预收款"),
                "supplier": ("supplier_advance", "供应商预付款"),
            }.get(data.get("side"), ("advance", "预收预付款"))
        )
    if kind == "operations.reimbursement":
        return BusinessLabel(
            *{
                "personal_advance": ("personal_advance", "个人垫付款"),
                "personal_payment": ("personal_advance", "个人垫付款"),
                "acceptance": ("reimbursement", "费用报销"),
            }.get(data.get("type"), ("reimbursement", "垫付报销款"))
        )
    if kind == "operations.other_income":
        return BusinessLabel(
            *{
                "bank_interest": ("bank_interest", "银行利息收入"),
                "company_reward": ("company_reward", "公司奖励收入"),
                "retained_verification": ("retained_verification", "账户验证收入"),
            }.get(data.get("income_kind"), ("other_income", "其他收入"))
        )
    if kind in {"taxes.assessment", "taxes.actual_declaration"}:
        if category == "tax" and resource.reference["slot"] == "surtax":
            return BusinessLabel("surtax", "增值税附加税费")
        return BusinessLabel(
            *{
                "vat": ("vat", "增值税"),
                "income_tax": ("income_tax", "企业所得税"),
                "payroll": ("withholding", "代扣个人所得税"),
            }.get(data.get("type"), ("tax", "税费"))
        )
    if kind == "money.movement":
        return BusinessLabel(
            *{
                "settlement": ("settlement", "收付款"),
                "managed_reserve": ("managed_reserve", "代管备用金费用"),
                "payroll_reserve": ("payroll_reserve", "工资代发及备用金费用"),
            }.get(data.get("type"), ("unknown", "资金用途未明确"))
        )
    if kind == "money.transfer":
        return (
            BusinessLabel("internal_transfer", "公司内部划转")
            if data.get("type") == "physical"
            else BusinessLabel("offset", "往来抵销")
        )
    if kind == "opening.package" and category is not None:
        return BusinessLabel("opening_obligation", "期初往来款")
    return BusinessLabel(*_KINDS.get(kind, ("unknown", "业务用途未明确")))


def settlement_label(
    content: AdoptedContent | AdoptedOriginProjection,
    source_labels: Sequence[BusinessLabel],
    direction: Literal["inflow", "outflow"],
) -> BusinessLabel:
    """Describe actual allocation purposes; no amount or ownership is inferred from a label."""
    label = business_label(content)
    if origin_fields(content)[0]["kind"] != "money.movement":
        return label
    verb = "支付" if direction == "outflow" else "收到"
    if label.code == "managed_reserve":
        return BusinessLabel(label.code, verb + label.title)
    if label.code == "payroll_reserve":
        return BusinessLabel(label.code, "支付工资代发及备用金费用")
    unique = tuple(dict.fromkeys(source_labels))
    if not unique:
        return BusinessLabel(
            "unknown", "付款用途未明确" if direction == "outflow" else "收款用途未明确"
        )
    titles = [item.title if item.code != "unknown" else "其他已登记款项" for item in unique]
    if len(unique) == 1 and unique[0].code == "loan_principal":
        verb = "归还" if direction == "outflow" else "收回"
    return BusinessLabel(
        unique[0].code if len(unique) == 1 else "mixed_settlement", verb + "、".join(titles)
    )
