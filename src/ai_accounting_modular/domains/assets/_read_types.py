"""Asset-owned lifecycle views, with formal values separate from current labels."""

from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import (
    AdoptedAssetQuantity,
    AmountCell,
    BusinessRef,
    CurrentObject,
    FactIssue,
    LedgerScope,
    ReadPage,
    ResultRef,
    ResultSourceRef,
)


@dataclass(frozen=True)
class AssetCardQuery:
    filter: Literal["all", "active", "fixed", "intangible", "pending", "exited"] = "all"
    asset_id: str | None = None


@dataclass(frozen=True)
class ProjectCostQuery:
    project_id: str | None = None
    include_consumed: bool = False


@dataclass(frozen=True)
class AssetPaymentSummary:
    scope: Literal["individual", "batch", "project_sources", "opening", "unestablished"]
    sources: tuple[BusinessRef, ...]
    obligation_count: int
    amount_fen: int | None
    paid_fen: int | None
    other_settled_fen: int | None
    remaining_fen: int | None


@dataclass(frozen=True)
class AssetExit:
    reference: ResultRef
    period: str
    actual_on: str
    kind: Literal["sale", "scrap"]
    cost_fen: int
    accumulated_fen: int
    carrying_fen: int
    gross_proceeds_fen: int
    net_proceeds_fen: int
    vat_fen: int
    gain_loss_fen: int
    buyer: CurrentObject | None


@dataclass(frozen=True)
class AssetCard:
    asset_id: str
    object: CurrentObject
    asset_type: Literal["fixed", "intangible"] | None
    state: Literal["unestablished", "pending", "active", "exited"]
    origin: ResultRef | None
    activation: ResultRef | None
    consumption: ResultRef | None
    acquired_period: str | None
    acquired_on: str | None
    in_use_period: str | None
    in_use_on: str | None
    card: AdoptedAssetQuantity | None
    cost_fen: int | None
    accumulated_fen: int | None
    net_fen: int | None
    month_charge_fen: int | None
    completed_months: int | None
    month_acquired: bool
    month_activated: bool
    month_exited: bool
    payment: AssetPaymentSummary
    exit: AssetExit | None
    issues: tuple[FactIssue, ...] = ()


@dataclass(frozen=True)
class AssetProjectCost:
    reference: BusinessRef
    source: ResultSourceRef
    project: CurrentObject
    party: CurrentObject | None
    period: str
    kind: str
    cost_type: str
    project_nature: Literal["purchased_intangible", "internal_development"] | None
    cost_fen: int
    consumed_fen: int
    remaining_fen: int
    payment: AssetPaymentSummary


@dataclass(frozen=True)
class AssetTypeSummary:
    asset_type: Literal["fixed", "intangible"]
    registered_count: int
    unestablished_count: int
    active_count: int
    active_cost_fen: int
    active_accumulated_fen: int | None
    active_net_fen: int | None
    pending_count: int
    pending_cost_fen: int
    exited_count: int
    month_acquired_count: int
    month_acquired_fen: int
    month_cost_adjustment_fen: int
    month_activated_count: int
    month_exited_count: int
    month_charge_fen: int | None


@dataclass(frozen=True)
class ProjectCostTotals:
    count: int
    cost_fen: int
    consumed_fen: int
    remaining_fen: int


@dataclass(frozen=True)
class AssetLedgerAmounts:
    """Only asset balances; their issue_indexes address this local issues tuple."""

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


@dataclass(frozen=True)
class AssetActivity:
    """One full asset projection; card_issues never share the ledger's issue pool."""

    version: str
    scope: LedgerScope
    cards: tuple[AssetCard, ...]
    project_costs: tuple[AssetProjectCost, ...]
    fixed: AssetTypeSummary
    intangible: AssetTypeSummary
    project_totals: ProjectCostTotals
    ledger: AssetLedgerAmounts
    card_issues: tuple[FactIssue, ...]


@dataclass(frozen=True)
class ProjectCostPage:
    page: ReadPage[AssetProjectCost]
    full_totals: ProjectCostTotals
    filtered_totals: ProjectCostTotals
