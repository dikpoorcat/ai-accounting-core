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

Fen = Annotated[StrictInt, Field(ge=0)]
PositiveFen = Annotated[StrictInt, Field(gt=0)]


class LaborResultStatus(StrEnum):
    REGISTERED = "registered"
    CALCULATED = "calculated"
    POSTED = "posted"
    DELETED = "deleted"
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
            for name in ("name", "relationship_start_date", "status")
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
    gross_remuneration_fen: Fen | None = None
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
        parts = (self.fixed_fee_fen, self.commission_fen)
        if any(value is not None for value in parts):
            if any(value is None for value in parts):
                raise ValueError("provide both remuneration breakdown amounts or omit both")
            total = sum(parts)
            if self.gross_remuneration_fen is None:
                self.gross_remuneration_fen = total
            elif self.gross_remuneration_fen != total:
                raise ValueError("remuneration breakdown must equal gross_remuneration_fen")
        if (
            self.service_start_date is not None
            and self.service_end_date is not None
            and self.service_end_date < self.service_start_date
        ):
            raise ValueError("service_end_date must not precede service_start_date")
        return self

    def missing_fields(self, index: int) -> list[str]:
        fields = [
            name
            for name in (
                "labor_person_id",
                "service_start_date",
                "service_end_date",
                "gross_remuneration_fen",
                "expense_role",
                "tax_identity",
                "income_grouping",
                "is_full_time_student",
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
    confirmation_note: str = Field(default="", max_length=2000)


class ConfirmLaborExternalDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    labor_line_id: uuid.UUID
    declaration_date: date
    external_declaration_reference: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    evidence_references: list[uuid.UUID] = Field(min_length=1, max_length=100)


class GetLaborRemunerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    labor_person_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def exactly_one_identity(self) -> GetLaborRemunerationRequest:
        supplied = [self.labor_person_id, self.batch_id]
        if sum(item is not None for item in supplied) != 1:
            raise ValueError("provide exactly one labor_person_id or batch_id")
        return self


class LaborResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LaborResultStatus
    labor_person_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    calculation_hash: str | None = None
    missing_information: list[LaborInformationRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
