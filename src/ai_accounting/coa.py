from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, LaborRemunerationTaxPolicyVersion, Organization, TaxRule

DEFAULT_ACCOUNTS = [
    ("1001", "库存现金", "asset", "debit", "cash"),
    ("1002", "银行存款", "asset", "debit", "bank"),
    ("1122", "应收账款", "asset", "debit", "accounts_receivable"),
    ("1221", "其他应收款", "asset", "debit", "employee_receivable"),
    ("2202", "应付账款", "liability", "credit", "accounts_payable"),
    ("2203", "合同负债及预收款", "liability", "credit", "contract_liability"),
    ("222101", "应交增值税", "liability", "credit", "vat_payable"),
    ("222102", "应交附加税费", "liability", "credit", "surtax_payable"),
    ("224101", "其他应付款—员工", "liability", "credit", "employee_payable"),
    ("2241", "其他应付款—股东", "liability", "credit", "owner_payable"),
    ("3001", "实收资本", "equity", "credit", "paid_in_capital"),
    ("5001", "主营业务收入", "revenue", "credit", "service_revenue"),
    ("5403", "税金及附加", "expense", "debit", "taxes_and_surcharges"),
    ("5602", "管理费用", "expense", "debit", "general_expense"),
    ("5603", "财务费用", "expense", "debit", "finance_expense"),
    ("6301", "营业外收入", "revenue", "credit", "tax_relief_income"),
    ("560201", "管理费用—职工薪酬", "expense", "debit", "payroll_management_expense"),
    ("560101", "销售费用—职工薪酬", "expense", "debit", "payroll_sales_expense"),
    ("540101", "主营业务成本—职工薪酬", "expense", "debit", "payroll_service_cost"),
    ("221101", "应付职工薪酬—工资", "liability", "credit", "employee_salary_payable"),
    ("221102", "应付职工薪酬—单位社保", "liability", "credit", "employer_social_payable"),
    (
        "221103",
        "应付职工薪酬—单位住房公积金",
        "liability",
        "credit",
        "employer_housing_fund_payable",
    ),
    (
        "224102",
        "其他应付款—代扣个人社保",
        "liability",
        "credit",
        "withheld_employee_social_payable",
    ),
    (
        "224103",
        "其他应付款—代扣个人住房公积金",
        "liability",
        "credit",
        "withheld_employee_housing_fund_payable",
    ),
    ("222103", "应交个人所得税", "liability", "credit", "individual_income_tax_payable"),
    ("224104", "其他应付款—个人劳务报酬", "liability", "credit", "labor_remuneration_payable"),
    ("560204", "管理费用—个人劳务", "expense", "debit", "labor_management_expense"),
    ("560104", "销售费用—个人劳务", "expense", "debit", "labor_sales_expense"),
    ("540104", "主营业务成本—个人劳务", "expense", "debit", "labor_service_cost"),
    ("1604", "在建工程—待启用固定资产", "asset", "debit", "fixed_asset_pending"),
    ("1601", "固定资产", "asset", "debit", "fixed_asset_cost"),
    ("1602", "累计折旧", "asset", "credit", "accumulated_depreciation"),
    (
        "560202",
        "管理费用—固定资产折旧",
        "expense",
        "debit",
        "management_depreciation_expense",
    ),
    (
        "560102",
        "销售费用—固定资产折旧",
        "expense",
        "debit",
        "sales_depreciation_expense",
    ),
    (
        "540102",
        "主营业务成本—固定资产折旧",
        "expense",
        "debit",
        "service_cost_depreciation",
    ),
    ("1606", "固定资产清理", "asset", "debit", "fixed_asset_clearance"),
    ("630101", "营业外收入—固定资产处置", "revenue", "credit", "fixed_asset_disposal_gain"),
    ("571101", "营业外支出—固定资产处置", "expense", "debit", "fixed_asset_disposal_loss"),
    ("1701", "无形资产", "asset", "debit", "intangible_asset_cost"),
    ("1702", "累计摊销", "asset", "credit", "accumulated_amortization"),
    (
        "560203",
        "管理费用—无形资产摊销",
        "expense",
        "debit",
        "management_amortization_expense",
    ),
    (
        "560103",
        "销售费用—无形资产摊销",
        "expense",
        "debit",
        "sales_amortization_expense",
    ),
    (
        "540103",
        "主营业务成本—无形资产摊销",
        "expense",
        "debit",
        "service_cost_amortization",
    ),
    (
        "571102",
        "营业外支出—无形资产报废",
        "expense",
        "debit",
        "intangible_asset_retirement_loss",
    ),
    ("2001", "短期借款", "liability", "credit", "short_term_borrowing"),
    ("2501", "长期借款", "liability", "credit", "long_term_borrowing"),
    ("2601", "应付利息", "liability", "credit", "interest_payable"),
    ("560301", "财务费用—利息", "expense", "debit", "borrowing_interest_expense"),
]


