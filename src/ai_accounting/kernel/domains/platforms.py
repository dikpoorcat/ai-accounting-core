"""Actual company payment-platform money, distinct from bank deposits and funds.

These facts describe liquid payment-account balances. Investment subscriptions,
redemptions and expense dispositions require their own explicit business facts.
"""

from typing import ClassVar, Literal

from pydantic import ConfigDict, Field, StrictBool, model_validator

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
from .transactions import (
    EXPENSE_ACCOUNTS,
    Allocation,
    ExpenseClass,
    Identifier,
    Payment,
    _through_month,
    bank_scopes,
    calculate_funding,
    calculate_payment,
)


def platform_scopes(period, account_id):
    return (
        str(period),
        f"platform:{account_id}",
        f"platform:{account_id}:{period}",
    )


MOVEMENT_CONSUMER_KINDS = (
    "platform_payment",
    "platform_funding",
    "bank_platform_transfer",
    "platform_expense_confirmation",
    "platform_boundary_disposition",
)


def movement_scope(subject_id):
    return f"platform-consumption:{subject_id}"


class PlatformMovement(Fact):
    """One preserved actual platform money row, before its business disposition."""

    kind: ClassVar[str] = "platform_movement"
    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.amount_fen": "result.amount_fen"}
    immutable: ClassVar[bool] = True
    platform_account_id: Identifier
    actual_date: ActualDate
    direction: Literal["inflow", "outflow"]
    amount_fen: PositiveFen
    source_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_location: str = Field(min_length=1, max_length=500)
    transaction_reference: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def actual_platform_row(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual platform movement date must belong to its source month")
        return self

    def identity_scopes(self):
        return (
            f"platform-row:{self.source_evidence_digest}:{self.source_location}",
            *(
                (f"platform-reference:{self.platform_account_id}:{self.transaction_reference}",)
                if self.transaction_reference
                else ()
            ),
        )

    def scopes(self):
        return (*platform_scopes(self.period, self.platform_account_id), *self.identity_scopes())

    def reads(self):
        return tuple(
            _through_month("fact", self.kind, scope, self.period)
            for scope in self.identity_scopes()
        )


def calculate_movement(version, context):
    fact: PlatformMovement = version.fact
    if fact.source_evidence_digest not in version.evidence:
        raise NeedsInformation("source_evidence_digest", "原平台交易位置必须指向本事实保全证据")
    for read in fact.reads():
        if any(item.subject_id != version.subject_id for item in context.select(read)):
            raise KernelError("duplicate_platform_movement", "同一平台原交易不能登记为多个来源")
    return Outcome(
        (),
        {
            "actual_date": str(fact.actual_date),
            "direction": fact.direction,
            "amount_fen": fact.amount_fen,
            "platform_account_id": fact.platform_account_id,
            "source_evidence_digest": fact.source_evidence_digest,
            "source_location": fact.source_location,
            "transaction_reference": fact.transaction_reference,
        },
    )


def movement_reads(ids, period):
    return tuple(
        read
        for source in ids
        for read in (
            Read("fact", "platform_movement", f"@{source}"),
            Read("calculation", "platform_movement", f"@{source}"),
            _through_month("fact", "*", movement_scope(source), period),
        )
    )


def movement_claims(ids):
    return tuple(Claim(movement_scope(source), 1) for source in ids)


def validate_movements(version, context, ids, *, direction, actual_date=None):
    """Consume complete rows once across all business dispositions, including unpaid drafts."""
    fact = version.fact
    rows = []
    for source in ids:
        row = context.one("platform_movement", f"@{source}")
        posted = context.calculations("platform_movement", f"@{source}")
        if len(posted) != 1 or posted[0].fact_id != row.id:
            raise NeedsInformation(
                "movement_ids", "平台原交易需要当前正式核验结果", sources=(source,)
            )
        if row.fact.period != fact.period:
            raise KernelError(
                "platform_movement_period", "平台原交易只能在其真实所属月采用核算处置"
            )
        if (
            row.fact.platform_account_id != fact.platform_account_id
            or row.fact.direction != direction
        ):
            raise KernelError("platform_movement_mismatch", "平台原交易账户或方向与业务处置不一致")
        if actual_date is not None and row.fact.actual_date != actual_date:
            raise KernelError("platform_movement_date", "业务实际资金日必须与所引用的原交易相符")
        scope = movement_scope(source)
        consumers = context.select(_through_month("fact", "*", scope, fact.period))
        claimed = sum_fen(
            claim.amount for item in consumers for claim in item.fact.claims() if claim.key == scope
        )
        if claimed != 1:
            raise KernelError(
                "platform_movement_consumed",
                "同一原交易须整条且仅采用一次核算处置",
                source_id=source,
            )
        rows.append(row)
    return sum_fen(row.fact.amount_fen for row in rows)


class MovementConsumption(Fact):
    movement_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_movements(self):
        if len(set(self.movement_ids)) != len(self.movement_ids):
            raise ValueError("duplicate platform source movement")
        return self

    def claims(self):
        return movement_claims(self.movement_ids)

    def reads(self):
        return movement_reads(self.movement_ids, self.period)


def validate_actual_money(version, context, direction):
    fact = version.fact
    total = validate_movements(
        version, context, fact.movement_ids, direction=direction, actual_date=fact.actual_date
    )
    if total != fact.amount_fen:
        raise KernelError("platform_movement_amount", "原平台交易合计必须等于本次实际资金金额")


class PlatformExpenseConfirmation(Fact):
    """Explicit adopted cost treatment of a bounded original platform out/return group."""

    kind: ClassVar[str] = "platform_expense_confirmation"
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.confirmed_amount_fen": "result.expense_fen"
    }
    platform_account_id: Identifier
    expense_class: ExpenseClass
    outgoing_movement_ids: tuple[Identifier, ...] = Field(min_length=1)
    returned_movement_ids: tuple[Identifier, ...] = ()
    confirmed_amount_fen: PositiveFen
    treatment_confirmed: StrictBool | None = None

    @property
    def movement_ids(self):
        return (*self.outgoing_movement_ids, *self.returned_movement_ids)

    @model_validator(mode="after")
    def distinct_sources(self):
        if len(set(self.movement_ids)) != len(self.movement_ids):
            raise ValueError("duplicate platform source movement")
        return self

    def scopes(self):
        return (
            *platform_scopes(self.period, self.platform_account_id),
            *(movement_scope(s) for s in self.movement_ids),
        )

    def claims(self):
        return movement_claims(self.movement_ids)

    def reads(self):
        return movement_reads(self.movement_ids, self.period)


