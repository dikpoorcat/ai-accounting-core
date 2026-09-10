"""Public business facts for atomic, composable accounting operations.

These models deliberately have no journal lines or debit/credit selectors.
Money movements allocate real funds to independently typed business facts.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from . import domain_action_schemas as domain
from .business_metadata import BusinessMetadata
from .enterprise_income_tax_schemas import IncomeTaxSourceAllocation
from .fact_requirements import (
    ActualFundsDate,
    ExternalDeclarationDate,
    RecognitionDate,
    RecognitionPeriod,
)
from .schemas import (
    BankTransactionReference,
    CounterpartyRef,
    Fen,
    InvoiceReference,
    PayrollContributionSupplementItem,
    PositiveFen,
    SalaryActualDeductionAllocation,
    SalaryWithholdingAllocation,
    TaxFacts,
)


class ComponentFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contributes_tax_sources: ClassVar[bool] = False
    supports_monthly_recognition: ClassVar[bool] = False

    key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    business_date: RecognitionDate = None
    recognition_period: RecognitionPeriod = None
    payment_date: date | None = Field(
        default=None,
        description="依组件业务解释；实际公司收付款日期来自funds并在唯一时复用。个人垫付等非现金业务的外部付款日是可选管理资料，不得按字段名推断为必填。",
        json_schema_extra={
            "x-accounting-fact": {"role": "contextual", "resolve_from": "component kind and funds"}
        },
    )
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    account_selections: dict[str, str] = Field(default_factory=dict)
    metadata: BusinessMetadata = Field(default_factory=BusinessMetadata)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        schema["x-recognition-precision"] = (
            ["day", "month"] if cls.supports_monthly_recognition else ["day"]
        )
        return schema

    @model_validator(mode="after")
    def recognition_precision_is_explicit(self):
        if self.recognition_period is not None:
            from .fact_dates import period_end

            period_end(self.recognition_period)
            if self.business_date is not None:
                raise ValueError("provide either recognition_period or business_date")
            if not self.supports_monthly_recognition:
                raise ValueError("this component requires its specific accounting dates")
        return self

    @property
    def recognition_date(self) -> date | None:
        from .fact_dates import period_end

        return (
            period_end(self.recognition_period) if self.recognition_period else self.business_date
        )

    def accounting_facts(self) -> dict:
        excluded = {"metadata"}
        if self.kind == "enterprise_income_tax_result":
            excluded.add("declaration_date")
        if (
            self.kind == "debt_transfer"
            or (self.kind == "expense" and self.payment_basis == "person_advance")
            or (self.kind == "refundable_deposit" and self.advanced_by)
        ):
            excluded.add("payment_date")
        return self.model_dump(mode="json", exclude=excluded)


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str | None = Field(default=None, min_length=1, max_length=200)
    component_id: uuid.UUID | None = None
    component_key: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> SourceReference:
        if self.event_key is not None and self.component_key is None:
            raise ValueError("event_key requires component_key")
        if (self.component_id is None) == (self.component_key is None):
            raise ValueError("provide exactly one component_id or component_key")
        return self


class ObligationAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_event_key: str | None = Field(default=None, min_length=1, max_length=200)
    open_item_id: uuid.UUID | None = None
    source_component_key: str | None = None
    source_open_item_key: str = "primary"
    amount_fen: PositiveFen

    @model_validator(mode="after")
    def one_source(self) -> ObligationAllocation:
        if self.source_event_key is not None and self.source_component_key is None:
            raise ValueError("source_event_key requires source_component_key")
        if (self.open_item_id is None) == (self.source_component_key is None):
            raise ValueError("provide exactly one open_item_id or source_component_key")
        return self


class ExpenseBusinessComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["expense"]
    amount_fen: PositiveFen | None = None
    expense_class: str | None = None
    expense_nature: Literal["bank_service_fee"] | None = None
    account_code: str | None = None
    payer: CounterpartyRef | None = None
    payment_basis: Literal["immediate", "supplier_credit", "person_advance"] | None = None
    invoice_references: list[InvoiceReference] = Field(default_factory=list)


class ServiceSaleComponent(ComponentFacts):
    contributes_tax_sources = True
    kind: Literal["service_sale"]
    amount_fen: PositiveFen | None = None
    recognition_basis: Literal["immediate", "credit"] | None = None
    fulfillment_date: date | None = None
    tax_obligation_date: date | None = None
    tax_facts: TaxFacts | None = None
    invoice_references: list[InvoiceReference] = Field(default_factory=list)


class CustomerAdvanceComponent(ComponentFacts):
    contributes_tax_sources = True
    kind: Literal["customer_advance"]
    amount_fen: PositiveFen | None = None
    tax_obligation_date: date | None = None
    tax_facts: TaxFacts | None = None
    invoice_references: list[InvoiceReference] = Field(default_factory=list)


class SupplierAdvanceComponent(ComponentFacts):
    kind: Literal["supplier_advance"]
    amount_fen: PositiveFen | None = None
    purchase_purpose: (
        Literal["goods_or_services", "operating_expense", "fixed_asset", "intangible_asset"] | None
    ) = None


class SupplierAdvanceApplicationComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["supplier_advance_application"]
    advances: list[ObligationAllocation] = Field(min_length=1)
    allocations: list[ObligationAllocation] = Field(min_length=1)


class SupplierAdvanceRefundComponent(ComponentFacts):
    kind: Literal["supplier_advance_refund"]
    advances: list[ObligationAllocation] = Field(min_length=1)


class DevelopmentCapitalizationFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conditions_met_date: date | None = None
    technically_feasible: StrictBool | None = None
    intention_to_complete_and_use: StrictBool | None = None
    probable_economic_benefits: StrictBool | None = None
    adequate_resources: StrictBool | None = None
    reliably_measurable_cost: StrictBool | None = None


class ProjectCostComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["project_cost"]
    amount_fen: PositiveFen | None = None
    project_nature: Literal["purchased_intangible", "internal_development"] | None = None
    cost_element: (
        Literal["purchase_price", "noncreditable_tax", "directly_attributable_cost"] | None
    ) = None
    rights_controlled: StrictBool | None = None
    development_conditions: DevelopmentCapitalizationFacts | None = None


class ProjectCostAllocation(SourceReference):
    amount_fen: PositiveFen


class ProjectCostExpenseComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["project_cost_expense"]
    cost_sources: list[ProjectCostAllocation] = Field(min_length=1)
    expense_class: Literal["general_expense", "sales_expense", "service_cost"] | None = None


class ServiceFulfillmentComponent(ComponentFacts):
    contributes_tax_sources = True
    kind: Literal["service_fulfillment"]
    amount_fen: PositiveFen | None = None
    source: SourceReference
    fulfillment_date: date | None = None
    tax_obligation_date: date | None = None
    tax_facts: TaxFacts | None = None
    invoice_references: list[InvoiceReference] = Field(default_factory=list)


class CustomerRefundComponent(ComponentFacts):
    contributes_tax_sources = True
    kind: Literal["customer_refund"]
    amount_fen: PositiveFen | None = None
    source: SourceReference
    refund_kind: Literal["advance", "sale_return"]
    tax_obligation_date: date | None = None
    tax_facts: TaxFacts | None = None


class ObligationSettlementComponent(ComponentFacts):
    kind: Literal["receivable_settlement", "payable_settlement"]
    allocations: list[ObligationAllocation] = Field(min_length=1)


class PassThroughComponent(ComponentFacts):
    kind: Literal["pass_through"]
    amount_fen: PositiveFen | None = None


class DebtTransferComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["debt_transfer"]
    payer: CounterpartyRef | None = None
    allocations: list[ObligationAllocation] = Field(min_length=1)


class RefundableDepositComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["refundable_deposit"]
    amount_fen: PositiveFen | None = None
    advanced_by: CounterpartyRef | None = None


class OwnerFundingComponent(ComponentFacts):
    kind: Literal["owner_funding"]
    amount_fen: PositiveFen | None = None
    counterparty: CounterpartyRef | None = None
    funding_kind: Literal["loan", "capital"] | None = None


class OtherIncomeComponent(ComponentFacts):
    kind: Literal["other_income"]
    amount_fen: PositiveFen | None = None
    income_kind: (
        Literal["government_grant", "bank_interest", "retained_verification_payment"] | None
    ) = None


class ManagedAccountReturnComponent(ComponentFacts):
    """Returned funds explicitly previously expensed in an owner-managed account."""

    kind: Literal["managed_account_return"]
    amount_fen: PositiveFen | None = None
    expense_class: Literal["general_expense"] | None = None


class ExpenseRecoveryComponent(ComponentFacts):
    kind: Literal["expense_recovery", "expense_reserve_settlement"]
    amount_fen: PositiveFen | None = None
    source: SourceReference
    allocations: list[ObligationAllocation] = Field(default_factory=list)


class FundsTransferComponent(ComponentFacts):
    kind: Literal["funds_transfer"]
    amount_fen: PositiveFen | None = None


class TaxSettlementComponent(ComponentFacts):
    kind: Literal["tax_settlement"]
    amount_fen: PositiveFen | None = None
    tax_type: Literal["vat", "surtax", "enterprise_income_tax"]
    settlement_kind: Literal["payment", "refund"]
    period_start: date | None = None
    period_end: date | None = None
    assessment_component_key: str | None = Field(default=None, min_length=1, max_length=100)
    income_tax_allocations: list[IncomeTaxSourceAllocation] = Field(default_factory=list)


class TaxReliefComponent(ComponentFacts):
    kind: Literal["tax_relief"]
    start_date: date
    end_date: date
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EnterpriseIncomeTaxAssessmentComponent(ComponentFacts):
    kind: Literal["enterprise_income_tax_assessment"]
    year: int = Field(ge=1, le=9999)
    quarter: int = Field(ge=1, le=4)
    treatment: Literal["accrue", "reduce"]
    amount_fen: PositiveFen | None = None


class EnterpriseIncomeTaxResultComponent(ComponentFacts):
    supports_monthly_recognition = True
    kind: Literal["enterprise_income_tax_result"]
    year: int = Field(ge=2013, le=9998)
    quarter: int = Field(ge=0, le=4)
    previous_result_id: uuid.UUID | None = None
    original_confirmation_id: uuid.UUID | None = None
    declaration_date: ExternalDeclarationDate = None
    amount_basis: Literal["quarter", "year_to_date", "annual", "adjustment_notice"]
    declared_tax_fen: StrictInt | None = Field(default=None, ge=0)
    adjustment_fen: StrictInt | None = None
    previously_recognized_fen: StrictInt | None = None
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PayrollAccrualComponent(ComponentFacts):
    kind: Literal["payroll_accrual"]
    batch_id: uuid.UUID
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    regular_payroll_component_keys: list[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            ),
        ]
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def local_regular_parents_are_unique(self):
        if len(self.regular_payroll_component_keys) != len(
            set(self.regular_payroll_component_keys)
        ):
            raise ValueError("regular_payroll_component_keys must be unique")
        return self


class LaborRemunerationAccrualComponent(ComponentFacts):
    kind: Literal["labor_remuneration_accrual"]
    batch_id: uuid.UUID
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PayrollContributionSupplementComponent(ComponentFacts):
    kind: Literal["payroll_contribution_supplement"]
    employee_id: uuid.UUID
    contribution_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    items: list[PayrollContributionSupplementItem] = Field(min_length=1)
    source: SourceReference | None = None

    @model_validator(mode="after")
    def supplement_shape_is_valid(self):
        keys = [(item.contribution_group.value, item.insurance_kind) for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("items must contain each contribution group and insurance kind once")
        contribution_year = int(self.contribution_period[:4])
        contribution_month = int(self.contribution_period[5:])
        if self.business_date is not None and (contribution_year, contribution_month) >= (
            self.business_date.year,
            self.business_date.month,
        ):
            raise ValueError("contribution_period must precede the posting month")
        return self


class SalarySettlementComponent(ComponentFacts):
    kind: Literal["salary_settlement"]
    amount_fen: Fen | None = None
    allocations: list[ObligationAllocation] = Field(min_length=1)
    withholding_allocations: list[SalaryWithholdingAllocation] = Field(default_factory=list)
    actual_deduction_allocations: list[SalaryActualDeductionAllocation] = Field(
        default_factory=list
    )


class LaborSettlementComponent(ComponentFacts):
    kind: Literal["labor_settlement"]
    source_open_item_id: uuid.UUID | None = None
    source_component_key: str | None = None
    source_open_item_key: str = "primary"
    amount_fen: PositiveFen | None = None
    settlement_mode: Literal["net_after_withholding", "gross_paid_without_withholding"]
    withholding_exception_evidence_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_labor_source(self):
        if (self.source_open_item_id is None) == (self.source_component_key is None):
            raise ValueError("provide exactly one posted or local labor obligation")
        return self


class LaborTaxSettlementComponent(ComponentFacts):
    kind: Literal["labor_tax_settlement"]
    source_open_item_id: uuid.UUID | None = None
    source_component_key: str | None = None
    amount_fen: PositiveFen | None = None

    @model_validator(mode="after")
    def one_tax_source(self):
        if (self.source_open_item_id is None) == (self.source_component_key is None):
            raise ValueError("provide exactly one posted obligation or local withholding component")
        return self


class BorrowingPaymentComponent(ComponentFacts):
    kind: Literal["borrowing_interest_payment", "borrowing_principal_repayment"]
    borrowing_id: uuid.UUID
    accrual_event_id: uuid.UUID | None = None
    accrual_component_key: str | None = None
    amount_fen: PositiveFen | None = None

    @model_validator(mode="after")
    def unambiguous_accrual(self):
        if self.accrual_event_id is not None and self.accrual_component_key is not None:
            raise ValueError("provide only one interest accrual source")
        if self.kind == "borrowing_principal_repayment" and (
            self.accrual_event_id is not None or self.accrual_component_key is not None
        ):
            raise ValueError("principal repayment has no interest accrual source")
        return self


class FixedAssetAcquisitionComponent(ComponentFacts):
    kind: Literal["fixed_asset_acquisition"]
    facts: domain.FixedAssetAcquisitionFacts


class FixedAssetActivationComponent(ComponentFacts):
    kind: Literal["fixed_asset_activation"]
    facts: domain.FixedAssetActivationFacts


class FixedAssetDepreciationComponent(ComponentFacts):
    kind: Literal["fixed_asset_depreciation"]
    facts: domain.FixedAssetDepreciationFacts
    activation_component_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )

    @model_validator(mode="after")
    def local_activation_has_no_ephemeral_asset_id(self):
        if self.activation_component_key is not None and self.facts.asset_id is not None:
            raise ValueError("local activation source must not include asset_id")
        return self


class FixedAssetDepreciationBatchComponent(ComponentFacts):
    kind: Literal["fixed_asset_depreciation_batch"]
    facts: domain.FixedAssetDepreciationBatchFacts
    activation_component_keys: list[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            ),
        ]
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def local_activations_are_unique(self):
        if len(self.activation_component_keys) != len(set(self.activation_component_keys)):
            raise ValueError("activation_component_keys must be unique")
        return self


class FixedAssetDisposalComponent(ComponentFacts):
    contributes_tax_sources = True
    kind: Literal["fixed_asset_disposal"]
    facts: domain.FixedAssetDisposalFacts


class IntangibleAssetAcquisitionComponent(ComponentFacts):
    kind: Literal["intangible_asset_acquisition"]
    facts: domain.IntangibleAssetAcquisitionFacts
    cost_sources: list[ProjectCostAllocation] = Field(default_factory=list)


class IntangibleAssetAmortizationComponent(ComponentFacts):
    kind: Literal["intangible_asset_amortization"]
    facts: domain.IntangibleAssetAmortizationFacts


class IntangibleAssetRetirementComponent(ComponentFacts):
    kind: Literal["intangible_asset_retirement"]
    facts: domain.IntangibleAssetRetirementFacts


class BorrowingDrawdownComponent(ComponentFacts):
    kind: Literal["borrowing_drawdown"]
    facts: domain.BorrowingDrawdownFacts


class BorrowingInterestAccrualComponent(ComponentFacts):
    kind: Literal["borrowing_interest_accrual"]
    facts: domain.BorrowingInterestAccrualFacts


BusinessComponent = Annotated[
    ExpenseBusinessComponent
    | ServiceSaleComponent
    | CustomerAdvanceComponent
    | SupplierAdvanceComponent
    | SupplierAdvanceApplicationComponent
    | SupplierAdvanceRefundComponent
    | ProjectCostComponent
    | ProjectCostExpenseComponent
    | ServiceFulfillmentComponent
    | CustomerRefundComponent
    | ObligationSettlementComponent
    | PassThroughComponent
    | DebtTransferComponent
    | RefundableDepositComponent
    | OwnerFundingComponent
    | OtherIncomeComponent
    | ManagedAccountReturnComponent
    | ExpenseRecoveryComponent
    | FundsTransferComponent
    | TaxSettlementComponent
    | TaxReliefComponent
    | EnterpriseIncomeTaxAssessmentComponent
    | EnterpriseIncomeTaxResultComponent
    | PayrollAccrualComponent
    | LaborRemunerationAccrualComponent
    | PayrollContributionSupplementComponent
    | SalarySettlementComponent
    | LaborSettlementComponent
    | LaborTaxSettlementComponent
    | BorrowingPaymentComponent
    | FixedAssetAcquisitionComponent
    | FixedAssetActivationComponent
    | FixedAssetDepreciationComponent
    | FixedAssetDepreciationBatchComponent
    | FixedAssetDisposalComponent
    | IntangibleAssetAcquisitionComponent
    | IntangibleAssetAmortizationComponent
    | IntangibleAssetRetirementComponent
    | BorrowingDrawdownComponent
    | BorrowingInterestAccrualComponent,
    Field(discriminator="kind"),
]


class FundsAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_key: str
    amount_fen: PositiveFen
    source_allocations: list[ObligationAllocation] = Field(default_factory=list)


class FundsSettlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=94)
    account_code: str = Field(min_length=1, max_length=30)
    direction: Literal["receipt", "payment"]
    payment_date: ActualFundsDate
    amount_fen: PositiveFen
    allocations: list[FundsAllocation] = Field(min_length=1)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def exact_allocation(self) -> FundsSettlement:
        if sum(a.amount_fen for a in self.allocations) != self.amount_fen:
            raise ValueError("funds allocations must equal the real movement amount")
        if len({a.component_key for a in self.allocations}) != len(self.allocations):
            raise ValueError("duplicate component within one funds allocation")
        return self


class RecordEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    posting_date: date
    description: str = Field(
        default="",
        max_length=2000,
        description=(
            "面向负责人的中文业务摘要；AI撰写时使用简体中文，保留原有名称和必要缩写，"
            "准确表述已核对的业务事实。不得直接提交英文工作笔记；不改变原始证据。"
        ),
    )
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    components: list[BusinessComponent] = Field(min_length=1)
    funds: list[FundsSettlement] = Field(default_factory=list)

    def component_dependencies(self, component: ComponentFacts) -> set[str]:
        dependencies = set(component.depends_on)
        source = getattr(component, "source", None)
        if source is not None and source.component_key and not source.event_key:
            dependencies.add(source.component_key)
        dependencies.update(
            a.source_component_key
            for a in [*getattr(component, "allocations", []), *getattr(component, "advances", [])]
            if a.source_component_key and not a.source_event_key
        )
        dependencies.update(
            a.component_key
            for a in getattr(component, "cost_sources", [])
            if a.component_key and not a.event_key
        )
        if getattr(component, "accrual_component_key", None):
            dependencies.add(component.accrual_component_key)
        if getattr(component, "assessment_component_key", None):
            dependencies.add(component.assessment_component_key)
        if getattr(component, "activation_component_key", None):
            dependencies.add(component.activation_component_key)
        dependencies.update(getattr(component, "activation_component_keys", []))
        dependencies.update(getattr(component, "regular_payroll_component_keys", []))
        dependencies.update(
            a.source_component_key
            for a in getattr(component, "income_tax_allocations", [])
            if a.source_component_key
        )
        if getattr(component, "source_component_key", None):
            dependencies.add(component.source_component_key)
        if component.kind == "borrowing_principal_repayment":
            dependencies.update(
                c.accrual_component_key
                for c in self.components
                if c.kind == "borrowing_interest_payment"
                and c.borrowing_id == component.borrowing_id
                and c.accrual_component_key
            )
        return dependencies

    @model_validator(mode="after")
    def valid_component_graph(self) -> RecordEventRequest:
        keys = {c.key for c in self.components}
        if len(keys) != len(self.components):
            raise ValueError("component keys must be unique")
        if len({f.key for f in self.funds}) != len(self.funds):
            raise ValueError("funds keys must be unique")
        if any(c.key.startswith("funds.") for c in self.components):
            raise ValueError("funds. is reserved for internally generated money components")
        graph: dict[str, set[str]] = {}
        for component in self.components:
            dependencies = self.component_dependencies(component)
            if dependencies - keys:
                raise ValueError(f"unknown component dependency: {component.key}")
            graph[component.key] = dependencies
        remaining = dict(graph)
        while remaining:
            ready = {key for key, deps in remaining.items() if not deps}
            if not ready:
                raise ValueError("cyclic component dependencies")
            remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
        for funds in self.funds:
            if {a.component_key for a in funds.allocations} - keys:
                raise ValueError("funds refer to unknown business component")
        return self


class ConfigureAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=100)
    business_class: str = Field(min_length=1, max_length=50)


COMPONENT_TYPES = sorted(
    {
        literal
        for cls in (
            ExpenseBusinessComponent,
            ServiceSaleComponent,
            CustomerAdvanceComponent,
            SupplierAdvanceComponent,
            SupplierAdvanceApplicationComponent,
            SupplierAdvanceRefundComponent,
            ProjectCostComponent,
            ProjectCostExpenseComponent,
            ServiceFulfillmentComponent,
            CustomerRefundComponent,
            ObligationSettlementComponent,
            PassThroughComponent,
            DebtTransferComponent,
            RefundableDepositComponent,
            OwnerFundingComponent,
            OtherIncomeComponent,
            ManagedAccountReturnComponent,
            ExpenseRecoveryComponent,
            FundsTransferComponent,
            TaxSettlementComponent,
            TaxReliefComponent,
            EnterpriseIncomeTaxAssessmentComponent,
            EnterpriseIncomeTaxResultComponent,
            PayrollAccrualComponent,
            LaborRemunerationAccrualComponent,
            PayrollContributionSupplementComponent,
            SalarySettlementComponent,
            LaborSettlementComponent,
            LaborTaxSettlementComponent,
            BorrowingPaymentComponent,
            FixedAssetAcquisitionComponent,
            FixedAssetActivationComponent,
            FixedAssetDepreciationComponent,
            FixedAssetDepreciationBatchComponent,
            FixedAssetDisposalComponent,
            IntangibleAssetAcquisitionComponent,
            IntangibleAssetAmortizationComponent,
            IntangibleAssetRetirementComponent,
            BorrowingDrawdownComponent,
            BorrowingInterestAccrualComponent,
        )
        for literal in cls.model_fields["kind"].annotation.__args__
    }
)
