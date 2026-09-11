"""Pure asset and financing modules sharing the common version/publishing path."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Context as DecimalContext
from decimal import Decimal, localcontext
from types import SimpleNamespace
from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

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
from .money import ACTUAL_PAYMENT_KINDS
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

    def reads_for(self, subject_id):
        return (
            *self.reads(),
            Read("fact", "reimbursed_asset_batch", f"accepted-asset:{subject_id}"),
        )


def calculate_acquisition(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetAcquisition = version.fact
    if ctx.facts("reimbursed_asset_batch", f"accepted-asset:{version.subject_id}"):
        raise KernelError("duplicate_asset_acceptance", "同一资产不能重复验收或承接")
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


class ReimbursementCreditor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    employee_id: Identifier
    amount_fen: PositiveFen


class AcceptedAssetCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    asset_id: Identifier
    asset_type: Literal["fixed", "intangible"]
    cost_fen: PositiveFen


class ReimbursedAssetBatch(Fact):
    """An accepted asset list and creditor totals, without an invented allocation matrix."""

    kind: ClassVar[str] = "reimbursed_asset_batch"
    cost_fen: PositiveFen
    company_acceptance_confirmed: Literal[True]
    assets: tuple[AcceptedAssetCost, ...] = Field(min_length=1)
    creditors: tuple[ReimbursementCreditor, ...] = Field(min_length=1)
    acquisition_date: ActualDate | None = None

    @model_validator(mode="after")
    def accepted_totals(self):
        if self.acquisition_date is not None and self.acquisition_date.period != self.period:
            raise ValueError("company acquisition day must belong to acceptance month")
        if sum_fen(row.cost_fen for row in self.assets) != self.cost_fen:
            raise ValueError("accepted asset list must equal the batch cost")
        if sum_fen(row.amount_fen for row in self.creditors) != self.cost_fen:
            raise ValueError("accepted creditor totals must equal the batch cost")
        if len({row.asset_id for row in self.assets}) != len(self.assets):
            raise ValueError("each accepted asset must be listed once")
        if len({row.employee_id for row in self.creditors}) != len(self.creditors):
            raise ValueError("each reimbursement creditor must be listed once")
        return self

    def scopes(self):
        return (str(self.period), *(f"accepted-asset:{row.asset_id}" for row in self.assets))

    def reads(self):
        return tuple(
            read
            for row in self.assets
            for read in (
                Read("fact", self.kind, f"accepted-asset:{row.asset_id}"),
                Read("fact", "reimbursed_asset", f"direct-accepted-asset:{row.asset_id}"),
                Read("fact", "asset", f"@{row.asset_id}"),
                Read("fact", "opening_asset", f"@{row.asset_id}"),
            )
        )


def calculate_reimbursed_asset_batch(version: FactVersion, ctx: Context) -> Outcome:
    fact: ReimbursedAssetBatch = version.fact
    for row in fact.assets:
        if (
            any(
                peer.subject_id != version.subject_id
                for peer in ctx.facts(fact.kind, f"accepted-asset:{row.asset_id}")
            )
            or ctx.facts("asset", f"@{row.asset_id}")
            or ctx.facts("reimbursed_asset", f"direct-accepted-asset:{row.asset_id}")
            or ctx.facts("opening_asset", f"@{row.asset_id}")
        ):
            raise KernelError("duplicate_asset_acceptance", "同一资产不能重复验收或承接")
    payables = [
        obligation(
            version,
            name=row.employee_id,
            amount=row.amount_fen,
            account="224101",
            normal="credit",
            counterparty=row.employee_id,
            cashflow="asset_acquisition",
        )
        for row in sorted(fact.creditors, key=lambda row: row.employee_id)
    ]
    lines = []
    for asset_type, account in (("fixed", "1604"), ("intangible", "189901")):
        amount = sum_fen(row.cost_fen for row in fact.assets if row.asset_type == asset_type)
        if amount:
            lines.append(Line(account, debit=amount))
    lines.append(Line("224101", credit=fact.cost_fen))
    base = outcome(lines, {"cost_fen": fact.cost_fen}, payables)
    return Outcome(
        base.lines,
        base.values,
        base.balances
        + tuple(
            BalanceEffect(f"asset:{row.asset_id}:carrying", row.cost_fen, "asset")
            for row in fact.assets
        ),
    )


class ReimbursedAsset(Fact):
    """Company acceptance of an asset funded personally before reimbursement.

    The recognition month is required; an exact company acquisition day is only
    supplied when known. Personal shopping dates remain in the original evidence.
    Accepting the claim establishes the employee debt, without recording cash.
    """

    kind: ClassVar[str] = "reimbursed_asset"
    identity_fields: ClassVar[tuple[str, ...]] = ("asset_type",)
    asset_type: Literal["fixed", "intangible"]
    cost_fen: PositiveFen
    company_acceptance_confirmed: Literal[True]
    creditors: tuple[ReimbursementCreditor, ...] = ()
    acceptance_id: Identifier | None = Field(
        default=None, description="整批验收来源；采用批次时不再提交逐资产债权人分摊。"
    )
    acquisition_date: ActualDate | None = Field(
        default=None,
        description="公司取得日，有准确依据时填写；未知日可使用核算月份，个人采购日不能替代。",
    )

    @model_validator(mode="after")
    def accepted_costs(self):
        if self.acquisition_date is not None and self.acquisition_date.period != self.period:
            raise ValueError("company acquisition day must belong to recognition month")
        if self.acceptance_id is not None and self.creditors:
            raise ValueError("batch acceptance cannot create duplicate direct creditor claims")
        if (
            self.acceptance_id is None
            and sum_fen(row.amount_fen for row in self.creditors) != self.cost_fen
        ):
            raise ValueError("accepted employee costs must exactly equal asset cost")
        if len({row.employee_id for row in self.creditors}) != len(self.creditors):
            raise ValueError("each reimbursement creditor must be listed once")
        return self

    def reads(self):
        return (
            (
                Read("fact", "reimbursed_asset_batch", f"@{self.acceptance_id}"),
                Read("calculation", "reimbursed_asset_batch", f"@{self.acceptance_id}"),
            )
            if self.acceptance_id is not None
            else ()
        )

    def reads_for(self, subject_id):
        return (
            *self.reads(),
            Read("fact", "reimbursed_asset_batch", f"accepted-asset:{subject_id}"),
        )

    def scopes_for(self, subject_id):
        return (
            *self.scopes(),
            *((f"direct-accepted-asset:{subject_id}",) if self.acceptance_id is None else ()),
        )


def calculate_reimbursed_asset(version: FactVersion, ctx: Context) -> Outcome:
    fact: ReimbursedAsset = version.fact
    occupied = ctx.facts("reimbursed_asset_batch", f"accepted-asset:{version.subject_id}")
    if fact.acceptance_id is None:
        if occupied:
            raise KernelError("duplicate_asset_acceptance", "同一资产不能重复验收或承接")
    elif len(occupied) != 1 or occupied[0].subject_id != fact.acceptance_id:
        raise KernelError("asset_acceptance_conflict", "资产卡片必须沿用唯一验收批次")
    if fact.acceptance_id is not None:
        accepted = ctx.one("reimbursed_asset_batch", f"@{fact.acceptance_id}")
        posted = ctx.calculations("reimbursed_asset_batch", f"@{fact.acceptance_id}")
        if len(posted) != 1 or posted[0].fact_id != accepted.id:
            raise NeedsInformation("acceptance_id", "需要已发布的资产验收批次")
        row = next(
            (item for item in accepted.fact.assets if item.asset_id == version.subject_id), None
        )
        if row is None or (
            row.asset_type != fact.asset_type
            or row.cost_fen != fact.cost_fen
            or fact.period != accepted.fact.period
            or fact.acquisition_date != accepted.fact.acquisition_date
        ):
            raise KernelError(
                "asset_acceptance_conflict", "资产卡片须复用验收清单的身份、成本及取得期间"
            )
        return Outcome(
            (),
            {
                "asset_type": fact.asset_type,
                "cost_fen": fact.cost_fen,
                "acceptance_fact_id": accepted.id,
                "obligations": [],
            },
        )
    pending = "1604" if fact.asset_type == "fixed" else "189901"
    payables = [
        obligation(
            version,
            name=row.employee_id,
            amount=row.amount_fen,
            account="224101",
            normal="credit",
            counterparty=row.employee_id,
            cashflow="asset_acquisition",
        )
        for row in sorted(fact.creditors, key=lambda row: row.employee_id)
    ]
    base = outcome(
        [Line(pending, debit=fact.cost_fen), Line("224101", credit=fact.cost_fen)],
        {"cost_fen": fact.cost_fen, "asset_type": fact.asset_type},
        payables,
    )
    return Outcome(
        base.lines,
        base.values,
        (
            *base.balances,
            BalanceEffect(f"asset:{version.subject_id}:carrying", fact.cost_fen, "asset"),
        ),
    )


def _acquisitions(ctx, asset_id):
    return (*ctx.facts("asset", f"@{asset_id}"), *ctx.facts("reimbursed_asset", f"@{asset_id}"))


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
            Read("fact", "reimbursed_asset", f"@{self.asset_id}"),
            Read("calculation", "reimbursed_asset", f"@{self.asset_id}"),
            Read("fact", self.kind, f"asset:{self.asset_id}"),
        )


def calculate_activation(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetActivation = version.fact
    sources = _acquisitions(ctx, fact.asset_id)
    if len(sources) != 1:
        raise NeedsInformation("asset_id", "需要唯一已确认的资产取得来源")
    source = sources[0].fact
    acquisition = ctx.calculations(source.kind, f"@{fact.asset_id}")
    if len(acquisition) != 1 or acquisition[0].fact_id != sources[0].id:
        raise NeedsInformation("asset_id", "需要已入账的资产取得来源")
    alternatives = ctx.facts("asset_activation", f"asset:{fact.asset_id}")
    if any(row.subject_id != version.subject_id for row in alternatives):
        raise KernelError("duplicate_asset_activation", "同一资产只能有一个启用身份")
    if fact.period < source.period or (
        source.acquisition_date is not None and fact.in_use_date < source.acquisition_date
    ):
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
            Read("fact", "reimbursed_asset", f"@{self.asset_id}"),
            Read("fact", "opening_asset", f"@{self.asset_id}"),
            Read("calculation", "opening_asset", f"@{self.asset_id}"),
            Read("fact", "asset_activation", f"asset:{self.asset_id}"),
            Read("calculation", "asset_activation", f"asset:{self.asset_id}"),
            Read("fact", "asset_disposal", f"asset:{self.asset_id}"),
            Read("fact", self.kind, f"asset:{self.asset_id}:{self.period}"),
        ]
        previous = previous_month(self.period)
        if previous is not None:
            reads.append(Read("calculation", self.kind, f"asset:{self.asset_id}:{previous}"))
        return tuple(reads)


def _asset_basis(ctx, asset_id):
    acquisitions = _acquisitions(ctx, asset_id)
    openings = ctx.facts("opening_asset", f"@{asset_id}")
    if len(acquisitions) + len(openings) != 1:
        raise NeedsInformation("asset_id", "需要唯一的资产取得或已核验接续卡片")
    if openings:
        card = openings[0]
        calculated = ctx.calculations("opening_asset", f"@{asset_id}")
        if len(calculated) != 1 or calculated[0].fact_id != card.id:
            raise NeedsInformation("opening_package", "期初资产卡片尚未由完整清单核验发布")
        if ctx.facts("asset_activation", f"asset:{asset_id}"):
            raise KernelError("duplicate_asset_activation", "期初在用资产不得再次确认启用")
        activation = SimpleNamespace(**(dict(card.fact) | {"period": card.fact.in_use_date.period}))
        return card.fact, activation, calculated[0].values, card.fact
    activation = ctx.one("asset_activation", f"asset:{asset_id}").fact
    calculated = ctx.calculations("asset_activation", f"asset:{asset_id}")
    if len(calculated) != 1:
        raise NeedsInformation("asset_activation", "需要资产正式启用结果")
    return acquisitions[0].fact, activation, calculated[0].values, None


def calculate_consumption(version: FactVersion, ctx: Context) -> Outcome:
    fact: AssetConsumption = version.fact
    asset, activation, activation_values, opening_card = _asset_basis(ctx, fact.asset_id)
    duplicates = ctx.facts(fact.kind, f"asset:{fact.asset_id}:{fact.period}")
    if any(item.subject_id != version.subject_id for item in duplicates):
        raise KernelError("duplicate_consumption", "资产每月只能确认一次摊折")
    disposals = ctx.facts("asset_disposal", f"asset:{fact.asset_id}")
    if any(item.fact.period < fact.period for item in disposals):
        raise KernelError("asset_already_disposed", "已经处置的资产不能继续摊折")
    start = YearMonth(activation_values["consumption_start"])
    if opening_card is not None and fact.period < opening_card.period:
        raise KernelError("before_opening", "接续资产不能在建账边界之前补造摊折")
    completed = fact.period.ordinal - start.ordinal
    if completed < 0 or completed >= activation.useful_life_months:
        raise KernelError("consumption_outside_life", "摊折月份不在确定的使用寿命内")
    previous = previous_month(fact.period)
    prior = ctx.calculations(fact.kind, f"asset:{fact.asset_id}:{previous}") if previous else ()
    first_continued = opening_card is not None and fact.period == opening_card.period
    if completed and len(prior) != 1 and not first_continued:
        raise NeedsInformation(
            "previous_consumption", "须先确认连续的上月摊折", sources=(str(previous),)
        )
    opening = (
        opening_card.accumulated_fen
        if first_continued
        else prior[0].values["closing_accumulated_fen"]
        if completed
        else 0
    )
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
            Read("fact", "reimbursed_asset", f"@{self.asset_id}"),
            Read("fact", "opening_asset", f"@{self.asset_id}"),
            Read("calculation", "opening_asset", f"@{self.asset_id}"),
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
    asset, activation, _, opening_card = _asset_basis(ctx, fact.asset_id)
    if opening_card is not None and fact.period < opening_card.period:
        raise KernelError("before_opening", "接续资产不能在建账边界之前补造处置")
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
    completed = (
        latest.values["completed_months"]
        if latest
        else (opening_card.completed_months if opening_card else 0)
    )
    if completed != expected_months:
        raise NeedsInformation("current_consumption", "处置前需要连续累计摊折至当月或寿命末月")
    accumulated = (
        latest.values["closing_accumulated_fen"]
        if latest
        else (opening_card.accumulated_fen if opening_card else 0)
    )
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
        opening_scope = f"payment:opening_loan:{self.drawdown_id}:principal"
        before = YearMonth.from_ordinal(self.period.ordinal + 1)
        return (
            Read("fact", "loan_agreement", f"@{self.agreement_id}"),
            Read("fact", "loan_drawdown", f"@{self.drawdown_id}"),
            Read("calculation", "loan_drawdown", f"@{self.drawdown_id}"),
            Read("fact", "opening_loan", f"@{self.drawdown_id}"),
            Read("calculation", "opening_loan", f"@{self.drawdown_id}"),
            *(Read("fact", kind, repayment_scope, before) for kind in ACTUAL_PAYMENT_KINDS),
            Read("fact", "employee_advance", repayment_scope, before),
            *(
                Read("fact", kind, opening_scope, before)
                for kind in (*ACTUAL_PAYMENT_KINDS, "employee_advance")
            ),
            Read("fact", self.kind, f"loan:{self.drawdown_id}"),
        )


def calculate_interest(version: FactVersion, ctx: Context) -> Outcome:
    fact: LoanInterest = version.fact
    agreement: LoanAgreement = ctx.one("loan_agreement", f"@{fact.agreement_id}").fact
    sources = (
        *ctx.facts("loan_drawdown", f"@{fact.drawdown_id}"),
        *ctx.facts("opening_loan", f"@{fact.drawdown_id}"),
    )
    if len(sources) != 1:
        raise NeedsInformation("drawdown_id", "需要唯一的真实放款或期初贷款本金来源")
    source = sources[0]
    drawdown = source.fact
    results = ctx.calculations(drawdown.kind, f"@{fact.drawdown_id}")
    if len(results) != 1 or results[0].fact_id != source.id:
        raise NeedsInformation("drawdown_id", "本金来源当前版本尚未正式核验发布")
    principal_start = (
        drawdown.interest_start if drawdown.kind == "opening_loan" else drawdown.actual_date
    )
    if drawdown.agreement_id != fact.agreement_id:
        raise KernelError("loan_agreement_conflict", "计息合同须为本次真实放款合同")
    if fact.period_start < principal_start or fact.period_end_exclusive > agreement.maturity_date:
        raise KernelError("interest_outside_contract", "计息区间不在放款和到期日之间")
    for other in ctx.facts(fact.kind, f"loan:{fact.drawdown_id}"):
        if other.subject_id == version.subject_id:
            continue
        if (
            other.fact.period_start < fact.period_end_exclusive
            and fact.period_start < other.fact.period_end_exclusive
        ):
            raise KernelError("overlapping_interest", "合同计息区间不能重复")
    repayment_scope = f"payment:{drawdown.kind}:{fact.drawdown_id}:principal"
    before = YearMonth.from_ordinal(fact.period.ordinal + 1)
    payments = (
        payment
        for kind in ACTUAL_PAYMENT_KINDS
        for payment in ctx.select(Read("fact", kind, repayment_scope, before))
    )
    outstanding = drawdown.principal_fen
    for payment in payments:
        if payment.fact.actual_date >= fact.period_end_exclusive:
            continue
        allocations = [a for a in payment.fact.allocations if a.scope == repayment_scope]
        if payment.fact.direction != "outflow" or payment.fact.actual_date < principal_start:
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
        if actual_day < principal_start:
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
            "loan-principal" if kind in (*ACTUAL_PAYMENT_KINDS, "employee_advance") else "*",
            before,
        )
        for source in ("fact", "calculation")
        for kind in (
            "asset",
            "reimbursed_asset",
            "reimbursed_asset_batch",
            "opening_asset",
            "asset_activation",
            "asset_consumption",
            "asset_disposal",
            "loan_agreement",
            "loan_drawdown",
            "opening_loan",
            "loan_interest",
            *ACTUAL_PAYMENT_KINDS,
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
    assets = {
        row.subject_id: row.fact
        for kind in ("asset", "reimbursed_asset")
        for row in rows[("fact", kind)]
    }
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
    posted_cards = {row.fact_id for row in rows[("calculation", "reimbursed_asset")]}
    card_facts = {row.subject_id: row for row in rows[("fact", "reimbursed_asset")]}
    for batch in rows[("fact", "reimbursed_asset_batch")]:
        for item in batch.fact.assets:
            card = card_facts.get(item.asset_id)
            if (
                card is None
                or card.fact.acceptance_id != batch.subject_id
                or card.id not in posted_cards
            ):
                issues.append(
                    {
                        "field": "reimbursed_asset",
                        "asset_id": item.asset_id,
                        "message": "整批验收清单中的每项资产都须形成相应正式卡片",
                    }
                )
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
    posted_openings = {row.fact_id for row in rows[("calculation", "opening_asset")]}
    for card in rows[("fact", "opening_asset")]:
        if card.subject_id in disposed:
            continue
        if card.id not in posted_openings:
            issues.append(
                {
                    "field": "opening_package",
                    "asset_id": card.subject_id,
                    "message": "期初资产卡片尚未由完整清单发布",
                }
            )
            continue
        fact = card.fact
        remaining = fact.useful_life_months - fact.completed_months
        if remaining <= 0:
            continue
        end = min(period.ordinal, fact.period.ordinal + remaining - 1)
        latest = consumed.get(card.subject_id)
        if latest is None or latest.period.ordinal != end:
            issues.append(
                {
                    "field": "asset_consumption",
                    "asset_id": card.subject_id,
                    "period": str(YearMonth.from_ordinal(end)),
                    "message": "期初资产须从建账月连续确认摊折",
                }
            )
    return issues


def _required_loan_work(period, rows):
    agreements = {row.subject_id: row.fact for row in rows[("fact", "loan_agreement")]}
    drawdowns = {
        row.subject_id: row
        for kind in ("loan_drawdown", "opening_loan")
        for row in rows[("fact", kind)]
    }
    posted_drawdowns = {
        row.fact_id
        for kind in ("loan_drawdown", "opening_loan")
        for row in rows[("calculation", kind)]
    }
    posted_payments = {
        row.fact_id for kind in ACTUAL_PAYMENT_KINDS for row in rows[("calculation", kind)]
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
    for payment in (row for kind in ACTUAL_PAYMENT_KINDS for row in rows[("fact", kind)]):
        if payment.id not in posted_payments or payment.fact.direction != "outflow":
            continue
        for allocation in payment.fact.allocations:
            if (
                allocation.source_kind in {"loan_drawdown", "opening_loan"}
                and allocation.obligation == "principal"
            ):
                repayments.setdefault(allocation.source_id, []).append(
                    (date.fromisoformat(payment.fact.actual_date), allocation.amount_fen)
                )
    posted_advances = {row.fact_id for row in rows[("calculation", "employee_advance")]}
    for advance in rows[("fact", "employee_advance")]:
        if advance.id not in posted_advances:
            continue
        for allocation in advance.fact.sources:
            if (
                allocation.source_kind in {"loan_drawdown", "opening_loan"}
                and allocation.obligation == "principal"
            ):
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
        start = date.fromisoformat(
            fact.interest_start if fact.kind == "opening_loan" else fact.actual_date
        )
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
        (ReimbursedAsset, calculate_reimbursed_asset),
        (ReimbursedAssetBatch, calculate_reimbursed_asset_batch),
        (AssetActivation, calculate_activation),
        (AssetConsumption, calculate_consumption),
        (AssetDisposal, calculate_disposal),
        (LoanAgreement, None),
        (LoanDrawdown, calculate_drawdown),
        (LoanInterest, calculate_interest),
    ):
        registry.register(model, evaluator)
    registry.register_readiness("assets_and_financing", required_reads, required_work)
