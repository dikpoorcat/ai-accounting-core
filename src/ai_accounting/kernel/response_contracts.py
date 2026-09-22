"""Dashboard and browser read contracts with one native and HTTP typed source.

Omission is represented by NotRequired, never by a serialization default. Money
has an explicit type even inside provenance and diagnostics. No arbitrary JSON
escape hatch is part of these two contracts.
"""

from __future__ import annotations

from typing import Annotated, Generic, Literal, NotRequired, TypeVar

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from typing_extensions import TypedDict

from .close_review import (
    DASHBOARD_CLOSE_REVIEW_ADAPTER,
    AdoptedPayrollConfirmation,
    AdoptedPolicy,
    CloseReviewSourceReference,
)
from .contracts import KernelError
from .response_types import Version1, Version2, Version3, Version4, Version6, WireFen

Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
Day = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")]
Count = Annotated[int, Field(ge=0)]
AccountType = Literal["bank", "cash", "payment_platform"]
CoverageState = Literal["missing", "partial", "complete", "not_applicable"]
T = TypeVar("T")


def _boolean_literal(value):
    if type(value) is not bool:
        raise ValueError("boolean required")
    return value


FalseValue = Annotated[Literal[False], BeforeValidator(_boolean_literal)]
TrueValue = Annotated[Literal[True], BeforeValidator(_boolean_literal)]


class ResponseObject(TypedDict):
    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")


class DashboardPeriod(ResponseObject):
    key: Month
    year: int
    month: int
    label: str
    short_label: str
    status: Literal["open", "closed"]
    start_date: Day
    end_date: Day
    closed_at: str | None


class DashboardQuarter(ResponseObject):
    key: str
    year: int
    quarter: int
    label: str
    complete: bool


class DashboardCompany(ResponseObject):
    company_id: str
    name: str
    taxpayer_id: str | None
    status: Literal["active"]


class DashboardContextResponse(ResponseObject):
    schema_version: Version2
    company: str | None
    companies: list[DashboardCompany]
    current_company: DashboardCompany | None
    periods: list[DashboardPeriod]
    quarters: list[DashboardQuarter]
    default_period: Month | None
    default_quarter: str | None
    generated_at: NotRequired[str]
    disclaimer: NotRequired[str]


class ReadSemantics(ResponseObject):
    knowledge: Literal["current_knowledge"]
    accounting: Literal["as_posted"]
    business_basis: Literal["current_known", "frozen_adoption"]
    display: Literal["current", "frozen_with_current_supplements"]
    system_time_replay: FalseValue
    recorded_at: Literal["system_recording_time"]
    recording_period: Literal["business_recording_period"]
    recorded_later: Literal["business_recording_period_after_selected_period"]


class SourceMetadata(ResponseObject):
    source_type: str
    id: str
    revision: int | None
    field: str | None
    source: str | None
    evidence_digest: str | None
    evidence: list[str]
    basis: Literal["frozen", "current_supplement", "current"]
    recorded_at: str | None


# Keys name display fields; values are always provenance, never arbitrary data.
FieldSources = dict[str, SourceMetadata | list[SourceMetadata]]


class PartySource(ResponseObject):
    party_id: str
    name: str
    source: str | None
    id: NotRequired[str]
    conflicting_ids: NotRequired[list[str]]
    field_sources: NotRequired[FieldSources]


class DirectAdoptionProof(ResponseObject):
    basis: Literal["direct_adoption"]
    close_period: Month
    publication_id: str
    role: str


class CalculationCurrentProof(ResponseObject):
    basis: Literal["calculation_current"]


class AssetBatchMemberProof(ResponseObject):
    basis: Literal["asset_batch_member"]
    owner_calculation_id: str
    owner_publication_id: NotRequired[str]
    membership_digest: NotRequired[str]


SelectionProof = DirectAdoptionProof | CalculationCurrentProof | AssetBatchMemberProof


class SelectionCandidate(ResponseObject):
    calculation_id: str
    fact_id: str
    result_digest: str
    kind: str
    has_journal_lines: bool
    trace_only: TrueValue


class DuplicateSourceLocation(ResponseObject):
    evidence_digest: str
    location: str


class DuplicateSignal(ResponseObject):
    code: Literal[
        "same_exact_material_location",
        "same_complete_signature_and_evidence",
        "same_complete_actual_money",
        "same_complete_signature",
        "shared_evidence",
        "same_material_location_different_signature",
        "same_actual_money_coordinates",
    ]
    matched_fields: list[str]
    evidence: NotRequired[list[str]]
    source_locations: NotRequired[list[DuplicateSourceLocation]]
    distinct_locations_proven: NotRequired[bool]


class ReadinessIssue(ResponseObject):
    """Fields emitted by material, accounting, report and close checkers.

    Checkers may attach the listed diagnostics. Adding a diagnostic requires an
    explicit contract change; monetary values cannot fall through an open map.
    """

    field: str
    message: str
    code: NotRequired[str]
    location: NotRequired[str | None]
    semantics: NotRequired[str]
    domain: NotRequired[str]
    category: NotRequired[str]
    inventory_id: NotRequired[Count]
    subject_id: NotRequired[str]
    fact_id: NotRequired[str]
    source_id: NotRequired[str]
    group_id: NotRequired[str]
    employee_id: NotRequired[str]
    asset_id: NotRequired[str]
    drawdown_id: NotRequired[str]
    bank_account_id: NotRequired[str]
    obligation_id: NotRequired[str]
    obligation_kind: NotRequired[str]
    evidence_digest: NotRequired[str]
    member_location: NotRequired[str]
    detail: NotRequired[str]
    amount_field: NotRequired[str]
    amount_fields: NotRequired[list[str]]
    period: NotRequired[Month]
    period_start: NotRequired[Day]
    period_end_exclusive: NotRequired[Day]
    pages: NotRequired[Count]
    voucher_version_id: NotRequired[str | None]
    voucher_number: NotRequired[int]
    version_id: NotRequired[str | None]
    calculation_id: NotRequired[str | None]
    obligation_key: NotRequired[str]
    allocation_index: NotRequired[Count]
    source_index: NotRequired[Count]
    row: NotRequired[Count]
    affected_lines: NotRequired[list[int]]
    line_no: NotRequired[int | None]
    account: NotRequired[str]
    allowed_precision: NotRequired[list[str]]
    reusable_sources: NotRequired[list[str]]
    expected_fen: NotRequired[WireFen | None]
    actual_fen: NotRequired[WireFen | None]
    reason: NotRequired[str]
    candidates: NotRequired[list[SelectionCandidate]]
    trace_targets: NotRequired[list[CalculationTarget]]
    statement: NotRequired[str]
    column: NotRequired[str]
    dimension: NotRequired[Literal["bank", "platform"]]
    expected_direction: NotRequired[Literal["inflow", "outflow"]]
    candidate_subject_id: NotRequired[str]
    pair_digest: NotRequired[str]
    review_period: NotRequired[Month]
    origin_periods: NotRequired[list[Month]]
    responsibility: NotRequired[Literal["direct", "closed_followup", "unassigned"]]
    signals: NotRequired[list[DuplicateSignal]]


class CalculationTarget(ResponseObject):
    calculation_id: str


class OrderDetails(ResponseObject):
    period: NotRequired[Month]


class OrderFailure(ResponseObject):
    code: Literal["already_closed", "earlier_period_open"]
    message: str
    details: OrderDetails


class Readiness(ResponseObject):
    period: Month
    order_failure: OrderFailure | None
    issues: list[ReadinessIssue]


class OpenClosure(ResponseObject):
    state: Literal["open"]


class ExactClosure(ResponseObject):
    state: Literal["exact_close"]
    digest: str


class LaterClosure(ResponseObject):
    state: Literal["covered_by_later_close"]
    sealing_boundary: Month
    sealing_digest: str


Closure = OpenClosure | ExactClosure | LaterClosure


class RecordedStatus(ResponseObject):
    status: Literal["recorded", "not_recorded"]


class RecordedReadiness(ResponseObject):
    status: Literal["ready"]
    source: Literal["exact_period_manifest"]
    readiness: RecordedStatus
    inventories: RecordedStatus
    material_coverage: RecordedStatus
    previous_close_digest: RecordedStatus


