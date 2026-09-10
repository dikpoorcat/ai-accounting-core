"""Typed transfers of obligations, sharing allocation claims with every settlement."""

from typing import ClassVar, Literal

from pydantic import Field

from ..contracts import (
    BalanceEffect,
    Claim,
    Fact,
    KernelError,
    Line,
    NeedsInformation,
    Outcome,
    Read,
)
from ..types import ActualDate, PositiveFen, YearMonth, sum_fen
from .transactions import Allocation, Identifier, _source_obligation, obligation


def claim_read(scope, period):
    return Read("fact", "*", scope, YearMonth.from_ordinal(period.ordinal + 1))


def allocated(context, scope, excluding, period):
    return sum_fen(
        claim.amount
        for version in context.select(claim_read(scope, period))
        if version.subject_id != excluding
        for claim in version.fact.claims()
        if claim.key == scope
    )


class EmployeeAdvance(Fact):
    kind: ClassVar[str] = "employee_advance"
    immutable: ClassVar[bool] = True
    material_category: ClassVar[str] = "transactions"
    identity_fields: ClassVar[tuple[str, ...]] = ("payer_id", "payer_kind")
    payer_id: Identifier
    payer_kind: Literal["employee", "owner"]
    payment_on_behalf_confirmed: Literal[True]
    actual_creditor_payment_date: ActualDate
    sources: tuple[Allocation, ...] = Field(min_length=1)

    def scopes(self):
        return (
            str(self.period),
            *(
                ("loan-principal",)
                if any(
                    source.source_kind == "loan_drawdown" and source.obligation == "principal"
                    for source in self.sources
                )
                else ()
            ),
        )

    def claims(self):
        return tuple(Claim(source.scope, source.amount_fen) for source in self.sources)

    def reads(self):
        return tuple(
            {
                read
                for source in self.sources
                for read in (
                    Read("calculation", source.source_kind, "@" + source.source_id),
                    claim_read(source.scope, self.period),
                )
            }
        )


def calculate_employee_advance(version, context):
    fact = version.fact
    if fact.actual_creditor_payment_date.period != fact.period:
        raise KernelError("payment_period_conflict", "个人代垫须按真实付出日期所属月份确认")
    if len({source.scope for source in fact.sources}) != len(fact.sources):
        raise KernelError("duplicate_allocation", "同一垫付来源不能重复列出")
    lines, effects = [], []
    for source in fact.sources:
        calculation, item = _source_obligation(context, source)
        if calculation.period > fact.period or item["normal"] != "credit":
            raise KernelError("invalid_advance_source", "垫付必须对应已成立的贷方应付义务")
        if "payment" not in item.get("settlement_modes", ("payment", "offset")):
            raise KernelError(
                "invalid_advance_source", "垫付来源必须允许真实支付，不能替代只允许抵销的确认"
            )
        if (
            calculation.values.get("actual_date")
            and fact.actual_creditor_payment_date < calculation.values["actual_date"]
        ):
            raise KernelError("payment_before_actual_source", "代垫不能早于来源资金实际发生日")
        if (
            allocated(context, source.scope, version.subject_id, fact.period) + source.amount_fen
            > item["amount_fen"]
        ):
            raise KernelError("overallocated_obligation", "垫付与已有核销超过来源应付款")
        lines.append(Line(item["account"], debit=source.amount_fen))
        effects.append(BalanceEffect(item["key"], -source.amount_fen, item["category"]))
    amount = sum_fen(source.amount_fen for source in fact.sources)
    payable = obligation(
        version,
        amount=amount,
        account="2241",
        normal="credit",
        counterparty=fact.payer_id,
        cashflow="employee_reimbursement"
        if fact.payer_kind == "employee"
        else "owner_reimbursement",
    )
    lines.append(Line("2241", credit=amount))
    return Outcome(
        tuple(lines),
        {
            "payer_id": fact.payer_id,
            "payer_kind": fact.payer_kind,
            "employee_id": fact.payer_id if fact.payer_kind == "employee" else None,
            "obligations": [payable],
        },
        (*effects, BalanceEffect(payable["key"], amount, "payable")),
    )


class PassThroughReturn(Fact):
    kind: ClassVar[str] = "pass_through_return"
    material_category: ClassVar[str] = "transactions"
    identity_fields: ClassVar[tuple[str, ...]] = ("source_id",)
    source_id: str = Field(min_length=1)
    amount_fen: PositiveFen
    refund_right_confirmed: Literal[True]

    def claims(self):
        return (Claim(f"payment:pass_through:{self.source_id}:remittance", self.amount_fen),)

    def reads(self):
        return (
            Read("fact", "pass_through", "@" + self.source_id),
            Read("calculation", "pass_through", "@" + self.source_id),
            claim_read(self.claims()[0].key, self.period),
            claim_read(f"payment:pass_through:{self.source_id}:collection", self.period),
        )


def calculate_pass_through_return(version, context):
    fact = version.fact
    source = context.one("pass_through", "@" + fact.source_id).fact
    allocation = Allocation(
        source_kind="pass_through",
        source_id=fact.source_id,
        obligation="remittance",
        amount_fen=fact.amount_fen,
    )
    calculation, item = _source_obligation(context, allocation)
    if calculation.period > fact.period:
        raise KernelError("return_before_source", "退回确认不能早于代收付业务")
    used = allocated(context, allocation.scope, version.subject_id, fact.period)
    received_scope = f"payment:pass_through:{fact.source_id}:collection"
    received = sum_fen(
        claim.amount
        for payment in context.select(claim_read(received_scope, fact.period))
        if getattr(payment.fact, "actual_payment", False)
        and payment.fact.direction == "inflow"
        and payment.fact.counterparty_id == source.payer_id
        for claim in payment.fact.claims()
        if claim.key == received_scope
    )
    if sum_fen((used, fact.amount_fen)) > min(item["amount_fen"], received):
        raise NeedsInformation("amount_fen", "退回金额超过已收到且尚未转付或退回的代收款")
    payable = obligation(
        version,
        amount=fact.amount_fen,
        account="224105",
        normal="credit",
        counterparty=source.payer_id,
        cashflow="pass_through_refund",
    )
    return Outcome(
        (Line(item["account"], debit=fact.amount_fen), Line("224105", credit=fact.amount_fen)),
        {"obligations": [payable]},
        (
            BalanceEffect(item["key"], -fact.amount_fen, item["category"]),
            BalanceEffect(payable["key"], fact.amount_fen, "payable"),
        ),
    )


def register(registry):
    registry.register(EmployeeAdvance, calculate_employee_advance)
    registry.register(PassThroughReturn, calculate_pass_through_return)