TAX_RULES = [
    {
        "code": "small_scale_vat_2026_2027",
        "jurisdiction": "CN",
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2027, 12, 31),
        "version": "2026.1",
        "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247426/content.html",
        "parameters": {
            "monthly_threshold_fen": 10_000_000,
            "quarterly_threshold_fen": 30_000_000,
            "standard_rate_percent": "3",
            "reduced_rate_percent": "1",
            "threshold_operator": "strictly_below",
        },
    },
    {
        "code": "small_scale_surtax_2023_2027",
        "jurisdiction": "CN",
        "effective_from": date(2023, 1, 1),
        "effective_to": date(2027, 12, 31),
        "version": "2023.12",
        "source_url": "https://www.mof.gov.cn/jrttts/202308/t20230802_3899936.htm",
        "parameters": {
            "small_tax_reduction_factor": "0.5",
            "education_surcharge_rate": "0.03",
            "local_education_surcharge_rate": "0.02",
            "basis_source_urls": [
                "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193055/content.html",
                "https://www.chinatax.gov.cn/chinatax/n810214/n810641/n2985871/c101728/c5160742/content.html",
            ],
        },
    },
    {
        "code": "small_scale_used_fixed_asset_vat_2026",
        "jurisdiction": "CN",
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
        "version": "2026.1",
        "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html",
        "parameters": {
            "tax_inclusive_base_rate_percent": "3",
            "effective_levy_rate_percent": "2",
            "calculation": "tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%",
        },
    },
]


LABOR_REMUNERATION_TAX_POLICIES = [
    {
        "code": "cn_resident_labor_remuneration_withholding",
        "version": "2019.1",
        "effective_from": date(2019, 1, 1),
        "effective_to": None,
        "primary_source_url": "https://12366.chinatax.gov.cn/bzds/070/070-5-4.html",
        "invoice_withholding_source_url": (
            "https://zhejiang.chinatax.gov.cn/art/2025/3/25/art_13314_634526.html"
        ),
        "legal_filing_source_url": (
            "https://www.chinatax.gov.cn/n810219/n810744/n3752930/n3752974/c3970366/content.html"
        ),
        "parameters": {
            "small_payment_threshold_fen": 400_000,
            "fixed_expense_deduction_fen": 80_000,
            "large_payment_expense_rate": "0.20",
            "withholding_brackets": [
                {
                    "upper_taxable_income_fen": 2_000_000,
                    "rate": "0.20",
                    "quick_deduction_fen": 0,
                },
                {
                    "upper_taxable_income_fen": 5_000_000,
                    "rate": "0.30",
                    "quick_deduction_fen": 200_000,
                },
                {
                    "upper_taxable_income_fen": None,
                    "rate": "0.40",
                    "quick_deduction_fen": 700_000,
                },
            ],
            "rounding": "half_up_to_fen",
            "filing_due_rule": "day_15_of_following_month",
            "student_internship_method_supported": False,
        },
    }
]


def seed_organization(
    session: Session,
    *,
    name: str,
    filing_cycle: str = "quarterly",
    jurisdiction: str = "CN",
    urban_maintenance_rate: Decimal = Decimal("0.07"),
    org_id: uuid.UUID | None = None,
    accounting_period_control_enabled: bool = True,
) -> Organization:
    organization = Organization(
        id=org_id or uuid.uuid4(),
        name=name,
        filing_cycle=filing_cycle,
        jurisdiction=jurisdiction,
        urban_maintenance_rate=urban_maintenance_rate,
        accounting_period_control_enabled=accounting_period_control_enabled,
    )
    session.add(organization)
    session.flush()
    for code, account_name, category, normal_side, system_role in DEFAULT_ACCOUNTS:
        session.add(
            Account(
                org_id=organization.id,
                code=code,
                name=account_name,
                category=category,
                normal_side=normal_side,
                system_role=system_role,
            )
        )
    for rule_data in TAX_RULES:
        existing = session.scalar(
            select(TaxRule).where(
                TaxRule.code == rule_data["code"], TaxRule.version == rule_data["version"]
            )
        )
        if existing is None:
            session.add(TaxRule(**rule_data))
    for policy_data in LABOR_REMUNERATION_TAX_POLICIES:
        existing_policy = session.scalar(
            select(LaborRemunerationTaxPolicyVersion).where(
                LaborRemunerationTaxPolicyVersion.code == policy_data["code"],
                LaborRemunerationTaxPolicyVersion.version == policy_data["version"],
            )
        )
        if existing_policy is None:
            session.add(LaborRemunerationTaxPolicyVersion(**policy_data))
    session.flush()
    return organization


def get_account_by_role(session: Session, org_id: uuid.UUID, role: str) -> Account:
    account = session.scalar(
        select(Account).where(
            Account.org_id == org_id, Account.system_role == role, Account.active.is_(True)
        )
    )
    if account is None:
        raise ValueError(f"missing active account mapping for system role: {role}")
    return account


def get_account_by_code(session: Session, org_id: uuid.UUID, code: str) -> Account:
    account = session.scalar(
        select(Account).where(
            Account.org_id == org_id, Account.code == code, Account.active.is_(True)
        )
    )
    if account is None:
        raise ValueError(f"unknown or inactive account code: {code}")
    return account
