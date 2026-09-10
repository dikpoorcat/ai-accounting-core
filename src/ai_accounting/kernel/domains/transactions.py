"""Business facts for transactions and actual-funds settlement.

The only account selections are closed templates below.  Callers identify a
business obligation, never an account or a debit/credit line.  Actual payments
are independent immutable facts so recalculating an accrual cannot rewrite a
bank payment.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ..contracts import (
    BalanceEffect,
    Claim,
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
from .taxes import VatPolicy, split_tax_inclusive

Identifier = Annotated[
    str, Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
ExpenseClass = Literal["administration", "sales", "service", "bank_fee"]
EXPENSE_ACCOUNTS = {
    "administration": "5602",
    "sales": "5601",
    "service": "5401",
    "bank_fee": "5603",
}


def bank_scopes(period, *bank_accounts):
    return (
        str(period),
        *(
            scope
            for account in bank_accounts
            for scope in (f"bank:{account}", f"bank:{account}:{period}")
        ),
    )


def obligation(
    version: FactVersion,
    *,
    amount: int,
    account: str,
    normal: str,
    name: str = "primary",
    counterparty: str | None = None,
    cashflow: str = "operating",
    category: str | None = None,
) -> dict:
    return {
        "name": name,
        "key": f"{version.fact.kind}:{version.subject_id}:{name}",
        "amount_fen": amount,
        "account": account,
        "normal": normal,
        "category": category or ("receivable" if normal == "debit" else "payable"),
        "counterparty_id": counterparty,
        "cashflow": cashflow,
    }


def outcome(
    lines: list[Line],
    values: dict,
    obligations: list[dict],
    *,
    explanation: tuple[dict, ...] = (),
) -> Outcome:
    return Outcome(
        lines=tuple(lines),
        values=values | {"obligations": obligations},
        balances=tuple(
            BalanceEffect(x["key"], x["amount_fen"], x["category"]) for x in obligations
        ),
        explanation=explanation,
    )


class ServiceSale(Fact):
    kind: ClassVar[str] = "service_sale"
    customer_id: Identifier
    gross_fen: PositiveFen
    fulfillment_date: ActualDate | None = Field(
        default=None, description="可省略的实际履约日；收入核算使用 period，不用月末补造实际日"
    )
    vat_policy_id: Identifier | None = None
    exemption_eligible: StrictBool | None = None
    tax_obligation_period: YearMonth | None = Field(
        default=None, description="明确的增值税纳税义务所属月，核算来源；不得从履约日推断"
    )
    tax_obligation_date: ActualDate | None = Field(
        default=None, description="税点需要日精度时提供；须与明确的税所属月一致"
    )

    def scopes(self):
        return (str(self.period), f"customer:{self.customer_id}", f"tax:{self.period}")

    def reads(self):
        return (Read("fact", "vat_policy", f"@{self.vat_policy_id}"),) if self.vat_policy_id else ()


def calculate_sale(version: FactVersion, ctx: Context) -> Outcome:
    fact: ServiceSale = version.fact
    if fact.fulfillment_date is not None and fact.fulfillment_date.period != fact.period:
        raise KernelError("recognition_period_conflict", "履约日期必须属于收入确认月份")
    if fact.vat_policy_id is None:
        raise NeedsInformation(
            "vat_policy_id", "需要已确认的有效增值税规则", sources=("vat_policy",)
        )
    if fact.exemption_eligible is None:
        raise NeedsInformation("exemption_eligible", "需要确认本项销售的免税适用事实")
    if fact.tax_obligation_period is None:
        raise NeedsInformation(
            "tax_obligation_period", "需要明确纳税义务所属月", precision=("month", "day")
        )
    if fact.tax_obligation_period < fact.period:
        raise KernelError(
            "tax_before_revenue_source", "税点早于收入确认的业务须复用已确认预收税务来源"
        )
    policy_fact = ctx.one("vat_policy", f"@{fact.vat_policy_id}")
    policy: VatPolicy = policy_fact.fact.policy
    _tax_policy_for_month(policy, fact.tax_obligation_period, fact.tax_obligation_date)
    net, vat = split_tax_inclusive(fact.gross_fen, policy.rate_percent)
    due = fact.tax_obligation_period == fact.period
    lines = [Line("1122", debit=fact.gross_fen)]
    if net:
        lines.append(Line("5001", credit=net))
    if vat:
        lines.append(Line("222101" if due else "222104", credit=vat))
    return outcome(
        lines,
        {
            "net_sales_fen": net if due else 0,
            "accrued_vat_fen": vat if due else 0,
            "recognized_revenue_fen": net,
            "vat_fen": vat,
            "vat_recognition": "payable" if due else "deferred",
            "tax_obligation_period": str(fact.tax_obligation_period),
            "tax_obligation_date": fact.tax_obligation_date,
            "exemption_eligible": fact.exemption_eligible,
            "policy_version": policy.version,
            "gross_fen": fact.gross_fen,
            "rate_percent": str(policy.rate_percent),
        },
        [
            obligation(
                version,
                amount=fact.gross_fen,
                account="1122",
                normal="debit",
                counterparty=fact.customer_id,
                cashflow="customer_receipts",
            )
        ],
    )


def _tax_policy_for_month(policy, period: YearMonth, actual: ActualDate | None):
    if actual is not None:
        if actual.period != period:
            raise KernelError("tax_precision_conflict", "实际纳税义务日与税所属月不一致")
        policy.require_effective(date.fromisoformat(actual))
        return
    start = date.fromisoformat(f"{period}-01")
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    try:
        policy.require_effective(start, end)
    except ValueError as error:
        raise NeedsInformation(
            "tax_obligation_date",
            "规则未覆盖整个税所属月，需要足以选择规则的实际税点日",
            precision=("day",),
        ) from error


def _through_month(source: str, kind: str, key: str, period: YearMonth) -> Read:
    after = YearMonth.from_ordinal(period.ordinal + 1) if period != "9999-12" else None
    return Read(source, kind, key, after)


class Expense(Fact):
    kind: ClassVar[str] = "expense"
    counterparty_id: Identifier
    amount_fen: PositiveFen
    expense_class: ExpenseClass
    creditor_kind: Literal["supplier", "employee"]

    def scopes(self):
        return (str(self.period), f"party:{self.counterparty_id}")


def calculate_expense(version: FactVersion, ctx: Context) -> Outcome:
    fact: Expense = version.fact
    payable = "2202" if fact.creditor_kind == "supplier" else "224101"
    return outcome(
        [
            Line(EXPENSE_ACCOUNTS[fact.expense_class], debit=fact.amount_fen),
            Line(payable, credit=fact.amount_fen),
        ],
        {
            "expense_class": fact.expense_class,
            "creditor_kind": fact.creditor_kind,
            "counterparty_id": fact.counterparty_id,
        },
        [
            obligation(
                version,
                amount=fact.amount_fen,
                account=payable,
                normal="credit",
                counterparty=fact.counterparty_id,
                cashflow="operating_payments",
            )
        ],
    )


class ProjectCost(Fact):
    kind: ClassVar[str] = "project_cost"
    project_id: Identifier
    supplier_id: Identifier
    amount_fen: PositiveFen
    project_nature: Literal["purchased_intangible", "internal_development"]
    capitalization_conditions_confirmed: StrictBool | None = None

    def scopes(self):
        return (str(self.period), f"project:{self.project_id}")


def calculate_project_cost(version: FactVersion, ctx: Context) -> Outcome:
    fact: ProjectCost = version.fact
    if fact.capitalization_conditions_confirmed is not True:
        raise NeedsInformation(
            "capitalization_conditions_confirmed", "需要已满足资本化条件的事实依据"
        )
    account = "189901" if fact.project_nature == "purchased_intangible" else "4301"
    result = outcome(
        [Line(account, debit=fact.amount_fen), Line("2202", credit=fact.amount_fen)],
        {
            "project_id": fact.project_id,
            "capitalized_fen": fact.amount_fen,
            "capital_account": account,
            "project_nature": fact.project_nature,
        },
        [
            obligation(
                version,
                amount=fact.amount_fen,
                account="2202",
                normal="credit",
                counterparty=fact.supplier_id,
                cashflow="asset_acquisition",
            )
        ],
    )
    return Outcome(
        result.lines,
        result.values,
        result.balances
        + (BalanceEffect(f"project-cost:{version.subject_id}", fact.amount_fen, "asset"),),
    )


class ProjectCostSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_id: Identifier
    amount_fen: PositiveFen


def project_cost_reads(sources: tuple[ProjectCostSource, ...]) -> tuple[Read, ...]:
    return tuple(
        sorted(
            {
                read
                for source in sources
                for read in (
                    Read("calculation", "project_cost", f"@{source.source_id}"),
                    Read("fact", "project_release", f"cost:{source.source_id}"),
                    Read("fact", "asset", f"cost:{source.source_id}"),
                )
            }
        )
    )


def consume_project_costs(
    version: FactVersion, ctx: Context, sources: tuple[ProjectCostSource, ...]
):
    """Check source capacities once and return frozen credit lines and effects."""
    lines, effects, project_ids = [], [], set()
    for source in sources:
        values = ctx.calculations("project_cost", f"@{source.source_id}")
        if len(values) != 1:
            raise NeedsInformation(
                "project_sources", "需要已确认的项目成本来源", sources=(source.source_id,)
            )
        prior = values[0]
        if prior.period > version.fact.period:
            raise KernelError("project_use_before_cost", "项目成本转出不能早于确认月份")
        total = source.amount_fen
        for kind in ("project_release", "asset"):
            for other in ctx.facts(kind, f"cost:{source.source_id}"):
                if other.subject_id == version.subject_id:
                    continue
                total = sum_fen(
                    (
                        total,
                        *(
                            item.amount_fen
                            for item in other.fact.project_sources
                            if item.source_id == source.source_id
                        ),
                    )
                )
        if total > prior.values["capitalized_fen"]:
            raise KernelError(
                "project_cost_overallocated", "分期确认、转费用及形成资产合计不得超过项目成本"
            )
        lines.append(Line(prior.values["capital_account"], credit=source.amount_fen))
        effects.append(
            BalanceEffect(f"project-cost:{source.source_id}", -source.amount_fen, "asset")
        )
        project_ids.add(prior.values["project_id"])
    return lines, effects, project_ids


class ProjectRelease(Fact):
    kind: ClassVar[str] = "project_release"
    project_id: Identifier
    project_sources: tuple[ProjectCostSource, ...] = Field(min_length=1)
    expense_class: Literal["administration", "sales", "service"]

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({s.source_id for s in self.project_sources}) != len(self.project_sources):
            raise ValueError("project source references must be unique")
        return self

    def scopes(self):
        return (
            str(self.period),
            f"project:{self.project_id}",
            *(f"cost:{source.source_id}" for source in self.project_sources),
        )

    def reads(self):
        return project_cost_reads(self.project_sources)


def calculate_project_release(version: FactVersion, ctx: Context) -> Outcome:
    fact: ProjectRelease = version.fact
    credits, effects, projects = consume_project_costs(version, ctx, fact.project_sources)
    if projects != {fact.project_id}:
        raise KernelError("project_identity_conflict", "成本来源必须属于同一明确项目")
    amount = sum_fen(source.amount_fen for source in fact.project_sources)
    return Outcome(
        (Line(EXPENSE_ACCOUNTS[fact.expense_class], debit=amount), *credits),
        {"project_id": fact.project_id, "released_fen": amount},
        tuple(effects),
    )


class PassThrough(Fact):
    kind: ClassVar[str] = "pass_through"
    payer_id: Identifier
    beneficiary_id: Identifier
    amount_fen: PositiveFen
    rights_and_obligation_confirmed: StrictBool | None = None

    def scopes(self):
        return (str(self.period), f"party:{self.beneficiary_id}")


def calculate_pass_through(version: FactVersion, ctx: Context) -> Outcome:
    fact: PassThrough = version.fact
    if fact.rights_and_obligation_confirmed is not True:
        raise NeedsInformation("rights_and_obligation_confirmed", "需要有依据的代收债权和转付义务")
    return outcome(
        [Line("122105", debit=fact.amount_fen), Line("224105", credit=fact.amount_fen)],
        {},
        [
            obligation(
                version,
                name="collection",
                amount=fact.amount_fen,
                account="122105",
                normal="debit",
                counterparty=fact.payer_id,
                cashflow="pass_through_receipts",
            ),
            obligation(
                version,
                name="remittance",
                amount=fact.amount_fen,
                account="224105",
                normal="credit",
                counterparty=fact.beneficiary_id,
                cashflow="pass_through_payments",
            ),
        ],
    )


class Advance(Fact):
    """Confirmed contractual advance; actual cash is independently settled.

    The explicit enforceable-obligation fact prevents an anticipated purchase
    or expected receipt from silently creating an accounting receivable/payable.
    """

    kind: ClassVar[str] = "advance"
    counterparty_id: Identifier
    amount_fen: PositiveFen
    side: Literal["customer", "supplier"]
    contractual_obligation_established: StrictBool | None = None
    vat_due_on_advance: StrictBool | None = None
    vat_policy_id: Identifier | None = None
    exemption_eligible: StrictBool | None = None
    tax_obligation_period: YearMonth | None = None
    tax_obligation_date: ActualDate | None = None

    def scopes(self):
        return (
            str(self.period),
            f"party:{self.counterparty_id}",
            *(
                (f"tax:{self.period}",)
                if self.side == "customer" and self.vat_due_on_advance
                else ()
            ),
        )

    def reads(self):
        return (Read("fact", "vat_policy", f"@{self.vat_policy_id}"),) if self.vat_policy_id else ()


def calculate_advance(version: FactVersion, ctx: Context) -> Outcome:
    fact: Advance = version.fact
    if fact.contractual_obligation_established is not True:
        raise NeedsInformation(
            "contractual_obligation_established", "须有已成立的预收预付权利义务依据"
        )
    if fact.side == "customer" and fact.vat_due_on_advance is None:
        raise NeedsInformation("vat_due_on_advance", "需要明确预收环节是否已到增值税纳税义务时点")
    amount = fact.amount_fen
    net, vat, rate, policy_version = amount, 0, None, None
    if fact.side == "customer" and fact.vat_due_on_advance:
        for field in ("vat_policy_id", "exemption_eligible", "tax_obligation_period"):
            if getattr(fact, field) is None:
                raise NeedsInformation(field, "有税点预收需要明确税务来源和所属月")
        if fact.tax_obligation_period != fact.period:
            raise KernelError("advance_tax_period", "预收税点须属于本次预收确认的纳税义务月")
        policy = ctx.one("vat_policy", f"@{fact.vat_policy_id}").fact.policy
        _tax_policy_for_month(policy, fact.tax_obligation_period, fact.tax_obligation_date)
        net, vat = split_tax_inclusive(amount, policy.rate_percent)
        rate, policy_version = str(policy.rate_percent), policy.version
    if fact.side == "customer":
        lines = [Line("1122", debit=amount)]
        if net:
            lines.append(Line("2203", credit=net))
        if vat:
            lines.append(Line("222101", credit=vat))
        main_account, main_normal, advance_account, advance_normal = (
            "1122",
            "debit",
            "2203",
            "credit",
        )
    else:
        lines = [Line("1123", debit=amount), Line("2202", credit=amount)]
        main_account, main_normal, advance_account, advance_normal = (
            "2202",
            "credit",
            "1123",
            "debit",
        )
    advance_obligation = obligation(
        version,
        name="advance",
        amount=net if fact.side == "customer" else amount,
        account=advance_account,
        normal=advance_normal,
        counterparty=fact.counterparty_id,
    )
    if fact.side == "customer" and fact.vat_due_on_advance:
        # Performance claims consume gross source capacity while this balance
        # carries the net book liability. A fulfillment derives both amounts;
        # generic cash/offset templates cannot perform that allocation.
        advance_obligation["settlement_modes"] = ["fulfillment"]
    return outcome(
        lines,
        {
            "side": fact.side,
            "gross_fen": amount,
            "book_advance_fen": net,
            "vat_due_on_advance": fact.vat_due_on_advance,
            "rate_percent": rate,
            "policy_version": policy_version,
            "vat_fen": vat,
            "net_sales_fen": net if vat or fact.vat_due_on_advance else 0,
            "accrued_vat_fen": vat,
            "exemption_eligible": fact.exemption_eligible,
            "counterparty_id": fact.counterparty_id,
        },
        [
            obligation(
                version,
                amount=amount,
                account=main_account,
                normal=main_normal,
                counterparty=fact.counterparty_id,
            ),
            advance_obligation,
        ],
    )


class AdvanceFulfillment(Fact):
    """Confirmed gross performance; allocation of net revenue is derived."""

    kind: ClassVar[str] = "advance_fulfillment"
    identity_fields: ClassVar[tuple[str, ...]] = ("advance_id",)
    advance_id: Identifier
    fulfilled_gross_fen: PositiveFen
    fulfillment_date: ActualDate | None = None
    vat_policy_id: Identifier | None = None
    exemption_eligible: StrictBool | None = None
    tax_obligation_period: YearMonth | None = None
    tax_obligation_date: ActualDate | None = None

    @property
    def source_scope(self):
        return f"payment:advance:{self.advance_id}:advance"

    def scopes(self):
        return (str(self.period), self.source_scope, f"tax:{self.period}")

    def claims(self):
        return (Claim(self.source_scope, self.fulfilled_gross_fen),)

    def reads(self):
        return (
            Read("calculation", "advance", f"@{self.advance_id}"),
            _through_month("fact", "*", self.source_scope, self.period),
            *(
                (Read("fact", "vat_policy", f"@{self.vat_policy_id}"),)
                if self.vat_policy_id
                else ()
            ),
        )


def _advance_consumption(version: FactVersion, ctx: Context, gross_fen: int):
    fact = version.fact
    candidates = ctx.calculations("advance", f"@{fact.advance_id}")
    if len(candidates) != 1:
        raise NeedsInformation("advance_id", "需要已确认的预收来源", sources=(fact.advance_id,))
    source = candidates[0]
    if source.values["side"] != "customer" or source.period > fact.period:
        raise KernelError("advance_consumption_source", "履约或退款须引用此前已成立的客户预收")
    peers = [
        item
        for item in ctx.select(
            _through_month(
                "fact",
                "*",
                fact.source_scope,
                fact.period,
            )
        )
        if item.subject_id != version.subject_id
    ]
    total_used = sum_fen(
        claim.amount
        for peer in peers
        for claim in peer.fact.claims()
        if claim.key == fact.source_scope
    )
    if sum_fen((total_used, gross_fen)) > source.values["gross_fen"]:
        raise KernelError("advance_already_consumed", "累计履约与退款/其他核销不能超过原预收含税额")
    earlier_used = sum_fen(
        claim.amount
        for peer in peers
        if (peer.fact.period, peer.subject_id) < (fact.period, version.subject_id)
        for claim in peer.fact.claims()
        if claim.key == fact.source_scope
    )
    return source, earlier_used


def calculate_advance_fulfillment(version: FactVersion, ctx: Context) -> Outcome:
    fact: AdvanceFulfillment = version.fact
    if fact.fulfillment_date is not None and fact.fulfillment_date.period != fact.period:
        raise KernelError("recognition_period_conflict", "履约日须与收入确认月一致")
    source, earlier_used = _advance_consumption(version, ctx, fact.fulfilled_gross_fen)
    already_taxed = source.values["vat_due_on_advance"]
    if already_taxed:
        rate = Decimal(source.values["rate_percent"])
        eligible = source.values["exemption_eligible"]
        policy_version = source.values["policy_version"]
    else:
        for field in ("vat_policy_id", "exemption_eligible", "tax_obligation_period"):
            if getattr(fact, field) is None:
                raise NeedsInformation(field, "未计税预收履约需要明确税务来源")
        if fact.tax_obligation_period != fact.period:
            raise NeedsInformation("tax_obligation_period", "此履约入口需要明确本期已到税点")
        policy = ctx.one("vat_policy", f"@{fact.vat_policy_id}").fact.policy
        _tax_policy_for_month(policy, fact.tax_obligation_period, fact.tax_obligation_date)
        rate, eligible, policy_version = (
            policy.rate_percent,
            fact.exemption_eligible,
            policy.version,
        )
    if already_taxed:
        net_before, _ = split_tax_inclusive(earlier_used, rate)
        net_after, _ = split_tax_inclusive(earlier_used + fact.fulfilled_gross_fen, rate)
        net = net_after - net_before
    else:
        net, _ = split_tax_inclusive(fact.fulfilled_gross_fen, rate)
    vat = fact.fulfilled_gross_fen - net
    debit = net if already_taxed else fact.fulfilled_gross_fen
    lines = []
    if debit:
        lines.append(Line("2203", debit=debit))
    if net:
        lines.append(Line("5001", credit=net))
    if not already_taxed and vat:
        lines.append(Line("222101", credit=vat))
    return Outcome(
        tuple(lines),
        {
            "advance_id": fact.advance_id,
            "gross_fulfilled_fen": fact.fulfilled_gross_fen,
            "recognized_revenue_fen": net,
            "released_advance_fen": debit,
            "net_sales_fen": 0 if already_taxed else net,
            "accrued_vat_fen": 0 if already_taxed else vat,
            "exemption_eligible": eligible,
            "policy_version": policy_version,
            "source_calculation": source.id,
        },
        (BalanceEffect(f"advance:{fact.advance_id}:advance", -debit, "payable"),),
    )


class AdvanceRefund(Fact):
    """A confirmed refund liability; actual cash remains a separate Payment."""

    kind: ClassVar[str] = "advance_refund"
    identity_fields: ClassVar[tuple[str, ...]] = ("advance_id",)
    advance_id: Identifier
    refunded_gross_fen: PositiveFen
    refund_right_confirmed: StrictBool | None = None

    @property
    def source_scope(self):
        return f"payment:advance:{self.advance_id}:advance"

    def scopes(self):
        return (str(self.period), self.source_scope, f"tax:{self.period}")

    def claims(self):
        return (Claim(self.source_scope, self.refunded_gross_fen),)

    def reads(self):
        return (
            Read("calculation", "advance", f"@{self.advance_id}"),
            _through_month("fact", "*", self.source_scope, self.period),
        )


def calculate_advance_refund(version: FactVersion, ctx: Context) -> Outcome:
    fact: AdvanceRefund = version.fact
    if fact.refund_right_confirmed is not True or not version.evidence:
        raise NeedsInformation("refund_right_confirmed", "预收退款须有明确退款义务及留存依据")
    source, earlier_used = _advance_consumption(version, ctx, fact.refunded_gross_fen)
    taxable = source.values["vat_due_on_advance"]
    net = fact.refunded_gross_fen
    if taxable:
        rate = Decimal(source.values["rate_percent"])
        before, _ = split_tax_inclusive(earlier_used, rate)
        after, _ = split_tax_inclusive(earlier_used + fact.refunded_gross_fen, rate)
        net = after - before
    vat = fact.refunded_gross_fen - net
    lines = [Line("2203", credit=fact.refunded_gross_fen)]
    if net:
        lines.append(Line("2203", debit=net))
    if vat:
        lines.append(Line("222101", debit=vat))
    result = outcome(
        lines,
        {
            "advance_id": fact.advance_id,
            "refunded_gross_fen": fact.refunded_gross_fen,
            "returned_net_fen": net,
            "credit_note_vat_fen": vat,
            "net_sales_fen": -net if taxable else 0,
            "accrued_vat_fen": -vat,
            "exemption_eligible": source.values["exemption_eligible"] if taxable else False,
            "original_sale_period": str(source.period) if taxable else None,
            "policy_version": source.values["policy_version"],
            "source_calculation": source.id,
        },
        [
            obligation(
                version,
                amount=fact.refunded_gross_fen,
                account="2203",
                normal="credit",
                counterparty=source.values["counterparty_id"],
                cashflow="customer_refunds",
            )
        ],
    )
    return Outcome(
        result.lines,
        result.values,
        result.balances + (BalanceEffect(f"advance:{fact.advance_id}:advance", -net, "payable"),),
    )


class Funding(Fact):
    kind: ClassVar[str] = "funding"
    funds_account: ClassVar[str] = "1002"
    funds_category: ClassVar[str] = "bank"
    owner_id: Identifier
    amount_fen: PositiveFen
    funding_kind: Literal["loan", "capital"]
    actual_date: ActualDate
    bank_account_id: Identifier
    immutable: ClassVar[bool] = True

    @property
    def funds_account_id(self):
        return self.bank_account_id

    def scopes(self):
        return bank_scopes(self.period, self.bank_account_id)

    @model_validator(mode="after")
    def actual_month(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual_date must belong to posting period")
        return self


def calculate_funding(version: FactVersion, ctx: Context) -> Outcome:
    fact: Funding = version.fact
    account = "2241" if fact.funding_kind == "loan" else "3001"
    obligations = (
        [
            obligation(
                version,
                amount=fact.amount_fen,
                account=account,
                normal="credit",
                counterparty=fact.owner_id,
                cashflow="financing_repayment",
            )
        ]
        if fact.funding_kind == "loan"
        else []
    )
    result = outcome(
        [
            Line(fact.funds_account, debit=fact.amount_fen, cashflow="financing_receipts"),
            Line(account, credit=fact.amount_fen),
        ],
        {
            "actual_date": str(fact.actual_date),
            f"{fact.funds_category}_account_id": fact.funds_account_id,
        },
        obligations,
    )
    return Outcome(
        result.lines,
        result.values,
        result.balances
        + (BalanceEffect(fact.funds_account_id, fact.amount_fen, fact.funds_category),),
    )


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_kind: Identifier
    source_id: Identifier
    obligation: Identifier
    amount_fen: PositiveFen
    recipient_id: Identifier | None = None

    @property
    def scope(self):
        return f"payment:{self.source_kind}:{self.source_id}:{self.obligation}"


class Payment(Fact):
    kind: ClassVar[str] = "payment"
    actual_payment: ClassVar[bool] = True
    funds_account: ClassVar[str] = "1002"
    funds_category: ClassVar[str] = "bank"
    immutable: ClassVar[bool] = True
    immutable_fields: ClassVar[tuple[str, ...]] = (
        "period",
        "actual_date",
        "direction",
        "bank_account_id",
        "counterparty_id",
        "amount_fen",
    )
    actual_date: ActualDate
    direction: Literal["inflow", "outflow"]
    bank_account_id: Identifier
    counterparty_id: Identifier
    payment_method: Literal["individual", "bank_batch"] = "individual"
    amount_fen: PositiveFen
    allocations: tuple[Allocation, ...] = Field(min_length=1)

    @property
    def funds_account_id(self):
        return self.bank_account_id

    @model_validator(mode="after")
    def validate_funds(self):
        if self.actual_date.period != self.period:
            raise ValueError("payment posting month must equal actual funds month")
        if sum_fen(a.amount_fen for a in self.allocations) != self.amount_fen:
            raise ValueError("allocations must exactly account for the actual funds amount")
        scopes = [a.scope for a in self.allocations]
        if len(scopes) != len(set(scopes)):
            raise ValueError("duplicate obligation allocation")
        return self

    def scopes(self):
        return (*bank_scopes(self.period, self.bank_account_id), *self.business_scopes())

    def business_scopes(self):
        return (
            *(a.scope for a in self.allocations),
            *(
                ("loan-principal",)
                if any(
                    a.source_kind == "loan_drawdown" and a.obligation == "principal"
                    for a in self.allocations
                )
                else ()
            ),
            *(
                f"service-tax:{a.source_id}"
                for a in self.allocations
                if a.source_kind == "service_sale"
            ),
            *(
                (f"tax:{self.period}",)
                if any(a.source_kind == "service_sale" for a in self.allocations)
                else ()
            ),
        )

    def claims(self):
        return tuple(Claim(a.scope, a.amount_fen) for a in self.allocations)

    def reads(self):
        reads = []
        for allocation in self.allocations:
            reads.extend(
                (
                    Read("calculation", allocation.source_kind, f"@{allocation.source_id}"),
                    _through_month("fact", "overpayment", allocation.scope, self.period),
                    _through_month("fact", "*", allocation.scope, self.period),
                )
            )
            if allocation.source_kind == "service_sale":
                reads.extend(
                    (
                        _through_month(
                            "fact", "*", f"service-tax:{allocation.source_id}", self.period
                        ),
                        _through_month(
                            "calculation",
                            "sale_return",
                            f"sale:{allocation.source_id}",
                            self.period,
                        ),
                    )
                )
        return tuple(sorted(set(reads)))


def _source_obligation(ctx: Context, allocation: Allocation) -> tuple[object, dict]:
    calculations = ctx.calculations(allocation.source_kind, f"@{allocation.source_id}")
    if len(calculations) != 1:
        raise NeedsInformation(
            "allocations.source_id", "需要当前有效的核销来源", sources=(allocation.source_id,)
        )
    source = calculations[0]
    candidates = [
        o for o in source.values.get("obligations", ()) if o["name"] == allocation.obligation
    ]
    if len(candidates) != 1:
        raise KernelError("unknown_obligation", "来源没有该项应收应付义务")
    return source, candidates[0]


def calculate_payment(version: FactVersion, ctx: Context) -> Outcome:
    fact: Payment = version.fact
    lines, balances, settlements = [], [], []
    expected_normal = "debit" if fact.direction == "inflow" else "credit"
    for allocation in fact.allocations:
        recipient = (
            allocation.recipient_id if fact.payment_method == "bank_batch" else fact.counterparty_id
        )
        if recipient is None:
            raise NeedsInformation("allocations.recipient_id", "汇总代付须明确每项实际收款人")
        if fact.payment_method == "bank_batch" and fact.direction != "outflow":
            raise KernelError("invalid_batch_direction", "银行汇总代付仅适用于实际付出")
        if fact.payment_method == "individual" and allocation.recipient_id not in (None, recipient):
            raise KernelError("payment_party_conflict", "单笔付款收款人信息冲突")
        source, item = _source_obligation(ctx, allocation)
        if "payment" not in item.get("settlement_modes", ("payment", "offset")):
            raise KernelError(
                "settlement_mode_not_authorized", "来源权利未授权直接资金核销，需采用明确业务处理"
            )
        if source.period > fact.period:
            raise KernelError("payment_before_obligation", "付款核销不能早于来源确认月份")
        if source.values.get("actual_date") and fact.actual_date < source.values["actual_date"]:
            raise KernelError(
                "payment_before_actual_source", "实际核销不能早于来源款项实际发生日期"
            )
        if item["normal"] != expected_normal:
            raise KernelError("payment_direction_conflict", "实际资金方向与应收应付方向冲突")
        if item.get("counterparty_id") not in (None, recipient):
            raise KernelError("payment_party_conflict", "实际交易方与核销来源不一致")
        paid = allocation.amount_fen
        for other in ctx.select(_through_month("fact", "*", allocation.scope, fact.period)):
            if other.subject_id == version.subject_id or not getattr(
                other.fact, "actual_payment", False
            ):
                continue
            paid = sum_fen(
                (
                    paid,
                    *(a.amount_fen for a in other.fact.allocations if a.scope == allocation.scope),
                )
            )
        settled = sum_fen(
            claim.amount
            for other in ctx.select(_through_month("fact", "*", allocation.scope, fact.period))
            if other.subject_id != version.subject_id
            and not getattr(other.fact, "actual_payment", False)
            for claim in other.fact.claims()
            if claim.key == allocation.scope
        )
        if settled and sum_fen((paid, settled)) > item["amount_fen"]:
            raise KernelError("overallocated_obligation", "实际款项与非现金核销合计超过来源义务")
        if paid > item["amount_fen"]:
            dispositions = ctx.select(
                _through_month("fact", "overpayment", allocation.scope, fact.period)
            )
            if (
                len(dispositions) != 1
                or dispositions[0].fact.amount_fen != paid - item["amount_fen"]
                or dispositions[0].fact.counterparty_id != recipient
            ):
                raise NeedsInformation(
                    "overpayment",
                    "实际已付款不能被重算覆盖；需确认超付差额形成的追收款",
                    sources=(allocation.scope,),
                )
        else:
            ctx.select(_through_month("fact", "overpayment", allocation.scope, fact.period))
        amount = allocation.amount_fen
        if fact.direction == "outflow":
            lines.extend(
                (
                    Line(item["account"], debit=amount),
                    Line(
                        fact.funds_account,
                        credit=amount,
                        cashflow=item.get("cashflow", "operating"),
                    ),
                )
            )
        else:
            lines.extend(
                (
                    Line(
                        fact.funds_account, debit=amount, cashflow=item.get("cashflow", "operating")
                    ),
                    Line(item["account"], credit=amount),
                )
            )
        balances.append(BalanceEffect(item["key"], -amount, item["category"]))
        settlements.append(
            {"source_calculation": source.id, "obligation": item["key"], "amount_fen": amount}
        )
    balances.append(
        BalanceEffect(
            fact.funds_account_id,
            fact.amount_fen if fact.direction == "inflow" else -fact.amount_fen,
            fact.funds_category,
        )
    )
    tax_lines, tax_sales, tax_transfers = payment_tax_transfers(version, ctx)
    lines.extend(tax_lines)
    return Outcome(
        tuple(lines),
        {
            "actual_date": str(fact.actual_date),
            f"{fact.funds_category}_account_id": fact.funds_account_id,
            "direction": fact.direction,
            "amount_fen": fact.amount_fen,
            "settlements": settlements,
            "tax_sales": tax_sales,
            "tax_transfers": tax_transfers,
        },
        tuple(balances),
    )


def _remaining_deferred_sale(ctx: Context, source, period):
    returns = ctx.select(
        _through_month("calculation", "sale_return", f"sale:{source.subject_id}", period)
    )
    net = source.values["recognized_revenue_fen"] - sum_fen(
        row.values["returned_net_fen"] for row in returns
    )
    vat = source.values["vat_fen"] - sum_fen(row.values["credit_note_vat_fen"] for row in returns)
    if net < 0 or vat < 0:
        raise KernelError("return_exceeds_sale", "转税前退回不得超过原收入及税额")
    return net, vat


def _tax_transfer_result(source, net, vat):
    lines = [Line("222104", debit=vat), Line("222101", credit=vat)] if vat else []
    sale = {
        "net_sales_fen": net,
        "accrued_vat_fen": vat,
        "exemption_eligible": source.values["exemption_eligible"],
        "source_id": source.subject_id,
    }
    transfer = {
        "source_id": source.subject_id,
        "source_calculation": source.id,
        "net_sales_fen": net,
        "vat_fen": vat,
    }
    return lines, sale, transfer


def payment_tax_transfers(version: FactVersion, ctx: Context):
    """Pure VAT addition to an actual receipt; no bank/cash account assumptions.

    The first receipt at an explicitly known tax point transfers the remaining
    source VAT once. Receipt amount does not silently become the tax base.
    """
    fact = version.fact
    lines, tax_sales, transfers = [], [], []
    if fact.direction != "inflow":
        return lines, tax_sales, transfers
    for allocation in fact.allocations:
        if allocation.source_kind != "service_sale":
            continue
        source, _ = _source_obligation(ctx, allocation)
        if source.values.get("vat_recognition") != "deferred":
            continue
        scope = f"service-tax:{source.subject_id}"
        peers = ctx.select(_through_month("fact", "*", scope, fact.period))
        declared = [
            peer
            for peer in peers
            if peer.fact.kind == "service_tax_point" and peer.fact.trigger == "declared"
        ]
        if declared:
            if len(declared) != 1:
                raise KernelError("duplicate_tax_point", "同一收入只允许一次明确税点确认")
            continue
        receipts = [
            peer
            for peer in peers
            if peer.fact.kind in {"payment", "cash_payment"} and peer.fact.direction == "inflow"
        ]
        first = min([version, *receipts], key=lambda item: (item.fact.actual_date, item.subject_id))
        if first.subject_id != version.subject_id:
            continue
        tax_month = source.values["tax_obligation_period"]
        tax_date = source.values.get("tax_obligation_date")
        if fact.period != tax_month or tax_date is not None and fact.actual_date != tax_date:
            raise NeedsInformation(
                "tax_obligation_period",
                "首笔实际回款与原明确税点不符，须先核对税点来源",
                sources=(source.subject_id,),
                precision=("month", "day"),
            )
        net, vat = _remaining_deferred_sale(ctx, source, fact.period)
        added, sale, transfer = _tax_transfer_result(source, net, vat)
        lines.extend(added)
        tax_sales.append(sale)
        transfers.append(transfer)
    return lines, tax_sales, transfers


class ServiceTaxPoint(Fact):
    """Declared noncash tax point, or evidence-backed reference to an actual receipt."""

    kind: ClassVar[str] = "service_tax_point"
    identity_fields: ClassVar[tuple[str, ...]] = ("sale_id", "trigger")
    sale_id: Identifier
    trigger: Literal["declared", "receipt"]
    declaration_confirmed: StrictBool | None = None
    payment_id: Identifier | None = None
    payment_kind: Literal["payment", "cash_payment"] = "payment"

    def scopes(self):
        return (
            str(self.period),
            f"tax:{self.period}",
            *((f"service-tax:{self.sale_id}",) if self.trigger == "declared" else ()),
        )

    def claims(self):
        # One whole-source recognition right, not one fen of user-entered tax.
        return (Claim(f"service-tax:{self.sale_id}", 1),) if self.trigger == "declared" else ()

    def reads(self):
        return (
            Read("calculation", "service_sale", f"@{self.sale_id}"),
            _through_month("fact", "*", f"service-tax:{self.sale_id}", self.period),
            _through_month("calculation", "sale_return", f"sale:{self.sale_id}", self.period),
            *(
                (Read("calculation", self.payment_kind, f"@{self.payment_id}"),)
                if self.payment_id
                else ()
            ),
        )


def calculate_service_tax_point(version: FactVersion, ctx: Context) -> Outcome:
    fact: ServiceTaxPoint = version.fact
    sources = ctx.calculations("service_sale", f"@{fact.sale_id}")
    if len(sources) != 1:
        raise NeedsInformation("sale_id", "需要明确的已确认收入来源", sources=(fact.sale_id,))
    source = sources[0]
    if source.values.get("vat_recognition") != "deferred":
        raise KernelError("tax_already_recognized", "原收入已确认纳税义务，不能重复转税")
    if fact.period != source.values["tax_obligation_period"]:
        raise KernelError("tax_point_period_conflict", "税点确认须属于原明确的纳税义务月")
    if fact.trigger == "receipt":
        if fact.payment_id is None:
            raise NeedsInformation("payment_id", "需要引用真实已入账回款，不得编造收款日期")
        payments = ctx.calculations(fact.payment_kind, f"@{fact.payment_id}")
        if len(payments) != 1 or payments[0].period != fact.period:
            raise NeedsInformation("payment_id", "需要本税点月份的真实回款结果")
        transfer = next(
            (
                item
                for item in payments[0].values.get("tax_transfers", ())
                if item["source_id"] == fact.sale_id
            ),
            None,
        )
        if transfer is None:
            raise NeedsInformation("payment_id", "回款结果没有该收入的待转税额转换")
        return Outcome(
            (),
            {
                "net_sales_fen": 0,
                "accrued_vat_fen": 0,
                "exemption_eligible": source.values["exemption_eligible"],
                "observed_payment": payments[0].id,
                "observed_transfer": transfer,
            },
        )
    if fact.declaration_confirmed is not True or not version.evidence:
        raise NeedsInformation("declaration_confirmed", "非收款税点须有明确税务事实与留存证据")
    peers = ctx.select(_through_month("fact", "*", f"service-tax:{fact.sale_id}", fact.period))
    if sum(
        claim.amount
        for peer in peers
        if peer.subject_id != version.subject_id
        for claim in peer.fact.claims()
        if claim.key == f"service-tax:{fact.sale_id}"
    ):
        raise KernelError("duplicate_tax_point", "原收入纳税义务已经确认，不能重复转税")
    net, vat = _remaining_deferred_sale(ctx, source, fact.period)
    lines, sale, transfer = _tax_transfer_result(source, net, vat)
    return Outcome(tuple(lines), sale | {"tax_transfers": [transfer]})


class Settlement(Fact):
    """Business-authorized noncash application of two opposing obligations."""

    kind: ClassVar[str] = "settlement"
    settlement_kind: Literal["advance_application", "debt_offset"]
    first: Allocation
    second: Allocation
    offset_right_confirmed: StrictBool | None = None

    @model_validator(mode="after")
    def equal_amounts(self):
        if self.first.amount_fen != self.second.amount_fen or self.first.scope == self.second.scope:
            raise ValueError("noncash settlement requires equal amounts of different obligations")
        return self

    def scopes(self):
        return (str(self.period), self.first.scope, self.second.scope)

    def claims(self):
        return tuple(Claim(a.scope, a.amount_fen) for a in (self.first, self.second))

    def reads(self):
        return tuple(
            sorted(
                {
                    read
                    for item in (self.first, self.second)
                    for read in (
                        Read("calculation", item.source_kind, f"@{item.source_id}"),
                        Read("fact", "*", item.scope),
                    )
                }
            )
        )


def calculate_settlement(version: FactVersion, ctx: Context) -> Outcome:
    fact: Settlement = version.fact
    if fact.offset_right_confirmed is not True:
        raise NeedsInformation("offset_right_confirmed", "需要有依据的预付款应用或债务抵销权利")
    _, first = _source_obligation(ctx, fact.first)
    _, second = _source_obligation(ctx, fact.second)
    if any(
        "offset" not in item.get("settlement_modes", ("payment", "offset"))
        for item in (first, second)
    ):
        raise KernelError("settlement_mode_not_authorized", "该项税务确认仅允许退款，不允许抵缴")
    if first["normal"] == second["normal"]:
        raise KernelError("settlement_direction", "非现金抵销的两项义务必须方向相反")
    if first.get("counterparty_id") != second.get("counterparty_id"):
        raise KernelError("settlement_party", "非现金抵销须属于同一交易方")
    if fact.settlement_kind == "advance_application":
        valid = [
            (ref, item)
            for ref, item in ((fact.first, first), (fact.second, second))
            if ref.source_kind == "advance" and ref.obligation == "advance"
        ]
        if len(valid) != 1:
            raise KernelError("advance_source_required", "预收预付应用必须使用明确的预收预付来源")
    for allocation, item in ((fact.first, first), (fact.second, second)):
        allocated = allocation.amount_fen
        for prior in ctx.facts("*", allocation.scope):
            if prior.subject_id != version.subject_id:
                allocated = sum_fen(
                    (
                        allocated,
                        *(a.amount for a in prior.fact.claims() if a.key == allocation.scope),
                    )
                )
        if allocated > item["amount_fen"]:
            raise KernelError("overallocated_obligation", "抵销与实际收付合计不能超过来源义务")
    debit, credit = (first, second) if first["normal"] == "credit" else (second, first)
    amount = fact.first.amount_fen
    return Outcome(
        (Line(debit["account"], debit=amount), Line(credit["account"], credit=amount)),
        {"settlement_kind": fact.settlement_kind, "amount_fen": amount},
        (
            BalanceEffect(first["key"], -amount, first["category"]),
            BalanceEffect(second["key"], -amount, second["category"]),
        ),
    )


class SaleReturn(Fact):
    kind: ClassVar[str] = "sale_return"
    identity_fields: ClassVar[tuple[str, ...]] = ("sale_id",)
    sale_id: Identifier
    returned_gross_fen: PositiveFen
    credit_note_vat_fen: NonNegativeFen
    customer_id: Identifier

    def scopes(self):
        return (str(self.period), f"sale:{self.sale_id}", f"tax:{self.period}")

    def reads(self):
        return (
            Read("calculation", "service_sale", f"@{self.sale_id}"),
            _through_month("fact", self.kind, f"sale:{self.sale_id}", self.period),
            *(
                Read("calculation", kind, f"service-tax:{self.sale_id}", self.period)
                for kind in ("payment", "cash_payment", "service_tax_point")
            ),
        )

    def claims(self):
        return (Claim(f"sale-return:{self.sale_id}", self.returned_gross_fen),)


def calculate_sale_return(version: FactVersion, ctx: Context) -> Outcome:
    fact: SaleReturn = version.fact
    source, sale_obligation = _source_obligation(
        ctx,
        Allocation(
            source_kind="service_sale",
            source_id=fact.sale_id,
            obligation="primary",
            amount_fen=fact.returned_gross_fen,
        ),
    )
    if source.period > fact.period or sale_obligation["counterparty_id"] != fact.customer_id:
        raise KernelError("return_source_conflict", "退回月份或客户与收入来源冲突")
    others = [
        item.fact
        for item in ctx.select(
            _through_month("fact", fact.kind, f"sale:{fact.sale_id}", fact.period)
        )
        if item.subject_id != version.subject_id
    ]
    gross = sum_fen((fact.returned_gross_fen, *(item.returned_gross_fen for item in others)))
    vat = sum_fen((fact.credit_note_vat_fen, *(item.credit_note_vat_fen for item in others)))
    source_vat = source.values.get("vat_fen", source.values["accrued_vat_fen"])
    if gross > source.values["gross_fen"] or vat > source_vat:
        raise KernelError("return_exceeds_sale", "销售退回与红字税额不能超过原确认销售")
    if fact.credit_note_vat_fen >= fact.returned_gross_fen:
        raise KernelError("invalid_return_vat", "红字税额须小于退回价税总额")
    if gross == source.values["gross_fen"] and vat != source_vat:
        raise KernelError("return_vat_remainder", "全额退回必须核对原销售全部税额")
    net = fact.returned_gross_fen - fact.credit_note_vat_fen
    taxable_period = source.period
    if source.values.get("vat_recognition") == "deferred":
        transferred = [
            row
            for kind in ("payment", "cash_payment", "service_tax_point")
            for row in ctx.select(
                Read("calculation", kind, f"service-tax:{fact.sale_id}", fact.period)
            )
            if any(
                item["source_id"] == fact.sale_id for item in row.values.get("tax_transfers", ())
            )
        ]
        taxable_period = transferred[0].period if transferred else None
        if taxable_period is None and fact.period > source.values["tax_obligation_period"]:
            raise NeedsInformation(
                "tax_obligation_period", "原明确税点已过，需要先核对待转税额确认来源"
            )
    lines = [Line("5001", debit=net), Line("2203", credit=fact.returned_gross_fen)]
    if fact.credit_note_vat_fen:
        lines.append(Line("222101" if taxable_period else "222104", debit=fact.credit_note_vat_fen))
    return outcome(
        lines,
        {
            "net_sales_fen": -net if taxable_period else 0,
            "sale_id": fact.sale_id,
            "original_sale_period": str(taxable_period) if taxable_period else None,
            "accrued_vat_fen": -fact.credit_note_vat_fen if taxable_period else 0,
            "returned_net_fen": net,
            "credit_note_vat_fen": fact.credit_note_vat_fen,
            "exemption_eligible": source.values["exemption_eligible"],
        },
        [
            obligation(
                version,
                amount=fact.returned_gross_fen,
                account="2203",
                normal="credit",
                counterparty=fact.customer_id,
                cashflow="customer_receipts",
            )
        ],
    )


class FundsTransfer(Fact):
    def scopes(self):
        return bank_scopes(
            self.period, self.source_bank_account_id, self.destination_bank_account_id
        )

    kind: ClassVar[str] = "funds_transfer"
    immutable: ClassVar[bool] = True
    actual_date: ActualDate
    source_bank_account_id: Identifier
    destination_bank_account_id: Identifier
    amount_fen: PositiveFen

    @model_validator(mode="after")
    def actual_funds(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual funds date must belong to accounting period")
        if self.source_bank_account_id == self.destination_bank_account_id:
            raise ValueError("internal transfer requires different actual bank accounts")
        return self


def calculate_transfer(version: FactVersion, ctx: Context) -> Outcome:
    fact: FundsTransfer = version.fact
    return Outcome(
        (Line("1002", debit=fact.amount_fen), Line("1002", credit=fact.amount_fen)),
        {"actual_date": str(fact.actual_date), "amount_fen": fact.amount_fen},
        (
            BalanceEffect(fact.source_bank_account_id, -fact.amount_fen, "bank"),
            BalanceEffect(fact.destination_bank_account_id, fact.amount_fen, "bank"),
        ),
    )


class BankIncome(Fact):
    def scopes(self):
        return bank_scopes(self.period, self.bank_account_id)

    kind: ClassVar[str] = "bank_income"
    immutable: ClassVar[bool] = True
    actual_date: ActualDate
    bank_account_id: Identifier
    amount_fen: PositiveFen
    income_kind: Literal["bank_interest", "government_grant", "retained_verification_payment"]
    counterparty_id: Identifier
    entitlement_confirmed: StrictBool | None = None

    @model_validator(mode="after")
    def actual_month(self):
        if self.actual_date.period != self.period:
            raise ValueError("actual receipt date must belong to accounting month")
        return self


def calculate_bank_income(version: FactVersion, ctx: Context) -> Outcome:
    fact: BankIncome = version.fact
    if fact.entitlement_confirmed is not True:
        raise NeedsInformation("entitlement_confirmed", "需要确认该实际收入归公司所有，无返还义务")
    account = "5603" if fact.income_kind == "bank_interest" else "6301"
    return Outcome(
        (
            Line("1002", debit=fact.amount_fen, cashflow="other_operating_receipts"),
            Line(account, credit=fact.amount_fen),
        ),
        {
            "actual_date": str(fact.actual_date),
            "bank_account_id": fact.bank_account_id,
            "amount_fen": fact.amount_fen,
            "income_kind": fact.income_kind,
        },
        (BalanceEffect(fact.bank_account_id, fact.amount_fen, "bank"),),
    )


class RefundableDeposit(Fact):
    kind: ClassVar[str] = "refundable_deposit"
    counterparty_id: Identifier
    amount_fen: PositiveFen
    refund_right_confirmed: StrictBool | None = None


def calculate_deposit(version: FactVersion, ctx: Context) -> Outcome:
    fact: RefundableDeposit = version.fact
    if fact.refund_right_confirmed is not True:
        raise NeedsInformation("refund_right_confirmed", "需要明确可退还押金的合同依据")
    return outcome(
        [Line("1221", debit=fact.amount_fen), Line("2202", credit=fact.amount_fen)],
        {},
        [
            obligation(
                version,
                name="payment",
                amount=fact.amount_fen,
                account="2202",
                normal="credit",
                counterparty=fact.counterparty_id,
            ),
            obligation(
                version,
                name="refund",
                amount=fact.amount_fen,
                account="1221",
                normal="debit",
                counterparty=fact.counterparty_id,
            ),
        ],
    )


class Overpayment(Fact):
    """Explicit recovery right for a previously paid, now reduced obligation."""

    kind: ClassVar[str] = "overpayment"
    source_kind: Identifier
    source_id: Identifier
    obligation_name: Identifier
    counterparty_id: Identifier
    amount_fen: PositiveFen
    recovery_right_confirmed: StrictBool | None = None

    @property
    def payment_scope(self):
        return f"payment:{self.source_kind}:{self.source_id}:{self.obligation_name}"

    def scopes(self):
        return (str(self.period), self.payment_scope)

    def reads(self):
        return (
            Read("calculation", self.source_kind, f"@{self.source_id}"),
            Read("fact", "*", self.payment_scope),
        )


def calculate_overpayment(version: FactVersion, ctx: Context) -> Outcome:
    fact: Overpayment = version.fact
    if fact.recovery_right_confirmed is not True:
        raise NeedsInformation("recovery_right_confirmed", "需要确认超付形成的追收权利")
    allocation = Allocation(
        source_kind=fact.source_kind,
        source_id=fact.source_id,
        obligation=fact.obligation_name,
        amount_fen=fact.amount_fen,
    )
    _, item = _source_obligation(ctx, allocation)
    if item["normal"] != "credit":
        raise KernelError("overpayment_direction", "追收超付仅适用于已经支付的应付款")
    paid = sum_fen(
        a.amount_fen
        for payment in ctx.facts("*", fact.payment_scope)
        if getattr(payment.fact, "actual_payment", False)
        for a in payment.fact.allocations
        if a.scope == fact.payment_scope
    )
    if paid - item["amount_fen"] != fact.amount_fen:
        raise KernelError("overpayment_difference_changed", "已确认追收差额与当前应付、实付不符")
    recovery = obligation(
        version,
        amount=fact.amount_fen,
        account="1221",
        normal="debit",
        counterparty=fact.counterparty_id,
        cashflow="operating_payments",
    )
    return Outcome(
        (Line("1221", debit=fact.amount_fen), Line(item["account"], credit=fact.amount_fen)),
        {"overpayment_fen": fact.amount_fen, "obligations": [recovery]},
        (
            BalanceEffect(item["key"], fact.amount_fen, item["category"]),
            BalanceEffect(recovery["key"], fact.amount_fen, "receivable"),
        ),
    )


def register(registry: Registry) -> None:
    for model, evaluator in (
        (ServiceSale, calculate_sale),
        (Expense, calculate_expense),
        (ProjectCost, calculate_project_cost),
        (ProjectRelease, calculate_project_release),
        (PassThrough, calculate_pass_through),
        (Advance, calculate_advance),
        (AdvanceFulfillment, calculate_advance_fulfillment),
        (AdvanceRefund, calculate_advance_refund),
        (Funding, calculate_funding),
        (Payment, calculate_payment),
        (ServiceTaxPoint, calculate_service_tax_point),
        (Overpayment, calculate_overpayment),
        (Settlement, calculate_settlement),
        (SaleReturn, calculate_sale_return),
        (FundsTransfer, calculate_transfer),
        (BankIncome, calculate_bank_income),
        (RefundableDeposit, calculate_deposit),
    ):
        registry.register(model, evaluator)
