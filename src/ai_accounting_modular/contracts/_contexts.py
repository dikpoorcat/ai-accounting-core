"""Process-local transaction contracts, never part of the JSON wire format."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ._errors import ContractError
from ._validation import validate_contract
from ._wire_types import (
    AdoptedBasis,
    BusinessRef,
    CurrentObject,
    DependencyRelationKind,
    Fact,
    JsonValue,
    ResultSourceRef,
    ResultTarget,
    SourceLocator,
)


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

    def get_fact(self, business_id: str) -> Fact | None:
        """Use the submission's correct current fact before the persisted fact."""
        ...

    def related_facts(
        self, producer_id: str, relation_kind: DependencyRelationKind
    ) -> tuple[Fact, ...]:
        """Read declared current dependencies with same-unit replacements/withdrawals.

        The application uses current_dependency and its submission overlay;
        finding these facts must not scan arbitrary business JSON bodies.
        """
        ...

    def get_object(self, object_id: str) -> CurrentObject | None:
        """Use the submission's current object before the persisted object."""
        ...

    def get_result(self, business_id: str, result_slot: str) -> EvaluatedSlot | None:
        """Use an evaluated submission slot before a persisted public result."""
        ...

    def resolve(
        self,
        refs: Sequence[BusinessRef],
        kind: Literal["result", "obligation", "cost", "funds"],
    ) -> list[JsonValue]:
        """Resolve public resources with evaluated submission slots taking priority."""
        ...

    def resolve_sources(self, ids: Sequence[str]) -> list[SourceLocator]: ...
