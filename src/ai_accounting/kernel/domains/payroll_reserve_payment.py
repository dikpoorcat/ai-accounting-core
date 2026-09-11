"""A whole gross wage bank batch whose confirmed return is expensed in the same month.

Bank rows identify the complete batch. Net obligation allocations do not claim a
person-to-bank-row mapping or a person's observed tax withholding/return amount.
"""

from typing import ClassVar, Literal

from pydantic import Field, StrictBool, model_validator

from ..contracts import KernelError, Line, NeedsInformation, Outcome, Read
from ..types import ActualDate, PositiveFen, YearMonth, sum_fen
from .managed_reserve import MANAGEMENT_ACCOUNT, scope_result, source_reads
from .transactions import (
    Allocation,
    Identifier,
    Payment,
    _source_obligation,
    _through_month,
    calculate_payment,
)


class PayrollNetAllocation(Allocation):
    source_kind: Literal["payroll", "payroll_bounded"]
    obligation: Literal["net"] = "net"
    recipient_id: Identifier


class PayrollReservePayment(Payment):
    kind: ClassVar[str] = "payroll_reserve_payment"
    material_category: ClassVar[str] = "bank"
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.amount_fen": "result.amount_fen",
        "fact.reserve_return_fen": "result.managed_reserve_cost_fen",
        "result.reserve_return_fen": "result.managed_reserve_cost_fen",
    }
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "bank_account_id",
        "amount_fen",
        "allocations",
        "scope_id",
        "platform_account_id",
        "reserve_return_fen",
        "return_period",
        "actual_return_date",
        "return_confirmed",
        "complete_group_confirmed",
    )
    direction: Literal["outflow"] = "outflow"
    payment_method: Literal["bank_batch"] = "bank_batch"
    counterparty_id: Literal["payroll-group"] = "payroll-group"
    allocations: tuple[PayrollNetAllocation, ...] = Field(min_length=1)
    scope_id: Identifier
    platform_account_id: Identifier
    reserve_return_fen: PositiveFen
    return_period: YearMonth | None
    actual_return_date: ActualDate | None = None
    return_confirmed: StrictBool | None
    complete_group_confirmed: StrictBool | None

    @model_validator(mode="after")
    def validate_funds(self):
        # Override only Payment's complete-allocation equation: the remainder is
        # a confirmed expense, never a synthetic payable or extra bank movement.
        if self.actual_date.period != self.period:
            raise ValueError("payment posting month must equal actual funds month")
        if (
            sum_fen((*[a.amount_fen for a in self.allocations], self.reserve_return_fen))
            != self.amount_fen
        ):
            raise ValueError(
                "net allocations and the confirmed return must equal actual gross funds"
            )
        if len({a.scope for a in self.allocations}) != len(self.allocations):
            raise ValueError("duplicate wage obligation")
        if len({a.recipient_id for a in self.allocations}) != len(self.allocations):
            raise ValueError("each person has exactly one wage source in this complete batch")
        return self

    def reads(self):
        return tuple(
            dict.fromkeys(
                (
                    *super().reads(),
                    *source_reads("managed_reserve_scope", self.scope_id),
                    *(Read("fact", a.source_kind, "@" + a.source_id) for a in self.allocations),
                )
            )
        )

    def validate_material_amount(
        self, amount_field, amount_fen, *, source_amounts, source_directions=()
    ):
        if amount_field not in {"fact.amount_fen", "result.amount_fen"}:
            # A bank original proves the whole exit; its partial expense meaning
            # requires the separate owner's return/boundary confirmation.
            if any(d in {"inflow", "outflow", "signed_net"} for d in source_directions):
                raise KernelError(
                    "reserve_return_material_basis",
                    "返池费用须用返款确认依据，不能把银行原行当返池原行",
                )
            return
        directions = source_directions or (None,) * len(source_amounts)
        if (
            not source_amounts
            or amount_fen == 0
            or any(
                original == 0
                or (original > 0) != (amount_fen > 0)
                or not (
                    (direction == "outflow" and original > 0)
                    or (direction in {None, "signed_net"} and original < 0)
                )
                for original, direction in zip(source_amounts, directions, strict=True)
            )
        ):
            raise KernelError(
                "material_funds_direction_mismatch",
                "工资毛额须对应完整银行原支出，保留原金额和列方向",
            )


