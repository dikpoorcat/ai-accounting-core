from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetOwnerWorkflowRequest(_StrictRequest):
    org_id: uuid.UUID
    period_id: uuid.UUID | None = None


class PreviewPayrollContributionAssessmentRequest(_StrictRequest):
    org_id: uuid.UUID
    period_id: uuid.UUID


class ConfirmWorkforceReviewRequest(_StrictRequest):
    org_id: uuid.UUID
    period_id: uuid.UUID
    workforce_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_state: Literal["no_change", "changes_resolved"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    supersedes_confirmation_id: uuid.UUID | None = None

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def required_text_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class ConfirmPayrollContributionAssessmentRequest(_StrictRequest):
    org_id: uuid.UUID
    period_id: uuid.UUID
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    declaration_status: Literal["declared_paid", "declared_unpaid", "not_declared"]
    declaration_date: date | None = None
    payment_date: date | None = None
    external_reference: str | None = Field(default=None, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    supersedes_confirmation_id: uuid.UUID | None = None

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def required_text_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    @field_validator("external_reference")
    @classmethod
    def optional_text_is_trimmed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def status_dates_are_consistent(self) -> ConfirmPayrollContributionAssessmentRequest:
        if self.declaration_status == "declared_paid":
            if self.declaration_date is None or self.payment_date is None:
                raise ValueError("declared_paid requires declaration_date and payment_date")
        elif self.declaration_status == "declared_unpaid":
            if self.declaration_date is None or self.payment_date is not None:
                raise ValueError("declared_unpaid requires only declaration_date")
        elif self.declaration_date is not None or self.payment_date is not None:
            raise ValueError("not_declared forbids declaration_date and payment_date")
        return self


class ConfirmPeriodMaterialCompletenessRequest(_StrictRequest):
    org_id: uuid.UUID
    period_id: uuid.UUID
    activity_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    supersedes_confirmation_id: uuid.UUID | None = None

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def required_text_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class ConfirmExternalObligationRequest(_StrictRequest):
    org_id: uuid.UUID
    obligation_id: uuid.UUID
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_status: Literal["submitted", "not_applicable"]
    completion_date: date
    external_reference: str | None = Field(default=None, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    supersedes_confirmation_id: uuid.UUID | None = None

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def required_text_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    @field_validator("external_reference")
    @classmethod
    def optional_text_is_trimmed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ConfirmHistoricalObligationCompletionRequest(_StrictRequest):
    """Confirm an applicable history cutoff without inventing filing dates."""

    org_id: uuid.UUID
    obligation_code: Literal[
        "periodic_tax_reporting",
        "annual_enterprise_income_tax",
        "annual_business_report",
    ]
    completion_through_identity: str = Field(min_length=4, max_length=7)
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_date_status: Literal["not_established"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    supersedes_confirmation_id: uuid.UUID | None = None

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def required_text_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    @model_validator(mode="after")
    def completion_identity_matches_obligation(
        self,
    ) -> ConfirmHistoricalObligationCompletionRequest:
        identity = self.completion_through_identity
        if self.obligation_code == "periodic_tax_reporting":
            valid = (
                len(identity) == 7
                and identity[:4].isdigit()
                and (
                    (identity[4:6] == "-Q" and identity[6] in "1234")
                    or (
                        identity[4] == "-"
                        and identity[5:].isdigit()
                        and 1 <= int(identity[5:]) <= 12
                    )
                )
            )
        else:
            valid = len(identity) == 4 and identity.isdigit()
        if not valid:
            raise ValueError("completion_through_identity does not match obligation_code")
        return self


class ConfirmOrganizationEstablishmentRequest(_StrictRequest):
    org_id: uuid.UUID
    establishment_date: date
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    supersedes_confirmation_id: uuid.UUID | None = None

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def required_text_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value
