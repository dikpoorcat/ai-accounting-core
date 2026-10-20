"""Documented fund costs, confirmations and actual cash remain distinct."""

from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import AmountCell, FactIssue, ResultRef, ResultSourceRef


@dataclass(frozen=True)
class MoneyFundProduct:
    fund_id: str
    name: str
    opening_cost: AmountCell
    closing_cost: AmountCell
    subscription_cost_fen: int
    redemption_cost_fen: int
    investment_income_fen: int


@dataclass(frozen=True)
class MoneyFundEvent:
    event_id: str
    business_id: str
    fund_id: str
    kind: Literal[
        "purchase_confirmation", "redemption_confirmation", "purchase_payment", "redemption_receipt"
    ]
    period: str
    business_period: str
    actual_on: str | None
    source: ResultSourceRef
    adopted: ResultRef
    correction: bool
    cost_fen: int | None
    net_proceeds_fen: int | None
    gain_loss_fen: int | None
    settlement_fen: int | None
    movement_id: str | None


@dataclass(frozen=True)
class MoneyFundActivity:
    """One complete month projection, ready for a reading-owned item index."""

    version: str
    opening_cost: AmountCell
    closing_cost: AmountCell
    subscription_cost_fen: int
    redemption_cost_fen: int
    investment_income_fen: int
    actual_payments_fen: int
    actual_receipts_fen: int
    event_count: int
    products: tuple[MoneyFundProduct, ...]
    events: tuple[MoneyFundEvent, ...]
    issues: tuple[FactIssue, ...]
