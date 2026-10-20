"""Process-local transaction contracts, never part of the JSON wire format."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ._errors import ContractError
from ._scalars import checked_fen, parse_fen
from ._validation import validate_contract
from ._wire_types import (
    AdoptedBasis,
    BusinessRef,
    CostConsumption,
    CostLot,
    CurrentObject,
    DependencyRelationKind,
    Fact,
    FundMovement,
    Obligation,
    ObligationReclassification,
    ResultSourceRef,
    ResultTarget,
    Settlement,
    SourceLocator,
)

ResourceKind = Literal["obligation", "cost", "funds"]


@dataclass(frozen=True)
class ResourceCutoff:
    """Economic month, optionally cut at necessary money or nonmoney actual dates."""

    period: str
    on: str | None = None

    def __post_init__(self) -> None:
        validate_contract("core.schema.json", "Month", self.period)
        if self.on is not None:
            validate_contract("core.schema.json", "Date", self.on)
            if self.on[:7] != self.period:
                raise ContractError("invalid_input", "资源截止实际日必须属于截止核算月。")


@dataclass(frozen=True)
class ResourceDefinition:
    """One public resource and the exact result that defines it."""

    kind: ResourceKind
    reference: BusinessRef
    source: ResultSourceRef
    value: Obligation | CostLot | FundMovement

    def __post_init__(self) -> None:
        schemas = {
            "obligation": ("core.schema.json", "Obligation"),
            "cost": ("core.schema.json", "CostLot"),
            "funds": ("business-facts.schema.json", "FundMovement"),
        }
        if type(self.kind) is not str or self.kind not in schemas:
            raise ContractError("invalid_input", "未知的公开资源类型。")
        validate_contract("core.schema.json", "BusinessRef", self.reference)
        validate_contract("business-facts.schema.json", "ResultSourceRef", self.source)
        validate_contract(*schemas[self.kind], self.value)
        resource_slot = self.value["public_slot"] if self.kind == "funds" else self.value["slot"]
        if (
            self.source["business_id"] != self.reference["business_id"]
            or resource_slot != self.reference["slot"]
        ):
            raise ContractError("invalid_input", "资源定义与公开身份及采用来源不一致。")
        if self.kind != "funds" and self.value["business_id"] != self.reference["business_id"]:
            raise ContractError("invalid_input", "资源定义不属于所声明业务。")


@dataclass(frozen=True)
class ResourceUse:
    """A formal use, with the consuming result's exact source identity."""

    kind: Literal["settlement", "reclassification", "cost"]
    source: ResultSourceRef
    value: Settlement | ObligationReclassification | CostConsumption

    def __post_init__(self) -> None:
        schemas = {
            "settlement": ("core.schema.json", "Settlement"),
            "reclassification": ("business-facts.schema.json", "ObligationReclassification"),
            "cost": ("core.schema.json", "CostConsumption"),
        }
        if type(self.kind) is not str or self.kind not in schemas:
            raise ContractError("invalid_input", "未知的资源使用类型。")
        validate_contract(*schemas[self.kind], self.value)
        validate_contract("business-facts.schema.json", "ResultSourceRef", self.source)
        consumer = (
            self.value["owner_business_id"]
            if self.kind == "reclassification"
            else self.value["consumer"]["business_id"]
        )
        if consumer != self.source["business_id"]:
            raise ContractError("invalid_input", "资源使用与消费者采用来源不一致。")


@dataclass(frozen=True)
class ResourceState:
    """A complete candidate quantity; remaining may be negative before validation."""

    definition: ResourceDefinition
    money_settled_fen: int = 0
    offset_settled_fen: int = 0
    agency_settled_fen: int = 0
    transferred_out_fen: int = 0
    excess_reclassified_fen: int = 0
    consumed_fen: int = 0
    remaining_fen: int = 0
    uses: tuple[ResourceUse, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "money_settled_fen",
            "offset_settled_fen",
            "agency_settled_fen",
            "transferred_out_fen",
            "excess_reclassified_fen",
            "consumed_fen",
            "remaining_fen",
        ):
            try:
                amount = checked_fen(getattr(self, name))
            except ValueError as exc:
                raise ContractError(
                    "invalid_input", "候选资源金额必须是有符号64位整数分。"
                ) from exc
            if name != "remaining_fen" and amount < 0:
                raise ContractError("invalid_input", "资源使用累计额不能为负。")


@dataclass(frozen=True)
class ResourceWindow:
    """Certain quantities plus same-month uses whose actual day is unknown.

    Bounds are mathematical comparisons, not additional posted money amounts.
    An uncertain use is neither silently included nor assumed not to exist.
    """

    certain: ResourceState
    uncertain_uses: tuple[ResourceUse, ...] = ()

    @property
    def _possible_excess(self) -> int:
        return sum(
            parse_fen(use.value["amount_fen"])
            for use in self.uncertain_uses
            if use.kind == "reclassification" and use.value["mode"] == "reclassify_excess"
        )

    @property
    def remaining_min_fen(self) -> int:
        subtracted = sum(parse_fen(use.value["amount_fen"]) for use in self.uncertain_uses)
        return self.certain.remaining_fen - (subtracted - self._possible_excess)

    @property
    def remaining_max_fen(self) -> int:
        return self.certain.remaining_fen + self._possible_excess

    @property
    def excess_reclassified_max_fen(self) -> int:
        return self.certain.excess_reclassified_fen + self._possible_excess


