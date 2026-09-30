"""Released v1 owner-review shape for historical close decoding."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictInt, TypeAdapter
from typing_extensions import TypedDict

WireFen = Annotated[StrictInt, Field(ge=-(2**63), le=2**63 - 1)]

CloseReviewSection = Literal[
    "vouchers", "adopted_bases", "policies", "payroll_confirmations", "evidence"
]

class _StrictObject(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)


class CloseReviewSourceReference(_StrictObject):
    source_type: Literal["voucher", "calculation", "fact", "evidence", "inventory"]
    id: str
    revision: int | None = None
    digest: str | None = None
    name: str | None = None
    media_type: str | None = None


class AdoptedPolicy(_StrictObject):
    label: str
    kind: str
    version: str | None
    effective_from: str | None
    effective_to: str | None
    official_urls: list[str]
    reference: CloseReviewSourceReference


class AdoptedPayrollConfirmation(_StrictObject):
    label: str
    mode: str
    calculation_reference: CloseReviewSourceReference
    confirmation_references: list[CloseReviewSourceReference]


class CloseReviewDetailItem(_StrictObject):
    key: str
    section: CloseReviewSection
    title: str
    subtitle: str
    status: str
    amount_fen: WireFen | None
    count: int | None
    references: list[CloseReviewSourceReference]


class CloseReviewBlock(_StrictObject):
    index: int
    first_key: str
    last_key: str
    count: int
    digest: str
    keys: list[str]


class CloseReviewDirectory(_StrictObject):
    section: CloseReviewSection
    label: str
    total_count: int
    block_size: int
    root_digest: str
    blocks: list[CloseReviewBlock]


class CloseReviewAccountingSummary(_StrictObject):
    voucher_count: int
    line_count: int
    total_debit_fen: WireFen
    total_credit_fen: WireFen
    month_revenue_fen: WireFen
    month_expense_fen: WireFen
    month_result_fen: WireFen
    ending_assets_fen: WireFen | None
    ending_liabilities_fen: WireFen | None
    ending_equity_fen: WireFen | None
    funds_total_fen: WireFen | None
    bank_fen: WireFen | None
    cash_fen: WireFen | None
    payment_platform_fen: WireFen | None
    actual_receipts_fen: WireFen
    actual_payments_fen: WireFen
    internal_transfer_fen: WireFen
    voucher_balanced: bool
    financial_position_balanced: bool | None
    financial_position_complete: bool


class CloseReviewBusinessSummary(_StrictObject):
    kind: str
    label: str
    action: Literal["business", "correction", "opening", "state"]
    reversal: bool
    count: int
    amount_label: str
    business_amount_fen: WireFen | None
    journal_total_fen: WireFen


class CloseReviewMaterialSummary(_StrictObject):
    category: str
    inventory_id: int
    expected: int
    received: int
    no_business: bool
    confirmation: CloseReviewSourceReference


class CloseReviewAdoptedBasisSummary(_StrictObject):
    policy_count: int
    payroll_confirmation_count: int
    evidence_count: int
    summary: str


class CloseReviewFollowupSummary(_StrictObject):
    close_issue_count: int
    settlement_issue_count: int
    external_issue_count: int
    file_issue_count: int
    followup_count: int


class OwnerReview(_StrictObject):
    presentation_contract: Literal["ai-accounting-kernel/2/close-review/1"]
    period: str
    accounting_summary: CloseReviewAccountingSummary
    business_summary: list[CloseReviewBusinessSummary]
    material_summary: list[CloseReviewMaterialSummary]
    adopted_basis_summary: CloseReviewAdoptedBasisSummary
    owner_confirmation: CloseReviewSourceReference
    followup_summary: CloseReviewFollowupSummary
    collections: list[CloseReviewDirectory]



_OWNER_REVIEW_ADAPTER = TypeAdapter(OwnerReview)

def require_owner_review(value):
    return _OWNER_REVIEW_ADAPTER.validate_python(value)
