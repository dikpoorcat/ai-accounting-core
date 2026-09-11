"""Evidence-backed continuation, distinct from transactions and actual cash."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
)
from ..types import ActualDate, Fen, NonNegativeFen, PositiveFen, YearMonth, sum_fen
from .investments import CostBasis, ShortTermClassification, cost_key, require_basis
from .payroll import PayrollOpeningState
from .transactions import Identifier, obligation


class OpeningDetail(Fact):
    immutable: ClassVar[bool] = True
    package_id: Identifier

    def scopes(self):
        return (str(self.period), f"opening:{self.package_id}")

    def reads(self):
        return (Read("calculation", "opening_package", "@" + self.package_id),)


class OpeningBank(OpeningDetail):
    kind: ClassVar[str] = "opening_bank"
    bank_account_id: Identifier
    balance_fen: NonNegativeFen

    def scopes(self):
        return (*super().scopes(), f"bank:{self.bank_account_id}")


class OpeningCash(OpeningDetail):
    kind: ClassVar[str] = "opening_cash"
    cash_account_id: Identifier
    balance_fen: NonNegativeFen

    def scopes(self):
        return (*super().scopes(), f"cash:{self.cash_account_id}")


ObligationNature = Literal[
    "customer_receivable",
    "supplier_service_payable",
    "supplier_administration_payable",
    "supplier_sales_payable",
    "employee_reimbursement",
    "owner_reimbursement",
    "deposit_receivable",
    "deposit_payable",
    "other_receivable",
    "other_payable",
]
OBLIGATION_MAPPING = {
    "customer_receivable": ("1122", "debit", "customer_receipts", None),
    "supplier_service_payable": ("2202", "credit", "operating_payments", "service"),
    "supplier_administration_payable": ("2202", "credit", "operating_payments", "administration"),
    "supplier_sales_payable": ("2202", "credit", "operating_payments", "sales"),
    "employee_reimbursement": ("2241", "credit", "employee_reimbursement", None),
    "owner_reimbursement": ("2241", "credit", "owner_reimbursement", None),
    "deposit_receivable": ("1221", "debit", "other_operating_receipts", None),
    "deposit_payable": ("2241", "credit", "pass_through_payments", None),
    "other_receivable": ("1221", "debit", "other_operating_receipts", None),
    "other_payable": ("2241", "credit", "pass_through_payments", None),
}


class OpeningObligation(OpeningDetail):
    kind: ClassVar[str] = "opening_obligation"
    counterparty_id: Identifier
    nature: ObligationNature
    outstanding_fen: PositiveFen
    business_reference: Identifier = Field(
        description="原业务或对账单身份，用于核对明细完整性，不能虚构历史收付"
    )


class OpeningAsset(OpeningDetail):
    kind: ClassVar[str] = "opening_asset"
    asset_type: Literal["fixed", "intangible"]
    cost_fen: PositiveFen
    accumulated_fen: NonNegativeFen
    in_use_date: ActualDate
    useful_life_months: Annotated[int, Field(strict=True, gt=0, le=1200)]
    completed_months: Annotated[int, Field(strict=True, ge=0, le=1200)]
    residual_fen: NonNegativeFen
    benefit_area: Literal["administration", "sales", "service"]
    rounding_policy: Literal["floor_final_remainder", "round_half_up_card"]

    @model_validator(mode="after")
    def consistent_card(self):
        if self.in_use_date.period >= self.period:
            raise ValueError("opening asset must have been in use before bookkeeping starts")
        if self.asset_type == "intangible" and (
            self.residual_fen or self.rounding_policy != "floor_final_remainder"
        ):
            raise ValueError("intangible opening uses zero residual and floor/final remainder")
        depreciable = self.cost_fen - self.residual_fen
        if depreciable < self.useful_life_months or self.accumulated_fen > depreciable:
            raise ValueError("invalid opening asset depreciable amount")
        start = self.in_use_date.period.ordinal + (self.asset_type == "fixed")
        expected_months = min(self.period.ordinal - start, self.useful_life_months)
        if self.completed_months != expected_months:
            raise ValueError("opening card must include every completed consumption month")
        base, remainder = divmod(depreciable, self.useful_life_months)
        if self.rounding_policy == "round_half_up_card":
            base += int(remainder * 2 >= self.useful_life_months)
            if base * (self.useful_life_months - 1) >= depreciable:
                raise ValueError("rounding would consume the asset before its final month")
        expected = (
            depreciable
            if self.completed_months == self.useful_life_months
            else base * self.completed_months
        )
        if expected != self.accumulated_fen:
            raise ValueError("opening accumulated consumption differs from the declared card rule")
        return self


class OpeningLoan(OpeningDetail):
    kind: ClassVar[str] = "opening_loan"
    agreement_id: Identifier
    lender_id: Identifier
    loan_term: Literal["short_term", "long_term"]
    principal_fen: PositiveFen
    accrued_interest_fen: NonNegativeFen
    interest_start: ActualDate = Field(description="接续计息起点；是建账边界，不是虚构放款日")

    @model_validator(mode="after")
    def cutover(self):
        if self.interest_start != f"{self.period}-01":
            raise ValueError("opening loan interest resumes at the bookkeeping boundary")
        return self

    def reads(self):
        return (*super().reads(), Read("fact", "loan_agreement", "@" + self.agreement_id))


TaxKind = Literal["vat", "surtax", "individual_income_tax", "enterprise_income_tax"]
TAX_MAPPING = {
    "vat": "222101",
    "surtax": "222102",
    "individual_income_tax": "222103",
    "enterprise_income_tax": "222106",
}


class OpeningTax(OpeningDetail):
    kind: ClassVar[str] = "opening_tax"
    authority_id: Identifier
    tax_kind: TaxKind
    balance_kind: Literal["payable", "refundable"]
    outstanding_fen: PositiveFen
    tax_period: YearMonth

    @model_validator(mode="after")
    def prior_tax(self):
        if self.tax_period >= self.period:
            raise ValueError("opening tax must belong to a prior accounting period")
        return self


class OpeningPayrollPayable(OpeningDetail):
    kind: ClassVar[str] = "opening_payroll_payable"
    employee_id: Identifier
    recipient_id: Identifier
    payroll_period: YearMonth
    component: Literal[
        "net",
        "withheld_tax",
        "employee_social",
        "employee_housing",
        "employer_social",
        "employer_housing",
    ]
    outstanding_fen: PositiveFen

    @model_validator(mode="after")
    def prior_payroll(self):
        if self.payroll_period >= self.period:
            raise ValueError("opening remuneration must belong to a prior period")
        if self.component == "net" and self.employee_id != self.recipient_id:
            raise ValueError("opening net wage must be payable to its employee")
        return self


class OpeningPayrollState(PayrollOpeningState):
    kind: ClassVar[str] = "opening_payroll_state"
    immutable: ClassVar[bool] = True
    package_id: Identifier
    separate_method_already_used: bool

    def calculation_state(self):
        source = PayrollOpeningState.model_validate(
            {
                key: value
                for key, value in self.model_dump().items()
                if key in PayrollOpeningState.model_fields
            }
        )
        return source.calculation_state()

    def scopes(self):
        return (*super().scopes(), f"opening:{self.package_id}")

    def reads(self):
        return (Read("calculation", "opening_package", "@" + self.package_id),)

    @model_validator(mode="after")
    def through_cutover(self):
        expected = (
            YearMonth.from_ordinal(self.period.ordinal - 1) if self.period[5:] != "01" else None
        )
        if self.through_period != expected:
            raise ValueError("opening cumulative payroll must end immediately before cutover")
        return self


class OpeningEquity(OpeningDetail):
    kind: ClassVar[str] = "opening_equity"
    equity_kind: Literal[
        "paid_in_capital", "capital_reserve", "surplus_reserve", "retained_earnings"
    ]
    balance_fen: Fen
    holder_or_basis_id: Identifier

    @model_validator(mode="after")
    def equity_sign(self):
        if self.equity_kind != "retained_earnings" and self.balance_fen < 0:
            raise ValueError("only retained earnings can represent an accumulated loss")
        return self


class OpeningMoneyFund(OpeningDetail):
    kind: ClassVar[str] = "opening_money_fund"
    fund_id: Identifier
    original_lot_reference: Identifier = Field(description="可复核的原申购或期初成本批次身份")
    classification: ShortTermClassification | None = None
    cost_basis: CostBasis | None = None
    cost_fen: PositiveFen | None = None

    def scopes(self):
        return (*super().scopes(), f"money-fund:{self.fund_id}")


MODELS = (
    OpeningBank,
    OpeningCash,
    OpeningObligation,
    OpeningAsset,
    OpeningLoan,
    OpeningTax,
    OpeningPayrollPayable,
    OpeningPayrollState,
    OpeningEquity,
    OpeningMoneyFund,
)
CATEGORIES = {
    model.kind: category
    for model, category in zip(
        MODELS,
        (
            "bank",
            "cash",
            "counterparties",
            "assets",
            "loans",
            "taxes",
            "payroll_payables",
            "payroll_cumulative",
            "equity",
            "assets",
        ),
        strict=True,
    )
}
OpeningKind = Literal[
    "opening_bank",
    "opening_cash",
    "opening_obligation",
    "opening_asset",
    "opening_loan",
    "opening_tax",
    "opening_payroll_payable",
    "opening_payroll_state",
    "opening_equity",
    "opening_money_fund",
]


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: OpeningKind
    subject_id: Identifier
    agreement_id: Identifier | None = None

    @model_validator(mode="after")
    def supporting_agreement(self):
        if (self.kind == "opening_loan") != (self.agreement_id is not None):
            raise ValueError("only opening loan members must identify their supporting agreement")
        return self


class Counts(BaseModel):
    """Every category must be stated; zero is an explicit no-items confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    bank: Annotated[int, Field(ge=0)]
    cash: Annotated[int, Field(ge=0)]
    counterparties: Annotated[int, Field(ge=0)]
    assets: Annotated[int, Field(ge=0)]
    loans: Annotated[int, Field(ge=0)]
    taxes: Annotated[int, Field(ge=0)]
    payroll_payables: Annotated[int, Field(ge=0)]
    payroll_cumulative: Annotated[int, Field(ge=0)]
    equity: Annotated[int, Field(ge=0)]


