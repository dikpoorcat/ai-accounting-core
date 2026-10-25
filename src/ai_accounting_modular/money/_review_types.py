"""Current, bounded-scope originals and proven links for accounting review."""

from dataclasses import dataclass, field
from typing import Literal

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadContext,
    ResultRef,
    validate_contract,
)


@dataclass(frozen=True)
class ReviewRowQuery:
    statement_ids: tuple[str, ...] = ()
    row_ids: tuple[str, ...] = ()
    account_id: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    kind: Literal["bank", "platform"] | None = None

    def __post_init__(self):
        for values in (self.statement_ids, self.row_ids):
            if not isinstance(values, tuple) or len(values) > 500:
                raise ContractError("invalid_input", "原行核查身份须为不重复的至多500项元组。")
            for identity in values:
                validate_contract("core.schema.json", "Id", identity)
            if len(values) != len(set(values)):
                raise ContractError("invalid_input", "原行核查身份不能重复。")
        if self.account_id is not None:
            validate_contract("core.schema.json", "Id", self.account_id)
        if (self.period_from is None) != (self.period_to is None):
            raise ContractError("invalid_input", "原行核查期间须同时给出起月和止月。")
        if self.period_from is not None:
            for period in (self.period_from, self.period_to):
                validate_contract("core.schema.json", "Month", period)
            if self.period_from > self.period_to:
                raise ContractError("invalid_input", "原行核查起月不能晚于止月。")
        if not (self.statement_ids or self.row_ids) and (
            self.account_id is None or self.period_from is None
        ):
            raise ContractError("invalid_input", "原行核查须指定身份，或账户和完整实际期间。")
        if self.kind is not None and self.kind not in ("bank", "platform"):
            raise ContractError("invalid_input", "原行核查类型只能是银行或平台。")


@dataclass(frozen=True)
class ReviewRowIdentity:
    business_id: str
    row_id: str
    account_id: str
    kind: Literal["bank", "platform"]
    actual_on: str
    direction: Literal["inflow", "outflow"]
    amount_fen: int
    adopted: ResultRef


@dataclass(frozen=True)
class ReviewRowLink:
    statement_business_id: str
    row_id: str
    relation: Literal["reconciliation", "platform_consumption"]
    movement_ids: tuple[str, ...]
    linked_business_ids: tuple[str, ...]
    reconciliation: ResultRef | None = None
    group_id: str | None = None


@dataclass(frozen=True)
class ReviewRowSelection:
    query: ReviewRowQuery
    version: str
    identities: tuple[ReviewRowIdentity, ...]
    links: tuple[ReviewRowLink, ...]
    statement_ids: tuple[str, ...]
    _read: ReadContext = field(repr=False, compare=False)

    @property
    def total_count(self):
        return len(self.identities)

    @property
    def row_ids(self):
        return tuple(item.row_id for item in self.identities)
