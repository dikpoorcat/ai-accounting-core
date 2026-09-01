from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import PayrollEmployeeItem, PayrollWageTaxDeclarationState


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
    regular_payroll_items: list[PayrollEmployeeItem] | None = Field(
        default=None,
        min_length=1,
        description=(
            "可选的当月常规工资确定事实。内核优先复用本期已有工资草稿；老板确认无变化时也可"
            "沿用最近一期已过账工资。仅在首次建立或确有变动且内核没有现成方案时提供。"
        ),
    )
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
    def regular_payroll_items_are_monthly_wage_facts(
        self,
    ) -> ConfirmWorkforceReviewRequest:
        if self.regular_payroll_items is None:
            return self
        employee_ids = [item.employee_id for item in self.regular_payroll_items]
        if len(employee_ids) != len(set(employee_ids)):
            raise ValueError("regular_payroll_items must contain each employee once")
        for item in self.regular_payroll_items:
            if item.annual_bonus_fen or item.regular_payroll_batch_id is not None:
                raise ValueError("regular_payroll_items only accepts regular monthly wage facts")
            if (
                item.wage_tax_declaration_state == PayrollWageTaxDeclarationState.DECLARED
                and item.tax_reported_salary_fen is None
            ):
                raise ValueError("tax_reported_salary_fen is required for a declared regular wage")
            if item.wage_tax_declaration_state == PayrollWageTaxDeclarationState.NOT_DECLARED and (
                item.tax_reported_salary_fen is not None
                or item.accounting_gross_salary_fen not in {None, 0}
                or item.tax_reporting_difference_reason is not None
                or item.special_additional_deduction_fen
                or item.other_legal_deduction_fen
                or item.tax_relief_fen
            ):
                raise ValueError("not-declared regular wage cannot include wage-tax facts")
            if (
                item.wage_tax_declaration_state == PayrollWageTaxDeclarationState.DECLARED
                and item.tax_reported_salary_fen is not None
            ):
                accounting_gross = (
                    item.accounting_gross_salary_fen
                    if item.accounting_gross_salary_fen is not None
                    else item.tax_reported_salary_fen
                )
                differs = accounting_gross != item.tax_reported_salary_fen
                if differs and not (
                    item.tax_reporting_difference_reason
                    and item.tax_reporting_difference_reason.strip()
                ):
                    raise ValueError(
                        "tax_reporting_difference_reason is required for a wage difference"
                    )
                if differs and not self.evidence_references:
                    raise ValueError("evidence is required for a wage reporting difference")
        return self


class ConfirmPayrollContributionAssessmentRequest(_StrictRequest):
    org_id: uuid.UUID
    period_id: uuid.UUID
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    declaration_status: Literal["declared"]
    declaration_date: date
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
