"""Formal display matters shared by activity and outstanding-item groups.

Only adopted typed meanings identify a matter. Dates, channels, free descriptions
and accounting amounts never do; an unresolved matter remains a separate item.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Matter:
    key: str
    title: str
    category: str
    open_category: str | None = None

    def payment_title(self, direction):
        if self.key == "salary" and direction == "outflow":
            return "支付工资"
        if self.key == "pass-through-collection":
            return "代收款"
        if self.key == "pass-through-remittance":
            return "代付款"
        if direction == "inflow":
            return "客户收款" if self.key == "customer-receivable" else "收回" + self.title
        if self.category == "tax" or self.key in {"social", "housing"}:
            return "缴纳" + ("个税" if self.key == "individual-income-tax" else self.title)
        return "支付" + self.title


SALARY = Matter("salary", "实发工资", "payroll", "payroll_payables")
BONUS = Matter("bonus", "奖金", "payroll", "payroll_payables")
SOCIAL = Matter("social", "社保", "payroll", "payroll_payables")
HOUSING = Matter("housing", "公积金", "payroll", "payroll_payables")
INDIVIDUAL_TAX = Matter("individual-income-tax", "个人所得税", "tax", "tax_payables")
LABOR = Matter("labor-payment", "个人劳务款", "labor", "labor_payables")
EXPENSE_MATTERS = {
    name: Matter("expense-" + name, title, "expense_supplier", "supplier_payables")
    for name, title in {
        "administration": "管理费用",
        "sales": "销售费用",
        "service": "服务费",
        "bank_fee": "银行手续费",
        "tax_late_fee": "税收滞纳金",
        "social_contribution_late_fee": "社保滞纳金",
    }.items()
}
CUSTOMER = Matter("customer-receivable", "客户款", "income_customer", "customer_receivables")
EMPLOYEE_REIMBURSEMENT = Matter(
    "employee-reimbursement",
    "员工报销款",
    "employee_reimbursement",
    "employee_payables",
)
OWNER_REIMBURSEMENT = Matter("owner-reimbursement", "负责人报销款", "financing_owner")
TAXES = {
    "vat": Matter("value-added-tax", "增值税", "tax", "tax_payables"),
    "surtax": Matter("surtax", "附加税", "tax", "tax_payables"),
    "income_tax": Matter("corporate-income-tax", "企业所得税", "tax", "tax_payables"),
    "individual_income_tax": INDIVIDUAL_TAX,
}


def obligation_matter(kind, component, *, values=None, data=None):
    """Resolve one exact obligation, normalizing its formal representations."""
    values, data = values or {}, data or {}
    if kind == "opening_payroll_payable":
        component = data.get("component")
    if kind in {"payroll", "payroll_bounded", "annual_bonus", "opening_payroll_payable"}:
        if component in {"tax", "withheld_tax"}:
            return INDIVIDUAL_TAX
        if component in {"employee_social", "employer_social"}:
            return SOCIAL
        if component in {"employee_housing", "employer_housing"}:
            return HOUSING
        if component in {"net", "net_salary", "salary", "bonus"}:
            return BONUS if kind == "annual_bonus" or component == "bonus" else SALARY
    if kind in {"labor", "labor_accrual", "labor_project_cost"}:
        if component in {"tax", "withheld_tax"}:
            return INDIVIDUAL_TAX
        if component in {"net", "primary"}:
            return LABOR
    if kind == "opening_tax":
        tax = data.get("tax_kind")
        return TAXES.get("income_tax" if tax == "enterprise_income_tax" else tax)
    if kind == "income_tax_assessment" and component == "tax":
        return TAXES["income_tax"]
    if kind == "tax_assessment":
        if component in {"vat", "surtax"}:
            return TAXES[component]
        for tax in ("vat", "surtax"):
            if isinstance(component, str) and component.startswith(tax + "_credit_"):
                return Matter(tax + "-credit", TAXES[tax].title + "抵减与退税", "tax")
    if kind == "opening_obligation":
        nature = values.get("nature", data.get("nature"))
        if nature == "customer_receivable":
            return CUSTOMER
        if nature in {
            "supplier_service_payable",
            "supplier_administration_payable",
            "supplier_sales_payable",
        }:
            return EXPENSE_MATTERS[nature.removeprefix("supplier_").removesuffix("_payable")]
        if nature == "employee_reimbursement":
            return EMPLOYEE_REIMBURSEMENT
        if nature == "owner_reimbursement":
            return OWNER_REIMBURSEMENT
        if nature == "deposit_receivable":
            return Matter("deposit-refund", "押金", "fund_movement")
        if nature == "deposit_payable":
            return Matter("deposit-payable", "应退押金", "fund_movement")
        # Generic other receivables/payables do not specify the business matter.
        return None
    if kind == "expense" and component == "primary":
        creditor = values.get("creditor_kind", data.get("creditor_kind"))
        if creditor in {"supplier", "individual"}:
            return EXPENSE_MATTERS.get(values.get("expense_class", data.get("expense_class")))
        if creditor == "employee":
            return EMPLOYEE_REIMBURSEMENT
    if kind in {"employee_advance", "reimbursement_acceptance"}:
        payer = values.get("payer_kind", data.get("payer_kind"))
        if payer in {"employee", "owner"}:
            return EMPLOYEE_REIMBURSEMENT if payer == "employee" else OWNER_REIMBURSEMENT
    if kind in {"reimbursed_asset", "reimbursed_asset_batch"}:
        return EMPLOYEE_REIMBURSEMENT
    if kind in {"service_sale", "advance_fulfillment"} and component == "primary":
        return CUSTOMER
    if kind == "pass_through" and component in {"collection", "remittance"}:
        return Matter(
            "pass-through-" + component,
            "代收款" if component == "collection" else "代付款",
            "pass_through",
        )
    if kind in {"refundable_deposit", "reimbursed_deposit"}:
        if component == "reimbursement":
            return EMPLOYEE_REIMBURSEMENT
        if component in {"payment", "refund"}:
            return Matter("deposit-" + component, "押金", "fund_movement")
    if kind in {"loan_drawdown", "opening_loan"} and component == "principal":
        return Matter("loan-principal", "借款本金", "financing_owner")
    if kind in {"loan_interest", "opening_loan"} and component == "interest":
        return Matter("loan-interest", "借款利息", "financing_owner")
    if kind in {"advance", "asset_advance"} and component in {"advance", "primary"}:
        side = values.get("side", data.get("side"))
        if kind == "asset_advance" or side == "supplier":
            return Matter("supplier-advance", "供应商预付款", "other", "supplier_advances")
        if side == "customer":
            return Matter("customer-advance", "客户预收款", "other")
    return None


def activity_matter(kind, data, *, direction=None):
    """Name a whole recognition event without changing its shared amount rule."""
    if kind in {"payment", "cash_payment", "platform_payment", "payroll_reserve_payment"}:
        return None  # Exact payment matters are resolved from their adopted slots.
    if kind in {"funding", "cash_funding", "platform_funding"}:
        funding = data.get("funding_kind")
        if funding in {"capital", "loan"}:
            return Matter(
                "funding-" + funding,
                "股东投入" if funding == "capital" else "借款到账",
                "financing_owner",
            )
        return None
    if kind in {"payroll", "payroll_bounded"}:
        return Matter("salary-accrual", "工资计提", "payroll")
    if kind == "annual_bonus":
        return Matter("bonus-accrual", "奖金计提", "payroll")
    if kind in {"labor", "labor_accrual"}:
        return Matter("labor-accrual", "个人劳务计提", "labor")
    if kind == "opening_obligation":
        source = obligation_matter(kind, "primary", data=data)
        return (
            Matter("opening-" + source.key, source.title + "期初", source.category)
            if source
            else None
        )
    if kind in {"opening_payroll_payable", "opening_tax"}:
        source = obligation_matter(kind, data.get("component"), data=data)
        return (
            Matter("opening-" + source.key, source.title + "期初", source.category)
            if source
            else None
        )
    if kind in {"employee_advance", "reimbursement_acceptance"}:
        source = obligation_matter(kind, "primary", data=data)
        return (
            Matter("recognition-" + source.key, source.title + "确认", source.category)
            if source
            else None
        )
    if kind == "expense":
        source = obligation_matter(kind, "primary", data=data)
        return (
            Matter("recognition-" + source.key, source.title + "确认", source.category)
            if source
            else None
        )
    if kind == "advance":
        source = obligation_matter(kind, "advance", data=data)
        return source
    if kind == "bank_income":
        name = data.get("income_kind")
        title = {
            "bank_interest": "银行利息入账",
            "government_grant": "补助到账",
            "retained_verification_payment": "确认验证款收入",
            "bank_promotion_reward": "奖励到账",
        }.get(name)
        return Matter("bank-income-" + name, title, "income_customer") if title else None
    if kind == "asset_disposal":
        name = data.get("disposal_kind")
        return (
            Matter("asset-disposal-" + name, "出售资产" if name == "sale" else "报废资产", "assets")
            if name in {"sale", "scrap"}
            else None
        )
    # Explicit recognition operations retain their own formal matter. A generic
    # raw money row, noncash settlement or unspecified payable is never merged.
    from .dashboard import KIND_NAMES, _group

    if (
        kind not in {"settlement", "platform_movement", "managed_reserve_internal_movement"}
        and kind in KIND_NAMES
    ):
        return Matter(
            "recognition-" + kind,
            KIND_NAMES[kind],
            _group(
                kind,
                creditor_kind=data.get("creditor_kind"),
                nature=data.get("nature"),
                payer_kind=data.get("payer_kind"),
                direction=direction,
            ),
        )
    return None