class UnavailableReadiness(ResponseObject):
    status: Literal["unavailable"]
    reason: Literal["no_exact_period_manifest"]


FrozenReadiness = RecordedReadiness | UnavailableReadiness


class CheckFollowup(ResponseObject):
    status: Literal["ready", "needs_information"]
    issues: list[ReadinessIssue]


class MaterialFollowup(CheckFollowup):
    inventory_count: Count
    coverage_digest: str


class AccountingFollowup(CheckFollowup):
    pending_subject_id: str | None
    unpublished_count: Count


class SettlementFollowup(ResponseObject):
    status: str
    cutoff_period: NotRequired[Month]
    current_cutoff_period: NotRequired[Month]
    issues: NotRequired[list[ReadinessIssue]]
    obligation_count: Count
    complete: bool
    unestablished_state_selection_count: Count
    movement_count: Count
    source_amount_fen: WireFen | None
    paid_fen: WireFen | None
    other_settled_fen: WireFen | None
    remaining_fen: WireFen | None


class ExternalFollowup(ResponseObject):
    status: Literal["completed", "followup_required", "unestablished"]
    obligation_count: Count
    completion_status_counts: dict[str, Count]
    basis_issue_count: Count
    scope_period: Month
    scope_semantics: Literal["obligation_interval_includes_selected_period"]
    fact_issues: list[ReadinessIssue]


class FileJobFollowup(ResponseObject):
    total_count: Count
    status_counts: dict[str, Count]
    issue_count: Count


class TaxImportMappingIssue(ResponseObject):
    code: str
    category: Literal["management_fact", "capability", "publication"]
    field: str
    message: str
    employee_id: NotRequired[str]
    component_codes: NotRequired[list[str]]
    amount_fen: NotRequired[WireFen]


class TaxImportMappingFollowup(ResponseObject):
    status: Literal[
        "ready", "needs_information", "unsupported", "pending_publication", "not_applicable"
    ]
    blocking_scope: Literal["tax_import_file"]
    mapping_fact_ids: list[str]
    calculation_ids: list[str]
    issues: list[TaxImportMappingIssue]


class PeriodCurrentFollowups(ResponseObject):
    knowledge: Literal["current_knowledge"]
    affects_frozen_readiness: FalseValue
    materials: MaterialFollowup
    accounting: AccountingFollowup
    close_requirements: CheckFollowup
    settlements: SettlementFollowup
    external: ExternalFollowup
    file_jobs: FileJobFollowup
    tax_import_mapping: TaxImportMappingFollowup


class PreparationReadSemantics(ResponseObject):
    knowledge: Literal["current_knowledge"]
    frozen_readiness: Literal["exact_period_manifest_only"]
    current_followups: Literal["never_changes_frozen_readiness"]


class PeriodPreparation(ResponseObject):
    company_id: str
    database_id: str
    period: Month
    as_of: Day
    as_of_semantics: Literal["current_knowledge"]
    closure: Closure
    read_semantics: PreparationReadSemantics
    projection: Literal["dashboard_period_preparation"]
    frozen_readiness: FrozenReadiness | None
    readiness: Readiness | None
    current_followups: PeriodCurrentFollowups


class CollectionPage(ResponseObject):
    total_count: Count
    filtered_count: Count
    returned_count: Count
    has_more: bool
    next_cursor: str | None
    collection_version: NotRequired[str]


class Collection(ResponseObject, Generic[T]):  # noqa: UP046 - Pydantic rebuild needs global T
    items: list[T]
    page: CollectionPage


class BankSourceCheck(ResponseObject):
    state: Literal["confirmed", "unestablished", "needs_review", "conflict"]
    message: str
    statement_confirmed: bool
    reconciliation_valid: bool
    statement_calculation_id: str | None
    selected_statement_calculation_ids: list[str]
    statement_fact_id: str
    reconciliation_calculation_id: str | None
    reconciliation_fact_id: str | None
    selection_source: str | None
    selection_proof: SelectionProof | None
    proof_method: (
        Literal["frozen_reconciliation_direct_statement", "independent_statement_selection"] | None
    )


class AccountStatement(ResponseObject):
    inflow_fen: WireFen | None
    outflow_fen: WireFen | None
    transaction_count: Count
    matched_count: Count
    unmatched_count: Count
    needs_review_count: Count
    coverage_state: CoverageState
    last_activity_date: Day | None
    account_code: str
    account_name: str


class Reconciliation(ResponseObject):
    state: Literal["pending", "not_applicable", "complete", "attention"]
    label: str
    source_check: NotRequired[BankSourceCheck]
    version: NotRequired[int | None]
    statement_closing_fen: NotRequired[WireFen]
    book_closing_fen: NotRequired[WireFen | None]
    difference_fen: NotRequired[WireFen | None]
    unmatched_count: NotRequired[Count]
    needs_review_count: NotRequired[Count]


class FundAccount(ResponseObject):
    account_id: str
    type: AccountType
    opening_fen: WireFen | None
    inflow_fen: WireFen
    outflow_fen: WireFen
    net_change_fen: WireFen
    attribution_adjustment_fen: WireFen | None
    closing_fen: WireFen | None
    movement_count: Count
    last_activity_date: Day | None
    negative_balance: bool
    code: str
    name: str
    active: bool | None
    field_sources: FieldSources
    statement: AccountStatement
    reconciliation: Reconciliation


class FundMovement(ResponseObject):
    id: str
    date: Day | None
    account_id: str
    account_code: str
    account_name: str
    account_type: AccountType
    direction: Literal["inflow", "outflow"]
    amount_fen: WireFen
    signed_amount_fen: WireFen
    reference: str
    calculation_id: str
    type: str
    summary: str
    display_summary: str
    list_summary: str
    field_sources: FieldSources
    party_sources: list[PartySource]
    party: str
    internal_transfer: bool
    component_kinds: list[str]


class BatchPaymentItem(ResponseObject):
    party: str
    amount_fen: WireFen


class BatchPayment(ResponseObject):
    bank_row_count: Count
    total_fen: WireFen
    items: list[BatchPaymentItem]


class BankStatementRow(ResponseObject):
    id: str
    date: Day
    reference: str
    account_id: str
    account_code: str
    account_name: str
    field_sources: FieldSources
    direction: Literal["inflow", "outflow"]
    amount_fen: WireFen
    signed_amount_fen: WireFen
    party: str
    party_sources: list[PartySource]
    memo: str
    state: Literal["matched", "unmatched", "needs_review"]
    source_check: BankSourceCheck
    batch_payment: NotRequired[BatchPayment]


class UnmatchedTotals(ResponseObject):
    count: Count
    inflow_fen: WireFen
    outflow_fen: WireFen


class BankStatement(ResponseObject):
    transaction_count: Count
    inflow_fen: WireFen | None
    outflow_fen: WireFen | None
    matched_count: Count
    unmatched_count: Count
    needs_review_count: Count
    unmatched_totals: UnmatchedTotals
    coverage_state: CoverageState
    statement_count: Count
    expected_account_count: Count
    provided_account_count: Count
    missing_account_count: Count


class InvestmentProduct(ResponseObject):
    fund_id: str
    opening_cost_fen: WireFen | None
    subscription_cost_fen: WireFen
    redemption_cost_fen: WireFen
    closing_cost_fen: WireFen | None
    investment_income_fen: WireFen
    name: str
    field_sources: FieldSources


class InvestmentEvent(ResponseObject):
    id: str
    date: Day | None
    period: Month
    fund_id: str
    name: str
    type: str
    reference: str
    cost_fen: WireFen | None
    net_proceeds_fen: WireFen | None
    investment_income_fen: WireFen | None
    settlement_fen: WireFen | None


class FundInvestments(ResponseObject):
    opening_cost_fen: WireFen | None
    subscription_cost_fen: WireFen
    redemption_cost_fen: WireFen
    closing_cost_fen: WireFen | None
    investment_income_fen: WireFen
    actual_payments_fen: WireFen
    actual_receipts_fen: WireFen
    event_count: Count


