"""Bounded process-local ledger queries and their formal read results."""

from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import (
    AdoptedBasis,
    AmountCell,
    BusinessRef,
    FactIssue,
    LedgerScope,
    ReadPage,
    ResourceDefinition,
    ResourceUse,
    ResultRef,
    ResultSourceRef,
    ResultTarget,
)


@dataclass(frozen=True)
class FinancialSummary:
    version: str
    scope: LedgerScope
    revenue: AmountCell
    expense: AmountCell
    net_profit: AmountCell
    assets: AmountCell
    liabilities: AmountCell
    equity: AmountCell
    capital: AmountCell
    retained_profit: AmountCell
    bank: AmountCell
    cash: AmountCell
    payment_platform: AmountCell
    fixed_cost: AmountCell
    fixed_accumulated: AmountCell
    fixed_net: AmountCell
    intangible_cost: AmountCell
    intangible_accumulated: AmountCell
    intangible_net: AmountCell
    long_term_cost: AmountCell
    long_term_accumulated: AmountCell
    long_term_net: AmountCell
    issues: tuple[FactIssue, ...]
    sources: tuple[ResultSourceRef, ...]


@dataclass(frozen=True)
class AccountBalanceKey:
    account_code: str
    object_id: str | None = None


@dataclass(frozen=True)
class AccountBalance:
    key: AccountBalanceKey
    opening: AmountCell
    ending: AmountCell
    first_recorded_period: str | None
    issues: tuple[FactIssue, ...]
    sources: tuple[ResultSourceRef, ...]


@dataclass(frozen=True)
class ObligationPositionQuery:
    cutoff_period: str
    business_ids: tuple[str, ...] = ()
    party_ids: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    direction: Literal["receivable", "payable"] | None = None
    include_settled: bool = True


@dataclass(frozen=True)
class CostPositionQuery:
    cutoff_period: str
    business_ids: tuple[str, ...] = ()
    object_ids: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    include_consumed: bool = True


@dataclass(frozen=True)
class PositionAmounts:
    amount_fen: int
    money_settled_fen: int
    agency_settled_fen: int
    offset_settled_fen: int
    transferred_out_fen: int
    excess_reclassified_fen: int
    consumed_fen: int
    remaining_fen: int


@dataclass(frozen=True)
class ResourcePosition:
    definition: ResourceDefinition
    amounts: PositionAmounts


@dataclass(frozen=True)
class PositionTotal:
    direction: Literal["receivable", "payable"] | None
    category: str
    resource_count: int
    amounts: PositionAmounts


@dataclass(frozen=True)
class PositionPage:
    page: ReadPage[ResourcePosition]
    full_totals: tuple[PositionTotal, ...]
    filtered_totals: tuple[PositionTotal, ...]


@dataclass(frozen=True)
class SettlementEventQuery:
    period_from: str
    period_to: str
    source_refs: tuple[BusinessRef, ...] = ()
    consumer_business_ids: tuple[str, ...] = ()
    movement_ids: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    source_business_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettlementEvent:
    use: ResourceUse
    movement: ResourceDefinition | None


@dataclass(frozen=True)
class VoucherLocator:
    voucher_id: str
    result_id: str
    generation: int


@dataclass(frozen=True)
class VoucherDirectoryEntry:
    voucher_id: str
    period: str
    number: int
    role: str
    summary: str
    result: ResultRef
    reverses_voucher_id: str | None


@dataclass(frozen=True)
class AdoptedDirectoryEntry:
    reference: ResultRef
    source: ResultSourceRef
    business_kind: str


@dataclass(frozen=True)
class AdoptedTarget:
    reference: ResultRef
    source: ResultSourceRef
    business_kind: str
    target: ResultTarget


@dataclass(frozen=True)
class AdoptedValues:
    reference: ResultRef
    source: ResultSourceRef
    business_kind: str
    target: ResultTarget
    baseline_target: ResultTarget | None
    baseline_reference: ResultRef | None


@dataclass(frozen=True)
class AdoptedContent:
    reference: ResultRef
    source: ResultSourceRef
    target: ResultTarget
    basis: AdoptedBasis
