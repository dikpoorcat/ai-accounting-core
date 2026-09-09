from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .fact_requirements import ExternalDeclarationDate

# Monetary accounting facts are always integer fen.  ``StrictInt`` is
# intentional: JSON 12.0, ``true`` and "12" must never be silently accepted
# as a monetary value merely because they can be coerced by Python.
Fen = Annotated[StrictInt, Field(ge=0)]
PositiveFen = Annotated[StrictInt, Field(gt=0)]


BANK_TRANSACTION_REFERENCES_OPTIONAL = (
    "optional; if provided, every row must belong to the selected bank account and its signed "
    "total must exactly match the typed settlement amount and direction"
)

BANK_TRANSACTION_REFERENCES_REQUIRED = (
    "required; every row must belong to the selected bank account and its signed total must "
    "exactly match the typed receipt amount"
)


class ResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    DELETED = "deleted"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class CounterpartyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    kind: str | None = None
    name: str | None = None
    external_ref: str | None = None

    @model_validator(mode="after")
    def has_identity(self) -> CounterpartyRef:
        if not self.id and not (self.kind and self.name):
            raise ValueError("counterparty requires id or both kind and name")
        if self.kind and self.kind not in {"customer", "supplier", "employee", "owner", "other"}:
            raise ValueError("unsupported counterparty kind")
        return self


class TaxFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # These are business facts, not calculator defaults.  Optionality is
    # deliberate: the service can return ``needs_information`` with an exact
    # field path instead of turning an omitted fact into a generic schema
    # failure or silently changing the accounting treatment.
    taxable: StrictBool | None = None
    rate_percent: Decimal | None = Field(default=None, ge=0, le=100)
    invoice_type: str | None = None
    waive_exemption: StrictBool | None = None
    tax_due_on_event: StrictBool | None = None

    @model_validator(mode="after")
    def valid_invoice_type(self) -> TaxFacts:
        if self.invoice_type is not None and self.invoice_type not in {
            "ordinary",
            "special",
            "none",
        }:
            raise ValueError("invoice_type must be ordinary, special, or none")
        if self.rate_percent is not None and self.rate_percent not in {
            Decimal("0"),
            Decimal("1"),
            Decimal("3"),
        }:
            raise ValueError("phase 1 supports VAT rates 0%, 1%, and 3% only")
        if self.taxable is False and self.waive_exemption is True:
            raise ValueError("a non-taxable event cannot waive exemption")
        return self


class InvoiceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    direction: str = "output"
    invoice_type: str = "ordinary"
    issue_date: date
    gross_amount_fen: PositiveFen
    tax_amount_fen: Fen


class BankTransactionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    fingerprint: str | None = None

    @model_validator(mode="after")
    def has_reference(self) -> BankTransactionReference:
        if not self.id and not self.fingerprint:
            raise ValueError("bank transaction reference requires id or fingerprint")
        return self


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_item_id: uuid.UUID
    amount_fen: PositiveFen


class SalaryWithholdingAllocation(BaseModel):
    """Explicit non-cash deductions for one salary-payable allocation.

    A partial salary payment must state these facts explicitly.  The service never
    derives them by proportion from a payroll line.
    """

    model_config = ConfigDict(extra="forbid")

    open_item_id: uuid.UUID | None = None
    source_component_key: str | None = None
    source_event_key: str | None = Field(default=None, min_length=1, max_length=200)
    source_open_item_key: str = "primary"
    employee_social_insurance_items: dict[str, Fen] = Field(default_factory=dict)
    employee_housing_fund_items: dict[str, Fen] = Field(default_factory=dict)
    individual_income_tax_fen: Fen = 0

    @model_validator(mode="after")
    def component_amounts_are_nonnegative(self) -> SalaryWithholdingAllocation:
        if (self.open_item_id is None) == (self.source_component_key is None):
            raise ValueError("provide exactly one posted or business-referenced salary obligation")
        if self.source_event_key is not None and self.source_component_key is None:
            raise ValueError("source_event_key requires source_component_key")
        components = [
            *self.employee_social_insurance_items.values(),
            *self.employee_housing_fund_items.values(),
        ]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in components
        ):
            raise ValueError("withholding component values must be non-negative integer fen")
        return self


class SalaryActualDeductionAllocation(BaseModel):
    """Employer-retained salary deduction that settles salary without creating a debt.

    The deduction is distinct from statutory social-insurance, housing-fund and
    income-tax withholdings.  The posting template always credits the same
    benefit-area payroll expense used by the source payroll line; callers cannot
    select an account or journal direction.
    """

    model_config = ConfigDict(extra="forbid")

    open_item_id: uuid.UUID | None = None
    source_component_key: str | None = None
    source_event_key: str | None = Field(default=None, min_length=1, max_length=200)
    source_open_item_key: str = "primary"
    amount_fen: PositiveFen

    @model_validator(mode="after")
    def one_salary_source(self) -> SalaryActualDeductionAllocation:
        if (self.open_item_id is None) == (self.source_component_key is None):
            raise ValueError("provide exactly one posted or business-referenced salary obligation")
        if self.source_event_key is not None and self.source_component_key is None:
            raise ValueError("source_event_key requires source_component_key")
        return self


class PayrollBatchKind(StrEnum):
    REGULAR = "regular"
    ANNUAL_BONUS = "annual_bonus"


class AnnualBonusTaxMethod(StrEnum):
    SEPARATE = "separate"
    COMBINED = "combined"


class PayrollWageTaxScope(StrEnum):
    """Accounting income applicability, independent of external filing progress."""

    WAGE_INCOME = "wage_income"
    CONTRIBUTIONS_ONLY = "contributions_only"


