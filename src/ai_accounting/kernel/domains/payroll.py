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
PAYROLL_KINDS = ("payroll", "payroll_bounded")
UNKNOWN_DEDUCTIONS = (
    "tax_exempt_income_fen",
    "special_additional_deduction_fen",
    "other_legal_deduction_fen",
    "tax_relief_fen",
)


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


def _tax_input(value: dict, period: YearMonth | None = None) -> CumulativeTaxPeriodInput:
    fields = dict(value)
    if fields["income_date"] is None and period is not None:
        fields["income_date"] = _month(period).end_date.isoformat()
    if len(fields["withholding_start_date"]) == 7:
        fields["withholding_start_date"] += "-01"
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
    withholding_start_date: ActualDate | YearMonth = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "wage_withholding_relationship_start",
                "allowed_precision": ["month", "day"],
                "reusable_sources": [
                    "confirmed_first_withholding_month",
                    "withholding_registration",
                ],
                "constraint": "保留明确月份或实际日；累计减除按月，不将月起点记作实际开始日",
            }
        },
    )
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


class PayrollWithholdingActual(Fact):
    """Confirmed actual withholding, distinct from a declaration or bank payment."""

    kind: ClassVar[str] = "payroll_withholding_actual"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    withheld_tax_fen: NonNegativeFen = Field(
        description="有原始扣税记录或负责人确认依据的本期实际工资扣税额，不是预估或仅拟申报金额",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "observed_wage_tax_withholding",
                "allowed_precision": ["integer_fen"],
                "reusable_sources": ["confirmed_tax_withholding", "actual_wage_tax_record"],
            }
        },
    )
    withholding_confirmed: Literal[True]
    reported_cumulative_standard_deduction_fen: NonNegativeFen | None = Field(
        default=None,
        description="原申报明确的本税期累计基本减除费用；未知保留空，不改写扣缴关系起点",
    )

    def scopes(self) -> tuple[str, ...]:
        return (employee_month(self.employee_id, self.period),)


class Payroll(Fact):
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.accounting_gross_salary_fen": "result.gross_fen"
    }
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
            for kind in (*PAYROLL_KINDS, "annual_bonus")
        )
        return (
            *(Read("fact", kind, current_scope) for kind in PAYROLL_KINDS),
            Read("fact", PayrollProfile.kind, f"@{self.profile_id}"),
            Read("fact", PayrollContributionPolicy.kind, f"@{self.contribution_policy_id}"),
            Read("fact", PayrollIncomeTaxPolicy.kind, f"@{self.income_tax_policy_id}"),
            Read("fact", PayrollContributionActual.kind, current_scope),
            Read("fact", PayrollOpeningState.kind, year_scope),
            Read("fact", "opening_payroll_state", year_scope),
            Read("calculation", "opening_payroll_state", year_scope),
            Read("fact", PayrollFirstWageTreatment.kind, year_scope),
            Read("fact", PayrollWithholdingActual.kind, current_scope),
            *prior,
        )


UnknownDeduction = Annotated[
    NonNegativeFen | None,
    Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "deduction_unknown_until_supported",
                "reusable_sources": ["confirmed_payroll_deductions", "tax_deduction_record"],
                "constraint": "null保留未知；仅税额上界为零时可发布，不作为已知零值",
            }
        },
    ),
]


class PayrollBounded(Fact):
    """Wage facts with explicit month precision and unresolved nonnegative deductions."""

    kind: ClassVar[str] = "payroll_bounded"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    material_amount_aliases: ClassVar[dict[str, str]] = Payroll.material_amount_aliases
    employee_id: Identifier
    profile_id: Identifier
    contribution_policy_id: Identifier
    income_tax_policy_id: Identifier
    accounting_gross_salary_fen: NonNegativeFen
    tax_reported_salary_fen: NonNegativeFen
    tax_exempt_income_fen: UnknownDeduction
    special_additional_deduction_fen: UnknownDeduction
    other_legal_deduction_fen: UnknownDeduction
    tax_relief_fen: UnknownDeduction
    expense_class: ExpenseClass
    contribution_basis: Literal["policy_until_actual", "actual_required"]
    tax_income_date: ActualDate | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "tax_income_date_when_day_affects_treatment",
                "allowed_precision": ["month", "day"],
                "reusable_sources": ["confirmed_tax_income_period", "actual_payment"],
                "constraint": "缺日仅当规则覆盖整个period且计算与日无关；不推定实际付款日",
            }
        },
    )

    @model_validator(mode="after")
    def valid_income_date(self):
        if self.tax_income_date is not None and self.tax_income_date.period != self.period:
            raise ValueError("tax income day must belong to the declared tax month")
        return self

    scopes = Payroll.scopes
    reads = Payroll.reads


