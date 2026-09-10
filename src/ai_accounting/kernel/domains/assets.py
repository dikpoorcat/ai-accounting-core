"""Pure asset and financing modules sharing the common version/publishing path."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Context as DecimalContext
from decimal import Decimal, localcontext
from typing import ClassVar, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from ai_accounting.borrowings import BorrowingCalculationError, calculate_simple_interest
from ai_accounting.fixed_assets import (
    DepreciationGroupMember,
    calculate_grouped_straight_line_depreciation,
    calculate_straight_line_depreciation,
)
from ai_accounting.intangible_assets import calculate_straight_line_amortization

from ..contracts import (
    BalanceEffect,
    Context,
    Fact,
    FactVersion,
    KernelError,
    Line,
    NeedsInformation,
    Outcome,
    Read,
    Registry,
)
from ..types import ActualDate, NonNegativeFen, PositiveFen, YearMonth, sum_fen
from .taxes import round_fen
from .transactions import (
    Identifier,
    ProjectCostSource,
    consume_project_costs,
    obligation,
    outcome,
    project_cost_reads,
)


def previous_month(period: YearMonth) -> YearMonth | None:
    return YearMonth.from_ordinal(period.ordinal - 1) if period.ordinal else None


class AssetAcquisition(Fact):
    kind: ClassVar[str] = "asset"
    identity_fields: ClassVar[tuple[str, ...]] = ("asset_type",)
    asset_type: Literal["fixed", "intangible"]
    supplier_id: Identifier | None = None
    acquisition_date: ActualDate
    cost_fen: PositiveFen
    acquisition_basis: Literal["direct_purchase", "project_completion"]
    project_sources: tuple[ProjectCostSource, ...] = ()

    @model_validator(mode="after")
    def acquisition_month(self):
        if self.acquisition_date.period != self.period:
            raise ValueError("acquisition date must belong to recognition month")
        if self.acquisition_basis == "project_completion":
            if self.asset_type != "intangible" or not self.project_sources:
                raise ValueError("project completion requires intangible asset project sources")
            if sum_fen(item.amount_fen for item in self.project_sources) != self.cost_fen:
                raise ValueError("capitalized sources must exactly equal acquisition cost")
            if len({item.source_id for item in self.project_sources}) != len(self.project_sources):
                raise ValueError("duplicate project source")
        elif self.project_sources:
            raise ValueError("direct purchase cannot consume existing project costs")
        return self

    def scopes(self):
        return (str(self.period), *(f"cost:{source.source_id}" for source in self.project_sources))

    def reads(self):
        return project_cost_reads(self.project_sources)


def calculate_acquisition(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetAcquisition = version.fact
    pending = "1604" if fact.asset_type == "fixed" else "189901"
    if fact.acquisition_basis == "project_completion":
        credits, effects, projects = consume_project_costs(version, ctx, fact.project_sources)
        if len(projects) != 1:
            raise KernelError("project_identity_conflict", "形成同一无形资产须使用同一项目来源")
        return Outcome(
            (Line(pending, debit=fact.cost_fen), *credits),
            {"cost_fen": fact.cost_fen, "asset_type": fact.asset_type, "obligations": []},
            (
                *effects,
                BalanceEffect(f"asset:{version.subject_id}:carrying", fact.cost_fen, "asset"),
            ),
        )
    if fact.supplier_id is None:
        raise NeedsInformation(
            "supplier_id", "直接购入资产需要明确供应商", sources=("purchase_evidence",)
        )
    payable = obligation(
        version,
        amount=fact.cost_fen,
        account="2202",
        normal="credit",
        counterparty=fact.supplier_id,
        cashflow="asset_acquisition",
    )
    base = outcome(
        [Line(pending, debit=fact.cost_fen), Line("2202", credit=fact.cost_fen)],
        {"cost_fen": fact.cost_fen, "asset_type": fact.asset_type},
        [payable],
    )
    return Outcome(
        base.lines,
        base.values,
        base.balances
        + (BalanceEffect(f"asset:{version.subject_id}:carrying", fact.cost_fen, "asset"),),
    )


class AssetActivation(Fact):
    kind: ClassVar[str] = "asset_activation"
    identity_fields: ClassVar[tuple[str, ...]] = ("asset_id",)
    asset_id: Identifier
    in_use_date: ActualDate
    useful_life_months: StrictInt = Field(gt=0, le=1200)
    residual_fen: NonNegativeFen
    benefit_area: Literal["administration", "sales", "service"]
    rounding_policy: Literal["floor_final_remainder", "round_half_up_card"]

    @model_validator(mode="after")
    def activation_month(self):
        if self.in_use_date.period != self.period:
            raise ValueError("in_use_date must belong to activation month")
        return self

    def scopes(self):
        return (str(self.period), f"asset:{self.asset_id}")

    def reads(self):
        return (
            Read("fact", "asset", f"@{self.asset_id}"),
            Read("calculation", "asset", f"@{self.asset_id}"),
            Read("fact", self.kind, f"asset:{self.asset_id}"),
        )


def calculate_activation(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetActivation = version.fact
    source: AssetAcquisition = ctx.one("asset", f"@{fact.asset_id}").fact
    acquisition = ctx.calculations("asset", f"@{fact.asset_id}")
    if not acquisition:
        raise NeedsInformation("asset_id", "需要已入账的资产取得来源")
    alternatives = ctx.facts("asset_activation", f"asset:{fact.asset_id}")
    if any(row.subject_id != version.subject_id for row in alternatives):
        raise KernelError("duplicate_asset_activation", "同一资产只能有一个启用身份")
    if fact.in_use_date < source.acquisition_date:
        raise KernelError("activation_before_acquisition", "启用日期不能早于取得日期")
    if source.asset_type == "intangible" and fact.residual_fen:
        raise KernelError("intangible_residual", "外购无形资产残值必须为零")
    if source.cost_fen - fact.residual_fen < fact.useful_life_months:
        raise KernelError("invalid_depreciable_amount", "每个摊折月份至少须可分配一分")
    if source.asset_type == "intangible" and fact.rounding_policy != "floor_final_remainder":
        raise KernelError("intangible_rounding_policy", "无形资产采用整分均摊、末月余数规则")
    if fact.rounding_policy == "round_half_up_card":
        depreciable = source.cost_fen - fact.residual_fen
        base, remainder = divmod(depreciable, fact.useful_life_months)
        rounded = base + int(remainder * 2 >= fact.useful_life_months)
        if rounded * (fact.useful_life_months - 1) >= depreciable:
            raise NeedsInformation(
                "rounding_policy",
                "所选四舍五入规则会在寿命结束前摊完；需明确采用可连续分配的摊折规则",
                sources=("floor_final_remainder",),
            )
    pending, active = ("1604", "1601") if source.asset_type == "fixed" else ("189901", "1701")
    start = fact.period.ordinal + (1 if source.asset_type == "fixed" else 0)
    return Outcome(
        (Line(active, debit=source.cost_fen), Line(pending, credit=source.cost_fen)),
        {
            "asset_id": fact.asset_id,
            "cost_fen": source.cost_fen,
            "asset_type": source.asset_type,
            "consumption_start": str(YearMonth.from_ordinal(start)),
            "rounding_policy": fact.rounding_policy,
        },
    )


class AssetConsumption(Fact):
    kind: ClassVar[str] = "asset_consumption"
    identity_fields: ClassVar[tuple[str, ...]] = ("asset_id", "period")
    asset_id: Identifier

    def scopes(self):
        return (str(self.period), f"asset:{self.asset_id}", f"asset:{self.asset_id}:{self.period}")

    def reads(self):
        reads = [
            Read("fact", "asset", f"@{self.asset_id}"),
            Read("fact", "asset_activation", f"asset:{self.asset_id}"),
            Read("calculation", "asset_activation", f"asset:{self.asset_id}"),
            Read("fact", "asset_disposal", f"asset:{self.asset_id}"),
            Read("fact", self.kind, f"asset:{self.asset_id}:{self.period}"),
        ]
        previous = previous_month(self.period)
        if previous is not None:
            reads.append(Read("calculation", self.kind, f"asset:{self.asset_id}:{previous}"))
        return tuple(reads)


def calculate_consumption(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetConsumption = version.fact
    asset: AssetAcquisition = ctx.one("asset", f"@{fact.asset_id}").fact
    activation: AssetActivation = ctx.one("asset_activation", f"asset:{fact.asset_id}").fact
    activation_results = ctx.calculations("asset_activation", f"asset:{fact.asset_id}")
    if len(activation_results) != 1:
        raise NeedsInformation("asset_activation", "需要资产正式启用结果")
    duplicates = ctx.facts(fact.kind, f"asset:{fact.asset_id}:{fact.period}")
    if any(item.subject_id != version.subject_id for item in duplicates):
        raise KernelError("duplicate_consumption", "资产每月只能确认一次摊折")
    disposals = ctx.facts("asset_disposal", f"asset:{fact.asset_id}")
    if any(item.fact.period < fact.period for item in disposals):
        raise KernelError("asset_already_disposed", "已经处置的资产不能继续摊折")
    start = YearMonth(activation_results[0].values["consumption_start"])
    completed = fact.period.ordinal - start.ordinal
    if completed < 0 or completed >= activation.useful_life_months:
        raise KernelError("consumption_outside_life", "摊折月份不在确定的使用寿命内")
    previous = previous_month(fact.period)
    prior = ctx.calculations(fact.kind, f"asset:{fact.asset_id}:{previous}") if previous else ()
    if completed and len(prior) != 1:
        raise NeedsInformation(
            "previous_consumption", "须先确认连续的上月摊折", sources=(str(previous),)
        )
    opening = prior[0].values["closing_accumulated_fen"] if completed else 0
    if asset.asset_type == "intangible":
        computed = calculate_straight_line_amortization(
            cost_fen=asset.cost_fen,
            useful_life_months=activation.useful_life_months,
            completed_months=completed,
            opening_accumulated_amortization_fen=opening,
        )
        amount, closing, accumulated = (
            computed.amortization_fen,
            computed.closing_accumulated_amortization_fen,
            "1702",
        )
        expenses = {"administration": "560203", "sales": "560103", "service": "540103"}
    else:
        if activation.rounding_policy == "round_half_up_card":
            with localcontext(DecimalContext(prec=80)):
                computed = calculate_grouped_straight_line_depreciation(
                    members=(
                        DepreciationGroupMember(
                            fact.asset_id, asset.cost_fen, activation.residual_fen
                        ),
                    ),
                    member_key=fact.asset_id,
                    useful_life_months=activation.useful_life_months,
                    completed_months=completed,
                    opening_accumulated_depreciation_fen=opening,
                ).member_result
        else:
            computed = calculate_straight_line_depreciation(
                cost_fen=asset.cost_fen,
                residual_value_fen=activation.residual_fen,
                useful_life_months=activation.useful_life_months,
                completed_months=completed,
                opening_accumulated_depreciation_fen=opening,
            )
        amount, closing, accumulated = (
            computed.depreciation_fen,
            computed.closing_accumulated_depreciation_fen,
            "1602",
        )
        expenses = {"administration": "560202", "sales": "560102", "service": "540102"}
    return Outcome(
        (Line(expenses[activation.benefit_area], debit=amount), Line(accumulated, credit=amount)),
        {
            "asset_id": fact.asset_id,
            "consumption_fen": amount,
            "completed_months": completed + 1,
            "closing_accumulated_fen": closing,
            "carrying_fen": asset.cost_fen - closing,
            "rounding_policy": activation.rounding_policy,
        },
        (BalanceEffect(f"asset:{fact.asset_id}:carrying", -amount, "asset"),),
    )


class AssetDisposal(Fact):
    kind: ClassVar[str] = "asset_disposal"
    identity_fields: ClassVar[tuple[str, ...]] = ("asset_id",)
    asset_id: Identifier
    disposal_date: ActualDate
    disposal_kind: Literal["sale", "scrap"]
    gross_proceeds_fen: NonNegativeFen
    buyer_id: Identifier | None = None
    vat_policy_id: Identifier | None = None
    exemption_eligible: StrictBool | None = None

    @model_validator(mode="after")
    def actual_month(self):
        if self.disposal_date.period != self.period:
            raise ValueError("disposal_date must belong to recognition month")
        if self.disposal_kind == "scrap" and self.gross_proceeds_fen:
            raise ValueError("scrap has no proceeds; use sale for a consideration")
        return self

    def scopes(self):
        return (str(self.period), f"asset:{self.asset_id}", f"tax:{self.period}")

    def reads(self):
        reads = [
            Read("fact", "asset", f"@{self.asset_id}"),
            Read("fact", "asset_activation", f"asset:{self.asset_id}"),
            Read("calculation", "asset_activation", f"asset:{self.asset_id}"),
            Read(
                "calculation",
                "asset_consumption",
                f"asset:{self.asset_id}",
                YearMonth.from_ordinal(self.period.ordinal + 1),
            ),
            Read("fact", self.kind, f"asset:{self.asset_id}"),
        ]
        if self.vat_policy_id:
            reads.append(Read("fact", "used_asset_vat_policy", f"@{self.vat_policy_id}"))
        return tuple(reads)


def calculate_disposal(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetDisposal = version.fact
    asset: AssetAcquisition = ctx.one("asset", f"@{fact.asset_id}").fact
    activation: AssetActivation = ctx.one("asset_activation", f"asset:{fact.asset_id}").fact
    if len(ctx.calculations("asset_activation", f"asset:{fact.asset_id}")) != 1:
        raise NeedsInformation("asset_activation", "处置前需要资产正式启用结果")
    if any(
        item.subject_id != version.subject_id
        for item in ctx.facts(fact.kind, f"asset:{fact.asset_id}")
    ):
        raise KernelError("duplicate_disposal", "同一资产只能处置一次")
    if fact.disposal_date < activation.in_use_date:
        raise KernelError("disposal_before_activation", "处置不能早于启用")
    consumption = ctx.select(
        Read(
            "calculation",
            "asset_consumption",
            f"asset:{fact.asset_id}",
            YearMonth.from_ordinal(fact.period.ordinal + 1),
        )
    )
    latest = max(consumption, key=lambda item: item.period, default=None)
    first_month = activation.period.ordinal + (1 if asset.asset_type == "fixed" else 0)
    expected_months = min(
        max(fact.period.ordinal - first_month + 1, 0), activation.useful_life_months
    )
    if expected_months and (latest is None or latest.values["completed_months"] != expected_months):
        raise NeedsInformation("current_consumption", "处置前需要连续累计摊折至当月或寿命末月")
    accumulated = latest.values["closing_accumulated_fen"] if latest else 0
    carrying = asset.cost_fen - accumulated
    if asset.asset_type == "intangible":
        disposal_day = date.fromisoformat(fact.disposal_date)
        if (
            fact.disposal_kind != "scrap"
            or disposal_day.day != calendar.monthrange(disposal_day.year, disposal_day.month)[1]
        ):
            raise KernelError("intangible_disposal_policy", "无形资产仅支持月末无收入报废")
    net, vat, tax_sales = fact.gross_proceeds_fen, 0, 0
    if fact.disposal_kind == "sale":
        if not fact.buyer_id or not fact.vat_policy_id:
            raise NeedsInformation("buyer_id,vat_policy_id", "出售资产需买方及明确适用税率来源")
        if fact.exemption_eligible is None:
            raise NeedsInformation("exemption_eligible", "需要确认本项资产出售的免税适用事实")
        policy = ctx.one("used_asset_vat_policy", f"@{fact.vat_policy_id}").fact.policy
        policy.require_effective(date.fromisoformat(fact.disposal_date))
        with localcontext(DecimalContext(prec=80)):
            tax_sales = round_fen(
                Decimal(fact.gross_proceeds_fen) * 100 / (100 + policy.tax_base_rate_percent)
            )
            vat = round_fen(Decimal(tax_sales) * policy.payable_rate_percent / 100)
        net = fact.gross_proceeds_fen - vat
    cost_account, accumulated_account = (
        ("1601", "1602") if asset.asset_type == "fixed" else ("1701", "1702")
    )
    lines = [Line(cost_account, credit=asset.cost_fen)]
    if accumulated:
        lines.append(Line(accumulated_account, debit=accumulated))
    if fact.gross_proceeds_fen:
        lines.append(Line("1122", debit=fact.gross_proceeds_fen))
    if vat:
        lines.append(Line("222101", credit=vat))
    difference = net - carrying
    if difference > 0:
        lines.append(Line("630101", credit=difference))
    elif difference < 0:
        lines.append(Line("571101" if asset.asset_type == "fixed" else "571102", debit=-difference))
    obligations = (
        [
            obligation(
                version,
                amount=fact.gross_proceeds_fen,
                account="1122",
                normal="debit",
                counterparty=fact.buyer_id,
                cashflow="asset_disposal",
            )
        ]
        if fact.gross_proceeds_fen
        else []
    )
    base = outcome(
        lines,
        {
            "asset_id": fact.asset_id,
            "gain_loss_fen": difference,
            "accrued_vat_fen": vat,
            "net_sales_fen": tax_sales,
            "exemption_eligible": fact.exemption_eligible is True,
            "carrying_fen": 0,
        },
        obligations,
    )
    return Outcome(
        base.lines,
        base.values,
        base.balances + (BalanceEffect(f"asset:{fact.asset_id}:carrying", -carrying, "asset"),),
    )


class LoanAgreement(Fact):
    kind: ClassVar[str] = "loan_agreement"
    lender_id: Identifier
    lender_is_licensed: StrictBool
    currency: Literal["CNY"]
    annual_rate_percent: Decimal = Field(gt=0, le=100, decimal_places=6)
    day_count_basis: Literal["actual_360", "actual_365"]
    maturity_date: ActualDate
    loan_term: Literal["short_term", "long_term"]

    @field_validator("annual_rate_percent", mode="before")
    @classmethod
    def decimal_rate(cls, value):
        if not isinstance(value, (Decimal, str)):
            raise ValueError("annual rate must be a decimal string or Decimal")
        return Decimal(value)


class LoanDrawdown(Fact):
    kind: ClassVar[str] = "loan_drawdown"
    immutable: ClassVar[bool] = True
    agreement_id: Identifier
    principal_fen: PositiveFen
    actual_date: ActualDate
    bank_account_id: Identifier

    @model_validator(mode="after")
    def actual_month(self):
        if self.actual_date.period != self.period:
            raise ValueError("drawdown posting period must equal actual funds month")
        return self

    def reads(self):
        return (Read("fact", "loan_agreement", f"@{self.agreement_id}"),)

    def scopes(self):
        return (
            str(self.period),
            f"bank:{self.bank_account_id}",
            f"bank:{self.bank_account_id}:{self.period}",
        )


def calculate_drawdown(version: FactVersion, ctx: Context) -> Outcome:
    fact: LoanDrawdown = version.fact
    agreement: LoanAgreement = ctx.one("loan_agreement", f"@{fact.agreement_id}").fact
    if not agreement.lender_is_licensed:
        raise KernelError("unsupported_lender", "本借款模块限持牌金融机构人民币固定利率贷款")
    if fact.actual_date >= agreement.maturity_date:
        raise KernelError("drawdown_after_maturity", "放款须早于到期日")
    account = "2001" if agreement.loan_term == "short_term" else "2501"
    payable = obligation(
        version,
        name="principal",
        amount=fact.principal_fen,
        account=account,
        normal="credit",
        counterparty=agreement.lender_id,
        cashflow="loan_repayment",
    )
    result = outcome(
        [
            Line("1002", debit=fact.principal_fen, cashflow="loan_receipts"),
            Line(account, credit=fact.principal_fen),
        ],
        {
            "principal_fen": fact.principal_fen,
            "actual_date": str(fact.actual_date),
            "bank_account_id": fact.bank_account_id,
        },
        [payable],
    )
    return Outcome(
        result.lines,
        result.values,
        result.balances + (BalanceEffect(fact.bank_account_id, fact.principal_fen, "bank"),),
    )


class LoanInterest(Fact):
    kind: ClassVar[str] = "loan_interest"
    identity_fields: ClassVar[tuple[str, ...]] = ("drawdown_id", "agreement_id")
    drawdown_id: Identifier
    agreement_id: Identifier
    period_start: ActualDate
    period_end_exclusive: ActualDate

    @model_validator(mode="after")
    def actual_period(self):
        start, end = (
            date.fromisoformat(self.period_start),
            date.fromisoformat(self.period_end_exclusive),
        )
        if (
            end <= start
            or self.period_start.period != self.period
            or YearMonth((end - timedelta(days=1)).isoformat()[:7]) != self.period
        ):
            raise ValueError("interest interval must fall within its accounting month")
        return self

    def scopes(self):
        return (str(self.period), f"loan:{self.drawdown_id}")

    def reads(self):
        repayment_scope = f"payment:loan_drawdown:{self.drawdown_id}:principal"
        before = YearMonth.from_ordinal(self.period.ordinal + 1)
        return (
            Read("fact", "loan_agreement", f"@{self.agreement_id}"),
            Read("fact", "loan_drawdown", f"@{self.drawdown_id}"),
            Read("calculation", "loan_drawdown", f"@{self.drawdown_id}"),
            Read("fact", "payment", repayment_scope, before),
            Read("fact", "cash_payment", repayment_scope, before),
            Read("fact", "employee_advance", repayment_scope, before),
            Read("fact", self.kind, f"loan:{self.drawdown_id}"),
        )


def calculate_interest(version: FactVersion, ctx: Context) -> Outcome:
    fact: LoanInterest = version.fact
    agreement: LoanAgreement = ctx.one("loan_agreement", f"@{fact.agreement_id}").fact
    drawdown: LoanDrawdown = ctx.one("loan_drawdown", f"@{fact.drawdown_id}").fact
    if not ctx.calculations("loan_drawdown", f"@{fact.drawdown_id}"):
        raise NeedsInformation("drawdown_id", "需要已入账的真实放款来源")
    if drawdown.agreement_id != fact.agreement_id:
        raise KernelError("loan_agreement_conflict", "计息合同须为本次真实放款合同")
    if (
        fact.period_start < drawdown.actual_date
        or fact.period_end_exclusive > agreement.maturity_date
    ):
        raise KernelError("interest_outside_contract", "计息区间不在放款和到期日之间")
    for other in ctx.facts(fact.kind, f"loan:{fact.drawdown_id}"):
        if other.subject_id == version.subject_id:
            continue
        if (
            other.fact.period_start < fact.period_end_exclusive
            and fact.period_start < other.fact.period_end_exclusive
        ):
            raise KernelError("overlapping_interest", "合同计息区间不能重复")
    repayment_scope = f"payment:loan_drawdown:{fact.drawdown_id}:principal"
    before = YearMonth.from_ordinal(fact.period.ordinal + 1)
    payments = (
        *ctx.select(Read("fact", "payment", repayment_scope, before)),
        *ctx.select(Read("fact", "cash_payment", repayment_scope, before)),
    )
    outstanding = drawdown.principal_fen
    for payment in payments:
        if payment.fact.actual_date >= fact.period_end_exclusive:
            continue
        allocations = [a for a in payment.fact.allocations if a.scope == repayment_scope]
        if payment.fact.direction != "outflow" or payment.fact.actual_date < drawdown.actual_date:
            raise KernelError("invalid_principal_repayment", "还本须为放款后的实际资金付出")
        for allocation in allocations:
            recipient = (
                allocation.recipient_id
                if payment.fact.payment_method == "bank_batch"
                else payment.fact.counterparty_id
            )
            if recipient is None:
                raise NeedsInformation("allocations.recipient_id", "汇总还款须明确本金的实际收款人")
            if recipient != agreement.lender_id:
                raise KernelError(
                    "loan_repayment_party_conflict", "本金须实际支付给本次放款的贷款人"
                )
        if fact.period_start < payment.fact.actual_date < fact.period_end_exclusive:
            raise KernelError("split_interest_at_repayment", "本金变动日需拆分计息区间")
        if payment.fact.actual_date <= fact.period_start:
            paid = sum_fen(a.amount_fen for a in allocations)
            outstanding = sum_fen((outstanding, -paid))
    for advance in ctx.select(Read("fact", "employee_advance", repayment_scope, before)):
        actual_day = advance.fact.actual_creditor_payment_date
        if actual_day >= fact.period_end_exclusive:
            continue
        if actual_day < drawdown.actual_date:
            raise KernelError("invalid_principal_repayment", "第三方代还本金不能早于真实放款")
        if fact.period_start < actual_day < fact.period_end_exclusive:
            raise KernelError("split_interest_at_repayment", "第三方代还本金日需拆分计息区间")
        if actual_day <= fact.period_start:
            outstanding = sum_fen(
                (
                    outstanding,
                    -sum_fen(
                        item.amount_fen
                        for item in advance.fact.sources
                        if item.scope == repayment_scope
                    ),
                )
            )
    # Actual excess repayment cannot turn the lender's outstanding principal
    # into a negative interest-bearing balance. The payment calculation handles
    # its explicit recovery. Reading its calculation here would create a cycle
    # when a single bank payment settles both this interest and principal.
    outstanding = max(outstanding, 0)
    actual_days = (
        date.fromisoformat(fact.period_end_exclusive) - date.fromisoformat(fact.period_start)
    ).days
    computed = None
    if outstanding:
        try:
            computed = calculate_simple_interest(
                principal_fen=outstanding,
                annual_rate_percent=agreement.annual_rate_percent,
                period_start=date.fromisoformat(fact.period_start),
                period_end=date.fromisoformat(fact.period_end_exclusive),
                day_count_basis=agreement.day_count_basis,
            )
        except BorrowingCalculationError as error:
            if error.code != "BORROWING_INTEREST_AMOUNT_MUST_BE_POSITIVE":
                raise
    if computed is None:
        denominator = 360 if agreement.day_count_basis == "actual_360" else 365
        with localcontext(DecimalContext(prec=80)):
            unrounded = (
                Decimal(outstanding)
                * agreement.annual_rate_percent
                * actual_days
                / (100 * denominator)
            )
        return Outcome(
            (),
            {
                "interest_fen": 0,
                "principal_fen": outstanding,
                "actual_days": actual_days,
                "annual_rate_percent": str(agreement.annual_rate_percent),
                "unrounded_interest_fen": format(unrounded, "f"),
                "obligations": [],
            },
        )
    payable = obligation(
        version,
        name="interest",
        amount=computed.interest_fen,
        account="2231",
        normal="credit",
        counterparty=agreement.lender_id,
        cashflow="interest_payments",
    )
    return outcome(
        [Line("5603", debit=computed.interest_fen), Line("2231", credit=computed.interest_fen)],
        {
            "interest_fen": computed.interest_fen,
            "principal_fen": outstanding,
            "actual_days": computed.actual_days,
            "annual_rate_percent": str(agreement.annual_rate_percent),
            "unrounded_interest_fen": format(computed.unrounded_interest_fen, "f"),
        },
        [payable],
    )


def required_reads(period: YearMonth):
    before = YearMonth.from_ordinal(period.ordinal + 1)
    return tuple(
        Read(
            source,
            kind,
            "loan-principal" if kind in {"payment", "cash_payment", "employee_advance"} else "*",
            before,
        )
        for source in ("fact", "calculation")
        for kind in (
            "asset",
            "asset_activation",
            "asset_consumption",
            "asset_disposal",
            "loan_agreement",
            "loan_drawdown",
            "loan_interest",
            "payment",
            "cash_payment",
            "employee_advance",
        )
        if source == "fact" or kind != "loan_agreement"
    )


def required_work(period: YearMonth, context):
    """Derive obligations only from explicitly confirmed asset/loan lifecycles."""
    rows = {(read.source, read.kind): context.select(read) for read in required_reads(period)}
    issues = _required_asset_work(period, rows)
    issues.extend(_required_loan_work(period, rows))
    return issues


def _required_asset_work(period, rows):
    assets = {row.subject_id: row.fact for row in rows[("fact", "asset")]}
    posted_activations = {row.fact_id for row in rows[("calculation", "asset_activation")]}
    disposal_facts = {row.subject_id: row for row in rows[("fact", "asset_disposal")]}
    disposed = {
        disposal_facts[row.subject_id].fact.asset_id
        for row in rows[("calculation", "asset_disposal")]
        if row.subject_id in disposal_facts and row.fact_id == disposal_facts[row.subject_id].id
    }
    consumed = {}
    for result in rows[("calculation", "asset_consumption")]:
        asset_id = result.values["asset_id"]
        if asset_id not in consumed or result.period > consumed[asset_id].period:
            consumed[asset_id] = result
    issues = []
    for activation in rows[("fact", "asset_activation")]:
        fact = activation.fact
        if fact.asset_id in disposed:
            continue
        asset = assets.get(fact.asset_id)
        if asset is None or activation.id not in posted_activations:
            issues.append(
                {
                    "field": "asset_activation",
                    "asset_id": fact.asset_id,
                    "message": "已有启用事实尚未形成完整的资产取得及启用结果",
                }
            )
            continue
        start = fact.period.ordinal + (1 if asset.asset_type == "fixed" else 0)
        end = min(period.ordinal, start + fact.useful_life_months - 1)
        if end < start:
            continue
        latest = consumed.get(fact.asset_id)
        if (
            latest is None
            or latest.period.ordinal != end
            or latest.values["completed_months"] != end - start + 1
        ):
            issues.append(
                {
                    "field": "asset_consumption",
                    "asset_id": fact.asset_id,
                    "period": str(YearMonth.from_ordinal(end)),
                    "message": "已启用资产须连续摊折至本期或已确定的寿命末月",
                }
            )
    return issues


def _required_loan_work(period, rows):
    agreements = {row.subject_id: row.fact for row in rows[("fact", "loan_agreement")]}
    drawdowns = {row.subject_id: row for row in rows[("fact", "loan_drawdown")]}
    posted_drawdowns = {row.fact_id for row in rows[("calculation", "loan_drawdown")]}
    posted_payments = {
        row.fact_id for kind in ("payment", "cash_payment") for row in rows[("calculation", kind)]
    }
    interest_facts = {row.subject_id: row for row in rows[("fact", "loan_interest")]}
    intervals = {}
    for result in rows[("calculation", "loan_interest")]:
        source = interest_facts.get(result.subject_id)
        if source is not None and source.id == result.fact_id:
            intervals.setdefault(source.fact.drawdown_id, []).append(
                (
                    date.fromisoformat(source.fact.period_start),
                    date.fromisoformat(source.fact.period_end_exclusive),
                )
            )
    repayments = {}
    for payment in (*rows[("fact", "payment")], *rows[("fact", "cash_payment")]):
        if payment.id not in posted_payments or payment.fact.direction != "outflow":
            continue
        for allocation in payment.fact.allocations:
            if allocation.source_kind == "loan_drawdown" and allocation.obligation == "principal":
                repayments.setdefault(allocation.source_id, []).append(
                    (date.fromisoformat(payment.fact.actual_date), allocation.amount_fen)
                )
    posted_advances = {row.fact_id for row in rows[("calculation", "employee_advance")]}
    for advance in rows[("fact", "employee_advance")]:
        if advance.id not in posted_advances:
            continue
        for allocation in advance.fact.sources:
            if allocation.source_kind == "loan_drawdown" and allocation.obligation == "principal":
                repayments.setdefault(allocation.source_id, []).append(
                    (
                        date.fromisoformat(advance.fact.actual_creditor_payment_date),
                        allocation.amount_fen,
                    )
                )
    next_month = date.fromisoformat(str(YearMonth.from_ordinal(period.ordinal + 1)) + "-01")
    issues = []
    for subject, version in drawdowns.items():
        fact = version.fact
        agreement = agreements.get(fact.agreement_id)
        if agreement is None or version.id not in posted_drawdowns:
            issues.append(
                {
                    "field": "loan_drawdown",
                    "drawdown_id": subject,
                    "message": "真实放款须有完整合同及正式本金确认",
                }
            )
            continue
        start = date.fromisoformat(fact.actual_date)
        end = min(next_month, date.fromisoformat(agreement.maturity_date))
        payments = sorted(repayments.get(subject, ()))
        boundaries = sorted({start, end, *(day for day, _ in payments if start < day < end)})
        posted = sorted(intervals.get(subject, ()))
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            principal = fact.principal_fen - sum(amount for day, amount in payments if day <= left)
            if principal <= 0:
                continue
            covered = left
            for first, last in posted:
                if first <= covered < last:
                    covered = last
                if covered >= right:
                    break
            if covered < right:
                issues.append(
                    {
                        "field": "loan_interest",
                        "drawdown_id": subject,
                        "period_start": covered.isoformat(),
                        "period_end_exclusive": right.isoformat(),
                        "message": "真实未清偿本金对应的计息区间尚未完整确认；零分利息也须保存结果",
                    }
                )
                break
    return issues


def register(registry: Registry) -> None:
    for model, evaluator in (
        (AssetAcquisition, calculate_acquisition),
        (AssetActivation, calculate_activation),
        (AssetConsumption, calculate_consumption),
        (AssetDisposal, calculate_disposal),
        (LoanAgreement, None),
        (LoanDrawdown, calculate_drawdown),
        (LoanInterest, calculate_interest),
    ):
        registry.register(model, evaluator)
    registry.register_readiness("assets_and_financing", required_reads, required_work)
