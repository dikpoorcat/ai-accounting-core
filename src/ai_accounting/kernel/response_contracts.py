"""Native read contracts and their HTTP representation, from one typed source.

Omission is represented by NotRequired, never by a serialization default. Money
has an explicit type even inside provenance and diagnostics. No arbitrary JSON
escape hatch is part of these two contracts.
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
    WithJsonSchema,
)
from typing_extensions import TypedDict

from .contracts import KernelError
from .types import Fen

WireFen = Annotated[
    Fen,
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "pattern": r"^(0|-?[1-9][0-9]*)$", "x-fen-int64": True},
        mode="serialization",
    ),
]
Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
Day = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")]
Count = Annotated[int, Field(ge=0)]
AccountType = Literal["bank", "cash", "payment_platform"]
CoverageState = Literal["missing", "partial", "complete", "not_applicable"]


def _integer_literal(value):
    if type(value) is not int:
        raise ValueError("integer required")
    return value


def _boolean_literal(value):
    if type(value) is not bool:
        raise ValueError("boolean required")
    return value


Version2 = Annotated[Literal[2], BeforeValidator(_integer_literal)]
Version3 = Annotated[Literal[3], BeforeValidator(_integer_literal)]
Version4 = Annotated[Literal[4], BeforeValidator(_integer_literal)]
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


class CurrentFollowups(ResponseObject):
    knowledge: Literal["current_knowledge"]
    affects_frozen_readiness: FalseValue
    materials: MaterialFollowup
    accounting: AccountingFollowup
    close_requirements: CheckFollowup
    settlements: SettlementFollowup
    external: ExternalFollowup
    file_jobs: FileJobFollowup


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
    current_followups: CurrentFollowups


class CollectionPage(ResponseObject):
    total_count: Count
    filtered_count: Count
    returned_count: Count
    has_more: bool
    next_cursor: str | None


class Collection[T](ResponseObject):
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
    rows: list[BankStatementRow]
    unmatched_totals: UnmatchedTotals
    coverage_state: CoverageState
    statement_count: Count
    expected_account_count: Count
    provided_account_count: Count
    missing_account_count: Count
    page: NotRequired[CollectionPage]


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
    products: list[InvestmentProduct]
    events: list[InvestmentEvent]
    event_count: Count
    page: NotRequired[CollectionPage]


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
    accounts: list[FundAccount]
    movements: list[FundMovement]
    investments: FundInvestments
    bank_statement: BankStatement
    collections: FundsCollections
    fact_issues: list[ReadinessIssue]
    period_preparation: PeriodPreparation | None
    movement_page: NotRequired[CollectionPage]


class FundsDashboardResponse(ResponseObject):
    schema_version: Version4
    snapshot_version: str | None
    selected_period: DashboardPeriod | None
    read_semantics: ReadSemantics
    data: FundsData | None


RESPONSE_ADAPTERS = {
    "dashboard_context": TypeAdapter(DashboardContextResponse),
    "dashboard_funds": TypeAdapter(FundsDashboardResponse),
}


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
