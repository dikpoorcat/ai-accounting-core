"""Strict public facts and results for deterministic accounting-period control.

The models deliberately contain no journal lines, balances supplied by a caller,
or caller-supplied check outcomes.  A close is an internal-control record, not a
financial report or statutory filing.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class AccountingPeriodResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class AccountingPeriodInformationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    fields: list[str]


class AccountingPeriodReviewFacts(BaseModel):
    """Explicit human review declarations; no authorization is implied."""

    model_config = ConfigDict(extra="forbid")

    voucher_completeness_reviewed: StrictBool | None = None
    bank_reconciliation_reviewed: StrictBool | None = None
    open_items_reviewed: StrictBool | None = None
    payroll_and_statutory_items_reviewed: StrictBool | None = None
    # Optional management checklist; these flags do not authorize or gate close.
    payroll_settlements_reviewed: StrictBool | None = None
    tax_items_reviewed: StrictBool | None = None
    asset_and_borrowing_schedules_reviewed: StrictBool | None = None

    def missing_fields(self) -> list[str]:
        return [
            field_name
            for field_name in type(self).model_fields
            if field_name != "payroll_settlements_reviewed" and getattr(self, field_name) is None
        ]

    def false_fields(self) -> list[str]:
        return [
            field_name
            for field_name in type(self).model_fields
            if field_name != "payroll_settlements_reviewed" and getattr(self, field_name) is False
        ]


class GenerateAccountingPeriodRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    confirmation_note: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("period_month")
    @classmethod
    def reject_year_zero(cls, value: str) -> str:
        if value.startswith("0000-"):
            raise ValueError("period month year must be 0001 through 9999")
        return value

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    def missing_information(self) -> list[AccountingPeriodInformationRequirement]:
        fields = ["idempotency_key"] if self.idempotency_key is None else []
        return (
            [
                AccountingPeriodInformationRequirement(
                    code="ACCOUNTING_PERIOD_GENERATION_CONFIRMATION_REQUIRED",
                    message=("an idempotency key is required to generate periods"),
                    fields=fields,
                )
            ]
            if fields
            else []
        )


class PreviewAccountingPeriodCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_id: uuid.UUID
    closing_date: date


class RequestAccountingPeriodCloseApprovalWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_id: uuid.UUID
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GetAccountingPeriodCloseApprovalRequest(RequestAccountingPeriodCloseApprovalWindowRequest):
    pass


class ConfigureHistoricalTestCloseModeRequest(BaseModel):
    """Explicit temporary control for a disposable historical rebuild database."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    enabled: StrictBool
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation_note: str = Field(min_length=1, max_length=2000)

    @field_validator("idempotency_key", "confirmation_note")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class ConfirmAccountingPeriodCloseRequest(PreviewAccountingPeriodCloseRequest):
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    management_commentary_context_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "经营结论生成依据，复用关账预览 assistant_review_checklist.management_commentary."
            "context_hash；由 AI 提交，不向负责人索要。"
        ),
    )
    management_commentary: str | None = Field(
        default=None,
        min_length=1,
        max_length=1200,
        description=(
            "AI 必须依据关账预览的 context、instruction 和 success_criteria 生成并提交的"
            "经营结论，供负责人关账前审阅；属于关账交付内容，不是负责人需补充的核算事实。"
        ),
    )
    owner_approval_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    review_facts: AccountingPeriodReviewFacts = Field(default_factory=AccountingPeriodReviewFacts)
    confirmation_note: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("idempotency_key", "confirmation_note", "management_commentary")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    def missing_information(self) -> list[AccountingPeriodInformationRequirement]:
        fields = [
            name
            for name in (
                "calculation_hash",
                "idempotency_key",
            )
            if getattr(self, name) is None
        ]
        requirements = (
            [
                AccountingPeriodInformationRequirement(
                    code="ACCOUNTING_PERIOD_CLOSE_CONFIRMATION_REQUIRED",
                    message=(
                        "the exact accounting preview hash and an idempotency key are required"
                    ),
                    fields=fields,
                )
            ]
            if fields
            else []
        )
        commentary_fields = [
            name
            for name in ("management_commentary", "management_commentary_context_hash")
            if getattr(self, name) is None
        ]
        if commentary_fields:
            requirements.append(
                AccountingPeriodInformationRequirement(
                    code="ACCOUNTING_PERIOD_CLOSE_COMMENTARY_REQUIRED",
                    message=(
                        "AI 须依据关账预览 assistant_review_checklist.management_commentary 的 "
                        "context、instruction 和 success_criteria 生成经营结论，供负责人审阅后，"
                        "提交原文及 context_hash。此项由 AI 完成，不得要求负责人撰写或提供哈希；"
                        "无业务或证据不足时如实说明，不能省略或编造经营原因。"
                    ),
                    fields=commentary_fields,
                )
            )
        return requirements


class GetAccountingPeriodsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_month: str | None = Field(default=None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")

    @field_validator("period_month")
    @classmethod
    def reject_year_zero(cls, value: str | None) -> str | None:
        if value is not None and value.startswith("0000-"):
            raise ValueError("period month year must be 0001 through 9999")
        return value


class AccountingPeriodResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AccountingPeriodResultStatus
    calendar_id: uuid.UUID | None = None
    period_id: uuid.UUID | None = None
    action_id: uuid.UUID | None = None
    close_id: uuid.UUID | None = None
    calculation_hash: str | None = None
    missing_information: list[AccountingPeriodInformationRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