class FundsCollections(ResponseObject):
    accounts: NotRequired[Collection[FundAccount]]
    movements: NotRequired[Collection[FundMovement]]
    statements: NotRequired[Collection[BankStatementRow]]
    investment_products: NotRequired[Collection[InvestmentProduct]]
    investment_events: NotRequired[Collection[InvestmentEvent]]


class FundsData(ResponseObject):
    opening_fen: WireFen | None
    net_change_fen: WireFen
    inflow_fen: WireFen
    outflow_fen: WireFen
    internal_transfer_fen: WireFen
    movement_count: Count
    total_fen: WireFen | None
    bank_opening_fen: WireFen | None
    bank_inflow_fen: WireFen
    bank_outflow_fen: WireFen
    bank_fen: WireFen | None
    cash_fen: WireFen | None
    payment_platform_fen: WireFen | None
    account_count: Count
    bank_account_count: Count
    cash_account_count: Count
    payment_platform_account_count: Count
    attention_account_count: Count
    investments: FundInvestments
    bank_statement: BankStatement
    collections: FundsCollections
    fact_issues: list[ReadinessIssue]
    period_preparation: PeriodPreparation | None


class FundsDashboardResponse(ResponseObject):
    schema_version: Version6
    snapshot_version: str | None
    selected_period: DashboardPeriod | None
    read_semantics: ReadSemantics
    read_context: DashboardReadContext
    data: FundsData | None


class DashboardReadContext(ResponseObject):
    company_id: str
    database_id: str
    as_of: Day
    read_version: str


class EvidenceDetail(ResponseObject):
    digest: str
    name: str
    media_type: str


class Recognition(ResponseObject):
    precision: Literal["month", "day"]
    period: Month
    date: Day | None
    label: str


class SourceReference(ResponseObject):
    type: str
    value: str


class AssetReference(ResponseObject):
    asset_id: str
    asset_type: Literal["fixed", "intangible"]
    name: str | None
    code: str | None
    field_sources: NotRequired[FieldSources]


class AssetMemberReference(AssetReference):
    calculation_id: str
    owner_calculation_id: str
    amount_fen: WireFen | None
    amount_label: str
    line_start: int | None
    line_count: Count


class ManagementMetadata(ResponseObject):
    purpose: str
    description: str


class ComponentManagement(ResponseObject):
    version: int
    version_scope: Literal["base_profile"]
    metadata: ManagementMetadata
    field_sources: FieldSources


class VoucherComponent(ResponseObject):
    id: str
    key: str
    kind: str
    group: str
    label: str
    description: str
    amount_fen: WireFen | None
    amount_label: str
    parties: list[str]
    management: ComponentManagement
    recognition: Recognition
    party_sources: list[PartySource]
    source_references: list[SourceReference]


class VoucherFundMovement(ResponseObject):
    id: str
    account_id: str
    category: str
    name: str
    field_sources: FieldSources
    direction: Literal["inflow", "outflow"]
    amount_fen: WireFen


class BusinessIdentity(ResponseObject):
    subject_id: str
    kind: str


class VoucherSettlement(ResponseObject):
    id: str
    key: str
    label: str
    account: str
    amount_fen: WireFen
    change_fen: WireFen
    direction: str
    relation_state: NotRequired[Literal["resolved", "unresolved"]]
    party: str
    party_id: str | None
    creditor_id: NotRequired[str | None]
    actual_recipient_id: NotRequired[str | None]
    source_label: str
    source_period: Month
    source_calculation_id: str
    source_fact_id: NotRequired[str]
    source_subject_id: NotRequired[str]
    source_kind: NotRequired[str]
    obligation_name: NotRequired[str]
    line_number: int | None
    field_sources: FieldSources


class VoucherLineParty(ResponseObject):
    id: str
    name: str
    amount_fen: WireFen


class VoucherLine(ResponseObject):
    line_number: int
    code: str
    account: str
    debit_fen: WireFen
    credit_fen: WireFen
    party: str
    source_label: str
    field_sources: FieldSources
    parties: list[VoucherLineParty]
    party_state: Literal["known", "multiple", "name_missing", "not_applicable", "unresolved"]
    component_id: str
    asset: NotRequired[AssetMemberReference]


class BriefVoucher(ResponseObject):
    number: str
    calculation_id: str
    voucher_version_id: str
    reverses_version_id: str | None
    date: Day | None
    recognition: Recognition
    type: str
    kind: str
    state: str
    summary: str
    display_summary: str
    list_summary: str
    field_sources: FieldSources
    asset: AssetReference | None
    asset_members: NotRequired[list[AssetMemberReference]]
    amount_fen: WireFen
    business_amount_fen: WireFen | None
    business_amount_label: str
    fund_inflow_fen: WireFen
    fund_outflow_fen: WireFen
    evidence: list[str]
    evidence_details: list[EvidenceDetail]
    components: list[VoucherComponent]
    funds: list[VoucherFundMovement]
    settlements: list[VoucherSettlement]
    lines: list[VoucherLine]


class ActivityTypeCount(ResponseObject):
    label: str
    count: Count


class BriefActivityRow(ResponseObject):
    date: Day | None
    recognition: Recognition
    reference: str
    calculation_id: str
    voucher_version_id: str
    title: str
    subject: str
    description: str
    display_description: str
    asset: AssetReference | None
    field_sources: FieldSources
    amount_fen: WireFen | None
    amount_label: str
    journal_total_fen: WireFen
    state: str
    party: str
    evidence: list[str]
    evidence_details: list[EvidenceDetail]
    components: list[VoucherComponent]
    funds: list[VoucherFundMovement]
    settlements: list[VoucherSettlement]


class BriefActivityGroup(ResponseObject):
    key: str
    label: str
    event_count: Count
    loaded_count: Count
    type_counts: list[ActivityTypeCount]
    rows: list[BriefActivityRow]


class CommentaryContentValidity(ResponseObject):
    status: Literal["current", "frozen", "stale", "unverifiable"]
    contract: str | None
    reason: NotRequired[str]
    method: NotRequired[str]


class CommentaryItem(ResponseObject):
    id: str
    period: Month
    revision: int
    text: str
    context_digest: str
    close_digest: str | None
    source: str
    evidence_digest: str
    digest: str
    supplementary: bool
    content_validity: CommentaryContentValidity


class CommentaryDetails(ResponseObject):
    status: Literal["frozen", "current", "stale", "not_provided"]
    current: CommentaryItem | None
    frozen: CommentaryItem | None
    latest: CommentaryItem | None
    supplements: list[CommentaryItem]


class MaterialCompleteness(ResponseObject):
    closed: bool
    satisfied: bool
    issues: list[ReadinessIssue]
    coverage_digest: NotRequired[str]


class BriefPositionIssue(ResponseObject):
    field: str
    message: str
    semantics: NotRequired[str]
    account: NotRequired[str]
    amount_fen: NotRequired[WireFen]


class BriefPosition(ResponseObject):
    assets_fen: WireFen | None
    liabilities_fen: WireFen | None
    capital_fen: WireFen | None
    equity_fen: WireFen | None
    bank_fen: WireFen
    bank_calculation: dict[str, WireFen | None]
    liability_calculation: dict[str, WireFen | None]
    fixed_asset_cost_fen: WireFen
    accumulated_depreciation_fen: WireFen
    fixed_asset_net_fen: WireFen | None
    intangible_asset_cost_fen: WireFen
    accumulated_amortization_fen: WireFen
    intangible_asset_net_fen: WireFen | None
    other_assets_fen: WireFen | None
    month_revenue_fen: WireFen | None
    month_expense_fen: WireFen | None
    month_result_fen: WireFen | None
    cumulative_result_fen: WireFen | None
    equation_valid: bool | None
    complete: bool
    issues: list[BriefPositionIssue]


class BriefCash(ResponseObject):
    transaction_count: Count
    matched_count: Count
    unmatched_count: Count
    needs_review_count: Count
    coverage_state: CoverageState
    missing_account_count: Count
    inflow_fen: WireFen | None
    outflow_fen: WireFen | None
    net_fen: WireFen | None