def _unique_subject(version: FactVersion, context: Context, key: str) -> None:
    kinds = PAYROLL_KINDS if version.fact.kind in PAYROLL_KINDS else (version.fact.kind,)
    facts = [item for kind in kinds for item in context.facts(kind, key)]
    if any(item.subject_id != version.subject_id for item in facts):
        raise KernelError("duplicate_remuneration", "同一人员和所属期存在另一笔有效薪酬")


def _optional_one(context: Context, kind: str, key: str) -> FactVersion | None:
    items = context.facts(kind, key)
    if len(items) > 1:
        raise KernelError("ambiguous_source", f"同一范围存在多个 {kind} 来源")
    return items[0] if items else None


@dataclass(frozen=True)
class _TaxBasis:
    # This internal state contains lower bounds only when unknown_fields is nonempty.
    lower_bound: CumulativeTaxState
    unknown_fields: tuple[dict, ...] = ()


def _prior_state(
    fact: Payroll | PayrollBounded, context: Context, first_withholding_period: YearMonth
) -> _TaxBasis:
    opening = _optional_one(
        context, PayrollOpeningState.kind, employee_year(fact.employee_id, fact.period)
    )
    continuation = _continuation_state(context, employee_year(fact.employee_id, fact.period))
    if opening is not None and continuation is not None:
        raise KernelError("ambiguous_source", "不能同时采用独立工资期初与公司接续累计期初")
    opening = opening or continuation
    candidates = []
    for month in range(int(first_withholding_period[5:]), int(fact.period[5:])):
        for kind in (*PAYROLL_KINDS, "annual_bonus"):
            scope = employee_month(fact.employee_id, f"{fact.period[:4]}-{month:02}")
            for calculation in context.calculations(kind, scope):
                if (
                    calculation.values.get("tax_state") is not None
                    or calculation.values.get("tax_state_bounds") is not None
                ):
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
        values = chosen[0].values
        if values.get("tax_state") is not None:
            return _TaxBasis(_state(values["tax_state"]))
        bounds = values["tax_state_bounds"]
        return _TaxBasis(_state(bounds["lower_bound"]), tuple(bounds["unknown_fields"]))
    if opening is None:
        raise NeedsInformation(
            "payroll_opening_state",
            "需要明确的个税累计期初数，已知为零也须明确确认",
            sources=(employee_year(fact.employee_id, fact.period),),
        )
    return _TaxBasis(opening.fact.calculation_state())


def _deduction_requirement(unknown_fields, message):
    error = NeedsInformation(unknown_fields[0]["field"], message)
    error.issues = [
        {
            **item,
            "message": message,
            "semantics": "accounting",
            "allowed_precision": ["integer_fen"],
            "reusable_sources": [item["fact_id"], "confirmed_payroll_deductions"],
        }
        for item in unknown_fields
    ]
    return error


def _assert_month_policy(policy, period, field):
    first = date(int(period[:4]), int(period[5:]), 1)
    last = _month(period).end_date
    if date.fromisoformat(policy.effective_from) > first or (
        policy.effective_to is not None and date.fromisoformat(policy.effective_to) < last
    ):
        raise NeedsInformation(
            field,
            "月份精度要求政策完整覆盖本月；日内政策变化需明确适用日期或月份处理依据",
            sources=(policy.kind,),
            precision=("day", "month"),
        )


def _withholding_upper_bound(policy, taxable_upper, relief_lower, withheld):
    # Nonnegative rates are monotone within each bracket, but custom policy data
    # need not be continuous at boundaries. Check every reachable upper endpoint.
    assessed_upper, lower = 0, 0
    for bracket in policy.brackets:
        if lower > taxable_upper:
            break
        upper = min(taxable_upper, bracket.upper_bound_fen or taxable_upper)
        assessed_upper = max(
            assessed_upper,
            int((Decimal(upper) * Decimal(bracket.rate)).quantize(Decimal("1")))
            - bracket.quick_deduction_fen,
        )
        if bracket.upper_bound_fen is None:
            break
        lower = bracket.upper_bound_fen + 1
    return max(0, assessed_upper - relief_lower - withheld)