class PayrollEmployeeItem(BaseModel):
    """Owner-approved payroll facts; all monetary values are integer fen."""

    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    wage_tax_scope: PayrollWageTaxScope = Field(
        default=PayrollWageTaxScope.WAGE_INCOME,
        title="工资所得适用范围",
        description=(
            "wage_income 表示存在工资所得；本月没有工资所得、仅处理社保时，"
            "明确填 contributions_only。此字段不表示是否完成外部申报。"
        ),
    )
    # The final wage amount approved for tax declaration.  The core never
    # accepts or calculates base pay, commission, performance, or attendance.
    tax_reported_salary_fen: Fen | None = Field(
        default=None,
        title="报税工资",
        description="负责人最终确认并准备向税务客户端申报的工资金额；常规工资必填，可为 0。",
    )
    accounting_gross_salary_fen: Fen | None = Field(
        default=None,
        title="账务应发工资",
        description=(
            "账务上实际形成的应发工资。省略时与报税工资相同；如与报税工资不同，"
            "必须提供工资批次证据。"
        ),
    )
    tax_reporting_difference_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        title="账税工资差异原因",
        description="可选的管理说明，不参与工资计算或入账判断。",
    )
    special_additional_deduction_fen: Fen = 0
    other_legal_deduction_fen: Fen = 0
    tax_relief_fen: Fen = 0
    annual_bonus_fen: Fen = 0
    # Combined annual-bonus taxation is permitted only against immutable facts
    # from this posted regular payroll batch.  A caller cannot provide free-form
    # monthly wage tax inputs.
    regular_payroll_batch_id: uuid.UUID | None = None


class PayrollContributionGroup(StrEnum):
    SOCIAL_INSURANCE = "social_insurance"
    HOUSING_FUND = "housing_fund"


class PayrollContributionActualState(StrEnum):
    DECLARED = "declared"
    NOT_DECLARED = "not_declared"


class PayrollFirstWageTaxTreatmentState(StrEnum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"


class RegisterPayrollFirstWageTaxTreatmentRequest(BaseModel):
    """Register the evidenced annual first-wage cumulative-deduction treatment."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    employee_id: uuid.UUID
    tax_year: int = Field(ge=1900, le=9999)
    first_wage_month: int = Field(ge=1, le=12)
    treatment_state: PayrollFirstWageTaxTreatmentState
    declaration_date: ExternalDeclarationDate = None
    confirmation_description: str = Field(default="", max_length=2000)
    evidence_references: list[uuid.UUID] = Field(min_length=1)
    supersedes_treatment_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def evidence_is_unique(self) -> RegisterPayrollFirstWageTaxTreatmentRequest:
        if len(self.evidence_references) != len(set(self.evidence_references)):
            raise ValueError("evidence_references must not contain duplicates")
        return self


class PayrollContributionActualItem(BaseModel):
    """One evidenced actual assessment, distinct from the company policy baseline."""

    model_config = ConfigDict(extra="forbid")

    contribution_group: PayrollContributionGroup
    insurance_kind: str = Field(min_length=1, max_length=50)
    actual_state: PayrollContributionActualState
    employee_amount_fen: Fen
    employer_amount_fen: Fen

    @model_validator(mode="after")
    def non_declaration_has_no_assessed_amount(self) -> PayrollContributionActualItem:
        if self.actual_state is PayrollContributionActualState.NOT_DECLARED and (
            self.employee_amount_fen or self.employer_amount_fen
        ):
            raise ValueError("not_declared contribution items must have zero actual amounts")
        return self


class RegisterPayrollContributionActualRequest(BaseModel):
    """Register immutable employee/month/kind actual assessment facts.

    The company policy remains the calculation baseline.  Only the explicitly
    supplied insurance kinds are replaced by these evidenced actual amounts.
    """

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    employee_id: uuid.UUID
    contribution_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    declaration_date: ExternalDeclarationDate = None
    reason_code: (
        Literal[
            "late_enrollment",
            "missing_declaration",
            "partial_declaration",
            "agency_assessment",
            "documented_correction",
            "other_documented",
        ]
        | None
    ) = None
    reason_description: str = Field(default="", max_length=2000)
    items: list[PayrollContributionActualItem] = Field(min_length=1)
    evidence_references: list[uuid.UUID] = Field(min_length=1)
    supersedes_actual_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def contribution_items_are_unique(self) -> RegisterPayrollContributionActualRequest:
        keys = [(item.contribution_group.value, item.insurance_kind) for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("items must contain each contribution group and insurance kind once")
        if len(self.evidence_references) != len(set(self.evidence_references)):
            raise ValueError("evidence_references must not contain duplicates")
        if len(self.supersedes_actual_ids) != len(set(self.supersedes_actual_ids)):
            raise ValueError("supersedes_actual_ids must not contain duplicates")
        return self


class PayrollContributionSupplementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contribution_group: PayrollContributionGroup
    insurance_kind: str = Field(min_length=1, max_length=50)
    employee_amount_fen: Fen
    employer_amount_fen: Fen
    employee_amount_treatment: Literal["employer_borne", "employee_receivable"]

    @model_validator(mode="after")
    def amount_is_positive(self) -> PayrollContributionSupplementItem:
        if self.employee_amount_fen + self.employer_amount_fen <= 0:
            raise ValueError("a contribution supplement item must have a positive amount")
        return self


class RecordPayrollContributionSupplementRequest(BaseModel):
    """Accrue an evidenced historical contribution supplement in an open period."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    employee_id: uuid.UUID
    contribution_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    posting_date: date
    due_date: date | None = None
    assessment_reference: str | None = Field(default=None, min_length=1, max_length=200)
    reason_code: (
        Literal[
            "late_enrollment",
            "missing_declaration",
            "agency_assessment",
            "documented_correction",
            "other_documented",
        ]
        | None
    ) = None
    reason_description: str = Field(default="", max_length=2000)
    items: list[PayrollContributionSupplementItem] = Field(min_length=1)
    evidence_references: list[uuid.UUID] = Field(min_length=1)

    @model_validator(mode="after")
    def supplement_shape_is_valid(self) -> RecordPayrollContributionSupplementRequest:
        keys = [(item.contribution_group.value, item.insurance_kind) for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("items must contain each contribution group and insurance kind once")
        if len(self.evidence_references) != len(set(self.evidence_references)):
            raise ValueError("evidence_references must not contain duplicates")
        if self.due_date is not None and self.due_date < self.posting_date:
            raise ValueError("due_date must not precede posting_date")
        contribution_year = int(self.contribution_period[:4])
        contribution_month = int(self.contribution_period[5:])
        if (contribution_year, contribution_month) >= (
            self.posting_date.year,
            self.posting_date.month,
        ):
            raise ValueError("supplement contribution_period must precede the posting month")
        return self


class RegisterEmployeeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    employee_code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    # This is the first month in which the person may enter the controlled
    # employee-payroll workflow.  It is an accounting role date, not a legal
    # conclusion about whether or when a labor relationship was formed.
    employment_start_date: date = Field(
        title="工资核算身份开始日",
        description="开始按员工工资口径核算的日期；不用于判断或证明劳动关系。",
    )
    # The withholding relationship is a separate accounting/tax fact.  It may
    # be supplied later, but payroll calculation will not infer it.
    tax_withholding_start_date: date | None = None
    employment_end_date: date | None = None
    status: Literal["active", "inactive", "terminated"] = "active"
    prior_labor_person_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def employment_dates_are_ordered(self) -> RegisterEmployeeRequest:
        if (
            self.employment_end_date is not None
            and self.employment_end_date < self.employment_start_date
        ):
            raise ValueError("employment_end_date must not precede employment_start_date")
        if (
            self.tax_withholding_start_date is not None
            and self.tax_withholding_start_date < self.employment_start_date
        ):
            raise ValueError("tax_withholding_start_date must not precede employment_start_date")
        return self


class RegisterEmployeePayrollProfileVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    employee_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None
    expense_role: Literal[
        "payroll_management_expense", "payroll_sales_expense", "payroll_service_cost"
    ]
    social_insurance_base_fen: Fen
    housing_fund_base_fen: Fen
    social_insurance_participating: bool = Field(
        default=True,
        title="本公司参保",
        description="该工资核算人员在本公司是否实际参加社保；不用于判断劳动关系。",
    )
    housing_fund_participating: bool = Field(
        default=True,
        title="本公司缴存公积金",
        description="该工资核算人员在本公司是否实际缴存住房公积金。",
    )
    resident_employee: bool | None = Field(
        default=None,
        title="居民个人",
        description="工资所得个税计算时必需；只处理社保且本月无工资所得时可暂缺。",
    )
    supersedes_profile_version_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def effective_dates_are_ordered(self) -> RegisterEmployeePayrollProfileVersionRequest:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        return self


class PayrollContributionRuleParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=50)
    base_kind: Literal["social_insurance", "housing_fund"]
    employee_rate: Decimal = Field(ge=0, le=1)
    employer_rate: Decimal = Field(ge=0, le=1)
    minimum_base_fen: Fen
    maximum_base_fen: Fen
    rounding_rule: Literal["half_up", "down", "up"]
    enabled: bool = True

    @model_validator(mode="after")
    def base_range_is_ordered(self) -> PayrollContributionRuleParameters:
        if self.maximum_base_fen < self.minimum_base_fen:
            raise ValueError("maximum_base_fen must not be below minimum_base_fen")
        return self


class IncomeTaxBracketParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upper_bound_fen: PositiveFen | None = None
    rate: Decimal = Field(ge=0, le=1)
    quick_deduction_fen: Fen


class IncomeTaxParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=50)
    primary_source_url: str = Field(min_length=1, max_length=4000)
    legal_basis_source_url: str = Field(min_length=1, max_length=4000)
    effective_from: date
    effective_to: date | None = None
    monthly_standard_deduction_fen: Fen
    brackets: list[IncomeTaxBracketParameters] = Field(min_length=1)

    @model_validator(mode="after")
    def effective_dates_are_ordered(self) -> IncomeTaxParameters:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("income-tax effective_to must not precede effective_from")
        return self


class AnnualBonusBracketParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upper_monthly_average_fen: PositiveFen | None = None
    rate: Decimal = Field(ge=0, le=1)
    quick_deduction_fen: Fen


class AnnualBonusParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=50)
    primary_source_url: str = Field(min_length=1, max_length=4000)
    effective_from: date
    effective_to: date | None = None
    brackets: list[AnnualBonusBracketParameters] = Field(min_length=1)

    @model_validator(mode="after")
    def effective_dates_are_ordered(self) -> AnnualBonusParameters:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("annual-bonus effective_to must not precede effective_from")
        return self


class StatutoryPaymentTargetParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agency_code: str = Field(min_length=1, max_length=100)
    agency_name: str = Field(min_length=1, max_length=200)


class PayrollPaymentTargetsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    social_insurance: StatutoryPaymentTargetParameters
    housing_fund: StatutoryPaymentTargetParameters
    individual_income_tax: StatutoryPaymentTargetParameters


class PayrollPolicyParameters(BaseModel):
    """Complete public policy contract stored as an immutable JSON snapshot."""

    model_config = ConfigDict(extra="forbid")

    contribution_rules: list[PayrollContributionRuleParameters] = Field(min_length=1)
    employee_contribution_shortfall_treatment: Literal["reject", "employer_borne"] = "reject"
    income_tax: IncomeTaxParameters
    annual_bonus: AnnualBonusParameters | None = None
    payment_targets: PayrollPaymentTargetsParameters | None = None


class RegisterPayrollPolicyVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    region: str = Field(min_length=1, max_length=100)
    effective_from: date
    effective_to: date | None = None
    version: str = Field(min_length=1, max_length=50)
    source_url: str = Field(min_length=1, max_length=4000)
    parameters: PayrollPolicyParameters
    supersedes_policy_version_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def effective_dates_are_ordered(self) -> RegisterPayrollPolicyVersionRequest:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        return self


class RegisterPayrollOpeningStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    employee_id: uuid.UUID
    tax_year: Annotated[StrictInt, Field(ge=1900, le=9999)]
    through_month: Annotated[StrictInt, Field(ge=1, le=12)]
    cumulative_income_fen: Fen
    cumulative_tax_exempt_income_fen: Fen
    cumulative_basic_deduction_fen: Fen
    cumulative_employee_social_insurance_fen: Fen
    cumulative_employee_housing_fund_fen: Fen
    cumulative_special_additional_deduction_fen: Fen
    cumulative_other_legal_deduction_fen: Fen
    cumulative_tax_relief_fen: Fen
    cumulative_tax_withheld_fen: Fen
    # The physical opening-state key is immutable.  A later through-month can
    # supersede an unused import; corrections after dependent payroll exist are
    # rejected with the affected payroll scope for typed correction review.
    supersedes_opening_state_id: uuid.UUID | None = None


class PreviewPayrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    batch_kind: PayrollBatchKind
    payroll_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    posting_date: date
    payment_date: date | None = Field(
        default=None,
        description=(
            "仅年终奖等以实际支付日决定个税所属期的批次必填。常规月薪按工资所属期计提，"
            "无需提供预计或实际支付日；实际发薪由后续银行流水和付款事件记录。"
        ),
    )
    employee_items: list[PayrollEmployeeItem] = Field(min_length=1)
    tax_method: AnnualBonusTaxMethod | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def payroll_shape_is_explicit(self) -> PreviewPayrollRequest:
        employee_ids = [item.employee_id for item in self.employee_items]
        if len(employee_ids) != len(set(employee_ids)):
            raise ValueError("employee_items must contain each employee once")
        if self.batch_kind == PayrollBatchKind.REGULAR:
            if self.tax_method is not None:
                raise ValueError("tax_method is only available for annual_bonus payroll")
            if any(item.annual_bonus_fen for item in self.employee_items):
                raise ValueError("annual_bonus_fen is only available for annual_bonus payroll")
            for item in self.employee_items:
                if (
                    item.wage_tax_scope == PayrollWageTaxScope.WAGE_INCOME
                    and item.tax_reported_salary_fen is None
                ):
                    raise ValueError(
                        "tax_reported_salary_fen is required when wage tax is declared, "
                        "including zero"
                    )
                if item.wage_tax_scope == PayrollWageTaxScope.CONTRIBUTIONS_ONLY and (
                    item.tax_reported_salary_fen is not None
                    or item.special_additional_deduction_fen
                    or item.other_legal_deduction_fen
                    or item.tax_relief_fen
                ):
                    raise ValueError(
                        "not_declared regular payroll cannot include wage-tax amounts or deductions"
                    )
                if (
                    item.wage_tax_scope == PayrollWageTaxScope.CONTRIBUTIONS_ONLY
                    and item.accounting_gross_salary_fen not in {None, 0}
                ):
                    raise ValueError(
                        "not_declared regular payroll cannot include accounting gross salary"
                    )
                if (
                    item.wage_tax_scope == PayrollWageTaxScope.WAGE_INCOME
                    and item.tax_reported_salary_fen is not None
                ):
                    accounting_gross = (
                        item.accounting_gross_salary_fen
                        if item.accounting_gross_salary_fen is not None
                        else item.tax_reported_salary_fen
                    )
                    differs = accounting_gross != item.tax_reported_salary_fen
                    if differs and not self.evidence_references:
                        raise ValueError(
                            "evidence_references are required for a wage reporting difference"
                        )
            if any(item.regular_payroll_batch_id is not None for item in self.employee_items):
                raise ValueError(
                    "regular_payroll_batch_id is only available for annual_bonus payroll"
                )
        else:
            if self.payment_date is None:
                raise ValueError("payment_date is required for annual_bonus payroll")
            if self.payment_date < self.posting_date:
                raise ValueError("payment_date must not precede posting_date")
            if any(
                item.wage_tax_scope != PayrollWageTaxScope.WAGE_INCOME
                for item in self.employee_items
            ):
                raise ValueError("wage_tax_scope is only available for regular payroll")
            if any(item.annual_bonus_fen <= 0 for item in self.employee_items):
                raise ValueError("annual_bonus_fen must be positive for annual_bonus payroll")
            if any(
                item.tax_reported_salary_fen is not None
                or item.accounting_gross_salary_fen is not None
                or item.special_additional_deduction_fen
                or item.other_legal_deduction_fen
                or item.tax_relief_fen
                for item in self.employee_items
            ):
                raise ValueError("annual_bonus payroll cannot include regular monthly wage facts")
            if self.tax_method == AnnualBonusTaxMethod.COMBINED and any(
                item.regular_payroll_batch_id is None for item in self.employee_items
            ):
                raise ValueError(
                    "combined annual_bonus payroll requires regular_payroll_batch_id per employee"
                )
        return self


class ConfirmPayrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    batch_id: uuid.UUID
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(default="", max_length=2000)


class PayrollResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    DELETED = "deleted"
    REVERSED = "reversed"
    SUPERSEDED = "superseded"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class PayrollResult(BaseModel):
    status: PayrollResultStatus
    batch_id: uuid.UUID | None = None
    calculation_hash: str | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    missing_information: list[dict[str, Any] | str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    rule_version: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class PayrollTaxIdentityDocumentType(StrEnum):
    RESIDENT_ID_CARD = "居民身份证"
    HONG_KONG_MACAO_TRAVEL_PERMIT = "港澳居民来往内地通行证"
    HONG_KONG_MACAO_TRAVEL_PERMIT_NON_CHINESE = "港澳居民来往内地通行证（非中国籍）"
    HONG_KONG_MACAO_RESIDENCE_PERMIT = "中华人民共和国港澳居民居住证"
    TAIWAN_TRAVEL_PERMIT = "台湾居民来往大陆通行证"
    TAIWAN_RESIDENCE_PERMIT = "中华人民共和国台湾居民居住证"
    CHINESE_PASSPORT = "中国护照"
    FOREIGN_PASSPORT = "外国护照"
    FOREIGN_PERMANENT_RESIDENCE_ID = "外国人永久居留身份证（外国人永久居留证）"
    FOREIGN_WORK_PERMIT_A = "中华人民共和国外国人工作许可证（A类）"
    FOREIGN_WORK_PERMIT_B = "中华人民共和国外国人工作许可证（B类）"
    FOREIGN_WORK_PERMIT_C = "中华人民共和国外国人工作许可证（C类）"
    OTHER = "其他个人证件"


class PayrollTaxImportEmployeeItem(BaseModel):
    """Explicit tax-client-only facts reconciled to one posted payroll line."""

    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    document_type: PayrollTaxIdentityDocumentType
    document_number: str = Field(min_length=1, max_length=100)
    cumulative_child_education_fen: Fen = 0
    cumulative_continuing_education_fen: Fen = 0
    cumulative_housing_loan_interest_fen: Fen = 0
    cumulative_housing_rent_fen: Fen = 0
    cumulative_elderly_support_fen: Fen = 0
    cumulative_infant_care_fen: Fen = 0
    current_personal_pension_fen: Fen = 0
    cumulative_personal_pension_fen: Fen
    enterprise_occupational_annuity_fen: Fen = 0
    commercial_health_insurance_fen: Fen = 0
    tax_deferred_pension_insurance_fen: Fen = 0
    official_transportation_fen: Fen = 0
    communication_fen: Fen = 0
    lawyer_case_expense_fen: Fen = 0
    housing_fund_adjustment_fen: Fen = 0
    tibet_additional_deduction_fen: Fen = 0
    other_deduction_fen: Fen = 0
    deductible_donation_fen: Fen = 0
    tax_relief_fen: Fen = 0
    treaty_relief_fen: Fen = 0
    remark: str = Field(default="", max_length=2000)

    @field_validator("document_number")
    @classmethod
    def document_number_is_trimmed(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("document_number must not be blank")
        return normalized

    @field_validator("remark")
    @classmethod
    def remark_is_trimmed(cls, value: str) -> str:
        return value.strip()


class GeneratePayrollTaxImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    payroll_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    employee_items: list[PayrollTaxImportEmployeeItem] = Field(min_length=1)
    pension_insurance_code: str = Field(default="pension", min_length=1, max_length=50)
    medical_insurance_code: str = Field(default="medical", min_length=1, max_length=50)
    unemployment_insurance_code: str = Field(default="unemployment", min_length=1, max_length=50)

    @model_validator(mode="after")
    def employee_and_insurance_mappings_are_unique(self) -> GeneratePayrollTaxImportRequest:
        employee_ids = [item.employee_id for item in self.employee_items]
        if len(employee_ids) != len(set(employee_ids)):
            raise ValueError("employee_items must contain each employee once")
        insurance_codes = {
            self.pension_insurance_code,
            self.medical_insurance_code,
            self.unemployment_insurance_code,
        }
        if len(insurance_codes) != 3:
            raise ValueError("social insurance import codes must be distinct")
        return self


class PayrollTaxImportResultStatus(StrEnum):
    GENERATED = "generated"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class PayrollTaxImportResult(BaseModel):
    status: PayrollTaxImportResultStatus
    export_id: uuid.UUID | None = None
    file_name: str | None = None
    file_path: Path | None = None
    media_type: str | None = None
    sha256: str | None = None
    row_count: int = 0
    source_batch_ids: list[uuid.UUID] = Field(default_factory=list)
    missing_information: list[dict[str, Any] | str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    idempotent_replay: bool = False


class FixedAssetCategory(StrEnum):
    PRODUCTION_EQUIPMENT = "production_equipment"
    TOOLS_FURNITURE = "tools_furniture"
    TRANSPORT = "transport"
    ELECTRONIC = "electronic"
    OTHER_MOVABLE_TANGIBLE = "other_movable_tangible"


class FixedAssetBenefitArea(StrEnum):
    MANAGEMENT = "management"
    SALES = "sales"
    SERVICE_DELIVERY = "service_delivery"


class FixedAssetAcquisitionSettlementKind(StrEnum):
    BANK = "bank"
    PAYABLE = "payable"
    EMPLOYEE_PAYABLE = "employee_payable"
    ALLOCATED_EMPLOYEE_PAYABLES = "allocated_employee_payables"


class FixedAssetDepreciationMethod(StrEnum):
    STRAIGHT_LINE = "straight_line"


class FixedAssetDepreciationRoundingPolicy(StrEnum):
    LEGACY_FLOOR_FINAL_REMAINDER = "floor_final_remainder_v1"
    ROUND_HALF_UP_CARD = "round_half_up_card_v1"
    ROUND_HALF_UP_GROUP = "round_half_up_group_v1"


class FixedAssetDisposalKind(StrEnum):
    SALE = "sale"
    RETIREMENT = "retirement"


class FixedAssetDisposalSettlementKind(StrEnum):
    BANK = "bank"
    RECEIVABLE = "receivable"
    NONE = "none"


class FixedAssetCostComponents(BaseModel):
    """Optional breakdown of an explicitly stated acquisition cost."""

    model_config = ConfigDict(extra="forbid")

    purchase_price_fen: Fen | None = None
    noncreditable_tax_fen: Fen | None = None
    transport_and_handling_fen: Fen | None = None
    installation_and_direct_cost_fen: Fen | None = None


class FixedAssetInformationRequirement(BaseModel):
    """A missing fact that may change fixed-asset accounting treatment."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    fields: list[str]


class FixedAssetEmployeeCostSource(BaseModel):
    """One evidence-backed employee advance included in a canonical asset card."""

    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=200)
    amount_fen: PositiveFen
    reimbursing_employee: CounterpartyRef
    due_date: date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)


class FixedAssetReadyForUseFacts(BaseModel):
    """Depreciation facts for an acquired asset that is already ready for use."""

    model_config = ConfigDict(extra="forbid")

    in_service_date: date | None = None
    depreciation_method: FixedAssetDepreciationMethod = FixedAssetDepreciationMethod.STRAIGHT_LINE
    useful_life_months: Annotated[StrictInt, Field(ge=13)] | None = None
    residual_value_fen: Fen | None = None
    benefit_area: FixedAssetBenefitArea | None = None
    depreciation_group_code: str | None = Field(default=None, min_length=1, max_length=100)
    depreciation_rounding_policy: FixedAssetDepreciationRoundingPolicy = (
        FixedAssetDepreciationRoundingPolicy.ROUND_HALF_UP_CARD
    )

    def missing_fields(self) -> list[str]:
        return [
            field_name
            for field_name in (
                "in_service_date",
                "useful_life_months",
                "residual_value_fen",
                "benefit_area",
            )
            if getattr(self, field_name) is None
        ]


class AcquireFixedAssetRequest(BaseModel):
    """Facts for one externally acquired fixed asset.

    ``ready_for_use`` records acquisition and activation in one deterministic
    event.  Omitting it retains the construction/not-yet-ready workflow.
    """

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    asset_code: str | None = Field(default=None, min_length=1, max_length=100)
    asset_name: str | None = Field(default=None, min_length=1, max_length=200)
    category: FixedAssetCategory | None = None
    expected_use_over_one_year: StrictBool | None = None
    purchase_date: date | None = None
    posting_date: date | None = None
    cost_fen: PositiveFen | None = None
    cost_components: FixedAssetCostComponents | None = None
    supplier: CounterpartyRef | None = None
    reimbursing_employee: CounterpartyRef | None = None
    employee_cost_sources: list[FixedAssetEmployeeCostSource] = Field(
        default_factory=list, max_length=100
    )
    settlement_method: FixedAssetAcquisitionSettlementKind | None = None
    bank_account_code: str | None = Field(default=None, min_length=1, max_length=30)
    payment_date: date | None = None
    due_date: date | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    claims_creditable_input_vat: StrictBool | None = None
    ready_for_use: FixedAssetReadyForUseFacts | None = None
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> AcquireFixedAssetRequest:
        if self.posting_date and self.purchase_date and self.posting_date < self.purchase_date:
            raise ValueError("posting_date must not precede purchase_date")
        if self.ready_for_use is not None:
            if (
                self.purchase_date
                and self.ready_for_use.in_service_date
                and self.ready_for_use.in_service_date < self.purchase_date
            ):
                raise ValueError("ready_for_use.in_service_date must not precede purchase_date")
            if (
                self.posting_date
                and self.ready_for_use.in_service_date
                and self.posting_date < self.ready_for_use.in_service_date
                and self.posting_date.replace(day=1)
                != self.ready_for_use.in_service_date.replace(day=1)
            ):
                raise ValueError(
                    "posting_date may precede ready_for_use.in_service_date only within "
                    "the same calendar month"
                )
        if self.settlement_method in {
            FixedAssetAcquisitionSettlementKind.PAYABLE,
            FixedAssetAcquisitionSettlementKind.EMPLOYEE_PAYABLE,
            FixedAssetAcquisitionSettlementKind.ALLOCATED_EMPLOYEE_PAYABLES,
        } and (self.bank_account_code is not None or self.bank_transaction_references):
            raise ValueError("a payable acquisition must not include bank facts")
        if (
            self.settlement_method is not FixedAssetAcquisitionSettlementKind.EMPLOYEE_PAYABLE
            and self.reimbursing_employee is not None
        ):
            raise ValueError(
                "reimbursing_employee is only accepted for employee-payable acquisition"
            )
        if (
            self.settlement_method
            is not FixedAssetAcquisitionSettlementKind.ALLOCATED_EMPLOYEE_PAYABLES
            and self.employee_cost_sources
        ):
            raise ValueError(
                "employee_cost_sources are only accepted for allocated employee payables"
            )
        if (
            self.settlement_method
            is FixedAssetAcquisitionSettlementKind.ALLOCATED_EMPLOYEE_PAYABLES
        ):
            source_keys = [item.source_key for item in self.employee_cost_sources]
            if len(source_keys) != len(set(source_keys)):
                raise ValueError("employee cost source keys must be unique")
            if (
                self.cost_fen is not None
                and sum(item.amount_fen for item in self.employee_cost_sources) != self.cost_fen
            ):
                raise ValueError("employee cost source amounts must equal asset cost")
        if self.cost_components is not None and self.cost_fen is not None:
            supplied = [
                value for value in self.cost_components.model_dump().values() if value is not None
            ]
            if supplied and sum(supplied) != self.cost_fen:
                raise ValueError("provided cost components must sum exactly to cost_fen")
        return self

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        missing: list[FixedAssetInformationRequirement] = []
        if self.category is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_CATEGORY_REQUIRED",
                    message="the accounting asset category is required",
                    fields=["category"],
                )
            )
        if self.expected_use_over_one_year is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_EXPECTED_USE_REQUIRED",
                    message="expected use over one year must be stated explicitly",
                    fields=["expected_use_over_one_year"],
                )
            )
        if self.claims_creditable_input_vat is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_INPUT_VAT_TREATMENT_REQUIRED",
                    message="whether acquisition input VAT is claimed creditable must be stated",
                    fields=["claims_creditable_input_vat"],
                )
            )
        dates = [
            field_name
            for field_name in ("purchase_date", "posting_date")
            if getattr(self, field_name) is None
        ]
        if dates:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_ACQUISITION_DATES_REQUIRED",
                    message="purchase and posting dates are required",
                    fields=dates,
                )
            )
        if self.cost_fen is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_COST_REQUIRED",
                    message="the total capitalisable acquisition cost is required",
                    fields=["cost_fen"],
                )
            )
        if self.settlement_method is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_ACQUISITION_SETTLEMENT_REQUIRED",
                    message="bank payment or supplier payable settlement must be selected",
                    fields=["settlement_method"],
                )
            )
        elif self.settlement_method is FixedAssetAcquisitionSettlementKind.BANK:
            if self.bank_account_code is None:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_BANK_ACCOUNT_REQUIRED",
                        message="a bank-paid acquisition requires its bank account code",
                        fields=["bank_account_code"],
                    )
                )
            if self.payment_date is None:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_PAYMENT_DATE_REQUIRED",
                        message="a bank-paid acquisition requires its payment date",
                        fields=["payment_date"],
                    )
                )
        elif self.settlement_method is FixedAssetAcquisitionSettlementKind.EMPLOYEE_PAYABLE:
            if self.reimbursing_employee is None:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_REIMBURSING_EMPLOYEE_REQUIRED",
                        message="an employee-payable acquisition requires its reimbursing employee",
                        fields=["reimbursing_employee"],
                    )
                )
        elif (
            self.settlement_method
            is FixedAssetAcquisitionSettlementKind.ALLOCATED_EMPLOYEE_PAYABLES
        ):
            if not self.employee_cost_sources:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_EMPLOYEE_COST_SOURCES_REQUIRED",
                        message=(
                            "a canonical asset funded by employee advances requires its "
                            "finite cost-source allocations"
                        ),
                        fields=["employee_cost_sources"],
                    )
                )
        if not self.evidence_references:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_EVIDENCE_REQUIRED",
                    message="at least one acquisition evidence reference is required",
                    fields=["evidence_references"],
                )
            )
        if self.ready_for_use is not None:
            if ready_fields := self.ready_for_use.missing_fields():
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_READY_FOR_USE_FACTS_REQUIRED",
                        message=(
                            "an asset recorded as ready for use requires its in-service date, "
                            "life, residual value, and benefit area"
                        ),
                        fields=[f"ready_for_use.{item}" for item in ready_fields],
                    )
                )
        return missing


class ActivateFixedAssetRequest(BaseModel):
    """Facts that freeze one asset's useful life and accounting use area."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    asset_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    activation_date: date | None = None
    posting_date: date | None = None
    depreciation_method: FixedAssetDepreciationMethod = FixedAssetDepreciationMethod.STRAIGHT_LINE
    useful_life_months: Annotated[StrictInt, Field(ge=13)] | None = None
    residual_value_fen: Fen | None = None
    benefit_area: FixedAssetBenefitArea | None = None
    depreciation_group_code: str | None = Field(default=None, min_length=1, max_length=100)
    depreciation_rounding_policy: FixedAssetDepreciationRoundingPolicy = (
        FixedAssetDepreciationRoundingPolicy.ROUND_HALF_UP_CARD
    )
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> ActivateFixedAssetRequest:
        if self.posting_date and self.activation_date and self.posting_date < self.activation_date:
            raise ValueError("posting_date must not precede activation_date")
        return self

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        fields = [
            field_name
            for field_name in (
                "asset_id",
                "activation_date",
                "posting_date",
                "useful_life_months",
                "residual_value_fen",
                "benefit_area",
            )
            if getattr(self, field_name) is None
        ]
        missing = []
        if fields:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_ACTIVATION_FACTS_REQUIRED",
                    message=(
                        "asset, activation date, life, residual value, and benefit area are "
                        "required"
                    ),
                    fields=fields,
                )
            )
        if not self.evidence_references:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_EVIDENCE_REQUIRED",
                    message="at least one activation evidence reference is required",
                    fields=["evidence_references"],
                )
            )
        return missing


