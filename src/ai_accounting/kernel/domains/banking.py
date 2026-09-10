"""Immutable statement revisions and explicit, complete bank reconciliation."""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts import Fact, KernelError, NeedsInformation, Outcome, Read
from ..types import ActualDate, Fen, YearMonth, sum_fen
from .transactions import Identifier

CashKind = Literal[
    "payment", "funding", "funds_transfer", "loan_drawdown", "bank_income", "cash_bank_transfer"
]
CASH_KINDS = (
    "payment",
    "funding",
    "funds_transfer",
    "loan_drawdown",
    "bank_income",
    "cash_bank_transfer",
)


def cash_amount(fact, bank_account_id: str) -> int:
    if fact.kind == "funds_transfer":
        if bank_account_id == fact.source_bank_account_id:
            return -fact.amount_fen
        if bank_account_id == fact.destination_bank_account_id:
            return fact.amount_fen
        raise KernelError("bank_account_mismatch", "内部转账账户与流水不一致")
    if fact.bank_account_id != bank_account_id:
        raise KernelError("bank_account_mismatch", "资金账户与流水不一致")
    if fact.kind == "cash_bank_transfer":
        return fact.amount_fen if fact.direction == "deposit" else -fact.amount_fen
    amount = fact.principal_fen if fact.kind == "loan_drawdown" else fact.amount_fen
    return -amount if fact.kind == "payment" and fact.direction == "outflow" else amount


def cash_reads(key: str, before_period: YearMonth | None = None):
    return tuple(
        Read(source, kind, key, before_period)
        for kind in CASH_KINDS
        for source in ("fact", "calculation")
    )


def published_cash(context, bank_account_id: str, key: str, before_period=None):
    """Unpublished known funds cannot vanish from the source population."""
    result = {}
    for kind in CASH_KINDS:
        facts = context.select(Read("fact", kind, key, before_period))
        calculations = context.select(Read("calculation", kind, key, before_period))
        by_subject = {row.subject_id: row for row in calculations}
        if {row.subject_id for row in facts} != set(by_subject):
            raise NeedsInformation(
                "actual_funds", "已确认的真实资金尚未全部正式处理", sources=(key,)
            )
        for item in facts:
            if by_subject[item.subject_id].fact_id != item.id:
                raise NeedsInformation(
                    "actual_funds", "真实资金当前事实与正式结果尚未衔接", sources=(item.subject_id,)
                )
            result[(kind, item.subject_id)] = (item.fact, cash_amount(item.fact, bank_account_id))
    return result


class BankOpening(Fact):
    """Explicit book opening; never a synthetic cash movement or free journal."""

    kind: ClassVar[str] = "bank_opening"
    identity_fields: ClassVar[tuple[str, ...]] = ("bank_account_id", "period")
    material_category: ClassVar[str] = "bank"
    bank_account_id: Identifier
    opening_fen: Fen
    basis: Literal["new_account", "existing_ledger"]

    def scopes(self):
        return (str(self.period), f"bank:{self.bank_account_id}")

    def reads(self):
        key = f"bank:{self.bank_account_id}"
        return (Read("fact", self.kind, key), *cash_reads(key, self.period))


def calculate_opening(version, context):
    fact: BankOpening = version.fact
    key = f"bank:{fact.bank_account_id}"
    if any(item.subject_id != version.subject_id for item in context.facts(fact.kind, key)):
        raise KernelError("duplicate_bank_opening", "每个实际银行账户仅有一个明确账面起点")
    sources = published_cash(context, fact.bank_account_id, key, fact.period)
    if fact.basis == "new_account" and (fact.opening_fen != 0 or sources):
        raise KernelError("new_bank_opening_conflict", "新开户的账面起点须为零且不得已有资金历史")
    book = sum_fen(amount for _, amount in sources.values())
    if book != fact.opening_fen:
        raise NeedsInformation(
            "opening_fen",
            "声明的账面期初与已发布资金来源不符，需要核对起点依据",
            sources=tuple(source_id for _, source_id in sources),
        )
    return Outcome(
        (),
        {
            "bank_account_id": fact.bank_account_id,
            "opening_fen": fact.opening_fen,
            "basis": fact.basis,
            "source_count": len(sources),
        },
    )


class BankEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reference: str = Field(min_length=1, max_length=500)
    actual_date: ActualDate
    signed_fen: Fen
    description: str | None = None

    @model_validator(mode="after")
    def nonzero(self):
        if self.signed_fen == 0:
            raise ValueError("bank transaction amount cannot be zero")
        return self