class OpeningPackage(Fact):
    kind: ClassVar[str] = "opening_package"
    immutable: ClassVar[bool] = True
    package_id: Identifier
    counts: Counts
    members: tuple[Member, ...]
    completeness_confirmed: Literal[True]

    def reads(self):
        return (
            Read("fact", self.kind, "*"),
            *(Read("fact", model.kind, f"opening:{self.package_id}") for model in MODELS),
            *(
                Read("fact", "loan_agreement", "@" + member.agreement_id)
                for member in self.members
                if member.agreement_id is not None
            ),
            *(
                Read("fact", "reimbursed_asset_batch", f"accepted-asset:{member.subject_id}")
                for member in self.members
                if member.kind == "opening_asset"
            ),
        )


def _line(account, signed):
    return Line(account, debit=signed) if signed > 0 else Line(account, credit=-signed)


def detail_output(version):
    """Internal fixed mappings; no public account or journal-direction input."""
    fact = version.fact
    lines, balances, obligations = [], [], []
    values = fact.model_dump(mode="json")

    def owed(account, normal, amount, party, cashflow, name="primary", expense_class=None):
        if not amount:
            return
        item = obligation(
            version,
            amount=amount,
            account=account,
            normal=normal,
            name=name,
            counterparty=party,
            cashflow=cashflow,
        )
        obligations.append(item)
        lines.append(_line(account, amount if normal == "debit" else -amount))
        balances.append(BalanceEffect(item["key"], amount, item["category"]))
        if expense_class is not None:
            values["expense_class"] = expense_class

    if isinstance(fact, (OpeningBank, OpeningCash)):
        bank = isinstance(fact, OpeningBank)
        if fact.balance_fen:
            lines.append(Line("1002" if bank else "1001", debit=fact.balance_fen))
            balances.append(
                BalanceEffect(
                    fact.bank_account_id if bank else fact.cash_account_id,
                    fact.balance_fen,
                    "bank" if bank else "cash",
                )
            )
        values["opening_fen"] = fact.balance_fen
    elif isinstance(fact, OpeningObligation):
        account, normal, cashflow, expense = OBLIGATION_MAPPING[fact.nature]
        owed(
            account,
            normal,
            fact.outstanding_fen,
            fact.counterparty_id,
            cashflow,
            expense_class=expense,
        )
    elif isinstance(fact, OpeningAsset):
        lines.append(Line("1601" if fact.asset_type == "fixed" else "1701", debit=fact.cost_fen))
        if fact.accumulated_fen:
            lines.append(
                Line("1602" if fact.asset_type == "fixed" else "1702", credit=fact.accumulated_fen)
            )
        balances.append(
            BalanceEffect(
                f"asset:{version.subject_id}:carrying",
                fact.cost_fen - fact.accumulated_fen,
                "asset",
            )
        )
        values["consumption_start"] = str(
            YearMonth.from_ordinal(fact.in_use_date.period.ordinal + (fact.asset_type == "fixed"))
        )
    elif isinstance(fact, OpeningMoneyFund):
        require_basis(version)
        if fact.cost_fen is None:
            raise NeedsInformation("cost_fen", "期初货币基金需要有据账面成本，不能从平台总余额推断")
        lines.append(Line("1101", debit=fact.cost_fen))
        balances.append(
            BalanceEffect(cost_key(version.subject_id), fact.cost_fen, "short_term_investment")
        )
    elif isinstance(fact, OpeningLoan):
        owed(
            "2001" if fact.loan_term == "short_term" else "2501",
            "credit",
            fact.principal_fen,
            fact.lender_id,
            "loan_repayment",
            "principal",
        )
        owed(
            "2231",
            "credit",
            fact.accrued_interest_fen,
            fact.lender_id,
            "interest_payments",
            "interest",
        )
    elif isinstance(fact, OpeningTax):
        owed(
            TAX_MAPPING[fact.tax_kind],
            "credit" if fact.balance_kind == "payable" else "debit",
            fact.outstanding_fen,
            fact.authority_id,
            "tax_payments" if fact.balance_kind == "payable" else "tax_refunds",
        )
    elif isinstance(fact, OpeningPayrollPayable):
        account = {
            "net": "221101",
            "withheld_tax": "222103",
            "employee_social": "224102",
            "employee_housing": "224103",
            "employer_social": "221102",
            "employer_housing": "221103",
        }[fact.component]
        owed(account, "credit", fact.outstanding_fen, fact.recipient_id, "payroll")
    elif isinstance(fact, OpeningEquity):
        account = {
            "paid_in_capital": "3001",
            "capital_reserve": "3002",
            "surplus_reserve": "3101",
            "retained_earnings": "3104",
        }[fact.equity_kind]
        if fact.balance_fen:
            lines.append(_line(account, -fact.balance_fen))
    values["obligations"] = obligations
    return tuple(lines), tuple(balances), values