class PreviewFixedAssetDepreciationRequest(BaseModel):
    """Request a deterministic calculation for one asset and one calendar month."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    asset_id: uuid.UUID | None = None
    depreciation_period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    posting_date: date | None = None

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        fields = [
            field_name
            for field_name in ("asset_id", "depreciation_period", "posting_date")
            if getattr(self, field_name) is None
        ]
        return (
            [
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_DEPRECIATION_FACTS_REQUIRED",
                    message="asset, YYYY-MM depreciation period, and posting date are required",
                    fields=fields,
                )
            ]
            if fields
            else []
        )


class ConfirmFixedAssetDepreciationRequest(PreviewFixedAssetDepreciationRequest):
    """Confirm an unchanged depreciation calculation by its SHA-256 hash."""

    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_note: str = Field(default="", max_length=2000)

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        missing = super().missing_information()
        fields = ["calculation_hash"] if self.calculation_hash is None else []
        if fields:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_CONFIRMATION_REQUIRED",
                    message="calculation hash is required",
                    fields=fields,
                )
            )
        return missing


class PreviewFixedAssetDepreciationBatchRequest(BaseModel):
    """Calculate every active asset due in one calendar month as one batch."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    depreciation_period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    posting_date: date | None = None

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        fields = [
            field_name
            for field_name in ("depreciation_period", "posting_date")
            if getattr(self, field_name) is None
        ]
        return (
            [
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_DEPRECIATION_BATCH_FACTS_REQUIRED",
                    message="YYYY-MM depreciation period and posting date are required",
                    fields=fields,
                )
            ]
            if fields
            else []
        )