class BankStatement(Fact):
    # A corrected extraction appends a version; old rows and original evidence
    # remain immutable, and actual payment facts are never rewritten.
    kind: ClassVar[str] = "bank_statement"
    identity_fields: ClassVar[tuple[str, ...]] = ("bank_account_id", "period")
    material_category: ClassVar[str] = "bank"
    bank_account_id: Identifier
    opening_fen: Fen
    closing_fen: Fen
    entries: tuple[BankEntry, ...]

    @model_validator(mode="after")
    def continuity(self):
        if len({entry.reference for entry in self.entries}) != len(self.entries):
            raise ValueError("statement transaction references must be unique")
        if any(entry.actual_date.period != self.period for entry in self.entries):
            raise ValueError("statement entry actual dates must belong to the statement month")
        if (
            sum_fen((self.opening_fen, *(entry.signed_fen for entry in self.entries)))
            != self.closing_fen
        ):
            raise ValueError("statement opening + actual funds must equal closing")
        return self

    def scopes(self):
        return (str(self.period), f"bank:{self.bank_account_id}:{self.period}")

    def reads(self):
        return (Read("fact", self.kind, f"bank:{self.bank_account_id}:{self.period}"),)


def calculate_statement(version, context):
    fact = version.fact
    alternatives = context.facts(fact.kind, f"bank:{fact.bank_account_id}:{fact.period}")
    if len(alternatives) != 1:
        raise KernelError(
            "duplicate_statement", "一个银行账户月份只能采用一份完整流水，补修使用原身份追加版本"
        )
    return Outcome(
        (),
        {
            "bank_account_id": fact.bank_account_id,
            "opening_fen": fact.opening_fen,
            "closing_fen": fact.closing_fen,
            "transaction_count": len(fact.entries),
        },
    )