class FundsOverview(ResponseObject):
    total_fen: WireFen | None
    bank_fen: WireFen | None
    cash_fen: WireFen | None
    payment_platform_fen: WireFen | None
    inflow_fen: WireFen
    outflow_fen: WireFen
    net_change_fen: WireFen
    internal_transfer_fen: WireFen


class UnmatchedBankActivity(ResponseObject):
    count: Count
    inflow_fen: WireFen
    outflow_fen: WireFen
    rows: list[BankStatementRow]
    rows_truncated: bool


class SettlementSourceEvent(ResponseObject):
    calculation_id: str
    fact_id: str
    posting_period: Month
    voucher_version_id: str | None
    direction: Literal[-1, 1]


class OpenItem(ResponseObject):
    id: str
    key: str
    category_key: str
    voucher: str
    party_key: str
    party: str
    field_sources: FieldSources
    description: str
    status: str
    source_business: BusinessIdentity
    source_period: NotRequired[Month]
    source_amount_fen: WireFen | None
    paid_fen: WireFen | None
    other_settled_fen: WireFen | None
    outstanding_fen: WireFen | None
    current_status: str | None
    current_outstanding_fen: WireFen | None
    subject_id: str | None
    account: str
    amount_fen: NotRequired[WireFen]
    cashflow: NotRequired[str]
    category: str
    counterparty_id: str | None
    creditor_id: NotRequired[str | None]
    name: str
    normal: NotRequired[str]
    source_calculation_id: str
    source_fact_id: str
    source_result_digest: NotRequired[str]
    state: NotRequired[str]
    settlement_status: str
    period_paid_fen: WireFen | None
    period_other_settled_fen: WireFen | None
    remaining_fen: WireFen | None
    source_event_count: Count
    source_events: list[SettlementSourceEvent]


class OpenItemGroup(ResponseObject):
    key: str
    party: str
    field_sources: FieldSources
    count: Count
    outstanding_fen: WireFen | None
    open_count: Count
    partial_count: Count


class OpenCategory(ResponseObject):
    key: str
    label: str
    direction: Literal["receivable", "payable"]
    unit: Literal["笔"]
    count: Count
    loaded_count: Count
    outstanding_fen: WireFen | None
    groups: list[OpenItemGroup]


class OpenTotals(ResponseObject):
    receivable_count: Count
    receivable_fen: WireFen | None
    payable_count: Count
    payable_fen: WireFen | None
    total_count: Count
    unestablished_count: Count
    complete: bool
    status: str
    issues: list[ReadinessIssue]


class BriefOpenItems(OpenTotals):
    categories: list[OpenCategory]
    current_outstanding: OpenTotals
    cutoff_period: Month
    current_cutoff_period: Month


class WorkforcePeriod(ResponseObject):
    payroll_period: NotRequired[Month]
    remuneration_period: NotRequired[Month]
    total_fen: WireFen
    has_reversal: bool
    has_amendment: bool
    correction_ids: list[str]
    gross_salary_fen: NotRequired[WireFen]
    employer_social_insurance_fen: NotRequired[WireFen]
    employer_housing_fund_fen: NotRequired[WireFen]
    employee_social_insurance_fen: NotRequired[WireFen]
    employee_housing_fund_fen: NotRequired[WireFen]
    gross_remuneration_fen: NotRequired[WireFen]
    theoretical_withholding_tax_fen: NotRequired[WireFen | None]


class EmployeeCost(ResponseObject):
    has_activity: bool
    breakdown_available: bool
    reason: str | None
    total_fen: WireFen
    controlled_total_fen: WireFen
    settlement_adjustment_fen: WireFen
    prior_period_settlement_adjustment_fen: WireFen
    batch_count: Count
    periods: list[WorkforcePeriod]
    annual_bonus_fen: WireFen | None
    gross_salary_fen: WireFen | None
    employer_social_insurance_fen: WireFen | None
    employer_housing_fund_fen: WireFen | None
    employee_social_insurance_fen: WireFen | None
    employee_housing_fund_fen: WireFen | None
    personal_withholding_fen: WireFen | None


class PersonalLaborCost(ResponseObject):
    has_activity: bool
    breakdown_available: bool
    reason: str | None
    total_fen: WireFen
    gross_remuneration_fen: WireFen | None
    booked_withholding_tax_fen: WireFen | None
    unwithheld_tax_fen: WireFen | None
    theoretical_withholding_tax_fen: WireFen | None
    withholding_status: str
    withholding_note: str
    settlement_modes: list[str]
    batch_count: Count
    periods: list[WorkforcePeriod]


class WorkforceCost(ResponseObject):
    has_activity: bool
    total_fen: WireFen
    capitalized_labor_fen: WireFen
    employee: EmployeeCost
    personal_labor: PersonalLaborCost


class LongTermAssets(ResponseObject):
    net_fen: WireFen | None
    fixed_net_fen: WireFen | None
    intangible_net_fen: WireFen | None
    fixed_active_count: Count
    intangible_active_count: Count
    pending_count: Count
    project_cost_fen: WireFen | None


class ValidationItem(ResponseObject):
    key: str
    label: str
    state: Literal["pass", "pending", "error", "neutral"]
    text: str


class BriefValidation(ResponseObject):
    state: Literal["complete", "attention", "error", "pending"]
    title: str
    summary: str
    integrity_valid: bool | None
    voucher_balanced: bool
    issues: list[ReadinessIssue]
    attention_count: Count
    items: list[ValidationItem]


class BriefCollections(ResponseObject):
    vouchers: NotRequired[Collection[BriefVoucher]]
    open_items: NotRequired[Collection[OpenItem]]
    businesses: NotRequired[Collection[BusinessEvent]]
    settlement_events: NotRequired[ScopedSettlementCollection]
    external_followups: NotRequired[Collection[ExternalObligation]]
    file_jobs: NotRequired[Collection[FileJob]]


class AdoptedBasisSources(ResponseObject):
    policies: list[AdoptedPolicy]
    payroll_confirmations: list[AdoptedPayrollConfirmation]
    evidence: list[CloseReviewSourceReference]


class BriefAdoptedBasis(AdoptedBasisSources):
    scope: Literal["current_voucher_page"]
    calculation_ids: list[str]


class BusinessAdoptedBasis(AdoptedBasisSources):
    basis: Literal["current_publication", "frozen_adoption"]
    calculation_ids: list[str]


class BriefData(ResponseObject):
    generated_at: str
    management_commentary: str
    management_commentary_details: CommentaryDetails
    material_completeness: MaterialCompleteness | None
    period_preparation: PeriodPreparation | None
    voucher_count: Count
    line_count: Count
    total_debit_fen: WireFen
    total_credit_fen: WireFen
    focused_voucher: BriefVoucher | None
    adopted_basis: BriefAdoptedBasis
    activity_groups: list[BriefActivityGroup]
    position: BriefPosition
    funds_overview: FundsOverview
    cash: BriefCash
    unmatched_bank_activity: UnmatchedBankActivity
    open_items: BriefOpenItems
    workforce_cost: WorkforceCost
    long_term_assets: LongTermAssets
    validation: BriefValidation
    collections: BriefCollections


class DashboardBriefResponse(ResponseObject):
    schema_version: Version6
    snapshot_version: str | None
    selected_period: DashboardPeriod | None
    read_semantics: ReadSemantics
    read_context: DashboardReadContext
    data: BriefData | None
    projection: NotRequired[Literal["dashboard_brief_deferred"]]


class TraceTarget(ResponseObject):
    calculation_id: str
    voucher_version_id: str | None
    selection_status: NotRequired[Literal["unestablished"]]


class VoucherEventLine(ResponseObject):
    line_no: int
    account: str
    debit: WireFen
    credit: WireFen
    cashflow: str | None


class VoucherEvent(ResponseObject):
    id: NotRequired[str]
    event_type: Literal["voucher"]
    voucher_version_id: str
    voucher_id: str
    voucher_number: int
    voucher_calculation_id: str
    calculation_id: str
    fact_id: str
    kind: str
    calculation_period: Month
    posting_period: Month
    result_digest: str
    role: str
    direction: int
    reverses_voucher_version_id: str | None
    selection_source: str
    lines: NotRequired[list[VoucherEventLine]]


