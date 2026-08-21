"""Strict public facts for non-employee personal labor remuneration.

The public contract deliberately contains no account code, journal side, tax
rate, quick deduction, or caller-supplied calculation.  Those facts are owned
by the effective policy and deterministic posting templates.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from .schemas import Allocation, SalaryWithholdingAllocation

Fen = Annotated[StrictInt, Field(ge=0)]
PositiveFen = Annotated[StrictInt, Field(gt=0)]


class LaborResultStatus(StrEnum):
    REGISTERED = "registered"
    CALCULATED = "calculated"
    POSTED = "posted"
    REVERSED = "reversed"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class LaborInformationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    fields: list[str]
    message: str


class RegisterLaborServicePersonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    person_code: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    relationship_start_date: date | None = None
    relationship_end_date: date | None = None
    status: Literal["active", "ended"] | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> RegisterLaborServicePersonRequest:
        if (
            self.relationship_start_date is not None
            and self.relationship_end_date is not None
            and self.relationship_end_date < self.relationship_start_date
        ):
            raise ValueError("relationship_end_date must not precede relationship_start_date")
        if self.status == "active" and self.relationship_end_date is not None:
            raise ValueError("active labor relationship must not have an end date")
        if self.status == "ended" and self.relationship_end_date is None:
            raise ValueError("ended labor relationship requires an end date")
        return self

    def missing_fields(self) -> list[str]:
        fields = [
            name
            for name in ("person_code", "name", "relationship_start_date", "status")
            if getattr(self, name) is None
        ]
        if not self.evidence_references:
            fields.append("evidence_references")
        return fields


class EndLaborServicePersonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    labor_person_id: uuid.UUID
    relationship_end_date: date
    idempotency_key: str = Field(min_length=1, max_length=200)
    evidence_references: list[uuid.UUID] = Field(min_length=1, max_length=100)


class LaborRemunerationItemFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labor_person_id: uuid.UUID | None = None
    service_start_date: date | None = None
    service_end_date: date | None = None
    fixed_fee_fen: Fen | None = None
    commission_fen: Fen | None = None
    expense_role: (
        Literal["labor_management_expense", "labor_sales_expense", "labor_service_cost"] | None
    ) = None
    tax_identity: Literal["resident", "nonresident"] | None = None
    income_grouping: Literal["single_occurrence", "continuous_monthly"] | None = None
    is_full_time_student: StrictBool | None = None
    external_declaration_status: Literal["not_due", "pending", "confirmed"] | None = None
    external_declaration_reference: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def service_dates_are_ordered(self) -> LaborRemunerationItemFacts:
        if (
            self.service_start_date is not None
            and self.service_end_date is not None
            and self.service_end_date < self.service_start_date
        ):
            raise ValueError("service_end_date must not precede service_start_date")
        if (
            self.external_declaration_status == "confirmed"
            and not self.external_declaration_reference
        ):
            raise ValueError("confirmed declaration status requires a reference")
        if self.external_declaration_status != "confirmed" and self.external_declaration_reference:
            raise ValueError("declaration reference is only accepted for confirmed status")
        return self

    def missing_fields(self, index: int) -> list[str]:
        fields = [
            name
            for name in (
                "labor_person_id",
                "service_start_date",
                "service_end_date",
                "fixed_fee_fen",
                "commission_fen",
                "expense_role",
                "tax_identity",
                "income_grouping",
                "is_full_time_student",
                "external_declaration_status",
            )
            if getattr(self, name) is None
        ]
        return [f"items.{index}.{name}" for name in fields]


class PreviewLaborRemunerationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    remuneration_period: str | None = Field(default=None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    business_date: date | None = None
    posting_date: date | None = None
    planned_payment_date: date | None = None
    items: list[LaborRemunerationItemFacts] = Field(default_factory=list, max_length=1000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    description: str = Field(default="个人劳务报酬计提", max_length=2000)

    def missing_fields(self) -> list[str]:
        fields = [
            name
            for name in (
                "remuneration_period",
                "business_date",
                "posting_date",
                "planned_payment_date",
            )
            if getattr(self, name) is None
        ]
        if not self.items:
            fields.append("items")
        for index, item in enumerate(self.items):
            fields.extend(item.missing_fields(index))
        if not self.evidence_references:
            fields.append("evidence_references")
        return fields


class ConfirmLaborRemunerationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    batch_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_note: str = Field(min_length=1, max_length=2000)


class LaborPayoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_open_item_id: uuid.UUID | None = None
    settlement_mode: Literal["net_after_withholding", "gross_paid_without_withholding"] | None = (
        None
    )


class PreviewUnifiedPayoutRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    business_date: date | None = None
    payment_date: date | None = None
    posting_date: date | None = None
    bank_account_code: str | None = Field(default=None, min_length=1, max_length=30)
    bank_transaction_id: uuid.UUID | None = None
    salary_allocations: list[Allocation] = Field(default_factory=list, max_length=1000)
    salary_withholding_allocations: list[SalaryWithholdingAllocation] = Field(
        default_factory=list, max_length=1000
    )
    labor_items: list[LaborPayoutItem] = Field(default_factory=list, max_length=1000)
    withholding_agency_code: str | None = Field(default=None, min_length=1, max_length=100)
    withholding_agency_name: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    withholding_exception_evidence_references: list[uuid.UUID] = Field(
        default_factory=list, max_length=100
    )
    description: str = Field(default="工资及个人劳务统一发放", max_length=2000)

    @model_validator(mode="after")
    def exception_evidence_is_scoped(self) -> PreviewUnifiedPayoutRunRequest:
        gross_without_withholding = any(
            item.settlement_mode == "gross_paid_without_withholding" for item in self.labor_items
        )
        if not gross_without_withholding and self.withholding_exception_evidence_references:
            raise ValueError(
                "withholding exception evidence is only accepted for gross_paid_without_withholding"
            )
        if not set(self.withholding_exception_evidence_references).issubset(
            self.evidence_references
        ):
            raise ValueError(
                "withholding exception evidence must also be included in evidence_references"
            )
        return self

    def missing_fields(self) -> list[str]:
        fields = [
            name
            for name in (
                "business_date",
                "payment_date",
                "posting_date",
                "bank_account_code",
                "bank_transaction_id",
            )
            if getattr(self, name) is None
        ]
        if not self.salary_allocations and not self.labor_items:
            fields.append("salary_allocations_or_labor_items")
        if self.salary_allocations and not self.salary_withholding_allocations:
            fields.append("salary_withholding_allocations")
        for index, item in enumerate(self.labor_items):
            if item.source_open_item_id is None:
                fields.append(f"labor_items.{index}.source_open_item_id")
            if item.settlement_mode is None:
                fields.append(f"labor_items.{index}.settlement_mode")
        if any(item.settlement_mode == "net_after_withholding" for item in self.labor_items) and (
            self.withholding_agency_code is None or self.withholding_agency_name is None
        ):
            if self.withholding_agency_code is None:
                fields.append("withholding_agency_code")
            if self.withholding_agency_name is None:
                fields.append("withholding_agency_name")
        if (
            any(
                item.settlement_mode == "gross_paid_without_withholding"
                for item in self.labor_items
            )
            and not self.withholding_exception_evidence_references
        ):
            fields.append("withholding_exception_evidence_references")
        if not self.evidence_references:
            fields.append("evidence_references")
        return fields


class ConfirmUnifiedPayoutRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    payout_run_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_note: str = Field(min_length=1, max_length=2000)


class PayLaborWithholdingTaxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    business_date: date | None = None
    payment_date: date | None = None
    posting_date: date | None = None
    amount_fen: PositiveFen | None = None
    bank_account_code: str | None = Field(default=None, min_length=1, max_length=30)
    bank_transaction_id: uuid.UUID | None = None
    allocations: list[Allocation] = Field(default_factory=list, max_length=1000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    description: str = Field(default="个人劳务报酬个税缴纳", max_length=2000)

    def missing_fields(self) -> list[str]:
        fields = [
            name
            for name in (
                "business_date",
                "payment_date",
                "posting_date",
                "amount_fen",
                "bank_account_code",
                "bank_transaction_id",
            )
            if getattr(self, name) is None
        ]
        if not self.allocations:
            fields.append("allocations")
        if not self.evidence_references:
            fields.append("evidence_references")
        return fields


class ConfirmLaborExternalDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    labor_line_id: uuid.UUID
    declaration_date: date
    external_declaration_reference: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    evidence_references: list[uuid.UUID] = Field(min_length=1, max_length=100)


class GetLaborRemunerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    labor_person_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    payout_run_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def exactly_one_identity(self) -> GetLaborRemunerationRequest:
        supplied = [self.labor_person_id, self.batch_id, self.payout_run_id]
        if sum(item is not None for item in supplied) != 1:
            raise ValueError("provide exactly one labor_person_id, batch_id, or payout_run_id")
        return self


class LaborResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LaborResultStatus
    labor_person_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    payout_run_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    calculation_hash: str | None = None
    missing_information: list[LaborInformationRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
