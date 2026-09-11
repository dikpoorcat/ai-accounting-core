"""Evidence-bound expense perimeter and settlement of debts from expensed reserves.

Cost claims attribute accounting capacity only. They never assert which historic
bank transfer physically funded a later reimbursement.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

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
from .transactions import Allocation, Identifier, _source_obligation, _through_month, bank_scopes

MANAGEMENT_ACCOUNT = "5602"
CostKind = Literal[
    "expense",
    "platform_expense_confirmation",
    "bank_platform_transfer",
    "managed_reserve_bank_expense",
    "payroll_reserve_payment",
]


class Detail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReserveCost(Detail):
    source_kind: CostKind
    source_id: Identifier
    bank_payment_id: Identifier | None = None


class TransferTreatment(Detail):
    transfer_id: Identifier
    treatment: Literal["expense_on_boundary", "already_expensed", "capital_pass_through"]
    expense_ids: tuple[Identifier, ...] = ()
    payment_ids: tuple[Identifier, ...] = ()
    funding_id: Identifier | None = None


def source_reads(kind, subject, *, calculated=True):
    return (Read("fact", kind, "@" + subject),) + (
        (Read("calculation", kind, "@" + subject),) if calculated else ()
    )


class ManagedReserveScope(Fact):
    kind: ClassVar[str] = "managed_reserve_scope"
    material_category: ClassVar[str] = "transactions"
    business_activity: ClassVar[bool] = False
    platform_account_ids: tuple[Identifier, ...] = Field(min_length=1)
    effective_from: YearMonth
    effective_through: YearMonth = Field(
        description="本次有据核验覆盖至该月；不是政策失效日，后续月份以新身份单向接续已采用范围"
    )
    predecessor_scope_id: Identifier | None = Field(
        default=None, description="可选的直接前驱范围；继承已采用成本目录，不改写前驱事实或闭期结果"
    )
    treatment_confirmed: StrictBool
    cost_sources: tuple[ReserveCost, ...] = ()
    transfer_treatments: tuple[TransferTreatment, ...] = ()

    @model_validator(mode="after")
    def bounded_scope(self):
        if self.effective_from > self.effective_through or self.period != self.effective_from:
            raise ValueError("scope period must equal its explicit first covered month")
        for items in (
            self.platform_account_ids,
            tuple(x.source_id for x in self.cost_sources),
            tuple(x.transfer_id for x in self.transfer_treatments),
        ):
            if len(items) != len(set(items)):
                raise ValueError("duplicate reserve identity")
        return self

    def scopes(self):
        return (
            str(self.period),
            *("reserve-transfer:" + x.transfer_id for x in self.transfer_treatments),
            *("reserve-cost:" + x.source_id for x in self.cost_sources),
            *(x.key for x in self.claims()),
        )

    def claims(self):
        return (
            (
                (Claim("reserve-successor:" + self.predecessor_scope_id, 1),)
                if self.predecessor_scope_id
                else ()
            )
            + tuple(Claim("reserve-membership:" + x.source_id, 1) for x in self.cost_sources)
            + tuple(
                Claim("reserve-transfer-treatment:" + x.transfer_id, 1)
                for x in self.transfer_treatments
            )
            + tuple(
                Claim("reserve-existing-path:" + source, 1)
                for item in self.transfer_treatments
                for source in (
                    *item.expense_ids,
                    *item.payment_ids,
                    *((item.funding_id,) if item.funding_id else ()),
                )
            )
        )

    def reads(self):
        reads = []
        if self.predecessor_scope_id:
            reads.extend(source_reads(self.kind, self.predecessor_scope_id))
        for cost in self.cost_sources:
            reads.extend(
                source_reads(
                    cost.source_kind,
                    cost.source_id,
                    calculated=cost.source_kind
                    not in {
                        "bank_platform_transfer",
                        "managed_reserve_bank_expense",
                        "payroll_reserve_payment",
                    },
                )
            )
            if cost.bank_payment_id:
                reads.extend(source_reads("payment", cost.bank_payment_id))
        for item in self.transfer_treatments:
            reads.extend(source_reads("bank_platform_transfer", item.transfer_id, calculated=False))
            for sid in item.expense_ids:
                reads.extend(source_reads("*", sid))
            for sid in item.payment_ids:
                reads.extend(source_reads("platform_payment", sid))
            if item.funding_id:
                reads.extend(source_reads("platform_funding", item.funding_id))
        reads.extend(Read("fact", "*", claim.key) for claim in self.claims())
        return tuple(dict.fromkeys(reads))


def published(context, kind, subject):
    version = context.one(kind, "@" + subject)
    results = context.calculations(kind, "@" + subject)
    if len(results) != 1 or results[0].fact_id != version.id:
        raise NeedsInformation("source_id", "需要来源当前事实对应的正式计算", sources=(subject,))
    return version, results[0]


def cost_amount(version):
    if version.fact.kind == "expense":
        if version.fact.expense_class != "administration":
            raise KernelError("reserve_cost_purpose", "备用金仅采用明确的管理费用来源")
        return version.fact.amount_fen
    if version.fact.kind == "platform_expense_confirmation":
        if version.fact.expense_class != "administration":
            raise KernelError("reserve_cost_purpose", "备用金仅采用明确的管理费用来源")
        return version.fact.confirmed_amount_fen
    raise KernelError("reserve_cost_kind", "该来源不是可采用的已确认管理费用")


def calculate_scope(version, context):
    fact = version.fact
    if fact.treatment_confirmed is not True or not version.evidence:
        raise NeedsInformation("treatment_confirmed", "需要有依据的资金核算边界及费用化范围确认")
    for claim in fact.claims():
        total = sum_fen(
            c.amount
            for row in context.facts("*", claim.key)
            for c in row.fact.claims()
            if c.key == claim.key
        )
        if total != 1:
            raise KernelError("reserve_scope_overlap", "同一来源不能重复纳入备用金范围")
    accounts = set(fact.platform_account_ids)
    inherited_costs = []
    predecessor_id = None
    if fact.predecessor_scope_id:
        previous, predecessor = published(context, fact.kind, fact.predecessor_scope_id)
        if previous.subject_id == version.subject_id:
            raise KernelError("reserve_scope_cycle", "范围不能以自身作为前驱")
        if accounts != set(predecessor.values["platform_account_ids"]):
            raise KernelError("reserve_scope_accounts", "接续范围必须保持同一组明确平台身份")
        if (
            YearMonth(predecessor.values["effective_through"]).ordinal + 1
            != fact.effective_from.ordinal
        ):
            raise KernelError(
                "reserve_scope_continuity", "新范围须紧接直接前驱的覆盖月份，不能倒序或重叠"
            )
        predecessor_id = predecessor.id
        inherited_costs = [
            dict(item, adopting_scope_id=item.get("adopting_scope_id", previous.subject_id))
            for item in predecessor.values["cost_sources"]
        ]
        # The direct predecessor has already frozen its complete adopted catalog.
        # Do not recursively fetch every prior scope or claim its sources again.
        if {item["source_id"] for item in inherited_costs} & {
            item.source_id for item in fact.cost_sources
        }:
            raise KernelError("reserve_duplicate_cost", "接续不得把已继承成本再声明为新成本")
    treatments = {}
    for treatment in fact.transfer_treatments:
        transfer = context.one("bank_platform_transfer", "@" + treatment.transfer_id).fact
        if (
            transfer.platform_account_id not in accounts
            or not fact.effective_from <= transfer.period <= fact.effective_through
        ):
            raise KernelError("reserve_transfer_scope", "原转款不属于该账户和有效月份范围")
        mode = treatment.treatment
        if mode == "expense_on_boundary":
            if (
                transfer.direction != "bank_to_platform"
                or treatment.expense_ids
                or treatment.payment_ids
                or treatment.funding_id
            ):
                raise KernelError(
                    "reserve_transfer_basis", "边界费用化须为原银行流出且不重复引用已确认成本"
                )
        elif mode == "capital_pass_through":
            if (
                transfer.direction != "platform_to_bank"
                or not treatment.funding_id
                or treatment.expense_ids
                or treatment.payment_ids
            ):
                raise KernelError("reserve_capital_basis", "资本过渡路径需要明确原出资")
            funding, _ = published(context, "platform_funding", treatment.funding_id)
            if (
                funding.fact.funding_kind != "capital"
                or funding.fact.platform_account_id != transfer.platform_account_id
                or funding.fact.actual_date != transfer.actual_date
                or funding.fact.amount_fen != transfer.amount_fen
            ):
                raise KernelError(
                    "reserve_capital_basis", "出资与原银行到账路径金额、实际日、账户不符"
                )
        else:
            if (
                transfer.direction != "bank_to_platform"
                or treatment.funding_id
                or not treatment.expense_ids
            ):
                raise KernelError("reserve_existing_cost_basis", "已费用化复核必须列明原完整成本")
            if len(set(treatment.expense_ids)) != len(treatment.expense_ids) or len(
                set(treatment.payment_ids)
            ) != len(treatment.payment_ids):
                raise KernelError("reserve_duplicate_cost", "复核来源不得重复")
            expenses = {sid: published(context, "*", sid)[0] for sid in treatment.expense_ids}
            actual_payments = [
                published(context, "platform_payment", sid)[0].fact for sid in treatment.payment_ids
            ]
            expected_payment_sources = set()
            for sid, expense in expenses.items():
                if expense.fact.period != transfer.period:
                    raise KernelError(
                        "reserve_existing_cost_period", "保留的完整费用路径必须在原转款同月"
                    )
                cost_amount(expense)
                if expense.fact.kind == "expense":
                    expected_payment_sources.add(sid)
                    matched = [
                        p
                        for p in actual_payments
                        if p.platform_account_id == transfer.platform_account_id
                        and p.period == transfer.period
                        and p.direction == "outflow"
                        and len(p.allocations) == 1
                        and p.allocations[0].source_kind == "expense"
                        and p.allocations[0].source_id == sid
                        and p.allocations[0].obligation == "primary"
                        and p.amount_fen == expense.fact.amount_fen
                    ]
                    if len(matched) != 1:
                        raise KernelError(
                            "reserve_existing_payment", "原供应商费用必须已有唯一完整平台实付"
                        )
                elif expense.fact.platform_account_id != transfer.platform_account_id:
                    raise KernelError("reserve_existing_cost_account", "原净费用组账户不符")
            if (
                len(actual_payments) != len(expected_payment_sources)
                or sum_fen(cost_amount(x) for x in expenses.values()) != transfer.amount_fen
            ):
                raise KernelError(
                    "reserve_existing_cost_total", "原成本及实际支付路径必须完整守恒，不能重复计费"
                )
        treatments[treatment.transfer_id] = mode
    costs = inherited_costs.copy()
    for cost in fact.cost_sources:
        original = context.one(cost.source_kind, "@" + cost.source_id)
        if not fact.effective_from <= original.fact.period <= fact.effective_through:
            raise KernelError("reserve_cost_period", "成本来源不在明确的备用金月份范围")
        if cost.source_kind == "payroll_reserve_payment":
            if (
                original.fact.scope_id != version.subject_id
                or cost.bank_payment_id
                or original.fact.platform_account_id not in accounts
            ):
                raise KernelError("reserve_cost_scope", "工资返池须引用本明确范围及实际范围账户")
            amount = original.fact.reserve_return_fen
        elif cost.source_kind == "managed_reserve_bank_expense":
            if original.fact.scope_id != version.subject_id or cost.bank_payment_id:
                raise KernelError("reserve_cost_scope", "真实银行退出须直接引用本明确范围")
            amount = original.fact.amount_fen
        elif cost.source_kind == "bank_platform_transfer":
            if treatments.get(cost.source_id) != "expense_on_boundary" or cost.bank_payment_id:
                raise KernelError(
                    "reserve_cost_transfer", "只允许把本范围明确费用化的银行退出金额计入容量"
                )
            amount = original.fact.amount_fen
        else:
            published(context, cost.source_kind, cost.source_id)
            amount = cost_amount(original)
            if cost.source_kind == "expense":
                if not cost.bank_payment_id:
                    raise KernelError(
                        "reserve_cost_payment", "新增备用金费用须引用向范围账户的完整实际银行付款"
                    )
                payment, _ = published(context, "payment", cost.bank_payment_id)
                p = payment.fact
                if (
                    p.direction != "outflow"
                    or p.counterparty_id not in accounts
                    or p.period != original.fact.period
                    or len(p.allocations) != 1
                    or p.allocations[0].source_kind != "expense"
                    or p.allocations[0].source_id != cost.source_id
                    or p.allocations[0].obligation != "primary"
                    or p.amount_fen != amount
                ):
                    raise KernelError("reserve_cost_payment", "原付款账户、用途或费用全额关系不符")
            elif cost.bank_payment_id or original.fact.platform_account_id not in accounts:
                raise KernelError("reserve_cost_account", "既有备用金净费用来源账户不符")
        costs.append(
            dict(
                source_kind=cost.source_kind,
                source_id=cost.source_id,
                period=str(original.fact.period),
                amount_fen=amount,
                adopting_scope_id=version.subject_id,
            )
        )
    return Outcome(
        (),
        dict(
            platform_account_ids=fact.platform_account_ids,
            effective_from=str(fact.effective_from),
            effective_through=str(fact.effective_through),
            predecessor_scope_id=fact.predecessor_scope_id,
            predecessor_calculation_id=predecessor_id,
            transfers=treatments,
            cost_sources=costs,
            capacity_meaning="accounting_cost_capacity_not_actual_cash_balance",
        ),
    )


class ReserveDebtAllocation(Allocation):
    source_kind: Literal["expense", "employee_advance", "reimbursement_acceptance"]


class ReserveSettlementInput(Detail):
    period: YearMonth
    scope_id: Identifier
    actual_date: ActualDate
    recipient_id: Identifier
    amount_fen: PositiveFen
    sources: tuple[ReserveDebtAllocation, ...] = Field(min_length=1)
    payment_confirmed: StrictBool

    @model_validator(mode="after")
    def paid_debts(self):
        if (
            self.actual_date.period != self.period
            or sum_fen(x.amount_fen for x in self.sources) != self.amount_fen
        ):
            raise ValueError("real payment date/month and complete debt allocations must agree")
        if len({x.scope for x in self.sources}) != len(self.sources):
            raise ValueError("duplicate debt allocation")
        return self


class CostClaim(Detail):
    source_kind: CostKind
    source_id: Identifier
    amount_fen: PositiveFen

    @property
    def scope(self):
        return "expense-return:" + self.source_id


class ManagedReserveObligationSettlement(Fact, ReserveSettlementInput):
    kind: ClassVar[str] = "managed_reserve_obligation_settlement"
    registration_command: ClassVar[str] = "confirm_managed_reserve_settlement"
    material_category: ClassVar[str] = "transactions"
    immutable: ClassVar[bool] = True
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "recipient_id",
        "amount_fen",
    )
    cost_claims: tuple[CostClaim, ...] = Field(min_length=1)

    def scopes(self):
        return (
            str(self.period),
            *(x.scope for x in self.sources),
            *(x.scope for x in self.cost_claims),
        )

    def claims(self):
        return tuple(Claim(x.scope, x.amount_fen) for x in (*self.sources, *self.cost_claims))

    def reads(self):
        reads = list(source_reads("managed_reserve_scope", self.scope_id))
        for cost in self.cost_claims:
            reads.extend(source_reads(cost.source_kind, cost.source_id))
        for source in self.sources:
            reads.extend(source_reads(source.source_kind, source.source_id))
        reads.extend(_through_month("fact", "*", claim.key, self.period) for claim in self.claims())
        return tuple(dict.fromkeys(reads))


def scope_result(context, scope_id, period):
    _, result = published(context, "managed_reserve_scope", scope_id)
    if not result.values["effective_from"] <= period <= result.values["effective_through"]:
        raise KernelError(
            "reserve_settlement_period", "本次采用目录尚未覆盖实际月份，请登记该月接续范围"
        )
    return result


def adopted_cost(context, kind, source_id, scope_id):
    source, result = published(context, kind, source_id)
    if kind in {
        "bank_platform_transfer",
        "managed_reserve_bank_expense",
        "payroll_reserve_payment",
    }:
        if (
            result.values.get("reserve_scope_id") != scope_id
            or result.values.get("accounting_treatment") != "reserve_expense"
        ):
            raise NeedsInformation(
                "cost_sources", "原银行退出尚未正式采用备用金费用处理", sources=(source_id,)
            )
        return source, result.values["managed_reserve_cost_fen"]
    return source, cost_amount(source)


def calculate_reserve_settlement(version, context):
    fact = version.fact
    if fact.payment_confirmed is not True or not version.evidence:
        raise NeedsInformation("payment_confirmed", "需要实际从已费用化备用金结清原债的依据")
    scope = scope_result(context, fact.scope_id, fact.period)
    eligible = {(x["source_kind"], x["source_id"]): x for x in scope.values["cost_sources"]}
    if (
        len({x.source_id for x in fact.cost_claims}) != len(fact.cost_claims)
        or sum_fen(x.amount_fen for x in fact.cost_claims) != fact.amount_fen
    ):
        raise KernelError("reserve_cost_claim_total", "成本容量编译结果须唯一且完整守恒")
    for claim in fact.cost_claims:
        if (claim.source_kind, claim.source_id) not in eligible:
            raise KernelError("reserve_cost_not_adopted", "成本不属于明确的备用金范围")
        source, amount = adopted_cost(
            context,
            claim.source_kind,
            claim.source_id,
            eligible[(claim.source_kind, claim.source_id)].get("adopting_scope_id", fact.scope_id),
        )
        if source.fact.period > fact.period:
            raise KernelError("reserve_future_cost", "结清不能先于已费用化的来源")
        claimed = sum_fen(
            c.amount
            for v in context.select(_through_month("fact", "*", claim.scope, fact.period))
            for c in v.fact.claims()
            if c.key == claim.scope
        )
        if claimed > amount:
            raise KernelError(
                "excess_expense_recovery", "费用回收及备用金旧债结清不能重复使用原成本容量"
            )
    lines, effects, settlements = [], [], []
    for source in fact.sources:
        original, _ = published(context, source.source_kind, source.source_id)
        calculation, item = _source_obligation(context, source)
        origin_day = getattr(original.fact, "actual_creditor_payment_date", None)
        if origin_day is not None and fact.actual_date < origin_day:
            raise KernelError("reserve_debt_date", "不能在原债实际成立前结清")
        if (
            calculation.period > fact.period
            or item["normal"] != "credit"
            or item["counterparty_id"] != fact.recipient_id
        ):
            raise KernelError("reserve_debt_source", "须为已成立且属于实际收款人的应付义务")
        if (
            calculation.values.get("actual_date")
            and str(fact.actual_date) < calculation.values["actual_date"]
        ):
            raise KernelError("reserve_debt_date", "不能在原债实际成立前结清")
        if "payment" not in item.get("settlement_modes", ("payment", "offset")):
            raise KernelError("reserve_debt_source", "该原债不允许实际付款结清")
        claimed = sum_fen(
            c.amount
            for v in context.select(_through_month("fact", "*", source.scope, fact.period))
            for c in v.fact.claims()
            if c.key == source.scope
        )
        if claimed > item["amount_fen"]:
            raise KernelError("excess_settlement", "原债已被其他付款或结清占用")
        lines.append(Line(item["account"], debit=source.amount_fen))
        effects.append(BalanceEffect(item["key"], -source.amount_fen, item["category"]))
        settlements.append(
            dict(
                source_calculation=calculation.id,
                obligation=item["key"],
                amount_fen=source.amount_fen,
            )
        )
    lines.append(Line(MANAGEMENT_ACCOUNT, credit=fact.amount_fen))
    return Outcome(
        tuple(lines),
        dict(
            amount_fen=fact.amount_fen,
            scope_id=fact.scope_id,
            expense_class="administration",
            recipient_id=fact.recipient_id,
            settlements=settlements,
            accepted_sources=[
                dict(
                    source_calculation_id=x["source_calculation"],
                    obligation=x["obligation"],
                    amount_fen=x["amount_fen"],
                )
                for x in settlements
            ],
            cost_claims=[x.model_dump() for x in fact.cost_claims],
            cost_attribution="deterministic_accounting_capacity_only_not_physical_funds_origin",
        ),
        tuple(effects),
    )


class ManagedReserveBankExpense(Fact):
    """Actual exit from a bank into the evidenced expensed reserve boundary."""

    kind: ClassVar[str] = "managed_reserve_bank_expense"
    material_category: ClassVar[str] = "bank"
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.amount_fen": "result.amount_fen",
        "result.managed_reserve_cost_fen": "result.amount_fen",
    }
    immutable: ClassVar[bool] = True
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "bank_account_id",
        "amount_fen",
    )
    scope_id: Identifier
    actual_date: ActualDate
    bank_account_id: Identifier
    amount_fen: PositiveFen

    @model_validator(mode="after")
    def actual_month(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual bank day must belong to the payment month")
        return self

    def scopes(self):
        return bank_scopes(self.period, self.bank_account_id)

    def reads(self):
        return source_reads("managed_reserve_scope", self.scope_id)

    def validate_material_amount(
        self, amount_field, amount_fen, *, source_amounts, source_directions=()
    ):
        if amount_field not in {
            "fact.amount_fen",
            "result.amount_fen",
            "result.managed_reserve_cost_fen",
        }:
            raise KernelError("reserve_material_amount", "银行退出必须引用同一真实金额")
        valid = bool(source_amounts) and amount_fen != 0
        for amount, direction in zip(
            source_amounts, source_directions or (None,) * len(source_amounts), strict=True
        ):
            valid = valid and amount != 0 and (amount > 0) == (amount_fen > 0)
            valid = valid and (
                (direction == "outflow" and amount > 0)
                or (direction in {None, "signed_net"} and amount < 0)
            )
        if not valid:
            raise KernelError(
                "material_funds_direction_mismatch", "实际银行退出须为原支出，保留原金额和列方向"
            )


def calculate_bank_expense(version, context):
    fact = version.fact
    scope = scope_result(context, fact.scope_id, fact.period)
    if not version.evidence or not any(
        x["source_kind"] == fact.kind and x["source_id"] == version.subject_id
        for x in scope.values["cost_sources"]
    ):
        raise NeedsInformation("scope_id", "真实银行退出须由明确核算边界逐项采用")
    return Outcome(
        (
            Line(MANAGEMENT_ACCOUNT, debit=fact.amount_fen),
            Line("1002", credit=fact.amount_fen, cashflow="managed_reserve_outflow"),
        ),
        dict(
            actual_date=str(fact.actual_date),
            bank_account_id=fact.bank_account_id,
            amount_fen=fact.amount_fen,
            direction="outflow",
            expense_class="administration",
            accounting_treatment="reserve_expense",
            reserve_scope_id=fact.scope_id,
            managed_reserve_cost_fen=fact.amount_fen,
        ),
        (BalanceEffect(fact.bank_account_id, -fact.amount_fen, "bank"),),
    )


def register(registry):
    registry.register(ManagedReserveBankExpense, calculate_bank_expense)
    registry.register(ManagedReserveScope, calculate_scope)
    registry.register(ManagedReserveObligationSettlement, calculate_reserve_settlement)