class StateResult(ResponseObject):
    id: NotRequired[str]
    event_type: Literal["state_result"]
    status: Literal["established"]
    calculation_id: str
    fact_id: str
    kind: str
    calculation_period: Month
    posting_period: Month
    result_digest: str
    opening: bool
    selection_source: str
    selection_proof: SelectionProof
    vouchers: list[VoucherEvent]
    payroll_confirmation: NotRequired[PayrollConfirmation]


class CandidateSelection(ResponseObject):
    calculation_id: str
    fact_id: str
    result_digest: str
    kind: str
    has_journal_lines: bool
    trace_only: TrueValue


class UnestablishedSelection(ResponseObject):
    id: NotRequired[str]
    subject_id: str
    posting_period: Month
    selection_status: Literal["unestablished"]
    reason: str
    candidates: list[CandidateSelection]
    trace_targets: list[CalculationTarget]


BusinessEvent = VoucherEvent | StateResult | UnestablishedSelection


class SettlementEvent(ResponseObject):
    id: str
    index: int
    mode: str
    settlement_business: BusinessIdentity
    settlement_calculation_id: str
    settlement_fact_id: str
    source_business: BusinessIdentity
    source_calculation_id: str
    source_fact_id: str
    obligation_key: str
    obligation_name: str
    amount_fen: WireFen | None
    party_key: tuple[str, str | None]
    creditor_id: str | None
    recipient_id: str | None
    line_numbers: list[int]
    relation_state: Literal["resolved", "unresolved"]
    posting_period: Month
    voucher_version_id: str | None
    direction: int
    signed_amount_fen: WireFen | None
    issues: list[ReadinessIssue]


class SourceHistoryItem(ResponseObject):
    id: str
    subject_id: str
    revision: int
    kind: str
    period: Month
    evidence: list[str]
    deleted: NotRequired[bool]
    knowledge: NotRequired[Literal["current_knowledge"]]
    recorded_at: str | None
    trace_targets: list[TraceTarget]


class FileReference(ResponseObject):
    id: NotRequired[str]
    source: NotRequired[str]
    subject_id: NotRequired[str]
    calculation_id: NotRequired[str]
    obligation: NotRequired[str]


class ReportPeriod(ResponseObject):
    year: int
    quarter: int
    quarter_start: Day
    quarter_end: Day
    label: str


class FileJob(ResponseObject):
    job_id: str
    kind: str
    status: str
    attempts: Count
    last_error: str | None
    result_issue: NotRequired[ReadinessIssue]
    contract_issues: NotRequired[list[ReadinessIssue]]
    association: Literal["direct_source", "period_scope"]
    references: list[FileReference]
    period: Month | ReportPeriod
    verified_when_succeeded: bool
    current_file_availability: Literal["not_checked"]


class ExternalObligation(ResponseObject):
    obligation_id: str
    kind: str
    label: str
    start_period: Month
    end_period: Month
    completion_status: str
    due_date: Day | None
    issue_count: Count
    fact_issues: list[ReadinessIssue]


class PayrollConfirmation(ResponseObject):
    mode: Literal["monthly_plan", "explicit_no_change"]
    confirmation_fact_id: str
    confirmation_subject_id: str
    confirmation_revision: int
    confirmation_kind: str
    evidence: list[str]


class SettlementObligation(ResponseObject):
    key: str
    name: str
    category: str
    account: str
    counterparty_id: str | None
    creditor_id: NotRequired[str | None]
    recipient_id: NotRequired[str | None]
    source_business: BusinessIdentity
    source_calculation_id: str
    source_fact_id: str
    source_period: NotRequired[Month]
    source_result_digest: NotRequired[str]
    source_amount_fen: WireFen | None
    amount_fen: NotRequired[WireFen | None]
    paid_fen: WireFen | None
    other_settled_fen: WireFen | None
    period_paid_fen: WireFen | None
    period_other_settled_fen: WireFen | None
    remaining_fen: WireFen | None
    settlement_status: str
    source_event_count: Count
    cashflow: NotRequired[str]
    normal: NotRequired[str]
    party_key: NotRequired[tuple[str, str | None] | None]
    party_role: NotRequired[str]
    state: NotRequired[Literal["resolved", "unresolved"]]
    source_events: NotRequired[list[SettlementSourceEvent]]
    reimbursement_acceptance_basis: NotRequired[Literal["company_confirmation_month"]]


class ScopedSettlementCollection(Collection[SettlementEvent]):
    scope_period: NotRequired[Month]
    current_cutoff_period: NotRequired[Month]
    cutoff_semantics: NotRequired[str]


class SourceSettlement(ResponseObject):
    subject_id: str
    settlement_view: Literal["historical"]
    movements_scope: Literal["business_related_settlement_events"]
    status: str
    obligations: list[SettlementObligation]
    issues: list[ReadinessIssue]
    cutoff_period: Month
    current_followups: SettlementCurrentFollowup


class SettlementCurrentFollowup(ResponseObject):
    status: str
    issues: list[ReadinessIssue]
    current_cutoff_period: Month
    cutoff_semantics: str
    obligations: list[SettlementObligation]


class TaxDetail(ResponseObject):
    calculation_id: str
    period: Month
    kind: str
    reversal: bool
    booked_tax_fen: WireFen
    calculated_tax_fen: WireFen | None
    actual_withholding_tax_fen: WireFen | None
    actual_withholding_fact_id: str | None


class FieldConflict(ResponseObject):
    field: str
    values: list[str]
    sources: list[SourceMetadata]


class EmployeeItem(ResponseObject):
    employee_id: str
    selection_status: Literal["established"]
    code: str
    name: str
    field_sources: FieldSources
    field_conflicts: list[FieldConflict]
    record_status: str
    period_state: str
    period_state_label: str
    in_period: bool | None
    employment_start_date: Day | None
    employment_end_date: Day | None
    tax_withholding_start_date: Day | None
    profile_available: bool
    expense_areas: list[str]
    social_insurance_participating: bool | None
    housing_fund_participating: bool | None
    social_insurance_base_fen: WireFen | None
    housing_fund_base_fen: WireFen | None
    has_payroll_activity: bool
    batch_count: Count
    tax_details: list[TaxDetail]
    declared_tax_fen: WireFen | None
    recorded_net_payments_fen: WireFen | None
    direct_net_payments_fen: WireFen | None
    other_net_settlements_fen: WireFen | None
    payroll_periods: list[Month]
    has_annual_bonus: bool
    gross_salary_fen: WireFen
    annual_bonus_fen: WireFen
    employer_social_insurance_fen: WireFen
    employer_housing_fund_fen: WireFen
    employee_social_insurance_fen: WireFen
    employee_housing_fund_fen: WireFen
    individual_income_tax_fen: WireFen
    net_salary_fen: WireFen
    tax_reported_salary_fen: WireFen
    personal_deduction_fen: WireFen
    company_cost_fen: WireFen
    wage_tax_scope: str
    wage_tax_scope_label: str


class UnestablishedEmployee(ResponseObject):
    employee_id: str
    name: str
    selection_status: Literal["unestablished"]
    candidate_selections: list[CandidateSelection]
    trace_targets: list[CalculationTarget]
    gross_salary_fen: None
    annual_bonus_fen: None
    employer_social_insurance_fen: None
    employer_housing_fund_fen: None
    employee_social_insurance_fen: None
    employee_housing_fund_fen: None
    individual_income_tax_fen: None
    net_salary_fen: None
    tax_reported_salary_fen: None
    personal_deduction_fen: None
    company_cost_fen: None
    recorded_net_payments_fen: None
    direct_net_payments_fen: None
    other_net_settlements_fen: None


