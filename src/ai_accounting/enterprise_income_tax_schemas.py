"""External CIT results and evidenced settlements; never arbitrary journal entries."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class IncomeTaxSourceAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID
    amount_fen: StrictInt = Field(gt=0)


class PreviewEnterpriseIncomeTaxResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    year: int = Field(ge=2013, le=9998)
    quarter: int = Field(ge=0, le=4, description="1–4季度；0表示年度汇算")
    previous_result_id: uuid.UUID | None = None
    original_confirmation_id: uuid.UUID | None = None
    declaration_date: date
    posting_date: date
    declaration_reference: str = Field(min_length=1, max_length=200)
    amount_basis: Literal["quarter", "year_to_date", "annual", "adjustment_notice"]
    declared_tax_fen: StrictInt | None = Field(default=None, ge=0)
    adjustment_fen: StrictInt | None = None
    previously_recognized_fen: StrictInt | None = None
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("declaration_reference", "confirmation_note")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def valid_shape(self) -> PreviewEnterpriseIncomeTaxResultRequest:
        if self.quarter == 0 and self.amount_basis in {"quarter", "year_to_date"}:
            raise ValueError("annual result requires annual or adjustment_notice basis")
        if self.quarter and self.amount_basis == "annual":
            raise ValueError("quarter result cannot use annual basis")
        if self.amount_basis == "adjustment_notice":
            if self.declared_tax_fen is not None:
                raise ValueError("adjustment notice cannot also supply declared_tax_fen")
        elif self.adjustment_fen is not None or self.previously_recognized_fen is not None:
            raise ValueError("adjustment fields are exclusive to adjustment_notice")
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("duplicate evidence")
        return self


class ConfirmEnterpriseIncomeTaxResultRequest(PreviewEnterpriseIncomeTaxResultRequest):
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r".*\S.*")


class QueryEnterpriseIncomeTaxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    year: int | None = Field(default=None, ge=2013, le=9998)
    as_of: date | None = None


class LinkEnterpriseIncomeTaxPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    event_id: uuid.UUID
    allocations: list[IncomeTaxSourceAllocation] = Field(min_length=1, max_length=100)
    evidence_references: list[uuid.UUID] = Field(min_length=1, max_length=100)
    confirmation_note: str = Field(min_length=1, max_length=2000, pattern=r".*\S.*")
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r".*\S.*")
