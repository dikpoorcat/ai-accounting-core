from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

# Monetary accounting facts are always integer fen.  ``StrictInt`` is
# intentional: JSON 12.0, ``true`` and "12" must never be silently accepted
# as a monetary value merely because they can be coerced by Python.
Fen = Annotated[StrictInt, Field(ge=0)]
PositiveFen = Annotated[StrictInt, Field(gt=0)]


class EventType(StrEnum):
    SERVICE_CASH_SALE = "service_cash_sale"
    SERVICE_CREDIT_SALE = "service_credit_sale"
    SERVICE_FULFILLMENT = "service_fulfillment"
    CUSTOMER_RECEIPT = "customer_receipt"
    CUSTOMER_ADVANCE = "customer_advance"
    CUSTOMER_REFUND = "customer_refund"
    EXPENSE_CASH = "expense_cash"
    EXPENSE_PAYABLE = "expense_payable"
    SUPPLIER_PAYMENT = "supplier_payment"
    EMPLOYEE_REIMBURSEMENT = "employee_reimbursement"
    OWNER_LOAN_RECEIVED = "owner_loan_received"
    OWNER_CONTRIBUTION_RECEIVED = "owner_contribution_received"
    OWNER_REPAYMENT = "owner_repayment"
    BANK_FEE = "bank_fee"
    INTERNAL_TRANSFER = "internal_transfer"
    TAX_PAYMENT = "tax_payment"
    TAX_RELIEF = "tax_relief"
    SALARY_PAYMENT = "salary_payment"
    SOCIAL_INSURANCE_PAYMENT = "social_insurance_payment"
    HOUSING_FUND_PAYMENT = "housing_fund_payment"
    INDIVIDUAL_INCOME_TAX_PAYMENT = "individual_income_tax_payment"
    PAYROLL = "payroll"
    FIXED_ASSET = "fixed_asset"
    FIXED_ASSET_ACQUISITION = "fixed_asset_acquisition"
    FIXED_ASSET_ACTIVATION = "fixed_asset_activation"
    FIXED_ASSET_DEPRECIATION = "fixed_asset_depreciation"
    FIXED_ASSET_DISPOSAL = "fixed_asset_disposal"
    INTANGIBLE_ASSET = "intangible_asset"
    LOAN_INTEREST = "loan_interest"
    INVENTORY = "inventory"


DISABLED_EVENT_TYPES = {
    EventType.PAYROLL,
    EventType.INTANGIBLE_ASSET,
    EventType.LOAN_INTEREST,
    EventType.INVENTORY,
}

INTERNAL_EVENT_TYPES = {
    EventType.TAX_RELIEF,
    EventType.FIXED_ASSET,
    EventType.FIXED_ASSET_ACQUISITION,
    EventType.FIXED_ASSET_ACTIVATION,
    EventType.FIXED_ASSET_DEPRECIATION,
    EventType.FIXED_ASSET_DISPOSAL,
}


