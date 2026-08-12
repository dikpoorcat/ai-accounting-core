"""Closed business-fact schemas for the Phase-1 intangible-asset workflow."""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
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

from .schemas import BankTransactionReference

MAX_FEN = 2**63 - 1
IntangibleFen = Annotated[StrictInt, Field(ge=0, le=MAX_FEN)]


class IntangibleAssetCategory(StrEnum):
    SOFTWARE = "software"
    PATENT = "patent"
    TRADEMARK = "trademark"
    COPYRIGHT = "copyright"
    NON_PATENTED_TECHNOLOGY = "non_patented_technology"
    OTHER_IDENTIFIABLE_NON_LAND = "other_identifiable_non_land"


class IntangibleAssetAcquisitionSettlement(StrEnum):
    BANK = "bank"
    PAYABLE = "payable"


class IntangibleAssetLifeBasis(StrEnum):
    LEGAL_OR_CONTRACTUAL = "legal_or_contractual"
    RELIABLY_ESTIMATED = "reliably_estimated"
    NOT_RELIABLY_ESTIMATED = "not_reliably_estimated"


class IntangibleAssetBenefitArea(StrEnum):
    MANAGEMENT = "management"
    SALES = "sales"
    SERVICE_DELIVERY = "service_delivery"


class IntangibleAssetResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    REVERSED = "reversed"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class IntangibleAssetCostComponents(BaseModel):
    """Finite capitalisable cost components, each stated in integer fen."""

    model_config = ConfigDict(extra="forbid")

    purchase_price_fen: IntangibleFen | None = None
    noncreditable_tax_fen: IntangibleFen | None = None
    directly_attributable_cost_fen: IntangibleFen | None = None

    def missing_fields(self) -> list[str]:
        return [
            field_name
            for field_name in (
                "purchase_price_fen",
                "noncreditable_tax_fen",
                "directly_attributable_cost_fen",
            )
            if getattr(self, field_name) is None
        ]


class IntangibleAssetInformationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    fields: list[str]


class IntangibleAssetSupplierReference(BaseModel):
    """Strict supplier identity with constraints visible in public JSON Schema."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    kind: Literal["supplier"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    external_ref: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("kind", mode="before")
    @classmethod
    def strip_supplier_kind(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("name", "external_ref")
    @classmethod
    def strip_identity_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("supplier identity text must not be blank")
        return stripped

    @model_validator(mode="after")
    def has_strict_identity(self) -> IntangibleAssetSupplierReference:
        if self.id is None and not (self.kind == "supplier" and self.name):
            raise ValueError("supplier requires id or kind='supplier' with name")
        return self


class AcquireIntangibleAssetRequest(BaseModel):
    """Explicit facts for one purchased and already-usable intangible asset."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    asset_code: str | None = Field(default=None, min_length=1, max_length=100)
    asset_name: str | None = Field(default=None, min_length=1, max_length=200)
    category: IntangibleAssetCategory | None = None
    rights_description: str | None = Field(default=None, min_length=1, max_length=2000)
    other_right_type_description: str | None = Field(default=None, min_length=1, max_length=500)
    identifiability_basis: str | None = Field(default=None, min_length=1, max_length=2000)
    supplier: IntangibleAssetSupplierReference | None = None
    acquisition_date: date | None = None
    available_for_use_date: date | None = None
    posting_date: date | None = None
    cost_components: IntangibleAssetCostComponents = Field(
        default_factory=IntangibleAssetCostComponents
    )
    settlement_method: IntangibleAssetAcquisitionSettlement | None = None
    bank_account_code: str | None = Field(default=None, min_length=1, max_length=30)
    payment_date: date | None = None
    due_date: date | None = None
    benefit_area: IntangibleAssetBenefitArea | None = None
    life_basis: IntangibleAssetLifeBasis | None = None
    useful_life_months: Annotated[StrictInt, Field(gt=0, le=119_988)] | None = None
    life_basis_explanation: str | None = Field(default=None, min_length=1, max_length=2000)
    is_available_for_use: StrictBool | None = None
    claims_creditable_input_vat: StrictBool | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @field_validator(
        "asset_code",
        "asset_name",
        "rights_description",
        "other_right_type_description",
        "identifiability_basis",
        "life_basis_explanation",
    )
    @classmethod
    def strip_required_business_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("business text must not be blank")
        return stripped

    @model_validator(mode="after")
    def locally_consistent_facts(self) -> AcquireIntangibleAssetRequest:
        if (
            self.posting_date
            and self.acquisition_date
            and self.posting_date < self.acquisition_date
        ):
            raise ValueError("posting_date must not precede acquisition_date")
        if self.due_date and self.acquisition_date and self.due_date < self.acquisition_date:
            raise ValueError("due_date must not precede acquisition_date")
        if (
            self.category is not None
            and self.category is not IntangibleAssetCategory.OTHER_IDENTIFIABLE_NON_LAND
            and (
                self.other_right_type_description is not None
                or self.identifiability_basis is not None
            )
        ):
            raise ValueError(
                "other right type and identifiability basis are only accepted for "
                "other_identifiable_non_land"
            )
        if self.settlement_method is IntangibleAssetAcquisitionSettlement.PAYABLE and (
            self.bank_account_code is not None or self.bank_transaction_references
        ):
            raise ValueError("a supplier-payable acquisition must not include bank facts")
        return self

    def missing_information(self) -> list[IntangibleAssetInformationRequirement]:
        missing: list[IntangibleAssetInformationRequirement] = []
        identity = [
            field_name
            for field_name in ("asset_code", "asset_name", "category", "rights_description")
            if getattr(self, field_name) is None
        ]
        if identity:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_IDENTITY_REQUIRED",
                    message=(
                        "asset code, name, supported category, and rights description are required"
                    ),
                    fields=identity,
                )
            )
        if self.category is IntangibleAssetCategory.OTHER_IDENTIFIABLE_NON_LAND:
            fields = [
                field_name
                for field_name in ("other_right_type_description", "identifiability_basis")
                if getattr(self, field_name) is None
            ]
            if fields:
                missing.append(
                    IntangibleAssetInformationRequirement(
                        code="INTANGIBLE_ASSET_OTHER_RIGHT_FACTS_REQUIRED",
                        message="other rights require their type and identifiability basis",
                        fields=fields,
                    )
                )
        dates = [
            field_name
            for field_name in ("acquisition_date", "available_for_use_date", "posting_date")
            if getattr(self, field_name) is None
        ]
        if dates:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_ACQUISITION_DATES_REQUIRED",
                    message="acquisition, available-for-use, and posting dates are required",
                    fields=dates,
                )
            )
        if fields := self.cost_components.missing_fields():
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_COST_COMPONENTS_REQUIRED",
                    message="every acquisition cost component must be stated, including zero",
                    fields=[f"cost_components.{item}" for item in fields],
                )
            )
        required = [
            field_name
            for field_name in (
                "supplier",
                "settlement_method",
                "benefit_area",
                "life_basis",
                "useful_life_months",
                "life_basis_explanation",
                "is_available_for_use",
                "claims_creditable_input_vat",
            )
            if getattr(self, field_name) is None
        ]
        if required:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_POLICY_FACTS_REQUIRED",
                    message=(
                        "supplier, settlement, readiness, life, VAT, and benefit facts are required"
                    ),
                    fields=required,
                )
            )
        if self.settlement_method is IntangibleAssetAcquisitionSettlement.BANK:
            fields = []
            if self.bank_account_code is None:
                fields.append("bank_account_code")
            if self.payment_date is None:
                fields.append("payment_date")
            if fields:
                missing.append(
                    IntangibleAssetInformationRequirement(
                        code="INTANGIBLE_ASSET_BANK_SETTLEMENT_FACTS_REQUIRED",
                        message="bank settlement requires its account code and payment date",
                        fields=fields,
                    )
                )
        elif (
            self.settlement_method is IntangibleAssetAcquisitionSettlement.PAYABLE
            and self.due_date is None
        ):
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_DUE_DATE_REQUIRED",
                    message="supplier-payable settlement requires its due date",
                    fields=["due_date"],
                )
            )
        if not self.evidence_references:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_EVIDENCE_REQUIRED",
                    message="at least one acquisition evidence reference is required",
                    fields=["evidence_references"],
                )
            )
        return missing


class PreviewIntangibleAssetAmortizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    asset_id: uuid.UUID | None = None
    amortization_period: str | None = Field(default=None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    posting_date: date | None = None

    @field_validator("amortization_period")
    @classmethod
    def reject_year_zero(cls, value: str | None) -> str | None:
        if value is not None and value.startswith("0000-"):
            raise ValueError("amortization period year must be 0001 through 9999")
        return value

    def missing_information(self) -> list[IntangibleAssetInformationRequirement]:
        fields = [
            field_name
            for field_name in ("asset_id", "amortization_period", "posting_date")
            if getattr(self, field_name) is None
        ]
        return (
            [
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_AMORTIZATION_FACTS_REQUIRED",
                    message="asset, YYYY-MM amortization period, and posting date are required",
                    fields=fields,
                )
            ]
            if fields
            else []
        )


class ConfirmIntangibleAssetAmortizationRequest(PreviewIntangibleAssetAmortizationRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_note: str = Field(default="", max_length=2000)

    def missing_information(self) -> list[IntangibleAssetInformationRequirement]:
        missing = super().missing_information()
        fields = ["calculation_hash"] if self.calculation_hash is None else []
        if fields:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_CONFIRMATION_REQUIRED",
                    message="calculation hash is required",
                    fields=fields,
                )
            )
        return missing


class RetireIntangibleAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    asset_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    retirement_date: date | None = None
    posting_date: date | None = None
    gross_proceeds_fen: IntangibleFen | None = None
    compensation_fen: IntangibleFen | None = None
    taxes_and_fees_fen: IntangibleFen | None = None
    residual_proceeds_fen: IntangibleFen | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def locally_consistent_dates(self) -> RetireIntangibleAssetRequest:
        if self.posting_date and self.retirement_date and self.posting_date < self.retirement_date:
            raise ValueError("posting_date must not precede retirement_date")
        return self

    def missing_information(self) -> list[IntangibleAssetInformationRequirement]:
        missing: list[IntangibleAssetInformationRequirement] = []
        fields = [
            field_name
            for field_name in (
                "asset_id",
                "retirement_date",
                "posting_date",
                "gross_proceeds_fen",
                "compensation_fen",
                "taxes_and_fees_fen",
                "residual_proceeds_fen",
            )
            if getattr(self, field_name) is None
        ]
        if fields:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_RETIREMENT_FACTS_REQUIRED",
                    message="retirement dates and every zero-income amount must be stated",
                    fields=fields,
                )
            )
        if not self.evidence_references:
            missing.append(
                IntangibleAssetInformationRequirement(
                    code="INTANGIBLE_ASSET_EVIDENCE_REQUIRED",
                    message="at least one retirement evidence reference is required",
                    fields=["evidence_references"],
                )
            )
        return missing


class IntangibleAssetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IntangibleAssetResultStatus
    asset_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    calculation_hash: str | None = None
    missing_information: list[IntangibleAssetInformationRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
