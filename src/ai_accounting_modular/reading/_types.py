"""Process-local read coordination; no page fields enter business owners."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ai_accounting_modular.contracts.public import (
    BusinessFindResult,
    BusinessGet,
    BusinessGetResult,
    BusinessQuery,
    ChangeSet,
    ContextRequest,
    ContextResult,
    ContractError,
    ReadContext,
    validate_contract,
)


class BusinessReader(Protocol):
    def get_businesses(
        self, query: BusinessGet, read_context: ReadContext
    ) -> BusinessGetResult: ...

    def find_businesses(
        self, query: BusinessQuery, read_context: ReadContext
    ) -> BusinessFindResult: ...

    def context(self, request: ContextRequest, read_context: ReadContext) -> ContextResult: ...


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
    """Explicit economic/posting impacts and independent display/material changes.

    The caller supplies actual affected ranges. In particular, a later-period
    correction does not turn its frozen original month into a posting change.
    Object display identities are changes.object_ids. This contains no old bodies.
    """

    changes: ChangeSet
    current_ranges: tuple[PeriodRange, ...] = ()
    posted_ranges: tuple[PeriodRange, ...] = ()
    source_ids: tuple[str, ...] = ()
    note_scopes: tuple[str, ...] = ()
    issue_scopes: tuple[str, ...] = ()

    def __post_init__(self):
        validate_contract("core.schema.json", "ChangeSet", self.changes)
        for value in self.current_ranges + self.posted_ranges:
            if not isinstance(value, PeriodRange):
                raise ContractError("invalid_input", "读取影响须使用明确月份范围。")
        for source_id in self.source_ids:
            validate_contract("core.schema.json", "Id", source_id)
        for scope in self.note_scopes + self.issue_scopes:
            if not isinstance(scope, str) or not 1 <= len(scope) <= 200:
                raise ContractError("invalid_input", "读取影响的说明或待办范围无效。")


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