class EmployeeSummary(ResponseObject):
    gross_salary_fen: WireFen | None
    annual_bonus_fen: WireFen | None
    employer_social_insurance_fen: WireFen | None
    employer_housing_fund_fen: WireFen | None
    employee_social_insurance_fen: WireFen | None
    employee_housing_fund_fen: WireFen | None
    individual_income_tax_fen: WireFen | None
    net_salary_fen: WireFen | None
    tax_reported_salary_fen: WireFen | None
    personal_deduction_fen: WireFen | None
    unestablished_count: Count
    registered_count: Count
    in_period_count: Count
    unknown_period_count: Count
    payroll_count: Count
    without_payroll_count: Count
    profile_missing_count: Count
    contributions_only_count: Count
    controlled_cost_fen: WireFen | None
    settlement_adjustment_fen: WireFen | None
    ledger_cost_fen: WireFen
    detail_reconciled: bool | None
    breakdown_available: bool
    breakdown_reason: str | None
    identity_note: str


class PayrollDeclaration(ResponseObject):
    fact_id: str
    revision: int
    source: Literal["current_record"]
    tax_period: Month
    recording_period: Month
    date: Day | None
    declared_tax_fen: WireFen
    recorded_later: bool
    recorded_at: str | None
    source_metadata: SourceMetadata


class PayrollDisbursement(ResponseObject):
    calculation_id: str
    recording_period: Month
    needs_review: bool
    matches_displayed_wage: bool
    employee_id: str
    tax_period: Month
    payroll_kind: str
    payroll_id: str
    payroll_fact_id: str
    payroll_calculation_id: str
    payroll_result_digest: str
    declaration_id: str
    declaration_fact_id: str
    declaration_evidence: list[str]
    calculated_tax_fen: WireFen
    declared_tax_fen: WireFen
    original_net_fen: WireFen
    target_net_fen: WireFen
    held_fen: WireFen
    withholding_recorded: bool


class PayrollSource(SourceSettlement):
    source_id: str
    calculation_id: str
    kind: str
    period: Month
    opening_period: Month | None
    component: str | None
    label: str
    declarations: list[PayrollDeclaration]
    disbursements: list[PayrollDisbursement]


class LaborSource(SourceSettlement):
    source_id: str
    calculation_id: str
    period: Month
    person_id: str
    name: str
    party: str
    field_sources: FieldSources
    capitalized: bool
    project_id: str | None
    gross_fen: WireFen
    net_fen: WireFen
    booked_tax_fen: WireFen
    theoretical_tax_fen: WireFen | None
    withholding_method: str
    withholding_label: str


class EmployeeCollections(ResponseObject):
    employees: NotRequired[Collection[EmployeeItem | UnestablishedEmployee]]
    payroll_sources: NotRequired[Collection[PayrollSource]]
    labor_sources: NotRequired[Collection[LaborSource]]
    settlement_events: NotRequired[ScopedSettlementCollection]


class EmployeesData(ResponseObject):
    employees: EmployeeSummary
    collections: EmployeeCollections
    workforce_cost: WorkforceCost
    period_preparation: PeriodPreparation | None


class DashboardEmployeesResponse(ResponseObject):
    schema_version: Version6
    snapshot_version: str | None
    selected_period: DashboardPeriod | None
    read_semantics: ReadSemantics
    read_context: DashboardReadContext
    data: EmployeesData | None


class LabeledSettlement(SourceSettlement):
    source_id: str
    label: str


class AssetBatchReference(ResponseObject):
    calculation_id: str
    owner_calculation_id: str
    voucher_version_id: str | None
    voucher_number: int | None
    period: Month
    label: str


class AssetItemBase(ResponseObject):
    asset_id: str
    asset_type: Literal["fixed", "intangible"]
    code: str
    name: str
    category: str
    category_label: str
    field_sources: FieldSources
    status: Literal["active", "pending_activation", "disposed", "retired"]
    status_label: str
    acquisition_date: Day | None
    posting_period: Month
    recognition_label: str
    source_label: str
    source_party_label: str
    source_parties: str | None
    settlement_scope: str
    settlements: list[LabeledSettlement]
    cost_fen: WireFen | None
    accumulated_charge_fen: WireFen | None
    month_charge_fen: WireFen | None
    book_value_fen: WireFen | None
    latest_charge_period: Month | None
    charge_state_label: str | None
    batch_references: list[AssetBatchReference]
    benefit_area_label: str | None
    useful_life_months: int | None
    acquisition_reference: str
    month_acquired: bool
    month_activated: bool
    month_exited: bool


class FixedAssetDisposal(ResponseObject):
    date: Day
    book_value_fen: WireFen | None
    reference: str
    settlement: SourceSettlement
    kind: Literal["sale", "retirement"]
    gross_proceeds_fen: WireFen
    gain_fen: WireFen
    loss_fen: WireFen
    party: str
    party_id: NotRequired[str]
    source: NotRequired[str | None]
    field_sources: NotRequired[FieldSources]


class IntangibleAssetRetirement(ResponseObject):
    date: Day
    book_value_fen: WireFen | None
    reference: str
    settlement: SourceSettlement


class FixedAssetItem(AssetItemBase):
    asset_type: Literal["fixed"]
    in_service_date: Day | None
    residual_value_fen: WireFen | None
    depreciation_method_label: str | None
    rounding_policy_label: str | None
    disposal: FixedAssetDisposal | None


class IntangibleAssetItem(AssetItemBase):
    asset_type: Literal["intangible"]
    available_for_use_date: Day | None
    life_basis_label: str
    life_basis_explanation: str
    rights_description: str
    retirement: IntangibleAssetRetirement | None


class UnestablishedAsset(ResponseObject):
    asset_id: str
    asset_type: Literal["fixed", "intangible"] | None
    name: str
    selection_status: Literal["unestablished"]
    candidate_selections: list[CandidateSelection]
    trace_targets: list[CalculationTarget]
    cost_fen: None
    accumulated_charge_fen: None
    month_charge_fen: None
    book_value_fen: None
    established_card: NotRequired[FixedAssetItem | IntangibleAssetItem]


AssetItem = FixedAssetItem | IntangibleAssetItem | UnestablishedAsset


class FixedAssetSummary(ResponseObject):
    registered_count: Count
    unestablished_count: Count
    active_count: Count
    active_cost_fen: WireFen | None
    active_accumulated_fen: WireFen | None
    active_net_fen: WireFen | None
    pending_count: Count
    pending_cost_fen: WireFen | None
    month_acquired_count: Count
    month_acquired_fen: WireFen
    month_cost_adjustment_fen: WireFen
    month_depreciation_fen: WireFen | None
    disposed_count: Count
    month_activated_count: Count
    month_disposed_count: Count


class IntangibleAssetSummary(ResponseObject):
    registered_count: Count
    unestablished_count: Count
    active_count: Count
    active_cost_fen: WireFen | None
    active_accumulated_fen: WireFen | None
    active_net_fen: WireFen | None
    pending_count: Count
    pending_cost_fen: WireFen | None
    month_acquired_count: Count
    month_acquired_fen: WireFen
    month_cost_adjustment_fen: WireFen
    month_amortization_fen: WireFen | None
    retired_count: Count
    month_retired_count: Count


class AssetProject(ResponseObject):
    source_id: str
    project_id: str
    period: Month
    kind: str
    label: str
    party: str
    party_id: NotRequired[str]
    source: NotRequired[str | None]
    field_sources: NotRequired[FieldSources]
    cost_fen: WireFen
    remaining_fen: WireFen
    settlement: SourceSettlement


class AssetDifferences(ResponseObject):
    cost_fen: WireFen | None
    accumulated_fen: WireFen | None
    net_fen: WireFen | None


class EstablishedCardTotals(ResponseObject):
    registered_count: Count
    cost_fen: WireFen
    accumulated_charge_fen: WireFen
    book_value_fen: WireFen


class AssetCollections(ResponseObject):
    assets: NotRequired[Collection[AssetItem]]
    projects: NotRequired[Collection[AssetProject]]
    source_history: NotRequired[Collection[SourceHistoryItem]]
    settlement_events: NotRequired[ScopedSettlementCollection]