def _continuation_state(context, scope):
    source = _optional_one(context, "opening_payroll_state", scope)
    results = context.calculations("opening_payroll_state", scope)
    if source is not None and (len(results) != 1 or results[0].fact_id != source.id):
        raise NeedsInformation("opening_package", "人员累计期初尚未由完整接续清单核验发布")
    return source


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
def calculate_payroll(version: FactVersion, context: Context) -> Outcome:
    fact = version.fact
    assert isinstance(fact, (Payroll, PayrollBounded))
    scope = employee_month(fact.employee_id, fact.period)
    _unique_subject(version, context, scope)
    withholding = _optional_one(context, PayrollWithholdingActual.kind, scope)
    if withholding is not None:
        if not withholding.evidence:
            raise NeedsInformation("withholding_actual.evidence", "确认实际工资扣税需要依据")
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
    period = _month(fact.period)
    income_day = period.end_date
    if isinstance(fact, PayrollBounded):
        _assert_month_policy(contribution_policy.fact, fact.period, "contribution_policy_id")
        if fact.tax_income_date is not None:
            income_day = date.fromisoformat(fact.tax_income_date)
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
    first_withholding_period = max(
        YearMonth(f"{fact.period[:4]}-01"), YearMonth(profile.withholding_start_date[:7])
    )
    if fact.period < first_withholding_period:
        if withholding is not None:
            raise NeedsInformation(
                "withholding_start_date", "实际扣税与未来扣缴起点冲突，需核对已有事实"
            )
        if fact.tax_reported_salary_fen:
            raise NeedsInformation(
                "tax_reported_salary_fen",
                "税报收入与已确认的未来扣缴起点冲突，须核对税收入月份和扣缴起点",
                sources=(profile_version.id,),
                precision=("month",),
            )
        if fact.accounting_gross_salary_fen != burden.employee_total_fen:
            raise NeedsInformation(
                "accounting_gross_salary_fen",
                "扣缴起点前仅可处理有据的零净工资社保成本；非零净工资须明确收入归属",
                sources=(profile_version.id, *([actual.id] if actual else [])),
                precision=("integer_fen",),
            )
        if fact.tax_exempt_income_fen:
            raise KernelError("invalid_tax_input", "免税收入不能超过已确认的税报收入")
        return _remuneration_outcome(
            version,
            {"management": "560201", "sales": "560101", "service": "540101"}[fact.expense_class],
            fact.accounting_gross_salary_fen,
            (
                ("net", "221101", 0, fact.employee_id),
                ("tax", "222103", 0, None),
                ("employee_social", "224102", burden.employee_social_insurance_fen, None),
                ("employee_housing", "224103", burden.employee_housing_fund_fen, None),
                ("employer_social", "221102", burden.employer_social_insurance_fen, None),
                ("employer_housing", "221103", burden.employer_housing_fund_fen, None),
            ),
            {
                "employee_id": fact.employee_id,
                "gross_fen": fact.accounting_gross_salary_fen,
                "net_fen": 0,
                "tax_fen": 0,
                "employee_contributions_fen": burden.employee_total_fen,
                "employer_contributions_fen": burden.employer_total_fen,
                "tax_status": "not_started",
                "withholding_start": profile.withholding_start_date,
                "tax_state": None,
                "prior_tax_state": None,
                "tax_input": None,
                "rule_versions": [contribution_policy.id],
                "source_versions": [profile_version.id, *([actual.id] if actual else [])],
            },
            _trace(contributions.trace, burden.trace)
            + (
                {
                    "step": "withholding_not_started",
                    "values": {
                        "profile_fact_id": profile_version.id,
                        "withholding_start": profile.withholding_start_date,
                        "recognition_period": str(fact.period),
                        "meaning": "本月仅确认已有承担依据的社保成本和义务，不启动工资税累计",
                    },
                },
            ),
            employer_cost_fen=burden.employer_total_fen,
        )
    tax_policy = context.one(PayrollIncomeTaxPolicy.kind, f"@{fact.income_tax_policy_id}")
    if isinstance(fact, PayrollBounded) and fact.tax_income_date is None:
        _assert_month_policy(tax_policy.fact, fact.period, "tax_income_date")
    prior_basis = _prior_state(fact, context, first_withholding_period)
    prior = prior_basis.lower_bound
    unknown_fields = (
        *prior_basis.unknown_fields,
        *(
            {
                "kind": fact.kind,
                "subject_id": version.subject_id,
                "fact_id": version.id,
                "period": str(fact.period),
                "field": field,
            }
            for field in UNKNOWN_DEDUCTIONS
            if getattr(fact, field) is None
        ),
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
        income_date=income_day,
        withholding_start_date=date.fromisoformat(
            profile.withholding_start_date
            + ("-01" if len(profile.withholding_start_date) == 7 else "")
        ),
        income_fen=fact.tax_reported_salary_fen,
        tax_exempt_income_fen=fact.tax_exempt_income_fen or 0,
        employee_contributions_fen=burden.employee_total_fen,
        special_additional_deduction_fen=fact.special_additional_deduction_fen or 0,
        other_legal_deduction_fen=fact.other_legal_deduction_fen or 0,
        tax_relief_fen=fact.tax_relief_fen or 0,
        standard_deduction_start_month=(
            treatment.fact.standard_deduction_start_month if treatment else None
        ),
    )
    tax = calculate_cumulative_withholding(
        tax_policy.fact.calculation_policy(), period, prior, tax_input
    )
    estimated_upper = _withholding_upper_bound(
        tax_policy.fact,
        tax.cumulative_taxable_income_fen,
        tax.new_state.cumulative_tax_relief_fen,
        prior.cumulative_withheld_tax_fen,
    )
    if unknown_fields and estimated_upper and withholding is None:
        raise _deduction_requirement(
            unknown_fields, "现有扣除下界不能证明本期应扣税额为零，需补齐所列本期或历史事实"
        )
    actual_values = {}
    if withholding is not None:
        calculated_tax = tax
        observed_tax = withholding.fact.withheld_tax_fen
        reported_deduction = withholding.fact.reported_cumulative_standard_deduction_fen
        # Keep the rule-derived deduction clock for the next policy estimate.
        # The observed report's different deduction is preserved below, never
        # rewritten into an invented withholding-start date. Future estimates
        # must offset the actual amount already withheld, not today's estimate.
        tax = replace(
            tax,
            current_withholding_tax_fen=observed_tax,
            new_state=replace(
                tax.new_state,
                cumulative_withheld_tax_fen=sum_fen(
                    (prior.cumulative_withheld_tax_fen, observed_tax)
                ),
            ),
        )
        actual_values = {
            "tax_state_basis": "policy_deductions_with_actual_withholding",
            (
                "calculated_tax_upper_bound_fen" if unknown_fields else "calculated_tax_fen"
            ): calculated_tax.current_withholding_tax_fen,
            "calculated_tax_state": None
            if unknown_fields
            else _state_values(calculated_tax.new_state),
            "actual_withholding": {
                "fact_id": withholding.id,
                "withheld_tax_fen": observed_tax,
                "reported_cumulative_standard_deduction_fen": reported_deduction,
                "difference_from_calculation_fen": sum_fen(
                    (observed_tax, -calculated_tax.current_withholding_tax_fen)
                ),
                "evidence": list(withholding.evidence),
                "cash_payment_recorded": False,
            },
        }
    payroll = calculate_regular_payroll(
        RegularPayrollInput(
            fact.tax_reported_salary_fen,
            fact.special_additional_deduction_fen or 0,
            fact.other_legal_deduction_fen or 0,
            fact.accounting_gross_salary_fen,
        ),
        contributions,
        tax,
        burden,
    )
    input_values = _tax_input_values(tax_input)
    input_values["withholding_start_date"] = profile.withholding_start_date
    if isinstance(fact, PayrollBounded):
        # The representative day used by the pure month-based algorithm is not
        # an observed income/payment date and must not enter the stored facts.
        input_values["income_date"] = fact.tax_income_date
        input_values.update({field: getattr(fact, field) for field in UNKNOWN_DEDUCTIONS})
    uncertainty = {}
    tax_trace = tax.trace
    if unknown_fields:
        uncertainty = {
            "tax_state_bounds": {
                "lower_bound": _state_values(tax.new_state),
                "unknown_fields": list(unknown_fields),
                "uncertain_cumulative_fields": sorted(
                    {"cumulative_" + item["field"] for item in unknown_fields}
                ),
                "current_withholding_upper_bound_fen": estimated_upper,
            },
        }
        if prior_basis.unknown_fields:
            uncertainty["prior_tax_state_bounds"] = {
                "lower_bound": _state_values(prior),
                "unknown_fields": list(prior_basis.unknown_fields),
            }
        tax_trace = (
            TraceEntry(
                "withholding_estimate_bounds"
                if withholding is not None
                else "zero_withholding_proof",
                {
                    "rule_fact_id": tax_policy.id,
                    "taxable_income_upper_bound_fen": tax.cumulative_taxable_income_fen,
                    "tax_relief_lower_bound_fen": tax.new_state.cumulative_tax_relief_fen,
                    "known_prior_withheld_fen": prior.cumulative_withheld_tax_fen,
                    "current_withholding_upper_bound_fen": estimated_upper,
                    "unknown_fields": list(unknown_fields),
                    "meaning": (
                        "未知扣除保留计算上界；实际扣税由独立确认来源采用"
                        if withholding is not None
                        else "扣除下界仅用于零税证明，不是已知扣除累计或已申报明细"
                    ),
                },
            ),
        )
    if withholding is not None:
        tax_trace = (*tax_trace, TraceEntry("actual_withholding_adopted", actual_values))
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
            "tax_state": None if unknown_fields else _state_values(tax.new_state),
            "prior_tax_state": None if prior_basis.unknown_fields else _state_values(prior),
            "tax_input": input_values,
            **uncertainty,
            **actual_values,
            "rule_versions": [contribution_policy.id, tax_policy.id],
            "source_versions": [
                profile_version.id,
                *([actual.id] if actual else []),
                *([treatment.id] if treatment else []),
                *([withholding.id] if withholding is not None else []),
            ],
        },
        tuple(
            entry
            | (
                {
                    "values": entry["values"]
                    | {"withholding_start_date": profile.withholding_start_date}
                }
                if "withholding_start_date" in entry.get("values", {})
                else {}
            )
            for entry in _trace(contributions.trace, burden.trace, tax_trace, payroll.trace)
        ),
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
    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.bonus_fen": "result.gross_fen"}
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
            Read("fact", "opening_payroll_state", employee_year(self.employee_id, self.period)),
            Read(
                "calculation", "opening_payroll_state", employee_year(self.employee_id, self.period)
            ),
            Read("fact", self.kind, employee_year(self.employee_id, self.period)),
        )
        if self.regular_payroll_id:
            result += tuple(
                Read("calculation", kind, f"@{self.regular_payroll_id}") for kind in PAYROLL_KINDS
            )
        return result