EVENT_REQUIREMENTS: dict[str, dict[str, Any]] = {
    EventType.SERVICE_CASH_SALE.value: {
        "amount": "gross_amount_fen",
        "required_dates": [
            "business_date",
            "fulfillment_date",
            "payment_date",
            "tax_obligation_date",
            "posting_date",
        ],
        "tax_facts": "required",
        "counterparty": "optional",
    },
    EventType.SERVICE_CREDIT_SALE.value: {
        "amount": "gross_amount_fen",
        "required_dates": [
            "business_date",
            "fulfillment_date",
            "tax_obligation_date",
            "posting_date",
        ],
        "tax_facts": "required",
        "counterparty": "customer required",
        "creates": "receivable open item",
    },
    EventType.SERVICE_FULFILLMENT.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "fulfillment_date", "posting_date"],
        "counterparty": "customer required",
        "tax_facts": "required",
        "required_details": [
            "recognition_source=contract_liability",
            "tax_previously_accrued",
            "original_event_id",
        ],
    },
    EventType.CUSTOMER_RECEIPT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "customer required",
        "required_choice": "allocations, or details.unallocated_treatment=advance",
    },
    EventType.CUSTOMER_ADVANCE.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "customer required",
        "tax_facts": "required; set tax_due_on_event explicitly",
    },
    EventType.CUSTOMER_REFUND.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "customer required",
        "required_details": ["refund_kind=advance|sale_return", "original_event_id"],
    },
    EventType.EXPENSE_CASH.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "purchase_vat": "included in expense; no input credit",
    },
    EventType.EXPENSE_PAYABLE.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "posting_date"],
        "counterparty": "supplier required",
        "creates": "payable open item",
    },
    EventType.SUPPLIER_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "supplier required",
        "allocations": "required and total must equal payment",
    },
    EventType.EMPLOYEE_REIMBURSEMENT.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "posting_date"],
        "counterparty": "employee required",
        "required_details": ["paid_now"],
    },
    EventType.OWNER_LOAN_RECEIVED.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "owner required",
    },
    EventType.OWNER_CONTRIBUTION_RECEIVED.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "owner required",
    },
    EventType.OWNER_REPAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "owner required",
        "constraint": "cannot exceed this owner's payable balance",
    },
    EventType.BANK_FEE.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
    },
    EventType.INTERNAL_TRANSFER.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "posting_date"],
        "required_details": ["source_account_code", "destination_account_code"],
    },
    EventType.TAX_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "required_details": ["tax_type=vat|surtax"],
        "constraint": "cannot exceed posted tax payable balance",
    },
    EventType.SALARY_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "allocations": (
            "required; salary open items only; total equals cash plus explicit withholdings"
        ),
        "salary_withholding_allocations": (
            "required; classified employee deductions per salary open item"
        ),
        "bank_transactions": "required; outflow total must equal payment",
        "creates": "withheld statutory payroll payables internally",
    },
    EventType.SOCIAL_INSURANCE_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "allocations": "required; social-insurance payables only; total must equal payment",
        "bank_transactions": "required; outflow total must equal payment",
    },
    EventType.HOUSING_FUND_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "allocations": "required; housing-fund payables only; total must equal payment",
        "bank_transactions": "required; outflow total must equal payment",
    },
    EventType.INDIVIDUAL_INCOME_TAX_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "allocations": "required; individual-income-tax payables only; total must equal payment",
        "bank_transactions": "required; outflow total must equal payment",
    },
    EventType.FIXED_ASSET.value: {
        "workflow": "specialized fixed-asset tools only",
    },
    EventType.FIXED_ASSET_ACQUISITION.value: {
        "workflow": "finance_acquire_fixed_asset only",
    },
    EventType.FIXED_ASSET_ACTIVATION.value: {
        "workflow": "finance_activate_fixed_asset only",
    },
    EventType.FIXED_ASSET_DEPRECIATION.value: {
        "workflow": "finance_preview_fixed_asset_depreciation then confirm only",
    },
    EventType.FIXED_ASSET_DISPOSAL.value: {
        "workflow": "finance_dispose_fixed_asset only",
    },
}


class ResultStatus(StrEnum):
    POSTED = "posted"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class BusinessDates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    posting_date: date
    fulfillment_date: date | None = None
    invoice_date: date | None = None
    payment_date: date | None = None
    tax_obligation_date: date | None = None


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


class AmountFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_fen: Fen | None = None
    gross_amount_fen: PositiveFen | None = None
    currency: str = "CNY"
    expense_account_role: str = "general_expense"

    @model_validator(mode="after")
    def cny_only(self) -> AmountFacts:
        if self.currency != "CNY":
            raise ValueError("phase 1 supports CNY only")
        if (self.amount_fen is None) == (self.gross_amount_fen is None):
            raise ValueError("provide exactly one of amount_fen or gross_amount_fen")
        return self


class TaxFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taxable: bool = True
    rate_percent: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    invoice_type: str = "none"
    waive_exemption: bool = False
    tax_due_on_event: bool = True

    @model_validator(mode="after")
    def valid_invoice_type(self) -> TaxFacts:
        if self.invoice_type not in {"ordinary", "special", "none"}:
            raise ValueError("invoice_type must be ordinary, special, or none")
        if self.rate_percent not in {Decimal("0"), Decimal("1"), Decimal("3")}:
            raise ValueError("phase 1 supports VAT rates 0%, 1%, and 3% only")
        if not self.taxable and self.waive_exemption:
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

    open_item_id: uuid.UUID
    employee_social_insurance_items: dict[str, Fen] = Field(default_factory=dict)
    employee_housing_fund_items: dict[str, Fen] = Field(default_factory=dict)
    individual_income_tax_fen: Fen = 0

    @model_validator(mode="after")
    def component_amounts_are_nonnegative(self) -> SalaryWithholdingAllocation:
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


class EventDetails(BaseModel):
    """Finite event-catalog detail fields; the public writer has no free-form map."""

    model_config = ConfigDict(extra="forbid")

    due_date: date | None = None
    tax_previously_accrued: bool | None = None
    refund_kind: Literal["advance", "sale_return"] | None = None
    paid_now: bool | None = None
    source_account: str | None = Field(default=None, min_length=1, max_length=50)
    destination_account: str | None = Field(default=None, min_length=1, max_length=50)
    tax_type: Literal["vat", "surtax"] | None = None
    original_event_id: uuid.UUID | None = None
    unallocated_treatment: Literal["advance"] | None = None
    recognition_source: Literal["contract_liability"] | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_internal_transfer_names(cls, value: Any) -> Any:
        """Map pre-contract names without advertising account-code entry fields."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "source_account_code" in normalized:
            normalized.setdefault("source_account", normalized["source_account_code"])
            normalized.pop("source_account_code")
        if "destination_account_code" in normalized:
            normalized.setdefault("destination_account", normalized["destination_account_code"])
            normalized.pop("destination_account_code")
        return normalized

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        value = getattr(self, key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and getattr(self, key, None) is not None


class PayrollBatchKind(StrEnum):
    REGULAR = "regular"
    ANNUAL_BONUS = "annual_bonus"


class AnnualBonusTaxMethod(StrEnum):
    SEPARATE = "separate"
    COMBINED = "combined"


class PayrollEmployeeItem(BaseModel):
    """Classified employee payroll facts; all monetary values are integer fen."""

    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    base_salary_fen: Fen = 0
    performance_pay_fen: Fen = 0
    taxable_allowance_fen: Fen = 0
    tax_exempt_income_fen: Fen = 0
    attendance_deduction_fen: Fen = 0
    special_additional_deduction_fen: Fen = 0
    other_legal_deduction_fen: Fen = 0
    tax_relief_fen: Fen = 0
    annual_bonus_fen: Fen = 0
    # Combined annual-bonus taxation is permitted only against immutable facts
    # from this posted regular payroll batch.  A caller cannot provide free-form
    # monthly wage tax inputs.
    regular_payroll_batch_id: uuid.UUID | None = None


class RegisterEmployeeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    employee_code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    employment_start_date: date
    employment_end_date: date | None = None
    status: Literal["active", "inactive", "terminated"] = "active"

    @model_validator(mode="after")
    def employment_dates_are_ordered(self) -> RegisterEmployeeRequest:
        if (
            self.employment_end_date is not None
            and self.employment_end_date < self.employment_start_date
        ):
            raise ValueError("employment_end_date must not precede employment_start_date")
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
    resident_employee: bool
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
    income_tax: IncomeTaxParameters
    annual_bonus: AnnualBonusParameters | None = None
    payment_targets: PayrollPaymentTargetsParameters


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
    # rejected and must be reversed/rebuilt.
    supersedes_opening_state_id: uuid.UUID | None = None


class PreviewPayrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    batch_kind: PayrollBatchKind
    payroll_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    posting_date: date
    payment_date: date
    employee_items: list[PayrollEmployeeItem] = Field(min_length=1)
    tax_method: AnnualBonusTaxMethod | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def payroll_shape_is_explicit(self) -> PreviewPayrollRequest:
        if self.payment_date < self.posting_date:
            raise ValueError("payment_date must not precede posting_date")
        employee_ids = [item.employee_id for item in self.employee_items]
        if len(employee_ids) != len(set(employee_ids)):
            raise ValueError("employee_items must contain each employee once")
        if self.batch_kind == PayrollBatchKind.REGULAR:
            if self.tax_method is not None:
                raise ValueError("tax_method is only available for annual_bonus payroll")
            if any(item.annual_bonus_fen for item in self.employee_items):
                raise ValueError("annual_bonus_fen is only available for annual_bonus payroll")
            if any(item.regular_payroll_batch_id is not None for item in self.employee_items):
                raise ValueError(
                    "regular_payroll_batch_id is only available for annual_bonus payroll"
                )
        else:
            if any(item.annual_bonus_fen <= 0 for item in self.employee_items):
                raise ValueError("annual_bonus_fen must be positive for annual_bonus payroll")
            if any(
                item.base_salary_fen
                or item.performance_pay_fen
                or item.taxable_allowance_fen
                or item.tax_exempt_income_fen
                or item.attendance_deduction_fen
                or item.special_additional_deduction_fen
                or item.other_legal_deduction_fen
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
    confirmed_by: str = Field(min_length=1, max_length=100)
    confirmation_note: str = Field(default="", max_length=2000)


class PayrollResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
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


class FixedAssetDepreciationMethod(StrEnum):
    STRAIGHT_LINE = "straight_line"


class FixedAssetDisposalKind(StrEnum):
    SALE = "sale"
    RETIREMENT = "retirement"


class FixedAssetDisposalSettlementKind(StrEnum):
    BANK = "bank"
    RECEIVABLE = "receivable"
    NONE = "none"


class FixedAssetCostComponents(BaseModel):
    """The finite Phase-1 capitalisable acquisition-cost facts, all in fen."""

    model_config = ConfigDict(extra="forbid")

    purchase_price_fen: Fen | None = None
    noncreditable_tax_fen: Fen | None = None
    transport_and_handling_fen: Fen | None = None
    installation_and_direct_cost_fen: Fen | None = None

    def missing_fields(self) -> list[str]:
        return [
            field_name
            for field_name in (
                "purchase_price_fen",
                "noncreditable_tax_fen",
                "transport_and_handling_fen",
                "installation_and_direct_cost_fen",
            )
            if getattr(self, field_name) is None
        ]


class FixedAssetInformationRequirement(BaseModel):
    """A missing fact that may change fixed-asset accounting treatment."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    fields: list[str]


