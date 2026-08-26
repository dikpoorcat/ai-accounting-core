"""Strict public contracts for small-enterprise quarterly financial statements."""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


class FinancialStatementResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class FinancialStatementDetailCode(StrEnum):
    MANAGEMENT_STARTUP = "management_startup"
    MANAGEMENT_ENTERTAINMENT = "management_entertainment"
    MANAGEMENT_RESEARCH = "management_research"
    MANAGEMENT_OTHER = "management_other"
    SALES_MERCHANDISE_REPAIR = "sales_merchandise_repair"
    SALES_ADVERTISING_PROMOTION = "sales_advertising_promotion"
    SALES_OTHER = "sales_other"
    FINANCE_INTEREST = "finance_interest"
    FINANCE_OTHER = "finance_other"


class EnterpriseIncomeTaxTreatment(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ZERO = "zero"
    ACCRUE = "accrue"
    REDUCE = "reduce"


class FinancialStatementInformationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    fields: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class PreviewQuarterlyFinancialStatementsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    year: int = Field(ge=1, le=9999)
    quarter: int = Field(ge=1, le=4)


class GetFinancialStatementRequirementsRequest(PreviewQuarterlyFinancialStatementsRequest):
    pass


class FinancialStatementClassificationAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail_code: FinancialStatementDetailCode
    amount_fen: StrictInt = Field(gt=0)


class ConfirmFinancialStatementClassificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    voucher_line_id: uuid.UUID
    allocations: list[FinancialStatementClassificationAllocation] = Field(
        min_length=1, max_length=20
    )
    supersedes_classification_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class ConfirmEnterpriseIncomeTaxQuarterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    year: int = Field(ge=1, le=9999)
    quarter: int = Field(ge=1, le=4)
    treatment: EnterpriseIncomeTaxTreatment
    amount_fen: StrictInt = Field(ge=0)
    posting_date: date | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    @model_validator(mode="after")
    def valid_treatment_shape(self) -> ConfirmEnterpriseIncomeTaxQuarterRequest:
        if self.treatment in {
            EnterpriseIncomeTaxTreatment.NOT_APPLICABLE,
            EnterpriseIncomeTaxTreatment.ZERO,
        }:
            if self.amount_fen != 0 or self.posting_date is not None:
                raise ValueError("zero or not-applicable confirmation cannot post an amount")
        elif self.amount_fen <= 0 or self.posting_date is None:
            raise ValueError("accrual or reduction requires a positive amount and posting date")
        return self


class FinancialStatementResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinancialStatementResultStatus
    calculation_hash: str | None = None
    classification_id: uuid.UUID | None = None
    enterprise_income_tax_confirmation_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    missing_information: list[FinancialStatementInformationRequirement] = Field(
        default_factory=list
    )
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
