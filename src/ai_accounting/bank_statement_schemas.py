"""Strict contracts for bank-statement preflight and reconciliation.

Public requests deliberately contain no caller-supplied actor, late flag, journal
line, account posting, or close-state assertion.  Authentication supplies the
actor separately and application services derive period state from the database.
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

MIN_SIGNED_BIGINT = -(2**63)
MAX_SIGNED_BIGINT = 2**63 - 1


def _strip_required(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


SignedFen = Annotated[
    StrictInt,
    Field(
        ge=MIN_SIGNED_BIGINT,
        le=MAX_SIGNED_BIGINT,
        description="Signed monetary amount in integer fen",
    ),
]
NonNegativeCount = Annotated[StrictInt, Field(ge=0, le=MAX_SIGNED_BIGINT)]
ColumnName = Annotated[
    str,
    BeforeValidator(_strip_required),
    Field(min_length=1, max_length=200),
]
OptionalExplanation = Annotated[str, Field(min_length=1, max_length=2000)]
SheetName = Annotated[str, Field(min_length=1, max_length=200)]
DateFormat = Literal["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]
ControlledFileName = Annotated[
    str,
    BeforeValidator(_strip_required),
    Field(min_length=1, max_length=255, pattern=r"^[^/\\]+$"),
]


class BankStatementFileFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"


class BankStatementPreviewStatus(StrEnum):
    CALCULATED = "calculated"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class BankStatementActionStatus(StrEnum):
    POSTED = "posted"
    PARTIALLY_POSTED = "partially_posted"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class BankStatementParseStatus(StrEnum):
    PARSED = "parsed"
    REJECTED = "rejected"


class BankStatementColumnMapping(BaseModel):
    """One-to-one mapping from fixed canonical fields to statement columns."""

    model_config = ConfigDict(extra="forbid")

    booking_date: ColumnName
    amount: ColumnName | None = None
    debit: ColumnName | None = None
    credit: ColumnName | None = None
    counterparty: ColumnName | None = None
    memo: ColumnName | None = None
    external_id: ColumnName | None = None
    currency: ColumnName | None = None

    @model_validator(mode="after")
    def valid_mapping(self) -> BankStatementColumnMapping:
        has_amount = self.amount is not None
        has_debit_credit = self.debit is not None and self.credit is not None
        has_partial_debit_credit = (self.debit is None) != (self.credit is None)
        if has_partial_debit_credit or has_amount == has_debit_credit:
            raise ValueError("map exactly amount, or both debit and credit")
        mapped = [value for value in self.model_dump(exclude_none=True).values()]
        if len(mapped) != len(set(mapped)):
            raise ValueError("each source column may map to only one canonical field")
        return self


class MissingExternalIdResolution(BaseModel):
    """Explicit human resolution for exactly one source row without a stable ID."""

    model_config = ConfigDict(extra="forbid")

    row_number: StrictInt = Field(ge=2)
    row_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["confirm_new", "confirm_duplicate"] | None = None
    duplicate_bank_transaction_id: uuid.UUID | None = None
    explanation: OptionalExplanation | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("explanation", mode="before")
    @classmethod
    def strip_optional_explanation(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class PreviewBankStatementImportRequest(BaseModel):
    """Preflight facts for parsing one already-loaded statement byte string."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    file_format: BankStatementFileFormat
    column_mapping: BankStatementColumnMapping
    sheet_name: SheetName | None = None
    date_format: DateFormat | None = None
    missing_external_id_resolutions: list[MissingExternalIdResolution] = Field(
        default_factory=list,
        max_length=10_000,
    )
    proceed_with_known_row_errors: StrictBool = False

    @field_validator("sheet_name", "date_format", mode="before")
    @classmethod
    def strip_optional_file_fact(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class ConfirmBankStatementImportRequest(PreviewBankStatementImportRequest):
    """Confirm an unchanged preflight; authenticated actor context stays internal."""

    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=200),
    ]


