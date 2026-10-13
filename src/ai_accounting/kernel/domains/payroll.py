"""Typed remuneration accounting from confirmed deductions and contribution facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from decimal import Context as DecimalContext
from functools import wraps
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_accounting.payroll import (
    CalculationValidationError,
    ContributionBaseKind,
    ContributionBases,
    ContributionLine,
    ContributionPolicy,
    ContributionResult,
    ContributionRule,
    EmployeeContributionShortfallTreatment,
    NeedsInformationError,
    RoundingRule,
    allocate_contribution_burden,
    calculate_contributions,
)
from ai_accounting.payroll import YearMonth as CalculationMonth
from ai_accounting.payroll.types import TraceEntry
from ai_accounting.policy_sources import OfficialPolicySourceURL

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
from ..payroll_confirmation import (
    FactRevisionReference,
    confirmation_reads,
    payroll_policy_source,
    resolve_payroll_confirmation,
)
from ..payroll_history import (
    AnnualBonusOpeningUsage,
    AnnualBonusPolicy,
    HistoricalAnnualBonus,
    HistoricalLaborRemuneration,
    HistoricalPayroll,
    HistoricalPayrollBounded,
    HistoricalPayrollProfile,
    LaborIncomeTaxPolicy,
    PayrollFirstWageTreatment,
    PayrollIncomeTaxPolicy,
    PayrollOpeningState,
    PayrollWithholdingActual,
)
from ..payroll_history import TaxBracketFact as TaxBracketFact
from ..remuneration_tax_basis import (
    remuneration_tax_reads,
    remuneration_tax_reads_after,
    resolve_remuneration_tax,
    tax_basis_values,
)
from ..types import ActualDate, NonNegativeFen, PositiveFen, YearMonth, checked, sum_fen

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Rate = Annotated[str, Field(pattern=r"^(?:0(?:\.[0-9]{1,18})?|1(?:\.0{1,18})?)$")]
SourceURL = OfficialPolicySourceURL
ExpenseClass = Literal["management", "sales", "service"]
PAYROLL_KINDS = ("payroll", "payroll_bounded")


def employee_month(employee_id: str, period: str) -> str:
    return f"employee:{employee_id}:month:{period}"


def employee_year(employee_id: str, period: str) -> str:
    return f"employee:{employee_id}:year:{period[:4]}"


def _month(period: YearMonth) -> CalculationMonth:
    return CalculationMonth(int(period[:4]), int(period[5:]))


def _trace(*groups) -> tuple[dict, ...]:
    return tuple(asdict(entry) for group in groups for entry in group)


class Detail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContributionRuleFact(Detail):
    code: Identifier
    base_kind: Literal["social_insurance", "housing_fund"]
    employee_rate: Rate
    employer_rate: Rate
    minimum_base_fen: NonNegativeFen
    maximum_base_fen: NonNegativeFen
    rounding: Literal["half_up", "down", "up"]
    enabled: bool

    def calculation_rule(self) -> ContributionRule:
        return ContributionRule(
            self.code,
            ContributionBaseKind(self.base_kind),
            Decimal(self.employee_rate),
            Decimal(self.employer_rate),
            self.minimum_base_fen,
            self.maximum_base_fen,
            RoundingRule(self.rounding),
            self.enabled,
        )


class EffectivePolicy(Fact):
    immutable: ClassVar[bool] = True
    version: Identifier
    effective_from: ActualDate
    effective_to: ActualDate | None
    primary_source_url: SourceURL

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("policy effective_to precedes effective_from")
        return self

    def assert_effective(self, day: date) -> None:
        if day < date.fromisoformat(self.effective_from) or (
            self.effective_to is not None and day > date.fromisoformat(self.effective_to)
        ):
            raise KernelError("policy_not_effective", "所引用规则不适用于业务所属日期")


class PayrollContributionPolicy(EffectivePolicy):
    kind: ClassVar[str] = "payroll_contribution_policy"
    jurisdiction: Identifier
    rules: Annotated[tuple[ContributionRuleFact, ...], Field(min_length=1)]

    def calculation_policy(self) -> ContributionPolicy:
        return ContributionPolicy(
            self.version,
            self.jurisdiction,
            date.fromisoformat(self.effective_from),
            date.fromisoformat(self.effective_to) if self.effective_to else None,
            self.primary_source_url,
            tuple(item.calculation_rule() for item in self.rules),
        )

    @model_validator(mode="after")
    def valid_policy(self):
        self.calculation_policy()
        return self


class PayrollProfile(Fact):
    kind: ClassVar[str] = "payroll_profile"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id",)
    employee_id: Identifier
    effective_from: YearMonth
    effective_to: YearMonth | None
    social_insurance_base_fen: NonNegativeFen | None
    housing_fund_base_fen: NonNegativeFen | None
    social_insurance_participating: bool
    housing_fund_participating: bool
    contribution_shortfall: Literal["reject", "employer_borne"]

    @model_validator(mode="after")
    def valid_periods(self):
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("profile effective_to precedes effective_from")
        return self


class ContributionActualItem(Detail):
    code: Identifier
    base_kind: Literal["social_insurance", "housing_fund"]
    state: Literal["declared", "not_declared"]
    employee_amount_fen: NonNegativeFen
    employer_amount_fen: NonNegativeFen

    @model_validator(mode="after")
    def undeclared_amounts_are_zero(self):
        if self.state == "not_declared" and (self.employee_amount_fen or self.employer_amount_fen):
            raise ValueError("not-declared actual amounts must be zero")
        return self


class PayrollContributionActual(Fact):
    kind: ClassVar[str] = "payroll_contribution_actual"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    items: Annotated[tuple[ContributionActualItem, ...], Field(min_length=1)]

    def scopes(self) -> tuple[str, ...]:
        return (employee_month(self.employee_id, self.period),)

    @model_validator(mode="after")
    def unique_items(self):
        keys = {(item.base_kind, item.code) for item in self.items}
        if len(keys) != len(self.items):
            raise ValueError("contribution actual kinds must be unique")
        return self


class Payroll(Fact):
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.accounting_gross_salary_fen": "result.gross_fen"
    }
    kind: ClassVar[str] = "payroll"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    profile_revision: FactRevisionReference
    contribution_policy_revision: FactRevisionReference
    accounting_gross_salary_fen: NonNegativeFen
    expense_class: ExpenseClass
    contribution_basis: Literal["policy_until_actual", "actual_required"]

    def scopes(self):
        return (employee_month(self.employee_id, self.period),)

    def reads(self, subject_id=None):
        reads = payroll_contribution_reads(self, subject_id)
        return reads + (
            remuneration_tax_reads(self.kind, subject_id) if subject_id is not None else ()
        )

    def reads_for(self, subject_id):
        return self.reads(subject_id)

    def reads_after(self, subject_id, context):
        return remuneration_tax_reads_after(self.kind, subject_id, context)


class PayrollBounded(Payroll):
    """Retains existing stable business identities; shares the current wage algorithm."""

    kind: ClassVar[str] = "payroll_bounded"


def payroll_contribution_reads(fact, subject_id=None):
    current_scope = employee_month(fact.employee_id, fact.period)
    return tuple(
        dict.fromkeys(
            (
                *(Read("fact", kind, current_scope) for kind in PAYROLL_KINDS),
                Read("fact", PayrollProfile.kind, "@" + fact.profile_revision.subject_id),
                Read(
                    "fact",
                    PayrollContributionPolicy.kind,
                    "@" + fact.contribution_policy_revision.subject_id,
                ),
                Read("fact", PayrollContributionActual.kind, current_scope),
                *confirmation_reads(fact, subject_id),
            )
        )
    )


def require_current_remuneration(version, model):
    if not isinstance(version.fact, model):
        raise NeedsInformation(
            "remuneration_revision",
            "历史报酬正文须明确修订为当前来源确认格式，不能重新计算个税",
            sources=(version.subject_id,),
        )


def _unique_subject(version: FactVersion, context: Context, key: str) -> None:
    kinds = PAYROLL_KINDS if version.fact.kind in PAYROLL_KINDS else (version.fact.kind,)
    facts = [item for kind in kinds for item in context.facts(kind, key)]
    if any(item.subject_id != version.subject_id for item in facts):
        raise KernelError("duplicate_remuneration", "同一人员和所属期存在另一笔有效薪酬")


def _optional_one(
    context: Context, kind: str, key: str, *, read: Read | None = None
) -> FactVersion | None:
    items = context.select(read) if read is not None else context.facts(kind, key)
    if len(items) > 1:
        raise KernelError("ambiguous_source", f"同一范围存在多个 {kind} 来源")
    return items[0] if items else None


def _remuneration_outcome(
    version: FactVersion,
    expense_account: str,
    gross_fen: int,
    obligations: tuple[tuple[str, str, int, str | None], ...],
    values: dict,
    explanation: tuple[dict, ...],
    *,
    employer_cost_fen: int = 0,
    cashflow: str | None = None,
) -> Outcome:
    cost = sum_fen((gross_fen, employer_cost_fen))
    lines = [Line(expense_account, debit=cost)] if cost else []
    balances, exported = [], []
    for name, account, amount, counterparty_id in obligations:
        checked(amount)
        if amount:
            lines.append(Line(account, credit=amount))
        key = f"{version.fact.kind}:{version.subject_id}:{name}"
        balances.append(BalanceEffect(key, amount, "payable"))
        exported.append(
            {
                "name": name,
                "key": key,
                "account": account,
                "normal": "credit",
                "amount_fen": amount,
                "category": "payable",
                "counterparty_id": counterparty_id,
                "cashflow": cashflow
                or ("labor" if version.fact.kind in {"labor", "labor_accrual"} else "payroll"),
                **(
                    {"reimbursement_acceptance_basis": "company_confirmation_month"}
                    if version.fact.kind in PAYROLL_KINDS
                    and name
                    in {
                        "employee_social",
                        "employee_housing",
                        "employer_social",
                        "employer_housing",
                    }
                    else {}
                ),
            }
        )
    return Outcome(tuple(lines), values | {"obligations": exported}, tuple(balances), explanation)


def _translate_errors(calculator):
    @wraps(calculator)
    def evaluate(version: FactVersion, context: Context) -> Outcome:
        try:
            # Integer fen (19 digits) and at most 18 decimal rate digits stay exact;
            # caller-local Decimal precision, rounding and traps cannot change a result.
            with localcontext(DecimalContext(prec=64, rounding=ROUND_HALF_UP)):
                return calculator(version, context)
        except NeedsInformationError as exc:
            first = exc.requirements[0]
            result = NeedsInformation(first.fields[0], first.message)
            result.issues = [
                {
                    "field": field,
                    "message": item.message,
                    "semantics": "accounting",
                    "reusable_sources": [],
                    "allowed_precision": [],
                }
                for item in exc.requirements
                for field in item.fields
            ]
            raise result from exc
        except CalculationValidationError as exc:
            raise KernelError(exc.code.lower(), str(exc)) from exc

    return evaluate


@dataclass(frozen=True)
class _ActualContributionLine(ContributionLine):
    """An assessed amount does not imply any known policy-calculation base."""

    input_base_fen: int | None
    capped_base_fen: int | None


def _assessed_contributions(
    policy: ContributionPolicy,
    bases: ContributionBases,
    day: date,
    actual: FactVersion | None,
    *,
    require_actual: bool,
) -> ContributionResult:
    policy.assert_effective(day)
    by_key = {(x.base_kind, x.code): x for x in actual.fact.items} if actual else {}
    policy_keys = {(str(rule.base_kind), rule.code) for rule in policy.rules}
    if by_key.keys() - policy_keys:
        raise KernelError("contribution_actual_kind_not_in_policy", "实际社保险种不在适用政策中")
    required = {
        (str(rule.base_kind), rule.code)
        for rule in policy.rules
        if rule.enabled and bases.participates_in(rule.base_kind)
    }
    if require_actual and not required <= by_key.keys():
        raise NeedsInformation(
            "contribution_actual.items",
            "实际数未覆盖全部适用险种",
            sources=("payroll_contribution_actual",),
            precision=("month",),
        )
    lines, trace = [], []
    for rule in policy.rules:
        item = by_key.get((str(rule.base_kind), rule.code))
        if item is None:
            calculated = calculate_contributions(replace(policy, rules=(rule,)), bases, day)
            lines.extend(calculated.lines)
            trace.extend(calculated.trace)
        else:
            lines.append(
                _ActualContributionLine(
                    rule.code,
                    rule.base_kind,
                    None,
                    None,
                    item.employee_amount_fen,
                    item.employer_amount_fen,
                    rule.enabled,
                )
            )
            trace.append(
                TraceEntry(
                    "contribution_actual",
                    {
                        "source_revision": actual.id,
                        "code": item.code,
                        "base_kind": item.base_kind,
                        "state": item.state,
                        "employee_amount_fen": item.employee_amount_fen,
                        "employer_amount_fen": item.employer_amount_fen,
                    },
                )
            )

    def total(kind: ContributionBaseKind, side: str) -> int:
        return sum_fen(
            getattr(item, f"{side}_contribution_fen") for item in lines if item.base_kind == kind
        )

    return ContributionResult(
        policy.version,
        policy.primary_source_url,
        tuple(lines),
        total(ContributionBaseKind.SOCIAL_INSURANCE, "employee"),
        total(ContributionBaseKind.SOCIAL_INSURANCE, "employer"),
        total(ContributionBaseKind.HOUSING_FUND, "employee"),
        total(ContributionBaseKind.HOUSING_FUND, "employer"),
        tuple(trace),
    )


@_translate_errors
def prepare_payroll_contributions(version: FactVersion, context: Context) -> dict:
    """Shared pre-tax owner-confirmed wage preparation for accounting and export."""
    require_current_remuneration(version, (Payroll, PayrollBounded))
    fact = version.fact
    scope = employee_month(fact.employee_id, fact.period)
    _unique_subject(version, context, scope)
    profile_version = payroll_policy_source(context, fact, "profile_revision", PayrollProfile.kind)
    profile = profile_version.fact
    contribution_policy = payroll_policy_source(
        context, fact, "contribution_policy_revision", PayrollContributionPolicy.kind
    )
    confirmation = resolve_payroll_confirmation(
        version,
        context,
        profile_version=profile_version,
        contribution_policy_version=contribution_policy,
    )
    period = _month(fact.period)
    policy = contribution_policy.fact
    if date.fromisoformat(policy.effective_from) > date(
        int(fact.period[:4]), int(fact.period[5:]), 1
    ) or (
        policy.effective_to is not None
        and date.fromisoformat(policy.effective_to) < period.end_date
    ):
        raise NeedsInformation(
            "contribution_policy_revision", "工资月份需由明确适用的社保公积金规则完整覆盖"
        )
    actual = _optional_one(context, PayrollContributionActual.kind, scope)
    if actual is not None and not actual.evidence:
        raise NeedsInformation("contribution_actual.evidence", "实际社保数需要留存证据")
    contributions = _assessed_contributions(
        policy.calculation_policy(),
        ContributionBases(
            profile.social_insurance_base_fen,
            profile.housing_fund_base_fen,
            profile.social_insurance_participating,
            profile.housing_fund_participating,
        ),
        period.end_date,
        actual,
        require_actual=fact.contribution_basis == "actual_required",
    )
    burden = allocate_contribution_burden(
        contributions,
        fact.accounting_gross_salary_fen,
        EmployeeContributionShortfallTreatment(profile.contribution_shortfall),
    )
    return {
        "profile_version": profile_version,
        "contribution_policy_version": contribution_policy,
        "actual": actual,
        "contributions": contributions,
        "burden": burden,
        "confirmation": confirmation,
    }


@_translate_errors
def calculate_payroll(version: FactVersion, context: Context) -> Outcome:
    prepared = prepare_payroll_contributions(version, context)
    fact = version.fact
    tax_source = resolve_remuneration_tax(version, context)
    tax = tax_source.fact.tax_amount_fen
    contributions, burden = prepared["contributions"], prepared["burden"]
    gross = fact.accounting_gross_salary_fen
    net = sum_fen((gross, -burden.employee_total_fen, -tax))
    if net < 0:
        raise KernelError("negative_net_pay", "已确认的员工承担社保公积金和扣税金额超过税前工资")
    actual = prepared["actual"]
    basis = tax_basis_values(tax_source)
    return _remuneration_outcome(
        version,
        {"management": "560201", "sales": "560101", "service": "540101"}[fact.expense_class],
        gross,
        (
            ("net", "221101", net, fact.employee_id),
            ("tax", "222103", tax, None),
            ("employee_social", "224102", burden.employee_social_insurance_fen, None),
            ("employee_housing", "224103", burden.employee_housing_fund_fen, None),
            ("employer_social", "221102", burden.employer_social_insurance_fen, None),
            ("employer_housing", "221103", burden.employer_housing_fund_fen, None),
        ),
        {
            "employee_id": fact.employee_id,
            "gross_fen": gross,
            "net_fen": net,
            "tax_fen": tax,
            "employee_contributions_fen": burden.employee_total_fen,
            "employer_contributions_fen": burden.employer_total_fen,
            "tax_basis": basis,
            "rule_versions": [prepared["contribution_policy_version"].id],
            "source_versions": [
                prepared["profile_version"].id,
                *([actual.id] if actual else []),
                tax_source.id,
            ],
            "payroll_confirmation": prepared["confirmation"],
        },
        (
            *_trace(contributions.trace, burden.trace),
            {"step": "confirmed_remuneration_tax", "values": {"tax_basis": basis}},
            {
                "step": "net_pay_reconciliation",
                "values": {
                    "gross_salary_fen": gross,
                    "employee_contributions_fen": burden.employee_total_fen,
                    "individual_income_tax_fen": tax,
                    "net_pay_fen": net,
                },
            },
        ),
        employer_cost_fen=burden.employer_total_fen,
    )


class AnnualBonus(Fact):
    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.bonus_fen": "result.gross_fen"}
    kind: ClassVar[str] = "annual_bonus"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    income_date: ActualDate
    bonus_fen: PositiveFen
    expense_class: ExpenseClass

    @model_validator(mode="after")
    def income_period_matches(self):
        if self.income_date.period != self.period:
            raise ValueError("bonus income date must belong to its period")
        return self

    def scopes(self):
        return (
            employee_month(self.employee_id, self.period),
            employee_year(self.employee_id, self.period),
        )

    def reads_for(self, subject_id):
        return (
            Read("fact", self.kind, employee_month(self.employee_id, self.period)),
            *remuneration_tax_reads(self.kind, subject_id),
        )

    def reads_after(self, subject_id, context):
        return remuneration_tax_reads_after(self.kind, subject_id, context)


def calculate_annual_bonus(version: FactVersion, context: Context) -> Outcome:
    require_current_remuneration(version, AnnualBonus)
    fact = version.fact
    _unique_subject(version, context, employee_month(fact.employee_id, fact.period))
    tax_source = resolve_remuneration_tax(version, context)
    tax = tax_source.fact.tax_amount_fen
    net = sum_fen((fact.bonus_fen, -tax))
    if net < 0:
        raise KernelError("negative_bonus_net", "已确认的奖金扣税金额超过奖金毛额")
    basis = tax_basis_values(tax_source)
    return _remuneration_outcome(
        version,
        {"management": "560201", "sales": "560101", "service": "540101"}[fact.expense_class],
        fact.bonus_fen,
        (("net", "221101", net, fact.employee_id), ("tax", "222103", tax, None)),
        {
            "employee_id": fact.employee_id,
            "gross_fen": fact.bonus_fen,
            "net_fen": net,
            "tax_fen": tax,
            "tax_basis": basis,
            "rule_versions": [],
            "source_versions": [tax_source.id],
        },
        ({"step": "confirmed_remuneration_tax", "values": {"tax_basis": basis}},),
    )


class LaborAccrual(Fact):
    """Earned remuneration whose actual payment and withholding have not occurred.

    This records a gross creditor claim, never a tax exemption or a completed
    payment/filing.  The recognition month is sufficient for the accrual; actual
    funds, tax treatment and external completion remain separate facts.
    """

    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.gross_fee_fen": "result.gross_fen"}

    kind: ClassVar[str] = "labor_accrual"
    identity_fields: ClassVar[tuple[str, ...]] = ("person_id", "period")
    person_id: Identifier
    expense_class: ExpenseClass
    gross_fee_fen: PositiveFen | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "earned_labor_gross_amount",
                "reusable_sources": ["confirmed_labor_schedule", "service_acceptance"],
            }
        },
    )
    tax_treatment: Literal["not_withheld_not_filed"] | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "labor_accrual_without_recorded_withholding",
                "reusable_sources": ["owner_confirmed_labor_status"],
            }
        },
    )

    def scopes(self) -> tuple[str, ...]:
        return (employee_month(self.person_id, self.period),)


def calculate_labor_accrual(version: FactVersion, context: Context) -> Outcome:
    fact = version.fact
    assert isinstance(fact, LaborAccrual)
    return labor_accrual_outcome(
        version,
        {"management": "560204", "sales": "560104", "service": "540104"}[fact.expense_class],
    )


def labor_accrual_outcome(
    version: FactVersion,
    cost_account: str,
    *,
    cashflow: str | None = None,
    extra_values: dict | None = None,
) -> Outcome:
    """One gross liability and an explicit unrecorded withholding state.

    Domain calculators select the cost destination. Public commands cannot
    supply accounts, journal lines or a second copy of this earned cost.
    """
    fact = version.fact
    if fact.gross_fee_fen is None:
        raise NeedsInformation("gross_fee_fen", "需要已确认的本期应计劳务毛额")
    if fact.tax_treatment is None:
        raise NeedsInformation(
            "tax_treatment", "需明确本项只计提劳务应付、尚未记录扣缴及申报，不能推定免税"
        )
    gross = fact.gross_fee_fen
    return _remuneration_outcome(
        version,
        cost_account,
        gross,
        (("net", "224104", gross, fact.person_id),),
        {
            "person_id": fact.person_id,
            "gross_fen": gross,
            "net_fen": gross,
            "tax_fen": 0,
            "withholding_method": "not_withheld_not_filed",
            "tax_assessed": False,
            "tax_review_required": True,
            "rule_versions": [],
            **(extra_values or {}),
        },
        (
            {
                "step": "labor_accrual",
                "recognition_period": str(fact.period),
                "gross_fee_fen": gross,
                "withholding_recorded_fen": 0,
                "tax_calculation": "not_performed",
                "external_filing": "not_recorded_by_this_accrual",
                "actual_payment": "separate_fact_required",
            },
        ),
        cashflow=cashflow,
    )


class LaborRemuneration(Fact):
    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.fixed_fee_fen": "result.gross_fen"}
    kind: ClassVar[str] = "labor"
    identity_fields: ClassVar[tuple[str, ...]] = ("person_id", "period")
    person_id: Identifier
    income_date: ActualDate
    expense_class: ExpenseClass
    remuneration_method: Literal["fixed", "commission"]
    fixed_fee_fen: PositiveFen | None = None
    commission_base_fen: PositiveFen | None = None
    commission_rate_ppm: Annotated[int, Field(gt=0, le=1_000_000)] | None = None

    @model_validator(mode="after")
    def valid_facts(self):
        if self.income_date.period != self.period:
            raise ValueError("labor income date must belong to its period")
        if self.remuneration_method == "fixed" and (
            self.commission_base_fen is not None or self.commission_rate_ppm is not None
        ):
            raise ValueError("fixed remuneration cannot also contain commission facts")
        if self.remuneration_method == "commission" and self.fixed_fee_fen is not None:
            raise ValueError("commission remuneration cannot also contain a fixed fee")
        return self

    def scopes(self):
        return (employee_month(self.person_id, self.period),)

    def reads_for(self, subject_id):
        return remuneration_tax_reads(self.kind, subject_id)

    def reads_after(self, subject_id, context):
        return remuneration_tax_reads_after(self.kind, subject_id, context)


def calculate_labor(version: FactVersion, context: Context) -> Outcome:
    require_current_remuneration(version, LaborRemuneration)
    fact = version.fact
    if fact.remuneration_method == "fixed":
        if fact.fixed_fee_fen is None:
            raise NeedsInformation("fixed_fee_fen", "需要已确认的固定劳务报酬金额")
        gross = fact.fixed_fee_fen
    else:
        if fact.commission_base_fen is None or fact.commission_rate_ppm is None:
            missing = [
                name
                for name in ("commission_base_fen", "commission_rate_ppm")
                if getattr(fact, name) is None
            ]
            error = NeedsInformation(missing[0], "需要佣金基数与明确比例")
            error.issues.extend(dict(error.issues[0], field=name) for name in missing[1:])
            raise error
        gross = checked(
            (fact.commission_base_fen * fact.commission_rate_ppm + 500_000) // 1_000_000
        )
        if gross <= 0:
            raise KernelError("zero_labor_remuneration", "佣金按分舍入后必须大于零")
    tax_source = resolve_remuneration_tax(version, context)
    tax = tax_source.fact.tax_amount_fen
    net = sum_fen((gross, -tax))
    if net < 0:
        raise KernelError("negative_labor_net", "已确认的劳务扣税金额超过劳务毛额")
    basis = tax_basis_values(tax_source)
    return _remuneration_outcome(
        version,
        {"management": "560204", "sales": "560104", "service": "540104"}[fact.expense_class],
        gross,
        (("net", "224104", net, fact.person_id), ("tax", "222103", tax, None)),
        {
            "person_id": fact.person_id,
            "gross_fen": gross,
            "net_fen": net,
            "tax_fen": tax,
            "tax_basis": basis,
            "rule_versions": [],
            "source_versions": [tax_source.id],
        },
        ({"step": "confirmed_remuneration_tax", "values": {"tax_basis": basis}},),
    )


def register(registry: Registry) -> None:
    from ..source24_history import PayrollPreRevision
    for model in (PayrollContributionPolicy, PayrollContributionActual):
        registry.register(model)
    registry.register(
        PayrollProfile,
        table="fact_payroll_profile_confirmed",
        historical=((HistoricalPayrollProfile, "fact_payroll_profile"),),
    )
    for model in (
        PayrollIncomeTaxPolicy,
        PayrollOpeningState,
        PayrollFirstWageTreatment,
        PayrollWithholdingActual,
        AnnualBonusPolicy,
        AnnualBonusOpeningUsage,
        LaborIncomeTaxPolicy,
    ):
        registry.register_history(model)
    for model, evaluator, historical in (
        (Payroll, calculate_payroll, HistoricalPayroll),
        (PayrollBounded, calculate_payroll, HistoricalPayrollBounded),
        (AnnualBonus, calculate_annual_bonus, HistoricalAnnualBonus),
        (LaborRemuneration, calculate_labor, HistoricalLaborRemuneration),
    ):
        registry.register(
            model,
            evaluator,
            table="fact_" + model.kind + "_confirmed",
            historical=(
                (historical, "fact_" + model.kind),
                *(((PayrollPreRevision, "fact_payroll_pre_revision"),)
                  if model.kind == "payroll" else ()),
            ),
        )
    registry.register(LaborAccrual, calculate_labor_accrual)
    from ..remuneration_tax_basis import register as register_tax_basis

    register_tax_basis(registry)
