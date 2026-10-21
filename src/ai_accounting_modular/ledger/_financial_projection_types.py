"""Opaque persistence and self-contained outputs of the Ledger financial model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ai_accounting_modular.contracts.public import AmountCell, FactIssue, LedgerScope

FINANCIAL_PROJECTION_FORMAT = 2

type FinancialBlockKey = tuple[str, str]
type FinancialBlockWrite = tuple[str, str, str]


class FinancialProjectionReader(Protocol):
    """One read snapshot; block names and JSON contents belong to Ledger."""

    @property
    def header_json(self) -> str | None: ...

    def get_blocks(self, keys: tuple[FinancialBlockKey, ...]) -> tuple[str | None, ...]:
        """Return exact opaque rows in input order, with None for absent rows."""
        ...


@dataclass(frozen=True)
class FinancialProjectionRequest:
    kind: Literal["summary_values", "snapshot"]
    scope: LedgerScope
    key: str
    descriptor_key: FinancialBlockKey


@dataclass(frozen=True)
class FinancialProjectionPlan:
    """Publish every change together with the caller's producer CAS.

    Each write is (collection, key, canonical JSON). The caller stores that text
    directly; it does not decode accounting contents or maintain node references.
    """

    header_json: str
    header_digest: str
    writes: tuple[FinancialBlockWrite, ...]
    deletes: tuple[FinancialBlockKey, ...]


@dataclass(frozen=True)
class FinancialSummaryValues:
    """Financial amounts without the complete Sources promised by FinancialSummary."""

    version: str
    scope: LedgerScope
    values: dict[str, AmountCell]
    issues: tuple[FactIssue, ...]


@dataclass(frozen=True)
class FinancialProjectionArtifact:
    """A complete consumer value bound only to its own exact request descriptor."""

    request_key: str
    descriptor_key: FinancialBlockKey
    descriptor_digest: str
    version: str
    value: FinancialSummaryValues | dict