class PreviewBankStatementFileImportRequest(BaseModel):
    """MCP envelope naming one file directly inside the controlled import root."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    source_file_name: ControlledFileName
    file_format: Literal["csv"]
    column_mapping: BankStatementColumnMapping
    date_format: DateFormat | None = None
    missing_external_id_resolutions: list[MissingExternalIdResolution] = Field(
        default_factory=list,
        max_length=10_000,
    )
    proceed_with_known_row_errors: StrictBool = False

    @field_validator("source_file_name")
    @classmethod
    def reject_dot_file_names(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("source_file_name must name a regular file")
        return value

    def calculation_request(self) -> PreviewBankStatementImportRequest:
        values = self.model_dump(exclude={"source_file_name"})
        values["sheet_name"] = None
        return PreviewBankStatementImportRequest.model_validate(values)


class ConfirmBankStatementFileImportRequest(PreviewBankStatementFileImportRequest):
    """MCP confirmation envelope; file name is excluded from accounting hashes."""

    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=200),
    ]

    def calculation_request(self) -> PreviewBankStatementImportRequest:
        return PreviewBankStatementImportRequest.model_validate(
            self.model_dump(exclude={"source_file_name", "calculation_hash", "idempotency_key"})
            | {"sheet_name": None}
        )


class BankStatementInformationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    fields: list[str]


class BankStatementIssue(BaseModel):
    """Caller-safe issue metadata; it never carries the rejected source value."""

    model_config = ConfigDict(extra="forbid")

    code: str
    row_number: StrictInt | None = Field(default=None, ge=2)
    field_path: str | None = None


class BankStatementNormalizedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_number: StrictInt = Field(ge=2)
    row_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    booking_date: date
    amount_fen: SignedFen
    currency: Literal["CNY"]
    external_id: str | None = Field(default=None, max_length=100)
    counterparty_name: str | None = Field(default=None, max_length=200)
    memo: str = Field(default="", max_length=2000)


class ParsedBankStatement(BaseModel):
    """Parser-only result; intentionally has no formal calculation hash."""

    model_config = ConfigDict(extra="forbid")

    status: BankStatementParseStatus
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_request_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: list[BankStatementNormalizedRow] = Field(default_factory=list)
    errors: list[BankStatementIssue] = Field(default_factory=list)
    trace: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] = Field(default_factory=dict)


class BankStatementPeriodProjection(BaseModel):
    """One server-derived accounting-period projection for one normalized row."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "closed", "not_generated", "control_disabled"]
    period_start_date: date
    period_end_date: date
    period_id: uuid.UUID | None = None
    close_id: uuid.UUID | None = None
    close_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    closed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def exact_state_fields(self) -> BankStatementPeriodProjection:
        if self.period_start_date > self.period_end_date:
            raise ValueError("period projection start must not follow end")
        close_values = (self.close_id, self.close_hash, self.closed_at)
        if self.status == "closed":
            if self.period_id is None or any(value is None for value in close_values):
                raise ValueError("closed period projection requires period and close snapshot")
        elif any(value is not None for value in close_values):
            raise ValueError("only a closed period projection may carry close snapshot fields")
        if self.status == "open" and self.period_id is None:
            raise ValueError("open period projection requires period_id")
        if self.status in {"not_generated", "control_disabled"} and self.period_id is not None:
            raise ValueError("unavailable period projection must not claim a generated period")
        return self


class BankStatementTransactionSnapshot(BaseModel):
    """Immutable bank-row facts resolved internally for deterministic duplicate checks."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: uuid.UUID
    org_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    external_id: str | None = Field(default=None, max_length=100)
    booking_date: date
    amount_fen: SignedFen
    currency: Literal["CNY"]
    counterparty_name: str | None = Field(default=None, max_length=200)
    memo: str = Field(default="", max_length=2000)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BankStatementImportRowSystemFact(BaseModel):
    """Required database snapshot for exactly one normalized source row."""

    model_config = ConfigDict(extra="forbid")

    row_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    period: BankStatementPeriodProjection
    existing_source_row_transaction: BankStatementTransactionSnapshot | None = None
    existing_external_id_transaction: BankStatementTransactionSnapshot | None = None
    manual_duplicate_target: BankStatementTransactionSnapshot | None = None


class BankStatementImportEvidenceFact(BaseModel):
    """Content identity of one resolution evidence object resolved by the server."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: uuid.UUID
    org_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BankStatementImportSystemFacts(BaseModel):
    """Mandatory server facts that turn a parse into a confirmable formal preview."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    as_of_date: date
    rows: list[BankStatementImportRowSystemFact] = Field(max_length=100_000)
    resolution_evidence: list[BankStatementImportEvidenceFact] = Field(
        default_factory=list,
        max_length=10_000,
    )


class BankStatementPreviewRow(BankStatementNormalizedRow):
    model_config = ConfigDict(extra="forbid")

    disposition: Literal[
        "ready",
        "stable_duplicate",
        "manual_new",
        "manual_duplicate",
        "needs_external_id_resolution",
    ]
    duplicate_bank_transaction_id: uuid.UUID | None = None
    period_status: Literal["open", "closed", "not_generated", "control_disabled"]
    period_id: uuid.UUID | None = None
    is_late: StrictBool
    original_close_id: uuid.UUID | None = None
    original_close_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    original_closed_at: AwareDatetime | None = None


class BankStatementImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BankStatementPreviewStatus
    calculation_hash: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rows: list[BankStatementPreviewRow] = Field(default_factory=list)
    missing_information: list[BankStatementInformationRequirement] = Field(default_factory=list)
    errors: list[BankStatementIssue] = Field(default_factory=list)
    trace: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] = Field(default_factory=dict)


class BankReconciliationDifferenceExplanation(BaseModel):
    """Evidence-backed explanation for one signed reconciliation difference."""

    model_config = ConfigDict(extra="forbid")

    difference_kind: Literal["statement_to_book"]
    amount_fen: SignedFen | None = None
    explanation: OptionalExplanation | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("explanation", mode="before")
    @classmethod
    def strip_optional_explanation(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class PreviewBankReconciliationRequest(BaseModel):
    """Human-provided statement facts; calculated ledger facts are never accepted here."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    coverage_start_date: date | None = None
    coverage_end_date: date | None = None
    statement_opening_balance_fen: SignedFen | None = None
    statement_closing_balance_fen: SignedFen | None = None
    statement_import_action_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    statement_evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    difference_explanations: list[BankReconciliationDifferenceExplanation] = Field(
        default_factory=list,
        max_length=1000,
    )


