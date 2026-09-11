"""Documented short-term money funds, at cost, with independent actual settlement.

SAS article 8, effective 2013-01-01; appendix accounts 1101/5111 and cash-flow
rows 8/11. No valuation estimates, implicit reinvestment or bank-account alias.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from ..types import ActualDate, NonNegativeFen, PositiveFen, sum_fen
from .transactions import Identifier, obligation, outcome

STANDARD_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
APPENDIX_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf"
RULE = {"version": "SAS-2013-article-8", "effective_from": "2013-01-01", "source": STANDARD_URL}
ShortTermClassification = Literal["readily_redeemable_held_not_over_one_year"]
CostBasis = Literal["documented_cost"]


def cost_key(subject_id):
    return f"money-fund-cost:{subject_id}"


def require_basis(version):
    fact = version.fact
    if not version.evidence:
        raise NeedsInformation("evidence", "货币基金确认需要基金结算单或明确的成本依据")
    if fact.classification is None:
        raise NeedsInformation(
            "classification", "须明确可随时赎回且持有意图不超过一年，不能从产品名称推断"
        )
    if fact.cost_basis is None:
        raise NeedsInformation("cost_basis", "须有可复核的账面成本，不能按余额或收益率反推")
    if fact.period < "2013-01":
        raise KernelError("investment_rule_period", "本规则只覆盖2013年起的小企业会计准则")


class MoneyFundSubscription(Fact):
    kind: ClassVar[str] = "money_fund_subscription"
    identity_fields: ClassVar[tuple[str, ...]] = ("fund_id", "counterparty_id")
    fund_id: Identifier = Field(description="具体基金产品身份；平台或余利宝名称不能代替银行账户")
    counterparty_id: Identifier = Field(description="与真实付款核销复用的基金结算交易方")
    classification: ShortTermClassification | None = None
    cost_basis: CostBasis | None = None
    purchase_price_fen: PositiveFen | None = None
    acquisition_fees_fen: NonNegativeFen | None = None
    excludes_declared_unpaid_distributions: Literal[True] | None = None
    confirmation_date: ActualDate | None = Field(
        default=None, description="可省略的申购确认实际日；period为核算所属月"
    )

    @model_validator(mode="after")
    def date_period(self):
        if self.confirmation_date and self.confirmation_date.period != self.period:
            raise ValueError("subscription confirmation date must agree with its month")
        return self

    def scopes(self):
        return (str(self.period), f"money-fund:{self.fund_id}")


def calculate_subscription(version, ctx):
    fact = version.fact
    require_basis(version)
    for field in (
        "purchase_price_fen",
        "acquisition_fees_fen",
        "excludes_declared_unpaid_distributions",
    ):
        if getattr(fact, field) is None:
            raise NeedsInformation(
                field, "申购成本须明确购买价款、相关税费及不含已宣告未发放收益；零费用需明确确认"
            )
    cost = sum_fen((fact.purchase_price_fen, fact.acquisition_fees_fen))
    payable = obligation(
        version,
        amount=cost,
        account="2241",
        normal="credit",
        counterparty=fact.counterparty_id,
        cashflow="investment_acquisition",
    )
    payable["settlement_modes"] = ["payment"]
    result = outcome(
        [Line("1101", debit=cost), Line("2241", credit=cost)],
        {
            "fund_id": fact.fund_id,
            "cost_fen": cost,
            "rule": RULE,
            "confirmation_date": str(fact.confirmation_date) if fact.confirmation_date else None,
        },
        [payable],
    )
    return Outcome(
        result.lines,
        result.values,
        result.balances
        + (BalanceEffect(cost_key(version.subject_id), cost, "short_term_investment"),),
    )


class RedemptionCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_kind: Literal["money_fund_subscription", "opening_money_fund"]
    source_id: Identifier
    cost_fen: PositiveFen


class MoneyFundRedemption(Fact):
    kind: ClassVar[str] = "money_fund_redemption"
    identity_fields: ClassVar[tuple[str, ...]] = ("fund_id", "counterparty_id")
    fund_id: Identifier
    counterparty_id: Identifier
    classification: ShortTermClassification | None = None
    cost_basis: CostBasis | None = None
    costs: tuple[RedemptionCost, ...] = Field(
        default=(), description="按明确申购或期初来源分配的本次账面成本，不能用实际回款倒推"
    )
    net_proceeds_fen: NonNegativeFen | None = Field(
        default=None,
        description="结算依据确认的赎回净款，已扣本次相关税费；真实银行到账另由payment确认",
    )
    confirmation_date: ActualDate | None = Field(
        default=None, description="可省略的赎回结算确认实际日；核算允许月精度"
    )

    @model_validator(mode="after")
    def source_identity(self):
        if len({item.source_id for item in self.costs}) != len(self.costs):
            raise ValueError("redemption requires distinct source cost lots")
        if self.confirmation_date and self.confirmation_date.period != self.period:
            raise ValueError("redemption confirmation date must agree with its month")
        return self

    def scopes(self):
        return (
            str(self.period),
            f"money-fund:{self.fund_id}",
            *(cost_key(item.source_id) for item in self.costs or ()),
        )

    def claims(self):
        return tuple(Claim(cost_key(item.source_id), item.cost_fen) for item in self.costs or ())

    def reads(self):
        return tuple(
            sorted(
                {
                    read
                    for item in self.costs or ()
                    for read in (
                        Read("calculation", item.source_kind, "@" + item.source_id),
                        Read("fact", self.kind, cost_key(item.source_id)),
                    )
                }
            )
        )


def calculate_redemption(version, ctx):
    fact = version.fact
    require_basis(version)
    if not fact.costs:
        raise NeedsInformation("costs", "赎回需要明确本次消耗的申购或期初账面成本来源")
    if fact.net_proceeds_fen is None:
        raise NeedsInformation("net_proceeds_fen", "须有赎回结算净款；不能根据预估收益构造实际回款")
    balances, sources = [], []
    for item in fact.costs:
        rows = ctx.calculations(item.source_kind, "@" + item.source_id)
        if len(rows) != 1:
            raise NeedsInformation(
                "costs.source_id", "需要已正式确认的申购或期初成本", sources=(item.source_id,)
            )
        source = rows[0]
        if source.values["fund_id"] != fact.fund_id:
            raise KernelError("money_fund_identity", "赎回只能消耗同一具体基金产品的成本")
        if source.period > fact.period or (
            fact.confirmation_date
            and source.values.get("confirmation_date")
            and fact.confirmation_date < source.values["confirmation_date"]
        ):
            raise KernelError("redemption_before_acquisition", "赎回不得早于成本来源")
        key = cost_key(item.source_id)
        used = sum_fen(
            (
                item.cost_fen,
                *(
                    claim.amount
                    for peer in ctx.facts(fact.kind, key)
                    if peer.subject_id != version.subject_id
                    for claim in peer.fact.claims()
                    if claim.key == key
                ),
            )
        )
        if used > source.values["cost_fen"]:
            raise KernelError("money_fund_cost_exceeded", "已确认的赎回成本合计超过该来源账面成本")
        balances.append(BalanceEffect(key, -item.cost_fen, "short_term_investment"))
        sources.append(
            {
                "source_calculation": source.id,
                "source_id": source.subject_id,
                "cost_fen": item.cost_fen,
            }
        )
    cost = sum_fen(item.cost_fen for item in fact.costs)
    gain = fact.net_proceeds_fen - cost
    lines, receivables = [Line("1101", credit=cost)], []
    if fact.net_proceeds_fen:
        lines.append(Line("1221", debit=fact.net_proceeds_fen))
        receivable = obligation(
            version,
            amount=fact.net_proceeds_fen,
            account="1221",
            normal="debit",
            counterparty=fact.counterparty_id,
            cashflow="investment_recovery",
        )
        receivable["settlement_modes"] = ["payment"]
        receivables.append(receivable)
    if gain:
        lines.append(Line("5111", credit=gain) if gain > 0 else Line("5111", debit=-gain))
    result = outcome(
        lines,
        {
            "fund_id": fact.fund_id,
            "cost_fen": cost,
            "net_proceeds_fen": fact.net_proceeds_fen,
            "investment_income_fen": gain,
            "cost_sources": sources,
            "rule": RULE,
            "confirmation_date": str(fact.confirmation_date) if fact.confirmation_date else None,
        },
        receivables,
    )
    return Outcome(result.lines, result.values, result.balances + tuple(balances))


def register(registry):
    registry.register(MoneyFundSubscription, calculate_subscription)
    registry.register(MoneyFundRedemption, calculate_redemption)
