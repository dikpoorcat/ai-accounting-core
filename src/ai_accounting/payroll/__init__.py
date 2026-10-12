"""Contribution calculations and immutable historical tax data grammars.

Current remuneration accounting accepts confirmed tax amounts. No wage or
bonus income-tax calculator is exposed in this runtime package.
"""
from .annual_bonus import AnnualBonusTaxPolicy
from .contributions import (
    ContributionActualOverride,
    ContributionBaseKind,
    ContributionBases,
    ContributionBurdenResult,
    ContributionLine,
    ContributionPolicy,
    ContributionResult,
    ContributionRule,
    EmployeeContributionShortfallTreatment,
    allocate_contribution_burden,
    apply_contribution_actuals,
    calculate_contributions,
)
from .income_tax import CumulativeIncomeTaxPolicy, CumulativeTaxState
from .types import (
    CalculationValidationError,
    ExpiredPolicyError,
    InformationRequirement,
    NeedsInformationError,
    RoundingRule,
    YearMonth,
)

__all__ = [
    "AnnualBonusTaxPolicy", "CalculationValidationError", "ContributionActualOverride",
    "ContributionBaseKind", "ContributionBases", "ContributionBurdenResult",
    "ContributionLine", "ContributionPolicy", "ContributionResult", "ContributionRule",
    "CumulativeIncomeTaxPolicy", "CumulativeTaxState", "EmployeeContributionShortfallTreatment",
    "ExpiredPolicyError", "InformationRequirement", "NeedsInformationError", "RoundingRule",
    "YearMonth", "allocate_contribution_burden", "apply_contribution_actuals",
    "calculate_contributions",
]