class AssetsData(ResponseObject):
    fixed_asset_cost_fen: WireFen
    accumulated_depreciation_fen: WireFen
    fixed_asset_net_fen: WireFen
    intangible_asset_cost_fen: WireFen
    accumulated_amortization_fen: WireFen
    intangible_asset_net_fen: WireFen
    active_count: Count
    registered_count: Count
    unestablished_count: Count
    ledger_cost_fen: WireFen
    ledger_accumulated_fen: WireFen
    ledger_net_fen: WireFen
    active_ledger_net_fen: WireFen
    pending_intangible_count: Count
    pending_intangible_cost_fen: WireFen | None
    project_cost_fen: WireFen
    reconciliation_scope: str
    card_cost_fen: WireFen | None
    card_accumulated_fen: WireFen | None
    card_net_fen: WireFen | None
    established_card_totals: EstablishedCardTotals | None
    pending_fixed_count: Count
    pending_fixed_cost_fen: WireFen | None
    month_charge_fen: WireFen | None
    month_acquired_count: Count
    month_acquired_fen: WireFen
    month_cost_adjustment_fen: WireFen
    month_activated_count: Count
    month_exited_count: Count
    reconciled: bool | None
    reconciliation_label: str
    differences: AssetDifferences
    fixed: FixedAssetSummary
    intangible: IntangibleAssetSummary
    collections: AssetCollections
    period_preparation: PeriodPreparation | None


class DashboardAssetsResponse(ResponseObject):
    schema_version: Version6
    snapshot_version: str | None
    selected_period: DashboardPeriod | None
    read_semantics: ReadSemantics
    read_context: DashboardReadContext
    data: AssetsData | None


class LatestBusinessSource(ResponseObject):
    id: str
    subject_id: str
    revision: int
    kind: str
    period: Month
    evidence: list[str]
    deleted: bool
    knowledge: Literal["current_knowledge"]
    recorded_at: str | None


class CurrentBusinessResult(ResponseObject):
    status: Literal["published"]
    knowledge: Literal["current_knowledge"]
    calculation_id: str
    subject_id: str
    kind: str
    fact_id: str
    result_digest: str
    posting_period: Month
    publication_id: str
    voucher_id: str | None
    has_journal_lines: bool
    current_voucher_version_id: str | None
    amount_fen: WireFen | None
    amount_label: str
    payroll_confirmation: NotRequired[PayrollConfirmation]


class AssetCardAdoptionProof(ResponseObject):
    basis: Literal["asset_card_adoption"]
    owner_calculation_id: str
    owner_publication_id: str


class FrozenAdoption(ResponseObject):
    close_period: Month
    publication_id: str
    calculation_id: str
    result_digest: str
    role: str
    selection_proof: SelectionProof | AssetCardAdoptionProof
    amount_fen: WireFen | None
    amount_label: str
    payroll_confirmation: NotRequired[PayrollConfirmation]


class AsPostedAccounting(ResponseObject):
    cutoff_period: Month
    status: str
    voucher_events: list[VoucherEvent]
    state_results: list[StateResult]
    unestablished_state_selections: list[UnestablishedSelection]
    asset_member_results: NotRequired[list[StateResult]]


class BusinessReview(ResponseObject):
    status: str
    latest_matches_publication: bool
    pending_causes: list[str]
    dispositions: list[str]
    disposition_count: Count


class BusinessSettlements(ResponseObject):
    cutoff_period: Month
    status: str
    obligations: list[SettlementObligation]
    issues: list[ReadinessIssue]
    business_count: Count
    movement_count: Count
    line_relation_count: Count


class CurrentBusinessSettlements(ResponseObject):
    cutoff_period: Month
    status: str
    obligations: list[SettlementObligation]
    issues: list[ReadinessIssue]
    business_count: Count
    movement_count: Count
    line_relation_count: Count
    unestablished_state_selections: list[UnestablishedSelection]
    complete: bool
    scope_period: Month
    current_cutoff_period: Month
    cutoff_semantics: str


class BusinessStatusCurrentFollowups(ResponseObject):
    settlements: CurrentBusinessSettlements


class ExternalSummary(ResponseObject):
    status: str
    obligation_count: Count
    completion_status_counts: dict[str, Count]
    basis_issue_count: Count
    as_of: Day
    as_of_semantics: Literal["current_knowledge"]
    completion_count: Count


class FileJobsSummary(ResponseObject):
    total_count: Count
    status_counts: dict[str, Count]
    issue_count: Count


class DisplayProfileValues(ResponseObject):
    display_name: str | None
    display_number: str | None
    purpose: str | None
    note: str | None
    employment_start: Day | None
    employment_end: Day | None
    employment_status: str | None
    active: bool | None
    category_label: str | None
    rights_description: str | None
    useful_life_basis: str | None
    counterparty_id: str | None
    beneficiary_id: str | None
    handler_id: str | None


class DisplayProfile(ResponseObject):
    entity_id: str
    values: DisplayProfileValues
    field_sources: FieldSources


class DisplayProfiles(ResponseObject):
    business: NotRequired[DisplayProfile]
    counterparties: NotRequired[list[DisplayProfile]]
    employees: NotRequired[list[DisplayProfile]]
    assets: NotRequired[list[DisplayProfile]]


class BusinessReadSemantics(ResponseObject):
    knowledge: Literal["current_knowledge"]
    accounting: Literal["as_posted"]
    business_basis: Literal["current_known", "frozen_adoption"]
    display: Literal["frozen_with_current_supplements"]
    as_of: Literal["external_deadlines_and_completion_only"]


class DuplicateCandidate(ResponseObject):
    subject_id: str
    fact_id: str | None
    kind: str
    period: Month
    signals: list[DuplicateSignal]


class DuplicateUnresolved(ResponseObject):
    message: str
    candidate_subject_id: str
    review_period: Month
    signals: list[DuplicateSignal]


class DuplicateCheck(ResponseObject):
    check_id: str
    action: Literal["clear", "reuse_existing", "create_separate"]
    proposed_subject_id: str
    result_fact_id: str
    selected_fact_id: str | None
    candidate_digest: str
    explanation: str
    created_at: str


class DuplicateChecks(ResponseObject):
    candidate_contract: str
    candidate_version: int
    subject_id: str
    status: Literal["clear", "review_required"]
    strong_candidates: list[DuplicateCandidate]
    weak_candidates: list[DuplicateCandidate]
    unresolved: list[DuplicateUnresolved]
    checks: list[DuplicateCheck]
    check_count: Count
    checks_truncated: bool


class IdentityCorrection(ResponseObject):
    id: str
    action: str
    before_fact_id: str
    after_fact_id: str | None
    replacement_subject_id: str | None
    digest: str


class EntityReference(ResponseObject):
    fact_id: str
    path: str
    recorded_entity_id: str
    current_entity_id: str
    role: str


class BusinessCollections(ResponseObject):
    events: NotRequired[Collection[BusinessEvent]]
    settlement_events: NotRequired[ScopedSettlementCollection]
    source_history: NotRequired[Collection[SourceHistoryItem]]
    file_jobs: NotRequired[Collection[FileJob]]


class BusinessIdentityDetails(ResponseObject):
    company_id: str
    database_id: str
    subject_id: str
    kind: str


class BusinessStatusData(ResponseObject):
    identity: BusinessIdentityDetails
    period: Month
    as_of: Day
    latest_source: LatestBusinessSource
    closure: Closure
    as_posted: AsPostedAccounting
    current_business_result: CurrentBusinessResult | None
    frozen_adoption: FrozenAdoption | None
    adopted_basis: BusinessAdoptedBasis | None
    review: BusinessReview
    settlements: BusinessSettlements
    external: ExternalSummary
    file_jobs: FileJobsSummary
    display_profiles: DisplayProfiles
    trace_targets: list[TraceTarget]
    read_semantics: BusinessReadSemantics
    projection: Literal["summary"]
    trace_target_count: Count
    current_followups: BusinessStatusCurrentFollowups
    duplicate_checks: DuplicateChecks
    identity_corrections: list[IdentityCorrection]
    entity_references: list[EntityReference]
    settlement_view: Literal["historical", "current"]
    collections: BusinessCollections


class DashboardBusinessStatusResponse(ResponseObject):
    schema_version: Version4
    snapshot_version: str
    selected_period: DashboardPeriod
    read_semantics: ReadSemantics
    read_context: DashboardReadContext
    data: BusinessStatusData


class CarryForwardOption(ResponseObject):
    fact_id: str
    subject_id: str
    revision: int
    period: Month
    label: str
    evidence_count: Count
    used: bool