def calculate_platform_expense(version, context):
    fact: PlatformExpenseConfirmation = version.fact
    if fact.treatment_confirmed is not True or not version.evidence:
        raise NeedsInformation(
            "treatment_confirmed",
            "需要明确采用本组原款净额作为公司费用的核算确认；原转款不自行证明费用",
        )
    gross = validate_movements(version, context, fact.outgoing_movement_ids, direction="outflow")
    returned = validate_movements(version, context, fact.returned_movement_ids, direction="inflow")
    if gross - returned != fact.confirmed_amount_fen:
        raise KernelError(
            "platform_expense_net", "已确认费用必须严格等于本组原出款减原退回，不得填差额凑平"
        )
    return Outcome(
        (
            Line(EXPENSE_ACCOUNTS[fact.expense_class], debit=fact.confirmed_amount_fen),
            Line("1012", credit=fact.confirmed_amount_fen, cashflow="operating_payments"),
        ),
        {
            "expense_class": fact.expense_class,
            "expense_fen": fact.confirmed_amount_fen,
            "gross_outflow_fen": gross,
            "returned_fen": returned,
            "platform_account_id": fact.platform_account_id,
            "outgoing_movement_ids": fact.outgoing_movement_ids,
            "returned_movement_ids": fact.returned_movement_ids,
            "obligations": [],
        },
        (BalanceEffect(fact.platform_account_id, -fact.confirmed_amount_fen, "platform"),),
    )


def required_reads(period: YearMonth):
    return tuple(
        Read(source, kind, str(period))
        for source in ("fact", "calculation")
        for kind in ("platform_movement", *MOVEMENT_CONSUMER_KINDS)
    )