def calculate_payroll_reserve_payment(version, context):
    fact = version.fact
    if not version.evidence or fact.complete_group_confirmed is not True:
        raise NeedsInformation("complete_group_confirmed", "需要完整工资组确已按毛额支付的原始依据")
    if fact.return_confirmed is not True:
        raise NeedsInformation("return_confirmed", "需要确认差额已经返入明确采用的备用金范围")
    if fact.return_period != fact.period:
        raise NeedsInformation(
            "return_period", "本处理仅支持已明确与银行付款同月发生的返款，不推定未知月份"
        )
    if fact.actual_return_date is not None and (
        fact.actual_return_date.period != fact.return_period
        or fact.actual_return_date < fact.actual_date
    ):
        raise KernelError("reserve_return_date", "已知返款实际日须在银行付款之后且属于同月")
    scope = scope_result(context, fact.scope_id, fact.period)
    if fact.platform_account_id not in scope.values["platform_account_ids"] or not any(
        x["source_kind"] == fact.kind
        and x["source_id"] == version.subject_id
        and x["amount_fen"] == fact.reserve_return_fen
        for x in scope.values["cost_sources"]
    ):
        raise NeedsInformation("scope_id", "返款金额、平台及本完整工资组须由明确费用化范围逐项采用")
    sources = []
    for allocation in fact.allocations:
        source, item = _source_obligation(context, allocation)
        current = context.one(allocation.source_kind, "@" + allocation.source_id)
        if source.fact_id != current.id:
            raise NeedsInformation(
                "allocations.source_id",
                "工资来源当前确认版本尚未正式处理",
                sources=(allocation.source_id,),
            )
        if item.get("category") != "payable" or item["amount_fen"] != allocation.amount_fen:
            raise NeedsInformation(
                "allocations.amount_fen",
                "本处理须完整核销原净薪义务，不支持部分、超付或已变动的工资组",
            )
        if any(
            claim.key == allocation.scope
            for peer in context.select(_through_month("fact", "*", allocation.scope, fact.period))
            if peer.subject_id != version.subject_id
            for claim in peer.fact.claims()
        ):
            raise NeedsInformation(
                "allocations",
                "完整工资组已有其他付款或核销，不得重复结清",
                sources=(allocation.source_id,),
            )
        sources.append(source)
    if (
        len({source.period for source in sources}) != 1
        or sum_fen(source.values["gross_fen"] for source in sources) != fact.amount_fen
    ):
        raise NeedsInformation(
            "allocations", "同一工资月份的完整已发布毛额合计须等于本次真实银行全额"
        )
    # Keep the existing recipient, timing, Claim and prior settlement protection.
    # Its journal lines are the net allocations and its bank balance is already
    # the *whole* actual amount. Add only the separately confirmed expense lines.
    payment = calculate_payment(version, context)
    return Outcome(
        (
            *payment.lines,
            Line(MANAGEMENT_ACCOUNT, debit=fact.reserve_return_fen),
            Line("1002", credit=fact.reserve_return_fen, cashflow="managed_reserve_outflow"),
        ),
        dict(payment.values)
        | dict(
            payroll_period=str(sources[0].period),
            payroll_source_ids=[source.subject_id for source in sources],
            net_settled_fen=sum_fen(a.amount_fen for a in fact.allocations),
            reserve_return_fen=fact.reserve_return_fen,
            return_period=str(fact.return_period),
            actual_return_date=str(fact.actual_return_date) if fact.actual_return_date else None,
            return_confirmed=True,
            platform_account_id=fact.platform_account_id,
            reserve_scope_id=fact.scope_id,
            managed_reserve_cost_fen=fact.reserve_return_fen,
            accounting_treatment="reserve_expense",
            expense_class="administration",
            allocation_meaning="settlement_of_complete_wage_group_not_person_bank_row_or_return_mapping",
        ),
        payment.balances,
    )


def register(registry):
    registry.register(PayrollReservePayment, calculate_payroll_reserve_payment)