class CarryForward(ResponseObject):
    selected_fact_id: str | None
    options: list[CarryForwardOption]


class ReportReadinessDetail(ResponseObject):
    primary: str
    secondary: str
    location: ReadinessIssue


class ReportReadinessItem(ResponseObject):
    key: str
    label: str
    state: Literal["pass", "pending", "attention"]
    summary: str
    details: list[ReportReadinessDetail]


class ReportSummary(ResponseObject):
    assets_total_fen: WireFen | None
    liabilities_total_fen: WireFen | None
    liabilities_equity_total_fen: WireFen | None
    current_net_profit_fen: WireFen | None
    year_to_date_net_profit_fen: WireFen | None
    current_cash_change_fen: WireFen | None
    ending_cash_fen: WireFen | None


class ReportStatementColumn(ResponseObject):
    key: str
    label: str


class ReportStatementRow(ResponseObject):
    line: int
    name: str
    values: dict[str, WireFen | None]
    is_total: bool
    has_amount: bool


class ReportStatement(ResponseObject):
    key: str
    label: str
    columns: list[ReportStatementColumn]
    rows: list[ReportStatementRow]


class ReportCheck(ResponseObject):
    code: str
    label: str
    passed: bool | None


class ReportChecks(ResponseObject):
    passed: Count
    total: Count
    items: list[ReportCheck]


class Organization(ResponseObject):
    name: str | None
    taxpayer_identification_number: str | None


class ReportEpochs(ResponseObject):
    accounting: Count
    material: Count
    management: Count


class ReportExport(ResponseObject):
    available: bool
    file_name: str
    calculation_hash: str | None
    preview_digest: str | None
    epochs: ReportEpochs | None


class ReportTemplate(ResponseObject):
    name: str
    sha256: str
    profile: str
    file_name: str


class ReportRule(ResponseObject):
    version: str
    adapter_version: str
    effective_from: Day
    source_url: str


class ReportTechnical(ResponseObject):
    calculation_hash: str
    template: ReportTemplate
    rule: ReportRule
    source_close_hashes: list[str]
    classification_count: Count
    income_tax_confirmation_count: Count
    requirement_codes: list[str]
    errors: list[str]


class DashboardQuarterlyReportResponse(ResponseObject):
    schema_version: Version3
    close_state: Literal["open", "closed"]
    readiness_state: Literal["ready", "blocked"]
    carry_forward: CarryForward
    status: Literal["ready", "blocked", "in_progress", "not_applicable", "error"]
    status_label: str
    headline: str
    message: str
    checked_at: str
    organization: Organization
    period: ReportPeriod
    readiness: list[ReportReadinessItem]
    summary: ReportSummary
    statements: list[ReportStatement]
    checks: ReportChecks
    draft: bool
    export: ReportExport
    technical: ReportTechnical
    read_context: DashboardReadContext
    period_preparations: list[PeriodPreparation] | None
    projection: NotRequired[Literal["dashboard_quarterly_report_deferred"]]


class BriefChecks(ResponseObject):
    material_completeness: MaterialCompleteness
    issues: list[ReadinessIssue]
    attention_count: Count
    items: list[ValidationItem]


class PeriodPreparationData(ResponseObject):
    period_preparation: PeriodPreparation
    brief_checks: BriefChecks


class DashboardPeriodPreparationResponse(ResponseObject):
    schema_version: Version3
    projection: Literal["dashboard_period_preparation_result"]
    read_context: DashboardReadContext
    period: Month
    data: PeriodPreparationData


class BrowserReportSource(ResponseObject):
    year: int
    quarter: int
    carry_forward_fact_id: str | None


class BrowserJob(ResponseObject):
    id: str
    kind: str
    status: Literal["pending", "running", "succeeded", "failed"]
    attempts: Count
    last_error: str | None
    download_available: bool
    download_file_name: str | None
    delivery_status: Literal["pending", "unavailable", "external", "invalid", "verified"]
    delivery_message: str | None
    report_source: NotRequired[BrowserReportSource]


class BrowserJobsResponse(ResponseObject):
    schema_version: Version1
    company_id: str
    database_id: str
    items: list[BrowserJob]


SecurityAction = Literal[
    "bootstrap_owner",
    "login",
    "approve_period_close",
    "change_password",
    "recover",
    "replace_recovery_code",
]


class BrowserSecurityRequestState(ResponseObject):
    schema_version: Version1
    request_id: str
    kind: SecurityAction
    status: Literal[
        "starting", "waiting_for_user", "running", "succeeded", "failed", "cancelled", "expired"
    ]
    catalog_instance_id: str
    error_code: str | None
    operation_committed: bool | None
    login_completed: bool
    recovery_code_acknowledged: bool
    browser_authenticated: NotRequired[bool]
    recovery_code: NotRequired[str | None]


class BrowserSecuritySessionStatus(ResponseObject):
    schema_version: Version1
    catalog_instance_id: str
    provisioned: bool
    login_name: str | None
    active: bool
    authenticated: bool
    owner_id: NotRequired[str]


BrowserSecurityStatusResponse = BrowserSecurityRequestState | BrowserSecuritySessionStatus


class ReportExportReceipt(ResponseObject):
    status: Literal["queued"]
    job_id: str
    preview_digest: str


RESPONSE_ADAPTERS = {
    "dashboard_context": TypeAdapter(DashboardContextResponse),
    "dashboard_brief": TypeAdapter(DashboardBriefResponse),
    "dashboard_funds": TypeAdapter(FundsDashboardResponse),
    "dashboard_employees": TypeAdapter(DashboardEmployeesResponse),
    "dashboard_assets": TypeAdapter(DashboardAssetsResponse),
    "dashboard_business_status": TypeAdapter(DashboardBusinessStatusResponse),
    "dashboard_quarterly_report": TypeAdapter(DashboardQuarterlyReportResponse),
    "dashboard_period_preparation": TypeAdapter(DashboardPeriodPreparationResponse),
    "browser_jobs": TypeAdapter(BrowserJobsResponse),
    "browser_security_status": TypeAdapter(BrowserSecurityStatusResponse),
    "report_export_receipt": TypeAdapter(ReportExportReceipt),
    "dashboard_close_review": DASHBOARD_CLOSE_REVIEW_ADAPTER,
}

for _adapter in RESPONSE_ADAPTERS.values():
    _adapter.rebuild(force=True, _types_namespace=globals())


def _field_names(schema):
    if isinstance(schema, dict):
        result = set(schema.get("properties", {}))
        for value in schema.values():
            result.update(_field_names(value))
        return result
    if isinstance(schema, list):
        return set().union(*(_field_names(value) for value in schema))
    return set()


_DECLARED_FIELDS = {
    command: _field_names(adapter.json_schema()) for command, adapter in RESPONSE_ADAPTERS.items()
}


def response_schemas(*, mode="validation"):
    return {name: adapter.json_schema(mode=mode) for name, adapter in RESPONSE_ADAPTERS.items()}


def validate_response(command, value):
    adapter = RESPONSE_ADAPTERS.get(command)
    if adapter is None:
        return value
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        # Values, validator messages and submitted dictionary keys can contain
        # private material. Emit only declared field names and list positions.
        paths = []
        for error in exc.errors(include_url=False, include_input=False, include_context=False):
            current, location = value, []
            for item in error["loc"]:
                if isinstance(current, list) and type(item) is int:
                    location.append(item)
                    current = current[item] if 0 <= item < len(current) else None
                else:
                    location.append(
                        item if type(item) is str and item in _DECLARED_FIELDS[command] else "<key>"
                    )
                    current = current.get(item) if isinstance(current, dict) else None
            if error["type"] == "extra_forbidden":
                location = (*location[:-1], "<extra>")
            paths.append(".".join(map(str, location)) or "$")
        raise KernelError(
            "response_contract_mismatch", "读取结果不符合接口合同", paths=sorted(set(paths))
        ) from None


def http_response(command, value):
    """Validate at an independent HTTP boundary too; never serialize unchecked data."""
    value = validate_response(command, value)
    return RESPONSE_ADAPTERS[command].dump_python(value, mode="json")
