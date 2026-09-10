"""Versioned remuneration facts and pure calculators, with no persistence imports.

Every policy and opening balance is supplied explicitly. Contribution selections
include empty scopes; later actual assessments therefore invalidate their users.
Actual cash belongs to the payment module, which revalidates unchanged payments
against the replacement obligations produced here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from decimal import Context as DecimalContext
from functools import wraps
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_accounting.payroll import (
    AnnualBonusScenarioInput,
    AnnualBonusTaxMethod,
    AnnualBonusTaxPolicy,
    AnnualBonusUsage,
    CalculationValidationError,
    ContributionBaseKind,
    ContributionBases,
    ContributionLine,
    ContributionPolicy,
    ContributionResult,
    ContributionRule,
    CumulativeIncomeTaxPolicy,
    CumulativeTaxPeriodInput,
    CumulativeTaxState,
    EmployeeContributionShortfallTreatment,
    NeedsInformationError,
    RegularPayrollInput,
    RoundingRule,
    allocate_contribution_burden,
    calculate_annual_bonus_scenarios,
    calculate_contributions,
    calculate_cumulative_withholding,
    calculate_regular_payroll,
    select_annual_bonus_tax_method,
)
from ai_accounting.payroll import (
    YearMonth as CalculationMonth,
)
from ai_accounting.payroll.annual_bonus import AnnualBonusBracket
from ai_accounting.payroll.income_tax import TaxBracket
from ai_accounting.payroll.types import TraceEntry

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
from ..types import ActualDate, NonNegativeFen, PositiveFen, YearMonth, checked, sum_fen

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Rate = Annotated[str, Field(pattern=r"^(?:0(?:\.[0-9]{1,18})?|1(?:\.0{1,18})?)$")]
SourceURL = Annotated[str, Field(pattern=r"^https?://[^/\s]+(?:/[^\s]*)?$")]
ExpenseClass = Literal["management", "sales", "service"]


def employee_month(employee_id: str, period: str) -> str:
    return f"employee:{employee_id}:month:{period}"


def employee_year(employee_id: str, period: str) -> str:
    return f"employee:{employee_id}:year:{period[:4]}"


def _month(period: YearMonth) -> CalculationMonth:
    return CalculationMonth(int(period[:4]), int(period[5:]))


def _state_values(state: CumulativeTaxState) -> dict:
    result = asdict(state)
    for key, value in result.items():
        if key.endswith("_fen"):
            checked(value)
    result["through_period"] = str(state.through_period) if state.through_period else None
    return result


def _state(value: dict) -> CumulativeTaxState:
    fields = dict(value)
    through = fields["through_period"]
    fields["through_period"] = _month(YearMonth(through)) if through else None
    return CumulativeTaxState(**fields)


def _tax_input_values(value: CumulativeTaxPeriodInput) -> dict:
    result = asdict(value)
    for key in ("income_date", "withholding_start_date"):
        result[key] = result[key].isoformat()
    return result


def _tax_input(value: dict) -> CumulativeTaxPeriodInput:
    fields = dict(value)
    for key in ("income_date", "withholding_start_date"):
        fields[key] = date.fromisoformat(fields[key])
    return CumulativeTaxPeriodInput(**fields)


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


class TaxBracketFact(Detail):
    upper_bound_fen: PositiveFen | None
    rate: Rate
    quick_deduction_fen: NonNegativeFen


class PayrollIncomeTaxPolicy(EffectivePolicy):
    kind: ClassVar[str] = "payroll_income_tax_policy"
    legal_basis_source_url: SourceURL
    monthly_standard_deduction_fen: NonNegativeFen
    brackets: Annotated[tuple[TaxBracketFact, ...], Field(min_length=1)]

    def calculation_policy(self) -> CumulativeIncomeTaxPolicy:
        return CumulativeIncomeTaxPolicy(
            self.version,
            date.fromisoformat(self.effective_from),
            date.fromisoformat(self.effective_to) if self.effective_to else None,
            self.primary_source_url,
            self.legal_basis_source_url,
            self.monthly_standard_deduction_fen,
            tuple(
                TaxBracket(x.upper_bound_fen, Decimal(x.rate), x.quick_deduction_fen)
                for x in self.brackets
            ),
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
    withholding_start_date: ActualDate
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


class PayrollOpeningState(Fact):
    kind: ClassVar[str] = "payroll_opening_state"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    through_period: YearMonth | None
    cumulative_income_fen: NonNegativeFen
    cumulative_tax_exempt_income_fen: NonNegativeFen
    cumulative_standard_deduction_fen: NonNegativeFen
    cumulative_employee_contributions_fen: NonNegativeFen
    cumulative_special_additional_deduction_fen: NonNegativeFen
    cumulative_other_legal_deduction_fen: NonNegativeFen
    cumulative_tax_relief_fen: NonNegativeFen
    cumulative_withheld_tax_fen: NonNegativeFen

    def scopes(self) -> tuple[str, ...]:
        return (employee_year(self.employee_id, self.period),)

    def calculation_state(self) -> CumulativeTaxState:
        values = self.model_dump(exclude={"period", "employee_id", "through_period"})
        return CumulativeTaxState(
            tax_year=int(self.period[:4]),
            through_period=_month(self.through_period) if self.through_period else None,
            **values,
        )

    @model_validator(mode="after")
    def valid_state(self):
        self.calculation_state()
        return self


class PayrollFirstWageTreatment(Fact):
    kind: ClassVar[str] = "payroll_first_wage_treatment"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    standard_deduction_start_month: Annotated[int, Field(ge=1, le=12)]

    def scopes(self) -> tuple[str, ...]:
        return (employee_year(self.employee_id, self.period),)


class Payroll(Fact):
    kind: ClassVar[str] = "payroll"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    profile_id: Identifier
    contribution_policy_id: Identifier
    income_tax_policy_id: Identifier
    accounting_gross_salary_fen: NonNegativeFen
    tax_reported_salary_fen: NonNegativeFen
    tax_exempt_income_fen: NonNegativeFen
    special_additional_deduction_fen: NonNegativeFen
    other_legal_deduction_fen: NonNegativeFen
    tax_relief_fen: NonNegativeFen
    expense_class: ExpenseClass
    contribution_basis: Literal["policy_until_actual", "actual_required"]

    def scopes(self) -> tuple[str, ...]:
        return (employee_month(self.employee_id, self.period),)

    def reads(self) -> tuple[Read, ...]:
        current_scope = employee_month(self.employee_id, self.period)
        year_scope = employee_year(self.employee_id, self.period)
        prior = tuple(
            Read("calculation", kind, employee_month(self.employee_id, f"{self.period[:4]}-{m:02}"))
            for m in range(1, int(self.period[5:]))
            for kind in ("payroll", "annual_bonus")
        )
        return (
            Read("fact", self.kind, current_scope),
            Read("fact", PayrollProfile.kind, f"@{self.profile_id}"),
            Read("fact", PayrollContributionPolicy.kind, f"@{self.contribution_policy_id}"),
            Read("fact", PayrollIncomeTaxPolicy.kind, f"@{self.income_tax_policy_id}"),
            Read("fact", PayrollContributionActual.kind, current_scope),
            Read("fact", PayrollOpeningState.kind, year_scope),
            Read("fact", PayrollFirstWageTreatment.kind, year_scope),
            *prior,
        )


def _unique_subject(version: FactVersion, context: Context, key: str) -> None:
    facts = context.facts(version.fact.kind, key)
    if any(item.subject_id != version.subject_id for item in facts):
        raise KernelError("duplicate_remuneration", "同一人员和所属期存在另一笔有效薪酬")


def _optional_one(context: Context, kind: str, key: str) -> FactVersion | None:
    items = context.facts(kind, key)
    if len(items) > 1:
        raise KernelError("ambiguous_source", f"同一范围存在多个 {kind} 来源")
    return items[0] if items else None


def _prior_state(fact: Payroll, context: Context) -> CumulativeTaxState:
    opening = _optional_one(
        context, PayrollOpeningState.kind, employee_year(fact.employee_id, fact.period)
    )
    candidates = []
    for read in fact.reads():
        if read.source == "calculation":
            for calculation in context.calculations(read.kind, read.key):
                if calculation.values.get("tax_state") is not None:
                    candidates.append(calculation)
    if candidates:
        if (
            opening is not None
            and opening.fact.through_period is not None
            and (opening.fact.through_period >= min(item.period for item in candidates))
        ):
            raise KernelError("opening_state_overlap", "期初累计数与已形成的工资计算范围重叠")
        latest_period = max(item.period for item in candidates)
        latest = [item for item in candidates if item.period == latest_period]
        combined_bonus = [item for item in latest if item.kind == "annual_bonus"]
        chosen = combined_bonus or latest
        if len(chosen) != 1:
            raise KernelError("ambiguous_tax_state", "同一税期存在不唯一的累计计算终态")
        return _state(chosen[0].values["tax_state"])
    if opening is None:
        raise NeedsInformation(
            "payroll_opening_state",
            "需要明确的个税累计期初数，已知为零也须明确确认",
            sources=(employee_year(fact.employee_id, fact.period),),
        )
    return opening.fact.calculation_state()


def _remuneration_outcome(
    version: FactVersion,
    expense_account: str,
    gross_fen: int,
    obligations: tuple[tuple[str, str, int, str | None], ...],
    values: dict,
    explanation: tuple[dict, ...],
    *,
    employer_cost_fen: int = 0,
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
                "cashflow": "payroll" if version.fact.kind != "labor" else "labor",
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
def calculate_payroll(version: FactVersion, context: Context) -> Outcome:
    fact = version.fact
    assert isinstance(fact, Payroll)
    scope = employee_month(fact.employee_id, fact.period)
    _unique_subject(version, context, scope)
    profile_version = context.one(PayrollProfile.kind, f"@{fact.profile_id}")
    profile = profile_version.fact
    if profile.employee_id != fact.employee_id or not (
        profile.effective_from <= fact.period
        and (profile.effective_to is None or fact.period <= profile.effective_to)
    ):
        raise KernelError("payroll_profile_mismatch", "员工工资档案不属于本员工和所属期")
    contribution_policy = context.one(
        PayrollContributionPolicy.kind, f"@{fact.contribution_policy_id}"
    )
    tax_policy = context.one(PayrollIncomeTaxPolicy.kind, f"@{fact.income_tax_policy_id}")
    period = _month(fact.period)
    actual = _optional_one(context, PayrollContributionActual.kind, scope)
    if actual is not None and not actual.evidence:
        raise NeedsInformation("contribution_actual.evidence", "实际社保数需要留存证据")
    contributions = _assessed_contributions(
        contribution_policy.fact.calculation_policy(),
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
    prior = _prior_state(fact, context)
    first_withholding_period = max(
        YearMonth(f"{fact.period[:4]}-01"), YearMonth(profile.withholding_start_date[:7])
    )
    if (prior.through_period is None and fact.period > first_withholding_period) or (
        prior.through_period is not None
        and YearMonth(str(prior.through_period)).ordinal != fact.period.ordinal - 1
    ):
        raise NeedsInformation(
            "payroll_opening_state.through_period",
            "需要截至上月的完整累计数；缺失月份不能自动当作零工资",
            sources=(employee_year(fact.employee_id, fact.period),),
            precision=("month",),
        )
    treatment = _optional_one(
        context, PayrollFirstWageTreatment.kind, employee_year(fact.employee_id, fact.period)
    )
    if treatment is not None and not treatment.evidence:
        raise NeedsInformation("first_wage_treatment.evidence", "首次工资扣除处理需要留存依据")
    tax_input = CumulativeTaxPeriodInput(
        income_date=period.end_date,
        withholding_start_date=date.fromisoformat(profile.withholding_start_date),
        income_fen=fact.tax_reported_salary_fen,
        tax_exempt_income_fen=fact.tax_exempt_income_fen,
        employee_contributions_fen=burden.employee_total_fen,
        special_additional_deduction_fen=fact.special_additional_deduction_fen,
        other_legal_deduction_fen=fact.other_legal_deduction_fen,
        tax_relief_fen=fact.tax_relief_fen,
        standard_deduction_start_month=(
            treatment.fact.standard_deduction_start_month if treatment else None
        ),
    )
    tax = calculate_cumulative_withholding(
        tax_policy.fact.calculation_policy(), period, prior, tax_input
    )
    payroll = calculate_regular_payroll(
        RegularPayrollInput(
            fact.tax_reported_salary_fen,
            fact.special_additional_deduction_fen,
            fact.other_legal_deduction_fen,
            fact.accounting_gross_salary_fen,
        ),
        contributions,
        tax,
        burden,
    )
    return _remuneration_outcome(
        version,
        {"management": "560201", "sales": "560101", "service": "540101"}[fact.expense_class],
        payroll.gross_salary_fen,
        (
            ("net", "221101", payroll.net_pay_fen, fact.employee_id),
            ("tax", "222103", payroll.individual_income_tax_fen, None),
            ("employee_social", "224102", payroll.employee_social_insurance_fen, None),
            ("employee_housing", "224103", payroll.employee_housing_fund_fen, None),
            ("employer_social", "221102", payroll.employer_social_insurance_fen, None),
            ("employer_housing", "221103", payroll.employer_housing_fund_fen, None),
        ),
        {
            "employee_id": fact.employee_id,
            "gross_fen": payroll.gross_salary_fen,
            "net_fen": payroll.net_pay_fen,
            "tax_fen": payroll.individual_income_tax_fen,
            "employee_contributions_fen": burden.employee_total_fen,
            "employer_contributions_fen": burden.employer_total_fen,
            "tax_state": _state_values(tax.new_state),
            "prior_tax_state": _state_values(prior),
            "tax_input": _tax_input_values(tax_input),
            "rule_versions": [contribution_policy.id, tax_policy.id],
            "source_versions": [
                profile_version.id,
                *([actual.id] if actual else []),
                *([treatment.id] if treatment else []),
            ],
        },
        _trace(contributions.trace, burden.trace, tax.trace, payroll.trace),
        employer_cost_fen=burden.employer_total_fen,
    )


class AnnualBonusPolicy(EffectivePolicy):
    kind: ClassVar[str] = "annual_bonus_policy"
    brackets: Annotated[tuple[TaxBracketFact, ...], Field(min_length=1)]

    def calculation_policy(self) -> AnnualBonusTaxPolicy:
        return AnnualBonusTaxPolicy(
            self.version,
            date.fromisoformat(self.effective_from),
            date.fromisoformat(self.effective_to) if self.effective_to else None,
            self.primary_source_url,
            tuple(
                AnnualBonusBracket(x.upper_bound_fen, Decimal(x.rate), x.quick_deduction_fen)
                for x in self.brackets
            ),
        )

    @model_validator(mode="after")
    def valid_policy(self):
        self.calculation_policy()
        return self


class AnnualBonusOpeningUsage(Fact):
    kind: ClassVar[str] = "annual_bonus_opening_usage"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    separate_method_already_used: bool

    def scopes(self) -> tuple[str, ...]:
        return (employee_year(self.employee_id, self.period),)


class AnnualBonus(Fact):
    kind: ClassVar[str] = "annual_bonus"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    income_date: ActualDate
    bonus_fen: PositiveFen
    expense_class: ExpenseClass
    bonus_policy_id: Identifier
    income_tax_policy_id: Identifier
    tax_method: Literal["separate", "combined"] | None = None
    regular_payroll_id: Identifier | None = None

    @model_validator(mode="after")
    def income_period_matches(self):
        if self.income_date.period != self.period:
            raise ValueError("bonus income date must belong to its period")
        return self

    def scopes(self) -> tuple[str, ...]:
        return (
            employee_month(self.employee_id, self.period),
            employee_year(self.employee_id, self.period),
        )

    def reads(self) -> tuple[Read, ...]:
        result = (
            Read("fact", AnnualBonusPolicy.kind, f"@{self.bonus_policy_id}"),
            Read("fact", PayrollIncomeTaxPolicy.kind, f"@{self.income_tax_policy_id}"),
            Read(
                "fact", AnnualBonusOpeningUsage.kind, employee_year(self.employee_id, self.period)
            ),
            Read("fact", self.kind, employee_year(self.employee_id, self.period)),
        )
        if self.regular_payroll_id:
            result += (Read("calculation", Payroll.kind, f"@{self.regular_payroll_id}"),)
        return result


@_translate_errors
def calculate_annual_bonus(version: FactVersion, context: Context) -> Outcome:
    fact = version.fact
    assert isinstance(fact, AnnualBonus)
    if fact.tax_method is None:
        raise NeedsInformation("tax_method", "需要明确选择奖金单独计税或并入综合所得")
    scope = employee_year(fact.employee_id, fact.period)
    prior_usage = context.one(AnnualBonusOpeningUsage.kind, scope)
    other_bonuses = [
        x for x in context.facts(AnnualBonus.kind, scope) if x.subject_id != version.subject_id
    ]
    if any(x.fact.period == fact.period for x in other_bonuses):
        raise KernelError("duplicate_remuneration", "同一人员同月奖金必须在同一笔事实中合并确认")
    usage = AnnualBonusUsage(
        int(fact.period[:4]),
        prior_usage.fact.separate_method_already_used
        or any(x.fact.tax_method == "separate" for x in other_bonuses),
    )
    bonus_policy = context.one(AnnualBonusPolicy.kind, f"@{fact.bonus_policy_id}")
    wage_policy = context.one(PayrollIncomeTaxPolicy.kind, f"@{fact.income_tax_policy_id}")
    regular = None
    if fact.regular_payroll_id:
        values = context.calculations(Payroll.kind, f"@{fact.regular_payroll_id}")
        if len(values) != 1:
            raise NeedsInformation("regular_payroll_id", "需要已确认且唯一的当月工资计算")
        regular = values[0]
        if regular.period != fact.period or regular.values["employee_id"] != fact.employee_id:
            raise KernelError("bonus_regular_mismatch", "奖金引用的工资不属于本员工同月")
    if fact.tax_method == "combined" and regular is None:
        raise NeedsInformation("regular_payroll_id", "奖金合并计税需要引用同月工资计算")
    regular_input = _tax_input(regular.values["tax_input"]) if regular else None
    prior_state = _state(regular.values["prior_tax_state"]) if regular else None
    scenario_input = AnnualBonusScenarioInput(
        _month(fact.period),
        date.fromisoformat(fact.income_date),
        fact.bonus_fen,
        prior_state,
        regular_input,
        usage,
        regular.values["tax_fen"] if regular else None,
    )
    scenarios = calculate_annual_bonus_scenarios(
        bonus_policy.fact.calculation_policy(),
        wage_policy.fact.calculation_policy(),
        scenario_input,
    )
    selected = select_annual_bonus_tax_method(scenarios, AnnualBonusTaxMethod(fact.tax_method))
    next_state = None
    if fact.tax_method == "combined":
        tax = calculate_cumulative_withholding(
            wage_policy.fact.calculation_policy(),
            _month(fact.period),
            prior_state,
            replace(
                regular_input,
                income_date=date.fromisoformat(fact.income_date),
                income_fen=regular_input.income_fen + fact.bonus_fen,
            ),
        )
        next_state = _state_values(tax.new_state)
    return _remuneration_outcome(
        version,
        {"management": "560201", "sales": "560101", "service": "540101"}[fact.expense_class],
        fact.bonus_fen,
        (
            ("net", "221101", selected.net_bonus_fen, fact.employee_id),
            ("tax", "222103", selected.tax_fen, None),
        ),
        {
            "employee_id": fact.employee_id,
            "gross_fen": fact.bonus_fen,
            "net_fen": selected.net_bonus_fen,
            "tax_fen": selected.tax_fen,
            "tax_method": fact.tax_method,
            "tax_state": next_state,
            "rule_versions": [bonus_policy.id, wage_policy.id],
        },
        _trace(scenarios.trace),
    )


class LaborIncomeTaxPolicy(EffectivePolicy):
    kind: ClassVar[str] = "labor_income_tax_policy"
    small_payment_threshold_fen: PositiveFen
    fixed_expense_deduction_fen: NonNegativeFen
    large_payment_expense_rate: Rate
    brackets: Annotated[tuple[TaxBracketFact, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def valid_brackets(self):
        if self.brackets[-1].upper_bound_fen is not None:
            raise ValueError("labor brackets require a final unbounded bracket")
        previous = 0
        for item in self.brackets[:-1]:
            if item.upper_bound_fen is None or item.upper_bound_fen <= previous:
                raise ValueError("labor tax bracket bounds must increase")
            previous = item.upper_bound_fen
        return self


class LaborRemuneration(Fact):
    kind: ClassVar[str] = "labor"
    identity_fields: ClassVar[tuple[str, ...]] = ("person_id", "period")
    person_id: Identifier
    income_date: ActualDate
    policy_id: Identifier
    expense_class: ExpenseClass
    recipient_tax_status: Literal["ordinary_resident"]
    remuneration_method: Literal["fixed", "commission"]
    withholding_method: (
        Literal["net_after_withholding", "gross_paid_without_withholding"] | None
    ) = None
    gross_payment_kind: Literal["payment", "cash_payment"] | None = None
    gross_payment_id: Identifier | None = None
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

    def reads(self) -> tuple[Read, ...]:
        reads = [Read("fact", LaborIncomeTaxPolicy.kind, f"@{self.policy_id}")]
        if self.gross_payment_kind and self.gross_payment_id:
            reads.append(Read("fact", self.gross_payment_kind, f"@{self.gross_payment_id}"))
        return tuple(reads)


@_translate_errors
def calculate_labor(version: FactVersion, context: Context) -> Outcome:
    fact = version.fact
    assert isinstance(fact, LaborRemuneration)
    if fact.withholding_method is None:
        raise NeedsInformation(
            "withholding_method", "需明确本项劳务按净额扣缴，还是有依据的历史毛额未扣税事实"
        )
    policy_version = context.one(LaborIncomeTaxPolicy.kind, f"@{fact.policy_id}")
    policy = policy_version.fact
    policy.assert_effective(date.fromisoformat(fact.income_date))
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
    if gross <= policy.small_payment_threshold_fen:
        taxable = max(gross - policy.fixed_expense_deduction_fen, 0)
    else:
        taxable = int(
            (Decimal(gross) * (1 - Decimal(policy.large_payment_expense_rate))).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
    bracket = next(
        x for x in policy.brackets if x.upper_bound_fen is None or taxable <= x.upper_bound_fen
    )
    tax = max(
        int((Decimal(taxable) * Decimal(bracket.rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        - bracket.quick_deduction_fen,
        0,
    )
    if tax > gross:
        raise KernelError("negative_labor_net", "适用政策导致税额超过劳务报酬")
    withheld = tax
    if fact.withholding_method == "gross_paid_without_withholding":
        if not fact.gross_payment_kind or not fact.gross_payment_id:
            raise NeedsInformation("gross_payment_id", "历史毛额未扣税须引用明确的真实付款来源")
        paid = context.one(fact.gross_payment_kind, f"@{fact.gross_payment_id}")
        actual = paid.fact
        matching = [
            item
            for item in actual.allocations
            if item.source_kind == fact.kind
            and item.source_id == version.subject_id
            and item.obligation == "net"
        ]
        if not paid.evidence:
            raise NeedsInformation("gross_payment_id", "毛额未扣税的实际付款须有已登记证据")
        if actual.direction != "outflow" or actual.actual_date != fact.income_date:
            raise KernelError(
                "gross_labor_payment_conflict", "历史毛额劳务须核对真实付款日和资金付出方向"
            )
        if len(matching) != 1 or matching[0].amount_fen != gross:
            raise NeedsInformation(
                "gross_payment_id", "实际付款须完整对应本项劳务毛额，不能推定未扣税差异"
            )
        recipient = (
            matching[0].recipient_id
            if actual.payment_method == "bank_batch"
            else actual.counterparty_id
        )
        if recipient != fact.person_id:
            raise KernelError(
                "gross_labor_recipient_conflict", "毛额实际收款人必须与本项劳务人员一致"
            )
        withheld = 0
    elif fact.gross_payment_id is not None or fact.gross_payment_kind is not None:
        raise KernelError("unexpected_gross_payment", "净额扣缴处理不能同时声明历史毛额未扣税付款")
    return _remuneration_outcome(
        version,
        {"management": "560204", "sales": "560104", "service": "540104"}[fact.expense_class],
        gross,
        (("net", "224104", gross - withheld, fact.person_id),)
        + (
            (("tax", "222103", withheld, None),)
            if fact.withholding_method == "net_after_withholding"
            else ()
        ),
        {
            "person_id": fact.person_id,
            "gross_fen": gross,
            "net_fen": gross - withheld,
            "tax_fen": withheld,
            "theoretical_tax_fen": tax,
            "unwithheld_tax_fen": tax - withheld,
            "withholding_method": fact.withholding_method,
            "taxable_income_fen": taxable,
            "expense_deduction_fen": gross - taxable,
            "rule_versions": [policy_version.id],
        },
        (
            {
                "step": "labor_withholding",
                "method": fact.withholding_method,
                "rate": bracket.rate,
                "quick_deduction_fen": bracket.quick_deduction_fen,
                "rounding": "half_up_to_fen",
            },
        ),
    )


def register(registry: Registry) -> None:
    for model in (
        PayrollContributionPolicy,
        PayrollIncomeTaxPolicy,
        PayrollProfile,
        PayrollContributionActual,
        PayrollOpeningState,
        PayrollFirstWageTreatment,
        AnnualBonusPolicy,
        AnnualBonusOpeningUsage,
        LaborIncomeTaxPolicy,
    ):
        registry.register(model)
    registry.register(Payroll, calculate_payroll)
    registry.register(AnnualBonus, calculate_annual_bonus)
    registry.register(LaborRemuneration, calculate_labor)