class ConfirmBankReconciliationRequest(PreviewBankReconciliationRequest):
    """Confirm an unchanged reconciliation using identity from authentication context."""

    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=200),
    ]


class BankReconciliationImportedTransactionFact(BaseModel):
    """One database-resolved statement row included in this reconciliation."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: uuid.UUID
    booking_date: date
    amount_fen: SignedFen


class BankReconciliationImportActionFact(BaseModel):
    """Immutable import-action snapshot resolved internally from public IDs."""

    model_config = ConfigDict(extra="forbid")

    action_id: uuid.UUID
    org_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    status: Literal["posted", "partially_posted"]
    request_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    transactions: list[BankReconciliationImportedTransactionFact] = Field(
        max_length=100_000,
    )


class BankReconciliationEvidenceFact(BaseModel):
    """Immutable evidence identity resolved internally from public IDs."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: uuid.UUID
    org_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BankReconciliationSystemFacts(BaseModel):
    """Database-derived facts supplied internally to the pure calculator."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_id: uuid.UUID
    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=30),
    ]
    period_start_date: date
    period_end_date: date
    book_closing_balance_fen: SignedFen
    unmatched_transaction_count: NonNegativeCount = 0
    pending_late_transaction_count: NonNegativeCount = 0
    import_actions: list[BankReconciliationImportActionFact] = Field(
        default_factory=list,
        max_length=1000,
    )
    statement_evidence: list[BankReconciliationEvidenceFact] = Field(
        default_factory=list,
        max_length=100,
    )

    @model_validator(mode="after")
    def valid_period(self) -> BankReconciliationSystemFacts:
        if self.period_start_date > self.period_end_date:
            raise ValueError("period start date must not follow period end date")
        return self


class BankReconciliationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BankStatementPreviewStatus
    calculation_hash: str | None = None
    missing_information: list[BankStatementInformationRequirement] = Field(default_factory=list)
    errors: list[BankStatementIssue] = Field(default_factory=list)
    warnings: list[dict[str, object]] = Field(default_factory=list)
    trace: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] = Field(default_factory=dict)


class BankReconciliationScopeAccountInput(BaseModel):
    """One explicitly named bank account in the complete desired scope."""

    model_config = ConfigDict(extra="forbid")

    bank_account_code: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=2, max_length=30, pattern=r"^[0-9A-Z][0-9A-Z._-]*$"),
    ]
    account_name: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=100),
    ]
    start_date: date
    end_date: date | None = None

    @model_validator(mode="after")
    def month_aligned_dates(self) -> BankReconciliationScopeAccountInput:
        if self.start_date.day != 1:
            raise ValueError("bank reconciliation start date must be a month first")
        if self.end_date is not None:
            if self.end_date.day != monthrange(self.end_date.year, self.end_date.month)[1]:
                raise ValueError("bank reconciliation end date must be a month end")
            if self.end_date < self.start_date:
                raise ValueError("bank reconciliation end date must not precede start")
        return self


class PreviewBankReconciliationScopeRequest(BaseModel):
    """Explicit complete bank scope; no account code, name, or date is inferred."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    action_type: Literal["initial_confirmation", "scope_change"]
    previous_action_id: uuid.UUID | None = None
    accounts: list[BankReconciliationScopeAccountInput] = Field(max_length=100)
    confirm_zero_accounts: StrictBool = False
    explanation: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=2000),
    ]
    evidence_references: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def explicit_complete_scope(self) -> PreviewBankReconciliationScopeRequest:
        codes = [item.bank_account_code for item in self.accounts]
        if len(codes) != len(set(codes)):
            raise ValueError("bank account codes must be unique")
        if len(self.evidence_references) != len(set(self.evidence_references)):
            raise ValueError("scope evidence references must be unique")
        if not self.accounts and not self.confirm_zero_accounts:
            raise ValueError("an empty bank scope requires explicit zero confirmation")
        if self.accounts and self.confirm_zero_accounts:
            raise ValueError("zero confirmation cannot include bank accounts")
        if self.action_type == "initial_confirmation" and self.previous_action_id is not None:
            raise ValueError("initial confirmation cannot name a previous action")
        if self.action_type == "scope_change" and self.previous_action_id is None:
            raise ValueError("scope change requires the previous action")
        return self


