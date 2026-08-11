"""Strict public facts and results for the specialized borrowing workflow."""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic.json_schema import WithJsonSchema

from .borrowings import MAX_FEN
from .schemas import BankTransactionReference

Fen = Annotated[StrictInt, Field(ge=0, le=MAX_FEN)]
PositiveFen = Annotated[StrictInt, Field(gt=0, le=MAX_FEN)]
StrictDecimalString = Annotated[
    Decimal,
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?:0|[1-9]\d{0,2})(?:\.\d{1,6})?$",
            "description": (
                "Finite decimal string with at most six fractional places; "
                "annual rate must be in (0, 100]."
            ),
        },
        mode="validation",
    ),
]


def _strict_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("decimal values must be JSON strings or Decimal, never float or bool")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        if re.fullmatch(r"(?:0|[1-9]\d{0,2})(?:\.\d{1,6})?", value) is None:
            raise ValueError("annual rate must be a plain finite decimal string")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal string") from exc
    else:
        raise ValueError("decimal values must be JSON strings or Decimal")
    if not parsed.is_finite():
        raise ValueError("decimal values must be finite")
    if parsed <= 0 or parsed > Decimal("100"):
        raise ValueError("annual rate must be in (0, 100]")
    precision = max(16, len(parsed.as_tuple().digits) + 8)
    try:
        with localcontext(Context(prec=precision, Emin=MIN_EMIN, Emax=MAX_EMAX)):
            normalized = parsed.quantize(Decimal("0.000001"))
    except InvalidOperation as exc:
        raise ValueError("annual rate cannot be represented at six decimal places") from exc
    if normalized != parsed:
        raise ValueError("annual rate supports at most six fractional decimal places")
    return normalized


class BorrowingLenderReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    external_ref: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("name", "external_ref", mode="before")
    @classmethod
    def identity_text_is_trimmed_and_nonblank(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("lender identity text must not be blank")
        return value

    @model_validator(mode="after")
    def identity_is_present(self) -> BorrowingLenderReference:
        if self.id is None and self.name is None:
            raise ValueError("lender id or name is required")
        return self


class BorrowingTermFacts(BaseModel):
    """Explicit Phase-1 boundary facts; no missing term is inferred."""

    model_config = ConfigDict(extra="forbid")

    single_drawdown: StrictBool | None = None
    fixed_rate: StrictBool | None = None
    simple_interest: StrictBool | None = None
    bullet_principal_at_maturity: StrictBool | None = None
    allows_prepayment: StrictBool | None = None
    allows_extension: StrictBool | None = None
    has_penalty_interest: StrictBool | None = None
    has_financing_fees: StrictBool | None = None

    def missing_fields(self) -> list[str]:
        return [name for name in type(self).model_fields if getattr(self, name) is None]

    def is_phase_one_supported(self) -> bool:
        return (
            self.single_drawdown is True
            and self.fixed_rate is True
            and self.simple_interest is True
            and self.bullet_principal_at_maturity is True
            and self.allows_prepayment is False
            and self.allows_extension is False
            and self.has_penalty_interest is False
            and self.has_financing_fees is False
        )


class BorrowingInformationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    fields: list[str] = Field(default_factory=list)


class BorrowingDayCountBasis(StrEnum):
    ACTUAL_360 = "actual_360"
    ACTUAL_365 = "actual_365"


class DrawBorrowingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    borrowing_code: str | None = Field(default=None, min_length=1, max_length=100)
    contract_name: str | None = Field(default=None, min_length=1, max_length=200)
    lender: BorrowingLenderReference | None = None
    lender_is_licensed_financial_institution: StrictBool | None = None
    currency: str | None = Field(default=None, min_length=1, max_length=10)
    principal_fen: PositiveFen | None = None
    drawdown_date: date | None = None
    due_date: date | None = None
    posting_date: date | None = None
    annual_rate_percent: StrictDecimalString | None = None
    day_count_basis: BorrowingDayCountBasis | None = None
    interest_due_dates: list[date] | None = None
    capitalization_applicable: StrictBool | None = None
    purpose_description: str | None = Field(default=None, min_length=1, max_length=2000)
    term_facts: BorrowingTermFacts = Field(default_factory=BorrowingTermFacts)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @field_validator("borrowing_code", "contract_name", "purpose_description", mode="before")
    @classmethod
    def required_text_is_trimmed_and_nonblank(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("required borrowing text must not be blank")
        return value

    @field_validator("annual_rate_percent", mode="before")
    @classmethod
    def decimal_is_strict(cls, value: object) -> Decimal | None:
        return _strict_decimal(value)

    @model_validator(mode="after")
    def dates_and_due_dates_are_ordered(self) -> DrawBorrowingRequest:
        if self.due_date and self.drawdown_date and self.due_date <= self.drawdown_date:
            raise ValueError("due_date must be after drawdown_date")
        if self.posting_date and self.drawdown_date and self.posting_date != self.drawdown_date:
            raise ValueError("drawdown posting_date must equal drawdown_date")
        if self.interest_due_dates is not None and self.drawdown_date and self.due_date:
            if not self.interest_due_dates:
                raise ValueError("interest_due_dates must not be empty")
            if self.interest_due_dates != sorted(set(self.interest_due_dates)):
                raise ValueError("interest_due_dates must be strictly ascending without duplicates")
            if self.interest_due_dates[0] <= self.drawdown_date:
                raise ValueError("first interest due date must follow drawdown date")
            if self.interest_due_dates[-1] != self.due_date:
                raise ValueError("last interest due date must equal due_date")
        return self

    def missing_information(self) -> list[BorrowingInformationRequirement]:
        fields = [
            name
            for name in (
                "borrowing_code",
                "contract_name",
                "lender",
                "lender_is_licensed_financial_institution",
                "currency",
                "principal_fen",
                "drawdown_date",
                "due_date",
                "posting_date",
                "annual_rate_percent",
                "day_count_basis",
                "interest_due_dates",
                "capitalization_applicable",
                "purpose_description",
            )
            if getattr(self, name) is None
        ]
        fields.extend(f"term_facts.{name}" for name in self.term_facts.missing_fields())
        result = []
        if fields:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_DRAW_FACTS_REQUIRED",
                    message=(
                        "complete lender, contract, rate, due-date, capitalization, "
                        "and term facts are required"
                    ),
                    fields=fields,
                )
            )
        if not self.bank_transaction_references:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_BANK_TRANSACTIONS_REQUIRED",
                    message="drawdown requires its bank transaction evidence",
                    fields=["bank_transaction_references"],
                )
            )
        if not self.evidence_references:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_EVIDENCE_REQUIRED",
                    message="contract evidence is required",
                    fields=["evidence_references"],
                )
            )
        return result


class PreviewBorrowingInterestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    borrowing_id: uuid.UUID | None = None
    period_start: date | None = None
    period_end: date | None = None

    @model_validator(mode="after")
    def period_is_ordered(self) -> PreviewBorrowingInterestRequest:
        if self.period_start and self.period_end and self.period_end <= self.period_start:
            raise ValueError("period_end must follow period_start")
        return self

    def missing_information(self) -> list[BorrowingInformationRequirement]:
        missing = [
            name
            for name in ("borrowing_id", "period_start", "period_end")
            if getattr(self, name) is None
        ]
        return (
            []
            if not missing
            else [
                BorrowingInformationRequirement(
                    code="BORROWING_INTEREST_FACTS_REQUIRED",
                    message="borrowing and an interest period are required",
                    fields=missing,
                )
            ]
        )


class ConfirmBorrowingInterestRequest(PreviewBorrowingInterestRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    def missing_information(self) -> list[BorrowingInformationRequirement]:
        result = super().missing_information()
        if self.calculation_hash is None:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_CONFIRMATION_REQUIRED",
                    message="calculation_hash is required",
                    fields=["calculation_hash"],
                )
            )
        return result


class PayBorrowingInterestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    borrowing_id: uuid.UUID | None = None
    accrual_event_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    payment_date: date | None = None
    posting_date: date | None = None
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def posting_is_payment(self) -> PayBorrowingInterestRequest:
        if self.payment_date and self.posting_date and self.payment_date != self.posting_date:
            raise ValueError("interest payment posting_date must equal payment_date")
        return self

    def missing_information(self) -> list[BorrowingInformationRequirement]:
        missing = [
            name
            for name in ("borrowing_id", "accrual_event_id", "payment_date", "posting_date")
            if getattr(self, name) is None
        ]
        result = (
            []
            if not missing
            else [
                BorrowingInformationRequirement(
                    code="BORROWING_INTEREST_PAYMENT_FACTS_REQUIRED",
                    message="payment facts are required",
                    fields=missing,
                )
            ]
        )
        if not self.bank_transaction_references:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_BANK_TRANSACTIONS_REQUIRED",
                    message="payment bank evidence is required",
                    fields=["bank_transaction_references"],
                )
            )
        if not self.evidence_references:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_EVIDENCE_REQUIRED",
                    message="payment evidence is required",
                    fields=["evidence_references"],
                )
            )
        return result


class RepayBorrowingPrincipalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    borrowing_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    repayment_date: date | None = None
    posting_date: date | None = None
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def posting_is_repayment(self) -> RepayBorrowingPrincipalRequest:
        if self.repayment_date and self.posting_date and self.repayment_date != self.posting_date:
            raise ValueError("principal repayment posting_date must equal repayment_date")
        return self

    def missing_information(self) -> list[BorrowingInformationRequirement]:
        missing = [
            name
            for name in ("borrowing_id", "repayment_date", "posting_date")
            if getattr(self, name) is None
        ]
        result = (
            []
            if not missing
            else [
                BorrowingInformationRequirement(
                    code="BORROWING_PRINCIPAL_REPAYMENT_FACTS_REQUIRED",
                    message="repayment facts are required",
                    fields=missing,
                )
            ]
        )
        if not self.bank_transaction_references:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_BANK_TRANSACTIONS_REQUIRED",
                    message="repayment bank evidence is required",
                    fields=["bank_transaction_references"],
                )
            )
        if not self.evidence_references:
            result.append(
                BorrowingInformationRequirement(
                    code="BORROWING_EVIDENCE_REQUIRED",
                    message="repayment evidence is required",
                    fields=["evidence_references"],
                )
            )
        return result


class BorrowingResultStatus(StrEnum):
    CALCULATED = "calculated"
    POSTED = "posted"
    REVERSED = "reversed"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class BorrowingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BorrowingResultStatus
    borrowing_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    calculation_hash: str | None = None
    missing_information: list[BorrowingInformationRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