def calculate_package(version: FactVersion, ctx: Context) -> Outcome:
    fact: OpeningPackage = version.fact
    if version.subject_id != fact.package_id:
        raise KernelError("opening_identity", "期初总清单业务身份须与 package_id 一致")
    if len(ctx.facts(fact.kind, "*")) != 1:
        raise KernelError("duplicate_opening", "一个公司只能采用一份期初接续总清单")
    expected = {(member.kind, member.subject_id) for member in fact.members}
    if len(expected) != len(fact.members) or len(
        {member.subject_id for member in fact.members}
    ) != len(fact.members):
        raise KernelError("duplicate_opening_member", "期初明细不能重复引用")
    sources = [
        row for model in MODELS for row in ctx.facts(model.kind, f"opening:{fact.package_id}")
    ]
    if {(row.fact.kind, row.subject_id) for row in sources} != expected:
        raise NeedsInformation(
            "members", "期初清单必须完整引用已确认明细，不得遗漏已接收的期初来源"
        )
    counts = {category: 0 for category in CATEGORIES.values()}
    seen = set()
    for row in sources:
        if row.fact.period != fact.period:
            raise KernelError("opening_period", "全部期初明细必须使用同一建账月份")
        if not row.evidence:
            raise NeedsInformation("evidence", "每项期初明细都需要明确依据")
        if isinstance(row.fact, OpeningAsset) and ctx.facts(
            "reimbursed_asset_batch", f"accepted-asset:{row.subject_id}"
        ):
            raise KernelError("duplicate_asset_acceptance", "已在验收批次中的资产不能再次计入期初")
        counts[CATEGORIES[row.fact.kind]] += 1
        identity = None
        if isinstance(row.fact, OpeningBank):
            identity = (row.fact.kind, row.fact.bank_account_id)
        elif isinstance(row.fact, OpeningCash):
            identity = (row.fact.kind, row.fact.cash_account_id)
        elif isinstance(row.fact, OpeningPayrollState):
            identity = (row.fact.kind, row.fact.employee_id)
        elif isinstance(row.fact, OpeningMoneyFund):
            identity = (row.fact.kind, row.fact.fund_id, row.fact.original_lot_reference)
        elif isinstance(row.fact, OpeningObligation):
            identity = (
                row.fact.kind,
                row.fact.counterparty_id,
                row.fact.nature,
                row.fact.business_reference,
            )
        if identity is not None and identity in seen:
            raise KernelError(
                "duplicate_opening_detail", "同一银行、现金、人员或原业务期初身份重复"
            )
        seen.add(identity)
    if counts != fact.counts.model_dump():
        raise NeedsInformation("counts", "期初明细数量与各类完整性确认不符，零项也必须明确确认")
    agreements = {
        member.subject_id: ctx.one("loan_agreement", "@" + member.agreement_id)
        for member in fact.members
        if member.agreement_id is not None
    }
    for row in sources:
        if isinstance(row.fact, OpeningLoan):
            agreement_version = agreements.get(row.subject_id)
            if agreement_version is None:
                raise NeedsInformation("agreement_id", "期初贷款需要既有合同来源")
            agreement = agreement_version.fact
            if agreement_version.subject_id != row.fact.agreement_id:
                raise KernelError("opening_loan_contract", "总清单与贷款明细引用不同合同")
            if (
                agreement.lender_id != row.fact.lender_id
                or agreement.loan_term != row.fact.loan_term
                or not agreement.lender_is_licensed
                or agreement.maturity_date <= row.fact.interest_start
            ):
                raise KernelError("opening_loan_contract", "期初贷款身份、期限或计息边界与合同不符")
    lines, balances, members = [], [], []
    for row in sorted(sources, key=lambda item: item.subject_id):
        member_lines, member_balances, values = detail_output(row)
        lines.extend(member_lines)
        balances.extend(member_balances)
        members.append(
            {
                "subject_id": row.subject_id,
                "fact_id": row.id,
                "kind": row.fact.kind,
                "values": values,
                "opening_lines": [asdict(line) for line in member_lines],
            }
        )
    debit, credit = sum_fen(line.debit for line in lines), sum_fen(line.credit for line in lines)
    if debit != credit:
        raise NeedsInformation(
            "opening_balance", "期初资产与负债、权益尚未平衡；需核对原账和明细，不能自动补差"
        )
    return Outcome(
        (),
        {
            "bookkeeping_start": str(fact.period),
            "members": members,
            "debit_fen": debit,
            "credit_fen": credit,
            "counts": counts,
        },
        tuple(balances),
        opening_lines=tuple(lines),
        opening=True,
    )


def calculate_detail(version: FactVersion, ctx: Context) -> Outcome:
    packages = ctx.calculations("opening_package", "@" + version.fact.package_id)
    if len(packages) != 1:
        raise NeedsInformation("opening_package", "须先完整核对并发布期初接续清单")
    members = [
        item for item in packages[0].values["members"] if item["subject_id"] == version.subject_id
    ]
    if len(members) != 1 or members[0]["fact_id"] != version.id:
        raise NeedsInformation("opening_package", "当前明细版本尚未由完整期初清单采用")
    return Outcome((), dict(members[0]["values"]))


def register(registry):
    for model in MODELS:
        registry.register(model, calculate_detail)
    registry.register(OpeningPackage, calculate_package)
