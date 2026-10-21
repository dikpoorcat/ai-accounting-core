"""Money-owned read values; formal account balances remain ledger-owned."""

from dataclasses import dataclass
from typing import Literal, TypedDict

from ai_accounting_modular.contracts.public import (
    BusinessRef,
    ContractError,
    FundMovement,
    ReadInputs,
    ResultRef,
    ResultSourceRef,
    SourceLocation,
    validate_contract,
)


@dataclass(frozen=True)
class MovementQuery:
    account_id: str | None = None

    def __post_init__(self):
        if self.account_id is not None:
            validate_contract("core.schema.json", "Id", self.account_id)


@dataclass(frozen=True)
class StatementQuery:
    account_id: str | None = None
    kind: Literal["bank", "platform"] | None = None

    def __post_init__(self):
        if self.account_id is not None:
            validate_contract("core.schema.json", "Id", self.account_id)
        if self.kind is not None and (
            not isinstance(self.kind, str) or self.kind not in {"bank", "platform"}
        ):
            raise ContractError("invalid_input", "原行类型只能是银行或平台。")


@dataclass(frozen=True)
class SettlementPart:
    source: BusinessRef
    amount_fen: int
    party_id: str | None


class MovementView(TypedDict):
    reference: BusinessRef
    source: ResultSourceRef
    movement: FundMovement
    allocations: tuple[SettlementPart, ...]


@dataclass(frozen=True)
class OriginalRowView:
    business_id: str
    adopted: ResultRef
    row_id: str
    account_id: str
    kind: Literal["bank", "platform"]
    actual_on: str
    direction: Literal["inflow", "outflow"]
    amount_fen: int
    reference: str | None
    counterparty_text: str | None
    memo: str | None
    source_location: SourceLocation | None
    match_group_id: str | None


@dataclass(frozen=True)
class ReconciliationGroup:
    group_id: str
    adopted: ResultRef
    statement_row_ids: tuple[str, ...]
    movement_ids: tuple[str, ...]


@dataclass(frozen=True)
class StatementActivity:
    count: int
    row_count: int
    inflow_fen: int | None
    outflow_fen: int | None
    last_activity_date: str | None
    coverage: Literal["missing", "partial", "complete", "not_applicable"]


@dataclass(frozen=True)
class AccountActivity:
    account_id: str
    kind: Literal["bank_account", "cash_account", "platform_account"]
    name: str
    inflow_fen: int
    outflow_fen: int
    movement_count: int
    last_activity_date: str | None
    statement: StatementActivity
    reconciliation: Literal["pending", "not_applicable", "complete", "attention"]
    reconciliation_difference_fen: int | None


@dataclass(frozen=True)
class FundsActivity:
    """Complete snapshot input for building a month model; this is not an AI page."""

    version: str
    inflow_fen: int
    outflow_fen: int
    internal_transfer_fen: int
    movement_count: int
    accounts: tuple[AccountActivity, ...]
    movements: tuple[MovementView, ...]
    original_rows: tuple[OriginalRowView, ...]
    reconciliation_groups: tuple[ReconciliationGroup, ...]
    inputs: ReadInputs
