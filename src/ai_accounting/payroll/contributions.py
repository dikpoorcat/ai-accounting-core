"""Pure social-insurance and housing-fund contribution calculation rules."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import StrEnum

from .types import (
    CalculationValidationError,
    ExpiredPolicyError,
    InformationRequirement,
    NeedsInformationError,
    RoundingRule,
    TraceEntry,
    require_decimal_rate,
    require_fen,
    require_source_url,
)


class ContributionBaseKind(StrEnum):
    SOCIAL_INSURANCE = "social_insurance"
    HOUSING_FUND = "housing_fund"


class EmployeeContributionShortfallTreatment(StrEnum):
    """Company policy for statutory employee contributions when gross pay is insufficient."""

    REJECT = "reject"
    EMPLOYER_BORNE = "employer_borne"


@dataclass(frozen=True)
class ContributionBases:
    """Employee profile bases in fen; absent values intentionally remain absent."""

    social_insurance_base_fen: int | None
    housing_fund_base_fen: int | None
    social_insurance_participating: bool = True
    housing_fund_participating: bool = True

    def __post_init__(self) -> None:
        if self.social_insurance_base_fen is not None:
            require_fen(self.social_insurance_base_fen, "social_insurance_base_fen")
        if self.housing_fund_base_fen is not None:
            require_fen(self.housing_fund_base_fen, "housing_fund_base_fen")

    def for_kind(self, base_kind: ContributionBaseKind) -> int | None:
        if base_kind == ContributionBaseKind.SOCIAL_INSURANCE:
            return self.social_insurance_base_fen
        if base_kind == ContributionBaseKind.HOUSING_FUND:
            return self.housing_fund_base_fen
        raise CalculationValidationError(
            "INVALID_BASE_KIND", f"unknown contribution base kind {base_kind}"
        )

    def participates_in(self, base_kind: ContributionBaseKind) -> bool:
        if base_kind == ContributionBaseKind.SOCIAL_INSURANCE:
            return self.social_insurance_participating
        if base_kind == ContributionBaseKind.HOUSING_FUND:
            return self.housing_fund_participating
        raise CalculationValidationError(
            "INVALID_BASE_KIND", f"unknown contribution base kind {base_kind}"
        )


@dataclass(frozen=True)
class ContributionRule:
    """One explicitly configured insurance type or housing-fund component."""

    code: str
    base_kind: ContributionBaseKind
    employee_rate: Decimal
    employer_rate: Decimal
    minimum_base_fen: int
    maximum_base_fen: int
    rounding_rule: RoundingRule
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.code:
            raise CalculationValidationError("INVALID_CONTRIBUTION_RULE", "code is required")
        if self.base_kind not in {
            ContributionBaseKind.SOCIAL_INSURANCE,
            ContributionBaseKind.HOUSING_FUND,
        }:
            raise CalculationValidationError("INVALID_BASE_KIND", "base_kind is not supported")
        require_decimal_rate(self.employee_rate, "employee_rate")
        require_decimal_rate(self.employer_rate, "employer_rate")
        require_fen(self.minimum_base_fen, "minimum_base_fen")
        require_fen(self.maximum_base_fen, "maximum_base_fen")
        if self.minimum_base_fen > self.maximum_base_fen:
            raise CalculationValidationError(
                "INVALID_CONTRIBUTION_RULE", "minimum_base_fen must not exceed maximum_base_fen"
            )
        if not self.enabled and (self.employee_rate != 0 or self.employer_rate != 0):
            raise CalculationValidationError(
                "INVALID_CONTRIBUTION_RULE",
                "a disabled contribution rule must explicitly use zero rates",
            )


@dataclass(frozen=True)
class ContributionPolicy:
    """Effective-dated, local policy configuration without city-specific code."""

    version: str
    jurisdiction: str
    effective_from: date
    effective_to: date | None
    primary_source_url: str
    rules: tuple[ContributionRule, ...]

    def __post_init__(self) -> None:
        if not self.version or not self.jurisdiction:
            raise CalculationValidationError(
                "INVALID_POLICY", "version and jurisdiction are required"
            )
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise CalculationValidationError(
                "INVALID_POLICY", "effective_to precedes effective_from"
            )
        require_source_url(self.primary_source_url)
        if not self.rules:
            raise CalculationValidationError(
                "INVALID_POLICY", "at least one contribution rule is required"
            )
        codes = [rule.code for rule in self.rules]
        if len(codes) != len(set(codes)):
            raise CalculationValidationError(
                "INVALID_POLICY", "contribution rule codes must be unique"
            )

    def assert_effective(self, on_date: date) -> None:
        if on_date < self.effective_from or (
            self.effective_to is not None and on_date > self.effective_to
        ):
            raise ExpiredPolicyError(self.version, on_date)


@dataclass(frozen=True)
class ContributionLine:
    code: str
    base_kind: ContributionBaseKind
    input_base_fen: int
    capped_base_fen: int
    employee_contribution_fen: int
    employer_contribution_fen: int
    enabled: bool


@dataclass(frozen=True)
class ContributionResult:
    policy_version: str
    primary_source_url: str
    lines: tuple[ContributionLine, ...]
    employee_social_insurance_fen: int
    employer_social_insurance_fen: int
    employee_housing_fund_fen: int
    employer_housing_fund_fen: int
    trace: tuple[TraceEntry, ...]

    @property
    def employee_total_fen(self) -> int:
        return self.employee_social_insurance_fen + self.employee_housing_fund_fen

    @property
    def employer_total_fen(self) -> int:
        return self.employer_social_insurance_fen + self.employer_housing_fund_fen


@dataclass(frozen=True)
class ContributionActualOverride:
    """Evidenced actual assessment for one policy contribution line."""

    actual_item_id: str
    code: str
    base_kind: ContributionBaseKind
    actual_state: str
    employee_amount_fen: int
    employer_amount_fen: int

    def __post_init__(self) -> None:
        if not self.actual_item_id or not self.code:
            raise CalculationValidationError(
                "INVALID_CONTRIBUTION_ACTUAL", "actual item id and contribution code are required"
            )
        require_fen(self.employee_amount_fen, "employee_amount_fen")
        require_fen(self.employer_amount_fen, "employer_amount_fen")
        if self.actual_state not in {"declared", "not_declared"}:
            raise CalculationValidationError(
                "INVALID_CONTRIBUTION_ACTUAL", "actual_state is not supported"
            )
        if self.actual_state == "not_declared" and (
            self.employee_amount_fen or self.employer_amount_fen
        ):
            raise CalculationValidationError(
                "INVALID_CONTRIBUTION_ACTUAL",
                "not-declared actual contribution amounts must be zero",
            )


@dataclass(frozen=True)
class ContributionBurdenResult:
    """Actual payroll deduction and employer burden after applying company policy."""

    employee_social_insurance_items: dict[str, int]
    employer_social_insurance_items: dict[str, int]
    employee_housing_fund_items: dict[str, int]
    employer_housing_fund_items: dict[str, int]
    employee_social_insurance_fen: int
    employer_social_insurance_fen: int
    employee_housing_fund_fen: int
    employer_housing_fund_fen: int
    employer_borne_employee_contributions_fen: int
    trace: tuple[TraceEntry, ...]

    @property
    def employee_total_fen(self) -> int:
        return self.employee_social_insurance_fen + self.employee_housing_fund_fen

    @property
    def employer_total_fen(self) -> int:
        return self.employer_social_insurance_fen + self.employer_housing_fund_fen


_ROUNDING = {
    RoundingRule.HALF_UP: ROUND_HALF_UP,
    RoundingRule.DOWN: ROUND_DOWN,
    RoundingRule.UP: ROUND_UP,
}


def _round_fen(value: Decimal, rule: RoundingRule) -> int:
    return int(value.quantize(Decimal("1"), rounding=_ROUNDING[rule]))


def calculate_contributions(
    policy: ContributionPolicy, bases: ContributionBases | None, on_date: date
) -> ContributionResult:
    """Calculate every configured contribution separately and retain its trace."""

    policy.assert_effective(on_date)
    if bases is None:
        raise NeedsInformationError(
            InformationRequirement(
                code="contribution_bases",
                message="employee social-insurance and housing-fund bases are required",
                fields=("social_insurance_base_fen", "housing_fund_base_fen"),
            )
        )

    missing_kinds = {
        rule.base_kind
        for rule in policy.rules
        if rule.enabled
        and bases.participates_in(rule.base_kind)
        and bases.for_kind(rule.base_kind) is None
    }
    if missing_kinds:
        fields = tuple(
            "social_insurance_base_fen"
            if base_kind == ContributionBaseKind.SOCIAL_INSURANCE
            else "housing_fund_base_fen"
            for base_kind in sorted(missing_kinds)
        )
        raise NeedsInformationError(
            InformationRequirement(
                code="contribution_bases",
                message="employee profile is missing a required contribution base",
                fields=fields,
            )
        )

    lines: list[ContributionLine] = []
    trace: list[TraceEntry] = []
    for rule in policy.rules:
        participating = bases.participates_in(rule.base_kind)
        input_base = bases.for_kind(rule.base_kind)
        if input_base is None:
            # Disabled rules do not need a base and remain explicitly zero.
            input_base = 0
        capped_base = (
            min(max(input_base, rule.minimum_base_fen), rule.maximum_base_fen)
            if participating
            else 0
        )
        employee = (
            _round_fen(Decimal(capped_base) * rule.employee_rate, rule.rounding_rule)
            if rule.enabled and participating
            else 0
        )
        employer = (
            _round_fen(Decimal(capped_base) * rule.employer_rate, rule.rounding_rule)
            if rule.enabled and participating
            else 0
        )
        line = ContributionLine(
            code=rule.code,
            base_kind=rule.base_kind,
            input_base_fen=input_base,
            capped_base_fen=capped_base,
            employee_contribution_fen=employee,
            employer_contribution_fen=employer,
            enabled=rule.enabled,
        )
        lines.append(line)
        trace.append(
            TraceEntry(
                step="contribution_line",
                values={
                    "code": rule.code,
                    "input_base_fen": input_base,
                    "capped_base_fen": capped_base,
                    "employee_rate": str(rule.employee_rate),
                    "employer_rate": str(rule.employer_rate),
                    "rounding_rule": rule.rounding_rule.value,
                    "enabled": rule.enabled,
                    "employee_participating": participating,
                },
            )
        )

    def total(base_kind: ContributionBaseKind, side: str) -> int:
        return sum(
            line.employee_contribution_fen if side == "employee" else line.employer_contribution_fen
            for line in lines
            if line.base_kind == base_kind
        )

    return ContributionResult(
        policy_version=policy.version,
        primary_source_url=policy.primary_source_url,
        lines=tuple(lines),
        employee_social_insurance_fen=total(ContributionBaseKind.SOCIAL_INSURANCE, "employee"),
        employer_social_insurance_fen=total(ContributionBaseKind.SOCIAL_INSURANCE, "employer"),
        employee_housing_fund_fen=total(ContributionBaseKind.HOUSING_FUND, "employee"),
        employer_housing_fund_fen=total(ContributionBaseKind.HOUSING_FUND, "employer"),
        trace=tuple(trace),
    )


def apply_contribution_actuals(
    result: ContributionResult,
    actuals: tuple[ContributionActualOverride, ...],
) -> ContributionResult:
    """Overlay sparse evidenced actual assessments without changing the policy baseline."""

    if not actuals:
        return result
    actual_by_key = {(item.base_kind, item.code): item for item in actuals}
    if len(actual_by_key) != len(actuals):
        raise CalculationValidationError(
            "DUPLICATE_CONTRIBUTION_ACTUAL", "actual contribution keys must be unique"
        )
    policy_keys = {(line.base_kind, line.code) for line in result.lines}
    unknown = sorted(
        f"{base_kind}:{code}"
        for base_kind, code in set(actual_by_key).difference(policy_keys)
    )
    if unknown:
        raise CalculationValidationError(
            "CONTRIBUTION_ACTUAL_KIND_NOT_IN_POLICY",
            f"actual contribution kinds are absent from the effective policy: {unknown}",
        )
    lines: list[ContributionLine] = []
    trace = list(result.trace)
    for line in result.lines:
        actual = actual_by_key.get((line.base_kind, line.code))
        if actual is None:
            lines.append(line)
            continue
        lines.append(
            replace(
                line,
                employee_contribution_fen=actual.employee_amount_fen,
                employer_contribution_fen=actual.employer_amount_fen,
            )
        )
        trace.append(
            TraceEntry(
                step="contribution_actual_override",
                values={
                    "actual_item_id": actual.actual_item_id,
                    "code": line.code,
                    "base_kind": str(line.base_kind),
                    "actual_state": actual.actual_state,
                    "policy_employee_contribution_fen": line.employee_contribution_fen,
                    "policy_employer_contribution_fen": line.employer_contribution_fen,
                    "actual_employee_contribution_fen": actual.employee_amount_fen,
                    "actual_employer_contribution_fen": actual.employer_amount_fen,
                },
            )
        )

    def total(base_kind: ContributionBaseKind, side: str) -> int:
        return sum(
            line.employee_contribution_fen if side == "employee" else line.employer_contribution_fen
            for line in lines
            if line.base_kind == base_kind
        )

    return ContributionResult(
        policy_version=result.policy_version,
        primary_source_url=result.primary_source_url,
        lines=tuple(lines),
        employee_social_insurance_fen=total(ContributionBaseKind.SOCIAL_INSURANCE, "employee"),
        employer_social_insurance_fen=total(ContributionBaseKind.SOCIAL_INSURANCE, "employer"),
        employee_housing_fund_fen=total(ContributionBaseKind.HOUSING_FUND, "employee"),
        employer_housing_fund_fen=total(ContributionBaseKind.HOUSING_FUND, "employer"),
        trace=tuple(trace),
    )


def allocate_contribution_burden(
    result: ContributionResult,
    gross_salary_fen: int,
    treatment: EmployeeContributionShortfallTreatment,
) -> ContributionBurdenResult:
    """Allocate statutory contributions without inventing wage cash or negative net pay.

    The policy order is the deterministic allocation order when gross salary is positive but
    below the statutory employee total.  Any unwithheld employee portion is added to the
    employer burden for the same insurance kind; the statutory agency total is unchanged.
    """

    require_fen(gross_salary_fen, "gross_salary_fen")
    if not isinstance(treatment, EmployeeContributionShortfallTreatment):
        raise CalculationValidationError(
            "INVALID_CONTRIBUTION_SHORTFALL_TREATMENT",
            "employee contribution shortfall treatment is not supported",
        )
    if treatment == EmployeeContributionShortfallTreatment.REJECT:
        if result.employee_total_fen > gross_salary_fen:
            raise CalculationValidationError(
                "NEGATIVE_NET_PAY",
                "statutory employee contributions exceed gross salary",
            )
        employee_budget = result.employee_total_fen
    else:
        employee_budget = min(gross_salary_fen, result.employee_total_fen)

    employee_social: dict[str, int] = {}
    employer_social: dict[str, int] = {}
    employee_housing: dict[str, int] = {}
    employer_housing: dict[str, int] = {}
    employer_borne_total = 0
    remaining_employee_budget = employee_budget
    allocation_trace: list[TraceEntry] = []
    for line in result.lines:
        employee_amount = min(line.employee_contribution_fen, remaining_employee_budget)
        remaining_employee_budget -= employee_amount
        employer_borne_amount = line.employee_contribution_fen - employee_amount
        employer_amount = line.employer_contribution_fen + employer_borne_amount
        employer_borne_total += employer_borne_amount
        if line.base_kind == ContributionBaseKind.SOCIAL_INSURANCE:
            employee_social[line.code] = employee_amount
            employer_social[line.code] = employer_amount
        else:
            employee_housing[line.code] = employee_amount
            employer_housing[line.code] = employer_amount
        allocation_trace.append(
            TraceEntry(
                step="contribution_burden_allocation",
                values={
                    "code": line.code,
                    "statutory_employee_contribution_fen": line.employee_contribution_fen,
                    "employee_deduction_fen": employee_amount,
                    "statutory_employer_contribution_fen": line.employer_contribution_fen,
                    "employer_borne_employee_shortfall_fen": employer_borne_amount,
                    "employer_total_fen": employer_amount,
                    "shortfall_treatment": treatment.value,
                },
            )
        )

    return ContributionBurdenResult(
        employee_social_insurance_items=employee_social,
        employer_social_insurance_items=employer_social,
        employee_housing_fund_items=employee_housing,
        employer_housing_fund_items=employer_housing,
        employee_social_insurance_fen=sum(employee_social.values()),
        employer_social_insurance_fen=sum(employer_social.values()),
        employee_housing_fund_fen=sum(employee_housing.values()),
        employer_housing_fund_fen=sum(employer_housing.values()),
        employer_borne_employee_contributions_fen=employer_borne_total,
        trace=tuple(allocation_trace),
    )
