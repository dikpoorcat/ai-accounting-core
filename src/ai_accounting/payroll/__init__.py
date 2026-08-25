"""Pure, effective-dated payroll calculators with no accounting-posting access.

Service code supplies explicit typed facts and reads typed results:

* ``ContributionPolicy`` + ``ContributionBases`` -> contribution lines and totals.
* ``CumulativeIncomeTaxPolicy`` + ``CumulativeTaxState`` -> cumulative withholding.
* ``RegularPayrollInput`` + those two results -> gross-to-net payroll reconciliation.
* ``AnnualBonusScenarioInput`` -> separate and combined tax scenarios; a caller must
  explicitly choose one with ``select_annual_bonus_tax_method``.

Any missing fact that could change a result raises ``NeedsInformationError``. Its
``as_response()`` method is deliberately shaped for service-layer mapping to a
``needs_information`` response. Invalid facts and non-effective policy versions raise
``CalculationValidationError`` or ``ExpiredPolicyError`` instead. Values ending in
``_fen`` are integer fen; rates are ``Decimal``.
"""

from .annual_bonus import (
    AnnualBonusScenarioInput,
    AnnualBonusScenarios,
    AnnualBonusTaxMethod,
    AnnualBonusTaxPolicy,
    AnnualBonusTaxScenario,
    AnnualBonusUsage,
    calculate_annual_bonus_scenarios,
    select_annual_bonus_tax_method,
)
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
from .income_tax import (
    CumulativeIncomeTaxPolicy,
    CumulativeTaxPeriodInput,
    CumulativeTaxResult,
    CumulativeTaxState,
    calculate_cumulative_withholding,
)
from .regular import RegularPayrollInput, RegularPayrollResult, calculate_regular_payroll
from .types import (
    CalculationValidationError,
    ExpiredPolicyError,
    InformationRequirement,
    NeedsInformationError,
    RoundingRule,
    YearMonth,
)

__all__ = [
    "AnnualBonusScenarioInput",
    "AnnualBonusScenarios",
    "AnnualBonusTaxScenario",
    "AnnualBonusTaxMethod",
    "AnnualBonusTaxPolicy",
    "AnnualBonusUsage",
    "CalculationValidationError",
    "ContributionBases",
    "ContributionBaseKind",
    "ContributionActualOverride",
    "ContributionBurdenResult",
    "ContributionLine",
    "ContributionPolicy",
    "ContributionResult",
    "ContributionRule",
    "EmployeeContributionShortfallTreatment",
    "CumulativeIncomeTaxPolicy",
    "CumulativeTaxPeriodInput",
    "CumulativeTaxResult",
    "CumulativeTaxState",
    "ExpiredPolicyError",
    "InformationRequirement",
    "NeedsInformationError",
    "RegularPayrollInput",
    "RegularPayrollResult",
    "RoundingRule",
    "YearMonth",
    "calculate_annual_bonus_scenarios",
    "allocate_contribution_burden",
    "apply_contribution_actuals",
    "calculate_contributions",
    "calculate_cumulative_withholding",
    "calculate_regular_payroll",
    "select_annual_bonus_tax_method",
]