class Match(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reference: str = Field(min_length=1, max_length=500)
    source_kind: CashKind
    source_id: Identifier


class BankReconciliation(Fact):
    kind: ClassVar[str] = "bank_reconciliation"
    material_category: ClassVar[str] = "bank"
    identity_fields: ClassVar[tuple[str, ...]] = ("statement_id", "bank_account_id", "period")
    statement_id: Identifier
    bank_account_id: Identifier = Field(description="从所引用银行流水复用的实际账户身份，必须一致")
    matches: tuple[Match, ...]

    def scopes(self):
        return (
            str(self.period),
            f"bank:{self.bank_account_id}",
            f"bank:{self.bank_account_id}:{self.period}",
        )

    def reads(self):
        key = f"bank:{self.bank_account_id}"
        return (
            Read("fact", "bank_statement", "@" + self.statement_id),
            Read("calculation", "bank_statement", "@" + self.statement_id),
            Read("fact", self.kind, f"{key}:{self.period}"),
            Read("calculation", "bank_opening", key),
            Read("calculation", self.kind, key, self.period),
            *cash_reads(f"{key}:{self.period}"),
            *(Read("fact", match.source_kind, "@" + match.source_id) for match in self.matches),
        )


def calculate_reconciliation(version, context):
    fact: BankReconciliation = version.fact
    source = context.one("bank_statement", "@" + fact.statement_id)
    statement = source.fact
    if statement.period != fact.period:
        raise KernelError("statement_period", "对账和流水月份不一致")
    if statement.bank_account_id != fact.bank_account_id:
        raise KernelError("bank_account_mismatch", "对账账户必须复用所引用流水的实际账户")
    snapshots = context.calculations("bank_statement", "@" + fact.statement_id)
    if len(snapshots) != 1 or snapshots[0].fact_id != source.id:
        raise NeedsInformation("statement_id", "流水当前确认版本尚未正式处理")
    alternatives = context.facts(fact.kind, f"bank:{fact.bank_account_id}:{fact.period}")
    if any(item.subject_id != version.subject_id for item in alternatives):
        raise KernelError("duplicate_reconciliation", "同一账户月份的对账应沿原身份修订")
    openings = context.calculations("bank_opening", f"bank:{fact.bank_account_id}")
    if len(openings) != 1 or openings[0].period > fact.period:
        raise NeedsInformation("bank_opening", "需要明确账面起点；银行流水期初不能自动代替账面期初")
    previous = context.select(
        Read("calculation", fact.kind, f"bank:{fact.bank_account_id}", fact.period)
    )
    prior = max(previous, key=lambda item: item.period, default=None)
    if prior:
        if prior.period.ordinal + 1 != fact.period.ordinal:
            raise NeedsInformation("previous_reconciliation", "需先完成中间月份的银行对账")
        opening_fen = prior.values["closing_fen"]
    else:
        if openings[0].period != fact.period:
            raise NeedsInformation("previous_reconciliation", "需从明确账面起点月份连续完成对账")
        opening_fen = openings[0].values["opening_fen"]
    if opening_fen != statement.opening_fen:
        raise KernelError("bank_opening_difference", "流水期初与账面起点或上期对账不一致")
    entries = {entry.reference: entry for entry in statement.entries}
    if len({match.reference for match in fact.matches}) != len(fact.matches) or set(entries) != {
        match.reference for match in fact.matches
    }:
        raise NeedsInformation("matches", "每笔银行流水必须有且仅有一项明确资金匹配")
    expected = published_cash(
        context, fact.bank_account_id, f"bank:{fact.bank_account_id}:{fact.period}"
    )
    matched = set()
    for match in fact.matches:
        key = (match.source_kind, match.source_id)
        if key in matched:
            raise KernelError("duplicate_cash_match", "一项实际资金不能重复匹配")
        matched.add(key)
        real = context.one(match.source_kind, "@" + match.source_id).fact
        amount = cash_amount(real, fact.bank_account_id)
        entry = entries[match.reference]
        if real.actual_date != entry.actual_date or amount != entry.signed_fen:
            raise KernelError("bank_match_difference", "匹配的实际日期、金额或方向存在差异")
        if key not in expected:
            raise NeedsInformation(
                "actual_funds", "匹配资金尚未形成当前账户月份的正式结果", sources=(match.source_id,)
            )
    if matched != set(expected):
        raise NeedsInformation("matches", "账面实际资金与银行流水尚未完整对应")
    if (
        sum_fen((opening_fen, *(amount for _, amount in expected.values())))
        != statement.closing_fen
    ):
        raise KernelError("bank_closing_difference", "正式账面资金变动与流水期末余额不符")
    return Outcome(
        (),
        {
            "bank_account_id": fact.bank_account_id,
            "statement_id": fact.statement_id,
            "opening_fen": opening_fen,
            "closing_fen": statement.closing_fen,
            "matched_count": len(matched),
            "balanced": True,
        },
    )


def required_reads(period: YearMonth):
    before = YearMonth.from_ordinal(period.ordinal + 1)
    return tuple(
        Read(source, kind, "*" if kind == "bank_opening" else str(period), before)
        for source in ("fact", "calculation")
        for kind in ("bank_opening", "bank_statement", "bank_reconciliation")
    )


def required_work(period: YearMonth, context):
    """An established bank account needs a statement even in an idle month."""
    selected = {(read.source, read.kind): context.select(read) for read in required_reads(period)}
    posted = {
        row.fact_id
        for kind in ("bank_opening", "bank_statement", "bank_reconciliation")
        for row in selected[("calculation", kind)]
    }
    openings = {row.fact.bank_account_id: row for row in selected[("fact", "bank_opening")]}
    statements = {
        row.fact.bank_account_id: row
        for row in selected[("fact", "bank_statement")]
        if row.fact.period == period
    }
    reconciliations = {
        row.fact.bank_account_id: row
        for row in selected[("fact", "bank_reconciliation")]
        if row.fact.period == period
    }
    accounts = set(openings) | {
        row.fact.bank_account_id for row in selected[("fact", "bank_statement")]
    }
    issues = []
    for account in sorted(accounts):
        established = openings.get(account)
        if established is None or established.id not in posted:
            issues.append(
                {
                    "field": "bank_opening",
                    "bank_account_id": account,
                    "message": "已知银行账户需要明确并正式确认账面起点",
                }
            )
        statement = statements.get(account)
        if statement is None or statement.id not in posted:
            issues.append(
                {
                    "field": "bank_statement",
                    "bank_account_id": account,
                    "period": str(period),
                    "message": "已启用账户本月需要完整流水；无资金变动也须明确核对",
                }
            )
        reconciliation = reconciliations.get(account)
        if reconciliation is None or reconciliation.id not in posted:
            issues.append(
                {
                    "field": "bank_reconciliation",
                    "bank_account_id": account,
                    "period": str(period),
                    "message": "已启用账户本月银行对账尚未完成",
                }
            )
    return issues


def register(registry):
    registry.register(BankOpening, calculate_opening)
    registry.register(BankStatement, calculate_statement)
    registry.register(BankReconciliation, calculate_reconciliation)
    registry.register_readiness("bank_accounts", required_reads, required_work)