class ConfirmBankReconciliationScopeRequest(PreviewBankReconciliationScopeRequest):
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=200),
    ]


class BankReconciliationScopePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BankStatementPreviewStatus
    calculation_hash: str | None = None
    missing_information: list[BankStatementInformationRequirement] = Field(default_factory=list)
    errors: list[BankStatementIssue] = Field(default_factory=list)
    warnings: list[dict[str, object]] = Field(default_factory=list)
    trace: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] = Field(default_factory=dict)


class PreviewLateBankEvidenceRequest(BaseModel):
    """Facts for appending one late-evidence handling action.

    The request only names existing typed workflow records.  It cannot carry
    journal lines, an actor, close state, or a caller-selected late flag.
    """

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    bank_transaction_id: uuid.UUID
    action_type: Literal["evidence_only", "omitted_entry"]
    handling_period_id: uuid.UUID | None = None
    target_event_id: uuid.UUID | None = None
    result_event_id: uuid.UUID | None = None
    result_voucher_id: uuid.UUID | None = None
    explanation: OptionalExplanation | None = None
    evidence_references: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("explanation", mode="before")
    @classmethod
    def strip_late_explanation(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def action_specific_references(self) -> PreviewLateBankEvidenceRequest:
        if self.action_type == "evidence_only":
            if self.result_event_id is not None or self.result_voucher_id is not None:
                raise ValueError("evidence_only cannot name an omitted-entry result")
        elif self.target_event_id is not None:
            raise ValueError("omitted_entry cannot name an evidence-only target")
        return self


class ConfirmLateBankEvidenceRequest(PreviewLateBankEvidenceRequest):
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: Annotated[
        str,
        BeforeValidator(_strip_required),
        Field(min_length=1, max_length=200),
    ]


class LateBankEvidencePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BankStatementPreviewStatus
    calculation_hash: str | None = None
    missing_information: list[BankStatementInformationRequirement] = Field(default_factory=list)
    errors: list[BankStatementIssue] = Field(default_factory=list)
    trace: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] = Field(default_factory=dict)


class GetBankStatementActivityRequest(BaseModel):
    """Typed current/close-time projection without unrestricted query text."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    original_period_id: uuid.UUID | None = None
    handling_period_id: uuid.UUID | None = None
    bank_transaction_id: uuid.UUID | None = None
    bank_account_code: (
        Annotated[
            str,
            BeforeValidator(_strip_required),
            Field(min_length=1, max_length=30),
        ]
        | None
    ) = None
    include_import_actions: StrictBool = True
    include_reconciliations: StrictBool = True
    limit: StrictInt = Field(default=200, ge=1, le=500)


class QueryBankStatementStateRequest(GetBankStatementActivityRequest):
    """Stable public name for the bounded current/history bank projection."""


class BankStatementActionResult(BaseModel):
    """Stable service result shared by formal bank confirmation commands."""

    model_config = ConfigDict(extra="forbid")

    status: BankStatementActionStatus
    action_id: uuid.UUID | None = None
    calculation_hash: str | None = None
    missing_information: list[BankStatementInformationRequirement] = Field(default_factory=list)
    errors: list[BankStatementIssue] = Field(default_factory=list)
    trace: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] = Field(default_factory=dict)
