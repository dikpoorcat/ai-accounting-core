"""Process-local read coordination; no page fields enter business owners."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ai_accounting_modular.contracts.public import (
    BusinessFindResult,
    BusinessGet,
    BusinessGetResult,
    BusinessQuery,
    ContractError,
    ReadContext,
    validate_contract,
)
from ai_accounting_modular.ledger.public import FormalChange


class WorkScopeView(Protocol):
    @property
    def working_period_to(self) -> str: ...

    @property
    def has_formal_results(self) -> bool: ...

    @property
    def closed_through(self) -> str | None: ...


class BusinessReader(Protocol):
    def get_businesses(
        self, query: BusinessGet, read_context: ReadContext
    ) -> BusinessGetResult: ...

    def find_businesses(
        self, query: BusinessQuery, read_context: ReadContext
    ) -> BusinessFindResult: ...

    def work_scope(self, read_context: ReadContext) -> WorkScopeView: ...


@dataclass(frozen=True)
class MonthScope:
    """An omitted month selects the company's actual default inside the read snapshot."""

    company_id: str
    period: str | None = None
    expected_head: str | None = None

    def __post_init__(self):
        validate_contract("core.schema.json", "Id", self.company_id)
        if self.period is not None:
            validate_contract("core.schema.json", "Month", self.period)
        if self.expected_head is not None:
            validate_contract("core.schema.json", "Digest", self.expected_head)


@dataclass(frozen=True)
class PeriodRange:
    period_from: str
    period_to: str

    def __post_init__(self):
        for period in (self.period_from, self.period_to):
            validate_contract("core.schema.json", "Month", period)
        if self.period_from > self.period_to:
            raise ContractError("invalid_input", "读取影响范围的起月不能晚于末月。")


@dataclass(frozen=True)
class ReadingImpact:
    """Changed public inputs; Ledger alone determines their real posting scope."""

    note_scopes: tuple[str, ...] = ()
    issue_scopes: tuple[str, ...] = ()
    formal_changes: tuple[FormalChange, ...] = ()
    objects: tuple[tuple[str, str], ...] = ()
    facts: tuple[tuple[str, str, str | None], ...] = ()
    closure_ranges: tuple[PeriodRange, ...] = ()
    working_period_changed: bool = False

    def __post_init__(self):
        for value in self.closure_ranges:
            if not isinstance(value, PeriodRange):
                raise ContractError("invalid_input", "读取影响须使用明确月份范围。")
        for scope in self.note_scopes + self.issue_scopes:
            if not isinstance(scope, str) or not 1 <= len(scope) <= 200:
                raise ContractError("invalid_input", "读取影响的说明或待办范围无效。")
        if any(not isinstance(value, FormalChange) for value in self.formal_changes):
            raise TypeError("formal reading changes must come from Ledger adoption")
        for object_id, kind in self.objects:
            validate_contract("core.schema.json", "Id", object_id)
            if not isinstance(kind, str) or not kind:
                raise TypeError("changed object kind is required")
        for business_id, kind, period in self.facts:
            validate_contract("core.schema.json", "Id", business_id)
            if not isinstance(kind, str) or not kind:
                raise TypeError("changed Fact kind is required")
            if period is not None:
                validate_contract("core.schema.json", "Month", period)

    def __bool__(self):
        return bool(
            self.formal_changes
            or self.objects
            or self.facts
            or self.note_scopes
            or self.issue_scopes
            or self.closure_ranges
            or self.working_period_changed
        )


@dataclass(frozen=True)
class ModelDelivery:
    state: str
    scope: Mapping
    version: str | None = None
    payload: Mapping | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SourceDownload:
    receipt: Mapping
    stream: object


@dataclass(frozen=True)
class ReportDownload:
    stream: object
    file_name: str
    sha256: str
    byte_size: int
    company_id: str
    database_id: str
    task_id: str
