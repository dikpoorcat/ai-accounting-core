"""Original immutable payroll fact grammars, used only for retained body decoding.

No historical payroll evaluator is installed or callable from this module.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_accounting.payroll import (
    AnnualBonusTaxPolicy,
    ContributionBaseKind,
    ContributionPolicy,
    ContributionRule,
    CumulativeIncomeTaxPolicy,
    CumulativeTaxState,
    RoundingRule,
)
from ai_accounting.payroll import (
    YearMonth as CalculationMonth,
)
from ai_accounting.payroll.annual_bonus import AnnualBonusBracket
from ai_accounting.payroll.income_tax import TaxBracket
from ai_accounting.policy_sources import OfficialPolicySourceURL

from .contracts import (
    Fact,
    KernelError,
    Read,
)
from .payroll_confirmation import FactRevisionReference, confirmation_reads
from .types import (
    ActualDate,
    NonNegativeFen,
    PositiveFen,
    SubjectId,
    YearMonth,
)

Identifier = Annotated[str, Field(min_length=1, max_length=200)]

Rate = Annotated[str, Field(pattern=r"^(?:0(?:\.[0-9]{1,18})?|1(?:\.0{1,18})?)$")]

SourceURL = OfficialPolicySourceURL

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
    period: YearMonth = Field(
        description="首次工资处理开始适用的核算月份，不是录入或负责人确认月份",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "first_wage_treatment_effective_month",
                "allowed_precision": ["month"],
                "reusable_sources": ["confirmed_first_wage_treatment_effective_month"],
                "constraint": "仅工资所属期不早于本月时适用；有源适用月不能改为录入确认月",
            }
        },
    )
    employee_id: Identifier
    standard_deduction_start_month: Annotated[
        int,
        Field(
            ge=1,
            le=12,
            description="适用后累计基本减除费用的起算月，不改变本处理的适用首月",
            json_schema_extra={
                "x-accounting-fact": {
                    "role": "accounting",
                    "meaning": "cumulative_standard_deduction_clock_start",
                    "allowed_precision": ["month_number"],
                    "reusable_sources": ["confirmed_first_wage_standard_deduction_start_month"],
                    "constraint": "仅决定累计减除费用时钟；不向更早工资月份追溯应用本处理",
                }
            },
        ),
    ]

    def scopes(self) -> tuple[str, ...]:
        return (employee_year(self.employee_id, self.period),)

def first_wage_read(employee_id: str, period: YearMonth) -> Read:
    """Only treatments already effective in this wage month may be adopted."""
    cutoff = YearMonth.from_ordinal(period.ordinal + 1) if period.ordinal < 119987 else None
    return Read("fact", PayrollFirstWageTreatment.kind, employee_year(employee_id, period), cutoff)

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
    profile_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_wage_profile_version",
                "reusable_sources": ["payroll_profile"],
            }
        }
    )
    contribution_policy_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_wage_contribution_rule_version",
                "reusable_sources": ["payroll_contribution_policy"],
            }
        }
    )
    income_tax_policy_revision: FactRevisionReference | None = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_adopted_wage_income_tax_rule_version",
                "reusable_sources": ["payroll_income_tax_policy"],
                "constraint": "扣缴已开始时必须明确；尚未开始且未采用时为null",
            }
        }
    )
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

    def reads(self, subject_id: str | None = None) -> tuple[Read, ...]:
        from .identity_corrections import opening_binding_reads

        current_scope = employee_month(self.employee_id, self.period)
        year_scope = employee_year(self.employee_id, self.period)
        prior = tuple(
            Read("calculation", kind, employee_month(self.employee_id, f"{self.period[:4]}-{m:02}"))
            for m in range(1, int(self.period[5:]))
            for kind in (*PAYROLL_KINDS, "annual_bonus")
        )
        reads = (
            *(Read("fact", kind, current_scope) for kind in PAYROLL_KINDS),
            Read("fact", PayrollProfile.kind, "@" + self.profile_revision.subject_id),
            Read(
                "fact",
                PayrollContributionPolicy.kind,
                "@" + self.contribution_policy_revision.subject_id,
            ),
            *(
                (
                    Read(
                        "fact",
                        PayrollIncomeTaxPolicy.kind,
                        "@" + self.income_tax_policy_revision.subject_id,
                    ),
                )
                if self.income_tax_policy_revision is not None
                else ()
            ),
            Read("fact", PayrollContributionActual.kind, current_scope),
            Read("fact", PayrollOpeningState.kind, year_scope),
            Read("fact", "opening_payroll_state", year_scope),
            Read("calculation", "opening_payroll_state", year_scope),
            *opening_binding_reads(year_scope),
            first_wage_read(self.employee_id, self.period),
            Read("fact", PayrollWithholdingActual.kind, current_scope),
            *prior,
        )
        return tuple(dict.fromkeys((*reads, *confirmation_reads(self, subject_id))))

    def reads_for(self, subject_id: str) -> tuple[Read, ...]:
        return self.reads(subject_id)

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
    profile_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_wage_profile_version",
                "reusable_sources": ["payroll_profile"],
            }
        }
    )
    contribution_policy_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_wage_contribution_rule_version",
                "reusable_sources": ["payroll_contribution_policy"],
            }
        }
    )
    income_tax_policy_revision: FactRevisionReference | None = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_adopted_wage_income_tax_rule_version",
                "reusable_sources": ["payroll_income_tax_policy"],
                "constraint": "扣缴已开始时必须明确；尚未开始且未采用时为null",
            }
        }
    )
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
    reads_for = Payroll.reads_for

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
    bonus_policy_id: SubjectId
    income_tax_policy_id: SubjectId
    tax_method: Literal["separate", "combined"] | None = None
    regular_payroll_id: SubjectId | None = None

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
        from .identity_corrections import opening_binding_reads

        result = (
            Read("fact", AnnualBonusPolicy.kind, f"@{self.bonus_policy_id}"),
            Read("fact", PayrollIncomeTaxPolicy.kind, f"@{self.income_tax_policy_id}"),
            Read(
                "fact", AnnualBonusOpeningUsage.kind, employee_year(self.employee_id, self.period)
            ),
            Read("fact", "opening_payroll_state", employee_year(self.employee_id, self.period)),
            *opening_binding_reads(employee_year(self.employee_id, self.period)),
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
    policy_id: SubjectId
    expense_class: ExpenseClass
    recipient_tax_status: Literal["ordinary_resident"]
    remuneration_method: Literal["fixed", "commission"]
    withholding_method: (
        Literal["net_after_withholding", "gross_paid_without_withholding"] | None
    ) = None
    gross_payment_kind: Literal["payment", "cash_payment"] | None = None
    gross_payment_id: SubjectId | None = None
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

HistoricalPayroll = Payroll
HistoricalPayrollBounded = PayrollBounded
HistoricalPayrollProfile = PayrollProfile
HistoricalAnnualBonus = AnnualBonus
HistoricalLaborRemuneration = LaborRemuneration

# The original service registered every payroll-domain kind in this work area.
# Body-only models no longer pass through active registration, so retain that
# original presentation/material meaning explicitly as well as their grammar.
for _historical_model in (
    PayrollContributionPolicy, PayrollIncomeTaxPolicy, PayrollProfile,
    PayrollContributionActual, PayrollOpeningState, PayrollFirstWageTreatment,
    PayrollWithholdingActual, Payroll, PayrollBounded, AnnualBonusPolicy,
    AnnualBonusOpeningUsage, AnnualBonus, LaborIncomeTaxPolicy, LaborRemuneration,
):
    _historical_model.material_category = "payroll"

SPECIAL_COLUMNS = (
    "child_education",
    "continuing_education",
    "housing_loan_interest",
    "housing_rent",
    "elderly_support",
    "infant_care",
)

OTHER_COLUMNS = (
    "current_personal_pension",
    "enterprise_occupational_annuity",
    "commercial_health_insurance",
    "tax_deferred_pension_insurance",
    "official_transportation",
    "communication",
    "lawyer_case_expense",
    "housing_fund_adjustment",
    "tibet_additional_deduction",
    "other_deduction",
    "deductible_donation",
)

class HistoricalTaxImportDetails(Fact):
    kind: ClassVar[str] = "tax_import_details_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: str = Field(min_length=1)
    payroll_result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    cumulative_special_fen: dict[str, NonNegativeFen]
    current_other_fen: dict[str, NonNegativeFen]
    cumulative_personal_pension_fen: NonNegativeFen
    tax_relief_fen: NonNegativeFen
    treaty_relief_fen: NonNegativeFen
    remark: str = ""

    def scopes(self):
        return (str(self.period), employee_month(self.employee_id, self.period))

    @model_validator(mode="after")
    def exact_columns(self):
        if set(self.cumulative_special_fen) != set(SPECIAL_COLUMNS) or set(
            self.current_other_fen
        ) != set(OTHER_COLUMNS):
            raise ValueError("every deduction column must be explicitly supplied, including zero")
        return self