@runtime_checkable
class ReadContext(Protocol):
    @property
    def company_id(self) -> str: ...

    @property
    def database_id(self) -> str: ...

    @property
    def head(self) -> int: ...

    @property
    def view(self) -> Literal["current", "as_posted"]: ...

    @property
    def as_of(self) -> str | None: ...

    def repository(self, owner: str) -> Any:
        """Get the caller owner's repository on this same transaction."""
        ...


@runtime_checkable
class UnitOfWork(ReadContext, Protocol):
    @property
    def next_head(self) -> int: ...

    def check_before_commit(self, callback: Callable[[], None]) -> None: ...


@dataclass(frozen=True)
class EvaluatedSlot:
    """An evaluated business outcome, without claiming stored vouchers exist."""

    source: ResultSourceRef
    target: ResultTarget
    basis: AdoptedBasis

    def __post_init__(self) -> None:
        for definition, value in (
            ("ResultSourceRef", self.source),
            ("ResultTarget", self.target),
            ("AdoptedBasis", self.basis),
        ):
            validate_contract("business-facts.schema.json", definition, value)
        if (
            self.source["result_slot"] != self.target["result_slot"]
            or self.target["result_slot"] != self.basis["result_slot"]
            or self.target["kind"] != self.basis["kind"]
        ):
            raise ContractError(
                "invalid_input", "求值结果的来源、目标和依据必须属于同一结果槽及类型"
            )


@runtime_checkable
class EvaluationContext(Protocol):
    """Domain inputs from one read unit and the same atomic submission."""

    @property
    def read_context(self) -> ReadContext: ...

    @property
    def evaluation_period_to(self) -> str:
        """Latest explicitly reached accounting month, never an invented today."""
        ...

    def get_fact(self, business_id: str) -> Fact | None:
        """Use the submission's correct current fact before the persisted fact."""
        ...

    def related_facts(
        self,
        producer_id: str,
        relation_kind: DependencyRelationKind,
        *,
        producer_kind: Literal["business", "object"] = "business",
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> tuple[Fact, ...]:
        """Read declared current dependencies with same-unit replacements/withdrawals.

        The application uses current_dependency and its submission overlay;
        finding these facts must not scan arbitrary business JSON bodies.
        """
        ...

    def get_object(self, object_id: str) -> CurrentObject | None:
        """Use the submission's current object before the persisted object."""
        ...

    def related_facts_many(
        self,
        producer_ids: Sequence[str],
        relation_kind: DependencyRelationKind,
        *,
        producer_kind: Literal["business", "object"] = "business",
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> tuple[Fact, ...]:
        """Unique current facts related to at most 500 explicit producer identities."""
        ...

    def get_result(self, business_id: str, result_slot: str) -> EvaluatedSlot | None:
        """Use an evaluated submission slot before a persisted public result."""
        ...

    def facts_in_scope(
        self, kinds: Sequence[str], period_from: str, period_to: str
    ) -> tuple[Fact, ...]: ...

    def movement_id(self, business_id: str, public_slot: str) -> str: ...

    def actual_settled_fen(self, source: BusinessRef, cutoff: ResourceCutoff) -> int:
        """Current real-money allocations through this economic cutoff, excluding nonmoney."""
        ...

    def fund_sources(
        self, fact: Fact, fund_movements: Sequence[FundMovement]
    ) -> tuple[SourceLocator, ...]: ...

    def resolve_sources(self, ids: Sequence[str]) -> list[SourceLocator]: ...

    def resource_definitions(
        self, refs: Sequence[BusinessRef], kind: ResourceKind
    ) -> tuple[ResourceDefinition, ...]: ...

    def resource_states(
        self,
        refs: Sequence[BusinessRef],
        kind: ResourceKind,
        cutoff: ResourceCutoff,
        *,
        excluding_consumer: tuple[str, str] | None = None,
    ) -> tuple[ResourceState, ...]: ...

    def resource_windows(
        self,
        refs: Sequence[BusinessRef],
        kind: ResourceKind,
        cutoff: ResourceCutoff,
        *,
        excluding_consumer: tuple[str, str] | None = None,
    ) -> tuple[ResourceWindow, ...]:
        """A complete candidate's quantities with explicit bounds for unknown use days."""
        ...

    def fund_movements(
        self, account_id: str, period_from: str, period_to: str
    ) -> tuple[ResourceDefinition, ...]:
        """Complete actual funds in the explicit account/month range."""
        ...