def required_work(period, context):
    selected = {(read.source, read.kind): context.select(read) for read in required_reads(period)}
    posted = {
        row.fact_id
        for kind in ("platform_movement", *MOVEMENT_CONSUMER_KINDS)
        for row in selected[("calculation", kind)]
    }
    consumed = {}
    for kind in MOVEMENT_CONSUMER_KINDS:
        for item in selected[("fact", kind)]:
            for source in item.fact.movement_ids:
                consumed.setdefault(source, []).append(item)
    issues = []
    for row in selected[("fact", "platform_movement")]:
        consumers = consumed.get(row.subject_id, [])
        if row.id not in posted or len(consumers) != 1 or consumers[0].id not in posted:
            issues.append(
                {
                    "field": "platform_movement",
                    "source_id": row.subject_id,
                    "message": "已接收平台原交易需要当前正式核验及唯一正式业务处置",
                }
            )
    return issues


class PlatformPayment(MovementConsumption):
    kind: ClassVar[str] = "platform_payment"
    immutable: ClassVar[bool] = True
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "direction",
        "platform_account_id",
        "counterparty_id",
        "amount_fen",
        "movement_ids",
    )
    funds_account: ClassVar[str] = "1012"
    funds_category: ClassVar[str] = "platform"
    actual_payment: ClassVar[bool] = True
    actual_date: ActualDate
    direction: Literal["inflow", "outflow"]
    platform_account_id: Identifier = Field(
        description="归本公司所有的支付平台货币余额账户；不表示基金或理财产品份额"
    )
    counterparty_id: Identifier
    amount_fen: PositiveFen
    payment_method: Literal["individual"] = "individual"
    allocations: tuple[Allocation, ...] = Field(min_length=1)

    @property
    def funds_account_id(self):
        return self.platform_account_id

    @model_validator(mode="after")
    def actual_platform_payment(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual platform payment date must belong to posting month")
        if sum_fen(item.amount_fen for item in self.allocations) != self.amount_fen:
            raise ValueError("allocations must exactly account for actual platform money")
        if len({item.scope for item in self.allocations}) != len(self.allocations):
            raise ValueError("duplicate obligation allocation")
        return self

    def scopes(self):
        return (
            *platform_scopes(self.period, self.platform_account_id),
            *Payment.business_scopes(self),
            *(movement_scope(s) for s in self.movement_ids),
        )

    def claims(self):
        return (
            *movement_claims(self.movement_ids),
            *(Claim(item.scope, item.amount_fen) for item in self.allocations),
        )

    def reads(self):
        return (*Payment.reads(self), *movement_reads(self.movement_ids, self.period))


class PlatformFunding(MovementConsumption):
    kind: ClassVar[str] = "platform_funding"
    immutable: ClassVar[bool] = True
    funds_account: ClassVar[str] = "1012"
    funds_category: ClassVar[str] = "platform"
    owner_id: Identifier
    amount_fen: PositiveFen
    funding_kind: Literal["loan", "capital"]
    actual_date: ActualDate
    platform_account_id: Identifier = Field(
        description="归本公司所有的支付平台货币余额账户；不表示基金或理财产品份额"
    )

    @property
    def funds_account_id(self):
        return self.platform_account_id

    @model_validator(mode="after")
    def actual_platform_funding(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual platform funding date must belong to posting month")
        return self

    def scopes(self):
        return (
            *platform_scopes(self.period, self.platform_account_id),
            *(movement_scope(s) for s in self.movement_ids),
        )


class BankPlatformTransfer(MovementConsumption):
    """One transfer with separately evidenced bank and platform money sides.

    Both positive magnitudes describe the same conserved transfer. Directions and
    accounts identify the side; alternate bank field names share one capacity.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "x-material-amount-dimensions": {
                "bank": {
                    "amount_field": "result.bank_amount_fen",
                    "account_field": "result.bank_account_id",
                    "direction_field": "result.bank_direction",
                    "meaning": "实际银行侧资金，fact.amount_fen及result.amount_fen共用此额度",
                },
                "platform": {
                    "amount_field": "result.platform_amount_fen",
                    "account_field": "result.platform_account_id",
                    "direction_field": "result.platform_direction",
                    "meaning": "同一内部划转的平台侧资金，独立原件容量；不增加业务金额或凭证",
                },
            }
        }
    )
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.amount_fen": "result.bank_amount_fen",
        "result.amount_fen": "result.bank_amount_fen",
    }
    kind: ClassVar[str] = "bank_platform_transfer"
    immutable: ClassVar[bool] = True
    actual_date: ActualDate
    direction: Literal["bank_to_platform", "platform_to_bank"]
    bank_account_id: Identifier
    platform_account_id: Identifier = Field(
        description="同一公司支付平台货币余额账户；仅货币账户内部转款，不用于基金申赎"
    )
    amount_fen: PositiveFen

    @model_validator(mode="after")
    def actual_transfer(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual bank/platform transfer date must belong to posting month")
        return self

    def validate_material_amount(
        self, amount_field, amount_fen, *, source_amounts, source_directions=()
    ):
        bank_fields = {"fact.amount_fen", "result.amount_fen", "result.bank_amount_fen"}
        if amount_field in bank_fields:
            dimension = "bank"
            positive = self.direction == "platform_to_bank"
        elif amount_field == "result.platform_amount_fen":
            dimension = "platform"
            positive = self.direction == "bank_to_platform"
        else:
            return  # The common checker rejects fields that are not business amounts.
        directions = source_directions or (None,) * len(source_amounts)
        valid = bool(source_amounts) and len(directions) == len(source_amounts)
        for amount, direction in zip(source_amounts, directions, strict=False):
            if direction in {None, "signed_net"}:
                normalized_positive = amount > 0
            elif direction in {"inflow", "outflow"}:
                valid = valid and amount >= 0
                normalized_positive = direction == "inflow"
            else:
                valid = False
                normalized_positive = None
            valid = valid and amount != 0 and normalized_positive == positive
        # Material links retain the original amount convention. Their sign must
        # match every original member; column metadata supplies the funds direction.
        valid = (
            valid
            and amount_fen != 0
            and all((amount > 0) == (amount_fen > 0) for amount in source_amounts)
        )
        if not valid:
            raise KernelError(
                "material_funds_direction_mismatch",
                "内部划转资料须保留原金额并按明确列方向匹配账户侧；联合组不得混合收支",
                dimension=dimension,
                expected_direction="inflow" if positive else "outflow",
            )

    def scopes(self):
        return (
            *bank_scopes(self.period, self.bank_account_id),
            *platform_scopes(self.period, self.platform_account_id),
            *(movement_scope(s) for s in self.movement_ids),
        )

    def reads_for(self, subject_id):
        return (
            *self.reads(),
            Read("fact", "managed_reserve_scope", "reserve-transfer:" + subject_id),
            Read("calculation", "managed_reserve_scope", "reserve-transfer:" + subject_id),
        )


def calculate_bank_platform_transfer(version, context):
    fact: BankPlatformTransfer = version.fact
    validate_actual_money(
        version, context, "outflow" if fact.direction == "platform_to_bank" else "inflow"
    )
    bank_amount = fact.amount_fen if fact.direction == "platform_to_bank" else -fact.amount_fen
    debit, credit = ("1002", "1012") if bank_amount > 0 else ("1012", "1002")
    scope_key = "reserve-transfer:" + version.subject_id
    scopes = context.facts("managed_reserve_scope", scope_key)
    decisions = context.calculations("managed_reserve_scope", scope_key)
    if scopes:
        if len(scopes) != 1 or len(decisions) != 1 or decisions[0].fact_id != scopes[0].id:
            raise NeedsInformation("managed_reserve_scope", "需要唯一且正式核验的备用金边界")
        treatment = decisions[0].values["transfers"].get(version.subject_id)
        if treatment == "expense_on_boundary":
            if fact.direction != "bank_to_platform":
                raise KernelError("reserve_transfer_direction", "只有银行退出边界可确认备用金费用")
            return Outcome(
                (
                    Line("5602", debit=fact.amount_fen),
                    Line("1002", credit=fact.amount_fen, cashflow="managed_reserve_outflow"),
                ),
                dict(
                    actual_date=str(fact.actual_date),
                    amount_fen=fact.amount_fen,
                    direction=fact.direction,
                    bank_account_id=fact.bank_account_id,
                    platform_account_id=fact.platform_account_id,
                    bank_amount_fen=fact.amount_fen,
                    platform_amount_fen=fact.amount_fen,
                    bank_direction="outflow",
                    platform_direction="inflow",
                    accounting_treatment="reserve_expense",
                    reserve_scope_id=scopes[0].subject_id,
                    managed_reserve_cost_fen=fact.amount_fen,
                    expense_class="administration",
                ),
                (BalanceEffect(fact.bank_account_id, bank_amount, "bank"),),
            )
    return Outcome(
        (Line(debit, debit=fact.amount_fen), Line(credit, credit=fact.amount_fen)),
        {
            "actual_date": str(fact.actual_date),
            "amount_fen": fact.amount_fen,
            "direction": fact.direction,
            "bank_account_id": fact.bank_account_id,
            "platform_account_id": fact.platform_account_id,
            "bank_amount_fen": fact.amount_fen,
            "platform_amount_fen": fact.amount_fen,
            "bank_direction": "inflow" if bank_amount > 0 else "outflow",
            "platform_direction": "outflow" if bank_amount > 0 else "inflow",
        },
        (
            BalanceEffect(fact.bank_account_id, bank_amount, "bank"),
            BalanceEffect(fact.platform_account_id, -bank_amount, "platform"),
        ),
    )


def calculate_platform_payment(version, context):
    validate_actual_money(version, context, version.fact.direction)
    return calculate_payment(version, context)


def calculate_platform_funding(version, context):
    validate_actual_money(version, context, "inflow")
    return calculate_funding(version, context)


class PlatformBoundaryDisposition(MovementConsumption):
    kind: ClassVar[str] = "platform_boundary_disposition"
    business_activity: ClassVar[bool] = False
    scope_id: Identifier
    platform_account_id: Identifier

    def scopes(self):
        return (
            *platform_scopes(self.period, self.platform_account_id),
            *(movement_scope(source) for source in self.movement_ids),
        )

    def reads(self):
        return (
            *movement_reads(self.movement_ids, self.period),
            Read("fact", "managed_reserve_scope", "@" + self.scope_id),
            Read("calculation", "managed_reserve_scope", "@" + self.scope_id),
        )

    def validate_material_amount(
        self, amount_field, amount_fen, *, source_amounts, source_directions=()
    ):
        if amount_field not in {"result.inflow_fen", "result.outflow_fen"}:
            raise KernelError("boundary_material_amount", "边界原行须按明确的收支维度引用")
        expected = "inflow" if amount_field == "result.inflow_fen" else "outflow"
        for amount, direction in zip(
            source_amounts, source_directions or (None,) * len(source_amounts), strict=True
        ):
            actual = (
                ("inflow" if amount > 0 else "outflow")
                if direction in {None, "signed_net"}
                else direction
            )
            if actual != expected or (direction in {"inflow", "outflow"} and amount < 0):
                raise KernelError("material_funds_direction_mismatch", "边界处置与原行收支方向不符")


def calculate_boundary_disposition(version, context):
    from .managed_reserve import scope_result

    fact = version.fact
    scope = scope_result(context, fact.scope_id, fact.period)
    if fact.platform_account_id not in scope.values["platform_account_ids"] or not version.evidence:
        raise NeedsInformation("scope_id", "平台原行需要明确采用的公司核算边界依据")
    totals = dict(inflow=0, outflow=0)
    for sid in fact.movement_ids:
        movement = context.one("platform_movement", "@" + sid)
        amount = validate_movements(version, context, (sid,), direction=movement.fact.direction)
        totals[movement.fact.direction] = sum_fen((totals[movement.fact.direction], amount))
    return Outcome(
        (),
        dict(
            scope_id=fact.scope_id,
            platform_account_id=fact.platform_account_id,
            inflow_fen=totals["inflow"],
            outflow_fen=totals["outflow"],
            disposition="company_source_outside_expensed_reserve_accounting_boundary",
            movement_ids=fact.movement_ids,
        ),
    )


def register(registry):
    registry.register(PlatformMovement, calculate_movement)
    registry.register(PlatformExpenseConfirmation, calculate_platform_expense)
    registry.register(PlatformPayment, calculate_platform_payment)
    registry.register(PlatformFunding, calculate_platform_funding)
    registry.register(BankPlatformTransfer, calculate_bank_platform_transfer)
    registry.register(PlatformBoundaryDisposition, calculate_boundary_disposition)

    registry.register_readiness("platform_movements", required_reads, required_work)
