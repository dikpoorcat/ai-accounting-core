"""Strict public contracts for multi-company lifecycle operations."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .taxpayer_identity import normalize_taxpayer_identification_number

FilingCycle = Literal["monthly", "quarterly"]
CompanyTargetStatus = Literal["active", "archived"]
_URBAN_RATES = {Decimal("0.07"), Decimal("0.05"), Decimal("0.01")}


class _CompanyFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    taxpayer_identification_number: str
    effective_from: date
    filing_cycle: FilingCycle
    urban_maintenance_rate: Decimal
    confirmation_note: str = Field(min_length=1, max_length=2000)

    @field_validator("name", "confirmation_note")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("required text is blank")
        return stripped

    @field_validator("taxpayer_identification_number")
    @classmethod
    def valid_taxpayer_id(cls, value: str) -> str:
        return normalize_taxpayer_identification_number(value)

    @field_validator("urban_maintenance_rate")
    @classmethod
    def valid_urban_rate(cls, value: Decimal) -> Decimal:
        if value not in _URBAN_RATES:
            raise ValueError("unsupported urban maintenance rate")
        return value


class CreateCompanyRequest(_CompanyFacts):
    idempotency_key: str = Field(min_length=1, max_length=200)
    make_primary: bool = False


class PreviewCompanyProfileChangeRequest(_CompanyFacts):
    org_id: uuid.UUID
    evidence_references: list[uuid.UUID] = Field(min_length=1)


class ConfirmCompanyProfileChangeRequest(PreviewCompanyProfileChangeRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreviewCompanyStatusChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    target_status: CompanyTargetStatus
    confirmation_note: str = Field(min_length=1, max_length=2000)

    @field_validator("confirmation_note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("confirmation note is blank")
        return stripped


class ConfirmCompanyStatusChangeRequest(PreviewCompanyStatusChangeRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfigureCloseBackupRequest(BaseModel):
    """Owner-confirmed local destination used after every successful close."""

    model_config = ConfigDict(extra="forbid")

    backup_directory: str = Field(min_length=3, max_length=2048)
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)

    @field_validator("backup_directory", "idempotency_key", "confirmation_note")
    @classmethod
    def strip_close_backup_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("required text is blank")
        return stripped
