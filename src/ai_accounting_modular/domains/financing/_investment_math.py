"""Exact investment quantities and cumulative cost intervals, without storage."""

import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import checked_fen, parse_fen

from ._common import fail

_QUANTITY = re.compile(r"(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?")


def quantity(value):
    if type(value) is not str or _QUANTITY.fullmatch(value) is None:
        fail("投资数量必须是非负、最多18位小数的规范字符串。", "invalid_input")
    return Decimal(value)


def decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def quantity_total(values):
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        return sum(values, Decimal(0))


def rounded(value):
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        return checked_fen(int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def proportional(cost, used, total):
    if total <= 0 or used < 0 or used > total:
        fail("投资成本分配数量超出该批次的正式数量。")
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        return rounded(Decimal(cost) * used / total)


def reference_key(reference):
    return reference["business_id"], reference["slot"]


@dataclass
class Lot:
    source: dict
    terms_id: str
    product_id: str
    unit: str
    original_quantity: Decimal
    original_cost: int
    historical_cost: int
    acquired_period: str
    acquired_on: str | None = None
    used_quantity: Decimal = Decimal(0)
    used_cost: int = 0
    origin_source: dict | None = None
    tax_buy_basis: int | None = None
    eit_tax_basis: int | None = None
    capitalized_interest: int = 0
    acquisition_sequence: int | None = None
    acquired_coupon_dates: tuple[str, ...] | None = None
    acquisition_origin: str | None = None
    eit_interest_realized_through: str | None = None
    eit_interest_source: dict | None = None

    @property
    def remaining_quantity(self):
        with localcontext() as arithmetic:
            arithmetic.prec = 80
            return self.original_quantity - self.used_quantity

    @property
    def remaining_cost(self):
        return self.original_cost - self.used_cost

    @property
    def remaining_interest(self):
        return self.capitalized_interest - proportional(
            self.capitalized_interest, self.used_quantity, self.original_quantity
        )

    def consume(self, used):
        before_quantity, before_cost = self.remaining_quantity, self.remaining_cost
        if used <= 0 or used > before_quantity:
            fail("投资退出数量超过当前正式持仓，不能用其他批次或未来取得补足。")
        after_used = quantity_total((self.used_quantity, used))
        cost = proportional(self.original_cost, after_used, self.original_quantity) - self.used_cost
        self.used_quantity, self.used_cost = after_used, self.used_cost + cost
        return {
            "type": "investment_lot_use",
            "source": deepcopy(self.source),
            "terms_id": self.terms_id,
            "quantity": decimal_text(used),
            "cost_fen": str(cost),
            "quantity_before": decimal_text(before_quantity),
            "quantity_after": decimal_text(self.remaining_quantity),
            "cost_before_fen": str(before_cost),
            "cost_after_fen": str(self.remaining_cost),
        }

    def wire(self, *, allocation_state=False):
        result = {
            "type": "investment_lot",
            "source": deepcopy(self.source),
            "terms_id": self.terms_id,
            "product_id": self.product_id,
            "quantity": decimal_text(self.remaining_quantity),
            "quantity_unit": self.unit,
            "historical_cost_fen": str(
                self.historical_cost
                - proportional(self.historical_cost, self.used_quantity, self.original_quantity)
            ),
            "carrying_fen": str(self.remaining_cost),
            "acquired_period": self.acquired_period,
        }
        if self.acquired_on is not None:
            result["acquired_on"] = self.acquired_on
        if self.acquisition_sequence is not None:
            result["acquisition_sequence"] = self.acquisition_sequence
        if self.acquired_coupon_dates is not None:
            result["acquired_coupon_dates"] = list(self.acquired_coupon_dates)
        for key in ("acquisition_origin", "eit_interest_realized_through", "eit_interest_source"):
            if (value := getattr(self, key)) is not None:
                result[key] = deepcopy(value)
        if allocation_state:
            result["allocation_state"] = {
                "original_quantity": decimal_text(self.original_quantity),
                "original_cost_fen": str(self.original_cost),
                "original_historical_cost_fen": str(self.historical_cost),
                "consumed_quantity": decimal_text(self.used_quantity),
                "consumed_cost_fen": str(self.used_cost),
                "original_accrued_interest_fen": str(self.capitalized_interest),
            }
        if self.origin_source is not None:
            result["origin_source"] = deepcopy(self.origin_source)
        for key, value in (
            ("tax_buy_basis_fen", self.tax_buy_basis),
            ("eit_tax_basis_fen", self.eit_tax_basis),
        ):
            if value is not None and not self.used_quantity:
                result[key] = str(value)
        if self.unit == "nominal_fen":
            result["debt_face_fen"] = decimal_text(self.remaining_quantity)
            result["accrued_interest_fen"] = str(self.remaining_interest)
        return result

    @classmethod
    def from_wire(cls, value):
        state = value.get("allocation_state", {})
        result = cls(
            deepcopy(value["source"]),
            value["terms_id"],
            value["product_id"],
            value["quantity_unit"],
            quantity(state.get("original_quantity", value["quantity"])),
            parse_fen(state.get("original_cost_fen", value["carrying_fen"])),
            parse_fen(state.get("original_historical_cost_fen", value["historical_cost_fen"])),
            value["acquired_period"],
            value.get("acquired_on"),
            origin_source=deepcopy(value.get("origin_source")),
            used_quantity=quantity(state.get("consumed_quantity", "0")),
            used_cost=parse_fen(state.get("consumed_cost_fen", "0")),
            tax_buy_basis=parse_fen(value["tax_buy_basis_fen"])
            if "tax_buy_basis_fen" in value
            else None,
            eit_tax_basis=parse_fen(value["eit_tax_basis_fen"])
            if "eit_tax_basis_fen" in value
            else None,
            capitalized_interest=parse_fen(
                state.get(
                    "original_accrued_interest_fen",
                    value.get(
                        "accrued_interest_fen",
                        value.get("opening_debt", {}).get("accrued_interest_fen", "0"),
                    ),
                )
            ),
            acquisition_sequence=value.get("acquisition_sequence"),
            acquired_coupon_dates=tuple(value["acquired_coupon_dates"])
            if "acquired_coupon_dates" in value
            else None,
            acquisition_origin=value.get("acquisition_origin"),
            eit_interest_realized_through=value.get("eit_interest_realized_through"),
            eit_interest_source=deepcopy(value.get("eit_interest_source")),
        )
        if state and (
            result.original_quantity <= 0
            or result.remaining_quantity != quantity(value["quantity"])
            or result.remaining_cost != parse_fen(value["carrying_fen"])
            or proportional(result.original_cost, result.used_quantity, result.original_quantity)
            != result.used_cost
            or result.historical_cost
            - proportional(result.historical_cost, result.used_quantity, result.original_quantity)
            != parse_fen(value["historical_cost_fen"])
            or result.remaining_interest != parse_fen(value.get("accrued_interest_fen", "0"))
        ):
            fail("正式投资持仓未延续其原累计分配区间。", "storage_corrupt")
        return result


@dataclass
class Event:
    business_id: str
    quantity: Decimal = Decimal(0)
    amount: int = 0
    cost: int = 0
    gain: int = 0
    book_change: int = 0
    uses: list = field(default_factory=list)
    created: list = field(default_factory=list)
    previous_arrangement: str | None = None
    separate_income_character: str | None = None


def rebase(
    lot, event, *, quantity_after=None, carrying_after=None, capitalized_interest_after=None
):
    """A real change starts a new interval; earlier allocations stay untouched."""
    remaining, cost, interest = lot.remaining_quantity, lot.remaining_cost, lot.remaining_interest
    event.uses.append(lot.consume(remaining))
    successor = Lot(
        {"business_id": event.business_id, "slot": "cost"},
        lot.terms_id,
        lot.product_id,
        lot.unit,
        remaining if quantity_after is None else quantity_after,
        cost if carrying_after is None else carrying_after,
        lot.historical_cost
        - proportional(
            lot.historical_cost,
            quantity_total((lot.used_quantity, remaining.copy_negate())),
            lot.original_quantity,
        ),
        lot.acquired_period,
        lot.acquired_on,
        origin_source=deepcopy(lot.origin_source or lot.source),
        capitalized_interest=interest
        if capitalized_interest_after is None
        else capitalized_interest_after,
        acquisition_sequence=lot.acquisition_sequence,
        acquired_coupon_dates=lot.acquired_coupon_dates,
        acquisition_origin=lot.acquisition_origin,
        eit_interest_realized_through=lot.eit_interest_realized_through,
        eit_interest_source=deepcopy(lot.eit_interest_source),
    )
    if successor.original_quantity <= 0 or successor.original_cost < 0:
        fail("投资实际调整后的数量须大于零，账面成本不得为负。")
    event.created.append(successor.wire())
    return successor
