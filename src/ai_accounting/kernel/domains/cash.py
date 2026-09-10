"""Explicit physical cash facts reuse the common settlement and publishing path."""

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from ..contracts import BalanceEffect, Claim, Fact, Line, Outcome
from ..types import ActualDate, PositiveFen, sum_fen
from .transactions import (
    Allocation,
    Identifier,
    Payment,
    bank_scopes,
    calculate_funding,
    calculate_payment,
)


class CashPayment(Fact):
    kind: ClassVar[str] = "cash_payment"
    immutable: ClassVar[bool] = True
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "direction",
        "cash_account_id",
        "counterparty_id",
        "amount_fen",
    )
    funds_account: ClassVar[str] = "1001"
    funds_category: ClassVar[str] = "cash"
    actual_payment: ClassVar[bool] = True
    actual_date: ActualDate
    direction: Literal["inflow", "outflow"]
    cash_account_id: Identifier
    counterparty_id: Identifier
    amount_fen: PositiveFen
    payment_method: Literal["individual"] = "individual"
    allocations: tuple[Allocation, ...] = Field(min_length=1)

    @property
    def funds_account_id(self):
        return self.cash_account_id

    @model_validator(mode="after")
    def actual_cash(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual cash date must belong to posting month")
        if sum_fen(item.amount_fen for item in self.allocations) != self.amount_fen:
            raise ValueError("allocations must exactly account for actual cash")
        if len({item.scope for item in self.allocations}) != len(self.allocations):
            raise ValueError("duplicate obligation allocation")
        return self

    def scopes(self):
        return (
            str(self.period),
            f"cash:{self.cash_account_id}",
            f"cash:{self.cash_account_id}:{self.period}",
            *Payment.business_scopes(self),
        )

    def claims(self):
        return tuple(Claim(item.scope, item.amount_fen) for item in self.allocations)

    def reads(self):
        # The pure settlement's dependencies are the same business obligations,
        # including explicit tax-point transitions, regardless of cash channel.
        return Payment.reads(self)


class CashFunding(Fact):
    kind: ClassVar[str] = "cash_funding"
    immutable: ClassVar[bool] = True
    funds_account: ClassVar[str] = "1001"
    funds_category: ClassVar[str] = "cash"
    owner_id: Identifier
    amount_fen: PositiveFen
    funding_kind: Literal["loan", "capital"]
    actual_date: ActualDate
    cash_account_id: Identifier

    @property
    def funds_account_id(self):
        return self.cash_account_id

    @model_validator(mode="after")
    def actual_cash(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual cash date must belong to posting month")
        return self

    def scopes(self):
        return (
            str(self.period),
            f"cash:{self.cash_account_id}",
            f"cash:{self.cash_account_id}:{self.period}",
        )


class CashBankTransfer(Fact):
    """One actual withdrawal/deposit moves funds between two controlled accounts."""

    kind: ClassVar[str] = "cash_bank_transfer"
    immutable: ClassVar[bool] = True
    actual_date: ActualDate
    direction: Literal["withdrawal", "deposit"]
    bank_account_id: Identifier
    cash_account_id: Identifier
    amount_fen: PositiveFen

    @model_validator(mode="after")
    def actual_cash(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual cash/bank transfer date must belong to posting month")
        return self

    def scopes(self):
        return (
            *bank_scopes(self.period, self.bank_account_id),
            f"cash:{self.cash_account_id}",
            f"cash:{self.cash_account_id}:{self.period}",
        )


def calculate_cash_bank_transfer(version, context):
    fact: CashBankTransfer = version.fact
    bank_amount = fact.amount_fen if fact.direction == "deposit" else -fact.amount_fen
    debit, credit = ("1002", "1001") if bank_amount > 0 else ("1001", "1002")
    return Outcome(
        (Line(debit, debit=fact.amount_fen), Line(credit, credit=fact.amount_fen)),
        {
            "actual_date": str(fact.actual_date),
            "amount_fen": fact.amount_fen,
            "direction": fact.direction,
            "bank_account_id": fact.bank_account_id,
            "cash_account_id": fact.cash_account_id,
        },
        (
            BalanceEffect(fact.bank_account_id, bank_amount, "bank"),
            BalanceEffect(fact.cash_account_id, -bank_amount, "cash"),
        ),
    )


def register(registry):
    registry.register(CashPayment, calculate_payment)
    registry.register(CashFunding, calculate_funding)
    registry.register(CashBankTransfer, calculate_cash_bank_transfer)
