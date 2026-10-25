"""Current, bounded financial review selections; never a retained read session."""

from dataclasses import dataclass, field
from typing import Literal

from ai_accounting_modular.contracts.public import (
    BusinessRef,
    ResultRef,
    ResultSourceRef,
    SourceLocation,
)

from ._types import ObligationDirectoryItem, ObligationDirectorySource, SettlementEventQuery


@dataclass(frozen=True)
class ReviewSourceMetadata:
    reference: ResultRef
    source: ResultSourceRef
    source_locations: tuple[SourceLocation, ...]


@dataclass(frozen=True)
class ReviewObligationQuery:
    cutoff_period: str
    business_ids: tuple[str, ...] = ()
    references: tuple[BusinessRef, ...] = ()
    categories: tuple[str, ...] = ()
    party_ids: tuple[str, ...] = ()
    direction: Literal["receivable", "payable"] | None = None
    period_from: str | None = None
    period_to: str | None = None
    status: Literal["unsettled", "settled", "all"] = "all"


@dataclass(frozen=True)
class ReviewObligationSelection:
    query: tuple[ReviewObligationQuery, ...]
    version: str
    items: tuple[ObligationDirectoryItem, ...]
    sources: tuple[ObligationDirectorySource, ...]
    _scope_versions: tuple[str, ...] = field(repr=False)
    _snapshot: tuple[str, str, int] = field(repr=False)
    settlements: "ReviewSettlementSelection | None" = None

    @property
    def total_count(self):
        return len(self.items)

    @property
    def references(self):
        return tuple(item.reference for item in self.items)

    @property
    def cutoff_period(self):
        return self.query[0].cutoff_period


@dataclass(frozen=True)
class ReviewMovementQuery:
    period_from: str
    period_to: str
    account_id: str | None = None
    movement_ids: tuple[str, ...] = ()
    business_ids: tuple[str, ...] = ()
    direction: Literal["inflow", "outflow"] | None = None


@dataclass(frozen=True)
class ReviewMovementHeader:
    reference: BusinessRef
    source: ResultSourceRef
    result_reference: ResultRef
    movement_id: str
    account_id: str
    actual_on: str
    direction: Literal["inflow", "outflow"]
    amount_fen: int
    party_id: str | None
    transfer_group_id: str | None
    platform_row_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewMovementSelection:
    query: tuple[ReviewMovementQuery, ...]
    version: str
    items: tuple[ReviewMovementHeader, ...]
    _scope_versions: tuple[str, ...] = field(repr=False)
    _snapshot: tuple[str, str, int] = field(repr=False)

    @property
    def total_count(self):
        return len(self.items)

    @property
    def movement_ids(self):
        return tuple(item.movement_id for item in self.items)

    @property
    def references(self):
        return tuple(item.reference for item in self.items)

    @property
    def period_from(self):
        return min(query.period_from for query in self.query)

    @property
    def period_to(self):
        return max(query.period_to for query in self.query)


@dataclass(frozen=True)
class ReviewSettlementHeader:
    source: ResultSourceRef
    result_reference: ResultRef
    resource_reference: BusinessRef
    consumer_reference: BusinessRef
    event_id: str
    component: str
    period: str
    actual_on: str | None
    movement_id: str | None
    target_reference: BusinessRef | None = None


@dataclass(frozen=True)
class ReviewSettlementSelection:
    query: tuple[SettlementEventQuery, ...]
    version: str
    items: tuple[ReviewSettlementHeader, ...]
    _rows: tuple[dict, ...] = field(repr=False)
    _snapshot: tuple[str, str, int] = field(repr=False)

    @property
    def total_count(self):
        return len(self.items)

    @property
    def source_refs(self):
        return tuple(
            {"business_id": business_id, "slot": slot}
            for business_id, slot in sorted(
                {
                    (item.resource_reference["business_id"], item.resource_reference["slot"])
                    for item in self.items
                }
            )
        )

    @property
    def movement_ids(self):
        return tuple(sorted({item.movement_id for item in self.items if item.movement_id}))

    @property
    def consumer_refs(self):
        return tuple(
            {"business_id": business_id, "slot": slot}
            for business_id, slot in sorted(
                {
                    (item.consumer_reference["business_id"], item.consumer_reference["slot"])
                    for item in self.items
                }
            )
        )