@_translate_errors
def calculate_annual_bonus(version: FactVersion, context: Context) -> Outcome:
    fact = version.fact
    assert isinstance(fact, AnnualBonus)
    if fact.tax_method is None:
        raise NeedsInformation("tax_method", "需要明确选择奖金单独计税或并入综合所得")
    scope = employee_year(fact.employee_id, fact.period)
    old_usage = _optional_one(context, AnnualBonusOpeningUsage.kind, scope)
    continued_usage = _continuation_state(context, scope)
    if old_usage is not None and continued_usage is not None:
        raise KernelError("ambiguous_source", "不能同时采用独立奖金期初与接续累计记录")
    prior_usage = old_usage or continued_usage
    if prior_usage is None:
        raise NeedsInformation("annual_bonus_opening_usage", "需要明确本年度此前是否使用过单独计税")
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
        values = [
            item
            for kind in PAYROLL_KINDS
            for item in context.calculations(kind, f"@{fact.regular_payroll_id}")
        ]
        if len(values) != 1:
            raise NeedsInformation("regular_payroll_id", "需要已确认且唯一的当月工资计算")
        regular = values[0]
        if regular.period != fact.period or regular.values["employee_id"] != fact.employee_id:
            raise KernelError("bonus_regular_mismatch", "奖金引用的工资不属于本员工同月")
        if regular.values.get("tax_status") == "not_started":
            raise NeedsInformation(
                "regular_payroll_id",
                "本月来源仅处理扣缴起点前的社保成本，不能作为奖金工资税累计依据",
                sources=(regular.fact_id,),
                precision=("month",),
            )
        if regular.values.get("tax_state_bounds"):
            raise _deduction_requirement(
                regular.values["tax_state_bounds"]["unknown_fields"],
                "奖金方案比较和合并计税需要完整累计扣除；工资零税证明不能替代完整明细",
            )
    if fact.tax_method == "combined" and regular is None:
        raise NeedsInformation("regular_payroll_id", "奖金合并计税需要引用同月工资计算")
    regular_input = _tax_input(regular.values["tax_input"], regular.period) if regular else None
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
            "theoretical_tax_fen": None,
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
    material_amount_aliases: ClassVar[dict[str, str]] = {"fact.fixed_fee_fen": "result.gross_fen"}
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
        PayrollWithholdingActual,
        AnnualBonusPolicy,
        AnnualBonusOpeningUsage,
        LaborIncomeTaxPolicy,
    ):
        registry.register(model)
    registry.register(Payroll, calculate_payroll)
    registry.register(PayrollBounded, calculate_payroll)
    registry.register(AnnualBonus, calculate_annual_bonus)
    registry.register(LaborRemuneration, calculate_labor)
    registry.register(LaborAccrual, calculate_labor_accrual)