class ConfirmFixedAssetDepreciationBatchRequest(PreviewFixedAssetDepreciationBatchRequest):
    """Confirm one unchanged all-assets monthly depreciation batch."""

    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_note: str = Field(default="", max_length=2000)

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        missing = super().missing_information()
        if self.calculation_hash is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_DEPRECIATION_BATCH_CONFIRMATION_REQUIRED",
                    message="batch calculation hash is required",
                    fields=["calculation_hash"],
                )
            )
        return missing


class DisposeFixedAssetRequest(BaseModel):
    """Facts for a sale or zero-income retirement of one active asset."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    asset_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    disposal_date: date | None = None
    posting_date: date | None = None
    disposal_kind: FixedAssetDisposalKind | None = None
    gross_proceeds_fen: PositiveFen | None = None
    invoice_type: Literal["ordinary", "special", "none"] | None = None
    waive_exemption: StrictBool | None = None
    settlement_method: FixedAssetDisposalSettlementKind | None = None
    bank_account_code: str | None = Field(default=None, min_length=1, max_length=30)
    customer: CounterpartyRef | None = None
    tax_obligation_date: date | None = None
    clearance_cost_fen: Fen | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def disposal_dates_are_ordered(self) -> DisposeFixedAssetRequest:
        if self.posting_date and self.disposal_date and self.posting_date < self.disposal_date:
            raise ValueError("posting_date must not precede disposal_date")
        if self.disposal_kind is FixedAssetDisposalKind.RETIREMENT:
            if (
                self.settlement_method is not None
                and self.settlement_method is not FixedAssetDisposalSettlementKind.NONE
            ):
                raise ValueError("a retirement must use settlement_method='none'")
            forbidden = {
                "gross_proceeds_fen": self.gross_proceeds_fen,
                "invoice_type": self.invoice_type,
                "waive_exemption": self.waive_exemption,
                "customer": self.customer,
                "tax_obligation_date": self.tax_obligation_date,
            }
            if any(value is not None for value in forbidden.values()):
                raise ValueError("a retirement cannot include sale or tax facts")
        if (
            self.disposal_kind is FixedAssetDisposalKind.SALE
            and self.settlement_method is FixedAssetDisposalSettlementKind.NONE
        ):
            raise ValueError("a sale must use bank or receivable settlement")
        bank_settled = (
            self.disposal_kind is FixedAssetDisposalKind.SALE
            and self.settlement_method is FixedAssetDisposalSettlementKind.BANK
        ) or bool(self.clearance_cost_fen)
        if (
            self.disposal_kind is not None
            and self.settlement_method is not None
            and self.clearance_cost_fen is not None
            and not bank_settled
            and (self.bank_account_code is not None or self.bank_transaction_references)
        ):
            raise ValueError("a disposal without bank settlement must not include bank facts")
        return self

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        fields = [
            field_name
            for field_name in ("asset_id", "disposal_date", "posting_date", "disposal_kind")
            if getattr(self, field_name) is None
        ]
        missing = []
        if fields:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_DISPOSAL_FACTS_REQUIRED",
                    message="asset, disposal date, posting date, and disposal kind are required",
                    fields=fields,
                )
            )
        if self.clearance_cost_fen is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_CLEARANCE_COST_REQUIRED",
                    message="clearance cost must be explicitly stated, including zero",
                    fields=["clearance_cost_fen"],
                )
            )
        if self.disposal_kind is FixedAssetDisposalKind.SALE:
            sale_fields = [
                field_name
                for field_name in (
                    "gross_proceeds_fen",
                    "invoice_type",
                    "waive_exemption",
                    "settlement_method",
                    "customer",
                    "tax_obligation_date",
                )
                if getattr(self, field_name) is None
            ]
            if sale_fields:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_SALE_FACTS_REQUIRED",
                        message=(
                            "sale proceeds, invoice, tax, settlement, and customer facts are "
                            "required"
                        ),
                        fields=sale_fields,
                    )
                )
        requires_bank_references = (
            self.disposal_kind is FixedAssetDisposalKind.SALE
            and self.settlement_method is FixedAssetDisposalSettlementKind.BANK
        ) or bool(self.clearance_cost_fen)
        if requires_bank_references and self.bank_account_code is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_BANK_ACCOUNT_REQUIRED",
                    message=(
                        "bank-settled disposal or clearance cost requires its bank account code"
                    ),
                    fields=["bank_account_code"],
                )
            )
        if self.disposal_kind is FixedAssetDisposalKind.RETIREMENT:
            retirement_fields = []
            if self.settlement_method is None:
                retirement_fields.append("settlement_method")
            if retirement_fields:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_RETIREMENT_SETTLEMENT_REQUIRED",
                        message="a retirement must explicitly state settlement_method='none'",
                        fields=retirement_fields,
                    )
                )
        if not self.evidence_references:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_EVIDENCE_REQUIRED",
                    message="at least one disposal evidence reference is required",
                    fields=["evidence_references"],
                )
            )
        return missing


class FixedAssetResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    DELETED = "deleted"
    REVERSED = "reversed"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class FixedAssetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FixedAssetResultStatus
    asset_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    calculation_hash: str | None = None
    missing_information: list[FixedAssetInformationRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class FinanceResult(BaseModel):
    status: ResultStatus
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    missing_information: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    rule_version: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class RegisterEvidenceRequest(BaseModel):
    """Public MCP contract for content-addressed supporting evidence."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    source: str = Field(min_length=1, max_length=50)
    file_path: Path | None = Field(
        default=None,
        description="批准证据目录内的文件路径；与 content_base64 必须且只能提供一个。",
    )
    content_base64: str | None = Field(
        default=None,
        description="内联 base64 内容；与 file_path 必须且只能提供一个。",
    )
    original_name: str | None = None
    media_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def reject_identity_and_secret_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = (
            "actor",
            "clientid",
            "confirmedby",
            "credentialversion",
            "executor",
            "owneraccountid",
            "ownersessionid",
            "password",
            "passwd",
            "recoverycode",
            "secret",
            "sessiontoken",
            "token",
        )

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                    if any(part in normalized for part in forbidden):
                        raise ValueError("identity and secret metadata keys are forbidden")
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)

        visit(value)
        return value

    @model_validator(mode="after")
    def exactly_one_content_source(self) -> RegisterEvidenceRequest:
        if (self.file_path is None) == (self.content_base64 is None):
            raise ValueError("provide exactly one of file_path or content_base64")
        return self


