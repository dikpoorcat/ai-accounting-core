"""Actual managed-reserve expenses and refunds through company funds accounts."""

from typing import ClassVar

from pydantic import Field, model_validator

from ..contracts import (
    BalanceEffect,
    Fact,
    KernelError,
    Line,
    NeedsInformation,
    Outcome,
)
from ..types import ActualDate, PositiveFen
from .money import FUNDS_ACCOUNT_BY_BALANCE_CATEGORY
from .platforms import (
    movement_claims,
    movement_reads,
    movement_scope,
    platform_scopes,
    validate_actual_money,
)
from .transactions import Identifier, bank_scopes

MANAGEMENT_ACCOUNT = "5602"
_FUNDS = {
    "bank": "bank_account_id",
    "cash": "cash_account_id",
    "platform": "platform_account_id",
}


class _ManagedReserveMoney(Fact):
    """One actual company-side funds movement, without a reserve balance or capacity."""

    material_category: ClassVar[str] = "transactions"
    actual_payment: ClassVar[bool] = True
    immutable: ClassVar[bool] = True
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "bank_account_id",
        "cash_account_id",
        "platform_account_id",
        "movement_ids",
        "amount_fen",
    )
    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.amount_fen": "result.amount_fen"}

    actual_date: ActualDate = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "actual_managed_reserve_funds_date",
                "allowed_precision": ["day"],
                "reusable_sources": ["bank_statement", "cash_record", "platform_movement"],
            }
        }
    )
    bank_account_id: Identifier | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "actual_company_bank_account",
                "reusable_sources": ["bank_statement", "company_funds_account"],
            }
        },
    )
    cash_account_id: Identifier | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "actual_company_cash_account",
                "reusable_sources": ["cash_record", "company_funds_account"],
            }
        },
    )
    platform_account_id: Identifier | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "actual_company_platform_account",
                "reusable_sources": ["platform_movement", "company_funds_account"],
            }
        },
    )
    movement_ids: tuple[Identifier, ...] = Field(
        default=(),
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "consumed_original_platform_movements",
                "reusable_sources": ["platform_movement"],
            }
        },
    )
    counterparty_id: Identifier | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "known_managed_reserve_counterparty",
                "reusable_sources": ["original_document", "owner_confirmation"],
                "constraint": "未知时省略，不虚构交易对象",
            }
        },
    )
    amount_fen: PositiveFen = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "actual_managed_reserve_funds_amount",
                "reusable_sources": ["bank_statement", "cash_record", "platform_movement"],
            }
        }
    )

    @model_validator(mode="after")
    def actual_company_money(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual managed-reserve funds date must belong to posting month")
        populated = [
            category for category, field in _FUNDS.items() if getattr(self, field) is not None
        ]
        if len(populated) != 1:
            raise ValueError("exactly one company funds account is required")
        if populated[0] == "platform":
            if not self.movement_ids:
                raise ValueError("platform managed-reserve money requires original movements")
            if len(set(self.movement_ids)) != len(self.movement_ids):
                raise ValueError("duplicate platform source movement")
        elif self.movement_ids:
            raise ValueError("bank or cash managed-reserve money cannot use platform movements")
        return self

    @property
    def funds_category(self):
        return next(
            category for category, field in _FUNDS.items() if getattr(self, field) is not None
        )

    @property
    def direction(self):
        return "outflow" if self.kind == "managed_reserve_expense" else "inflow"

    @property
    def funds_account_id(self):
        return getattr(self, _FUNDS[self.funds_category])

    @property
    def funds_account(self):
        return FUNDS_ACCOUNT_BY_BALANCE_CATEGORY[self.funds_category]

    def scopes(self):
        if self.funds_category == "bank":
            funds = bank_scopes(self.period, self.funds_account_id)
        elif self.funds_category == "cash":
            funds = (
                str(self.period),
                f"cash:{self.funds_account_id}",
                f"cash:{self.funds_account_id}:{self.period}",
            )
        else:
            funds = platform_scopes(self.period, self.funds_account_id)
        return (*funds, *(movement_scope(source) for source in self.movement_ids))

    def claims(self):
        return movement_claims(self.movement_ids)

    def reads(self):
        return movement_reads(self.movement_ids, self.period)

    def validate_material_amount(
        self, amount_field, amount_fen, *, source_amounts, source_directions=()
    ):
        if amount_field not in {"fact.amount_fen", "result.amount_fen"}:
            return
        expected = "outflow" if self.kind == "managed_reserve_expense" else "inflow"
        directions = source_directions or (None,) * len(source_amounts)
        valid = bool(source_amounts) and len(directions) == len(source_amounts)
        for original, direction in zip(source_amounts, directions, strict=False):
            if direction in {None, "signed_net"}:
                actual = "inflow" if original > 0 else "outflow"
            elif direction in {"inflow", "outflow"}:
                valid = valid and original > 0
                actual = direction
            else:
                valid = False
                actual = None
            valid = (
                valid
                and original != 0
                and actual == expected
                and (original > 0) == (amount_fen > 0)
            )
        if not valid or amount_fen == 0:
            raise KernelError(
                "material_funds_direction_mismatch",
                "备用金收付资料须保留原金额，并与原收支列或净额符号方向一致",
                expected_direction=expected,
            )


class ManagedReserveExpense(_ManagedReserveMoney):
    """An actual transfer from a company funds account into managed reserve."""

    kind: ClassVar[str] = "managed_reserve_expense"


class ManagedReserveRefund(_ManagedReserveMoney):
    """An actual return from managed reserve into a company funds account."""

    kind: ClassVar[str] = "managed_reserve_refund"


def _calculate_managed_reserve(version, context, *, direction):
    fact = version.fact
    if not version.evidence:
        raise NeedsInformation("evidence", "需要实际备用金收付的明确依据")
    if fact.funds_category == "platform":
        validate_actual_money(version, context, direction)
    outgoing = direction == "outflow"
    lines = (
        (
            Line(MANAGEMENT_ACCOUNT, debit=fact.amount_fen),
            Line(fact.funds_account, credit=fact.amount_fen, cashflow="managed_reserve_outflow"),
        )
        if outgoing
        else (
            Line(fact.funds_account, debit=fact.amount_fen, cashflow="other_operating_receipts"),
            Line(MANAGEMENT_ACCOUNT, credit=fact.amount_fen),
        )
    )
    return Outcome(
        lines,
        {
            "actual_date": str(fact.actual_date),
            "direction": direction,
            "amount_fen": fact.amount_fen,
            "funds_category": fact.funds_category,
            f"{fact.funds_category}_account_id": fact.funds_account_id,
            "counterparty_id": fact.counterparty_id,
            "movement_ids": fact.movement_ids,
            "expense_class": "administration",
            "accounting_treatment": ("reserve_expense" if outgoing else "reserve_refund"),
        },
        (
            BalanceEffect(
                fact.funds_account_id,
                -fact.amount_fen if outgoing else fact.amount_fen,
                fact.funds_category,
            ),
        ),
    )


def calculate_expense(version, context):
    return _calculate_managed_reserve(version, context, direction="outflow")


def calculate_refund(version, context):
    return _calculate_managed_reserve(version, context, direction="inflow")


def register(registry):
    registry.register(ManagedReserveExpense, calculate_expense)
    registry.register(ManagedReserveRefund, calculate_refund)