class AcquireFixedAssetRequest(BaseModel):
    """Facts for one externally acquired, not-yet-enabled fixed asset."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    asset_code: str | None = Field(default=None, min_length=1, max_length=100)
    asset_name: str | None = Field(default=None, min_length=1, max_length=200)
    category: FixedAssetCategory | None = None
    expected_use_over_one_year: StrictBool | None = None
    purchase_date: date | None = None
    posting_date: date | None = None
    cost_components: FixedAssetCostComponents = Field(default_factory=FixedAssetCostComponents)
    supplier: CounterpartyRef | None = None
    settlement_method: FixedAssetAcquisitionSettlementKind | None = None
    payment_date: date | None = None
    due_date: date | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    claims_creditable_input_vat: StrictBool | None = None
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> AcquireFixedAssetRequest:
        if self.posting_date and self.purchase_date and self.posting_date < self.purchase_date:
            raise ValueError("posting_date must not precede purchase_date")
        if self.due_date and self.purchase_date and self.due_date < self.purchase_date:
            raise ValueError("due_date must not precede purchase_date")
        return self

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        missing: list[FixedAssetInformationRequirement] = []
        identity = [
            field_name
            for field_name in ("asset_code", "asset_name", "category")
            if getattr(self, field_name) is None
        ]
        if identity:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_IDENTITY_REQUIRED",
                    message="asset code, name, and supported category are required",
                    fields=identity,
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
        if cost_fields := self.cost_components.missing_fields():
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_COST_COMPONENTS_REQUIRED",
                    message="every capitalisable cost component must be stated, including zero",
                    fields=[f"cost_components.{item}" for item in cost_fields],
                )
            )
        if self.supplier is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_SUPPLIER_REQUIRED",
                    message="supplier identity is required",
                    fields=["supplier"],
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
            if self.payment_date is None:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_PAYMENT_DATE_REQUIRED",
                        message="a bank-paid acquisition requires its payment date",
                        fields=["payment_date"],
                    )
                )
            if not self.bank_transaction_references:
                missing.append(
                    FixedAssetInformationRequirement(
                        code="FIXED_ASSET_BANK_TRANSACTIONS_REQUIRED",
                        message="a bank-paid acquisition requires bank transaction references",
                        fields=["bank_transaction_references"],
                    )
                )
        elif self.due_date is None:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_DUE_DATE_REQUIRED",
                    message="a supplier-payable acquisition requires its due date",
                    fields=["due_date"],
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
    depreciation_period: str | None = Field(
        default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    )
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
    confirmed_by: str | None = Field(default=None, min_length=1, max_length=100)
    confirmation_note: str = Field(default="", max_length=2000)

    def missing_information(self) -> list[FixedAssetInformationRequirement]:
        missing = super().missing_information()
        fields = [
            field_name
            for field_name in ("calculation_hash", "confirmed_by")
            if getattr(self, field_name) is None
        ]
        if fields:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_CONFIRMATION_REQUIRED",
                    message="calculation hash and confirmer identity are required",
                    fields=fields,
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
        if requires_bank_references and not self.bank_transaction_references:
            missing.append(
                FixedAssetInformationRequirement(
                    code="FIXED_ASSET_BANK_TRANSACTIONS_REQUIRED",
                    message=(
                        "bank-settled disposal or clearance cost requires bank transaction "
                        "references"
                    ),
                    fields=["bank_transaction_references"],
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


class RecordEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    event_type: EventType
    business_dates: BusinessDates
    counterparty: CounterpartyRef | None = None
    amounts: AmountFacts
    tax_facts: TaxFacts | None = None
    invoice_references: list[InvoiceReference] = Field(default_factory=list)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    allocations: list[Allocation] = Field(default_factory=list)
    salary_withholding_allocations: list[SalaryWithholdingAllocation] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)
    details: EventDetails = Field(default_factory=EventDetails)

    @model_validator(mode="after")
    def zero_cash_is_limited_to_salary_withholding_settlement(self) -> RecordEventRequest:
        if self.amounts.amount_fen != 0:
            return self
        if self.event_type != EventType.SALARY_PAYMENT:
            raise ValueError("zero amount_fen is only available for salary payment withholding")
        if self.amounts.gross_amount_fen is not None:
            raise ValueError("zero-cash salary payment must use amount_fen only")
        return self


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
    file_path: Path | None = None
    content_base64: str | None = None
    original_name: str | None = None
    media_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_content_source(self) -> RegisterEvidenceRequest:
        if (self.file_path is None) == (self.content_base64 is None):
            raise ValueError("provide exactly one of file_path or content_base64")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        """Publish the same mutually-exclusive content rule enforced at runtime."""

        schema = handler(core_schema)
        schema["oneOf"] = [
            {
                "required": ["file_path"],
                "properties": {
                    "file_path": {"type": "string"},
                    "content_base64": {"type": "null"},
                },
            },
            {
                "required": ["content_base64"],
                "properties": {
                    "file_path": {"type": "null"},
                    "content_base64": {"type": "string"},
                },
            },
        ]
        return schema


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
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
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


class TaxPeriodRequest(BaseModel):
    org_id: uuid.UUID
    start_date: date
    end_date: date
    post_adjustment: bool = False
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def valid_range(self) -> TaxPeriodRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.post_adjustment and not self.idempotency_key:
            raise ValueError("idempotency_key is required when post_adjustment=true")
        return self


class ReverseEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    event_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    posting_date: date