class BankStatementColumnMapping(BaseModel):
    """Fixed canonical fields accepted when importing a bank statement."""

    model_config = ConfigDict(extra="forbid")

    booking_date: str = Field(min_length=1)
    amount: str | None = Field(default=None, min_length=1)
    debit: str | None = Field(default=None, min_length=1)
    credit: str | None = Field(default=None, min_length=1)
    counterparty: str | None = Field(default=None, min_length=1)
    memo: str | None = Field(default=None, min_length=1)
    external_id: str | None = Field(default=None, min_length=1)
    currency: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def amount_mapping_is_complete(self) -> BankStatementColumnMapping:
        if self.amount is None and (self.debit is None or self.credit is None):
            raise ValueError("map amount, or map both debit and credit")
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        """Expose the conditional amount mapping requirement to MCP clients."""

        schema = handler(core_schema)
        schema["allOf"] = [
            {
                "anyOf": [
                    {
                        "required": ["amount"],
                        "properties": {"amount": {"type": "string", "minLength": 1}},
                    },
                    {
                        "required": ["debit", "credit"],
                        "properties": {
                            "debit": {"type": "string", "minLength": 1},
                            "credit": {"type": "string", "minLength": 1},
                        },
                    },
                ]
            }
        ]
        return schema


class ImportBankStatementRequest(BaseModel):
    """Public MCP contract for a caller-provided bank statement file."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    file_path: Path
    bank_account_code: str = "1002"
    column_mapping: BankStatementColumnMapping
    sheet_name: str | None = None
    date_format: str | None = None


class TaxPeriodPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    start_date: date
    end_date: date
    adjustment_posting_date: date

    @model_validator(mode="after")
    def valid_range(self) -> TaxPeriodPreviewRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.adjustment_posting_date < self.end_date:
            raise ValueError("adjustment_posting_date must not precede end_date")
        return self


class TaxPeriodConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    start_date: date
    end_date: date
    adjustment_posting_date: date
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_range(self) -> TaxPeriodConfirmRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.adjustment_posting_date < self.end_date:
            raise ValueError("adjustment_posting_date must not precede end_date")
        return self


class ReverseEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    event_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=1000)
    posting_date: date
