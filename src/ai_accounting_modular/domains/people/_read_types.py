"""People-owned monetary projections; membership stays in relationships."""

from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import (
    BusinessRef,
    FactIssue,
    LedgerScope,
    ReadInputs,
    ResultRef,
    ResultSourceRef,
)
from ai_accounting_modular.ledger.public import ResourcePosition, SettlementEvent

type RemunerationType = Literal["wage", "bonus", "labor", "opening"]


@dataclass(frozen=True)
class RemunerationAmounts:
    gross_fen: int = 0
    cost_fen: int = 0
    net_fen: int = 0
    tax_fen: int = 0
    employee_social_fen: int = 0
    employee_housing_fen: int = 0
    employer_social_fen: int = 0
    employer_housing_fen: int = 0
    disbursement_target_fen: int = 0
    held_fen: int = 0


@dataclass(frozen=True)
class RemunerationSource:
    """Exact positive remuneration values; an opening payable cannot supply missing gross pay."""

    reference: ResultRef
    source: ResultSourceRef
    business_period: str
    person_id: str
    remuneration_type: Literal["wage", "bonus", "labor"]
    expense_area: str | None
    project_id: str | None
    tax_state: str
    tax_period: str | None
    amounts: RemunerationAmounts


@dataclass(frozen=True)
class RemunerationRosterPerson:
    """Roles evidenced by selected remuneration, independent of current relationship membership."""

    person_id: str
    roles: tuple[Literal["employee", "labor"], ...]


@dataclass(frozen=True)
class RemunerationEntry:
    entry_id: str
    adopted: ResultRef
    effective: ResultRef
    source: ResultSourceRef
    sign: Literal[-1, 1]
    business_period: str
    person_id: str
    remuneration_type: Literal["wage", "bonus", "labor"]
    expense_area: str | None
    project_id: str | None
    tax_state: str
    tax_period: str | None
    amounts: RemunerationAmounts
    parent_remuneration_source: ResultSourceRef | None = None

    @property
    def remuneration_id(self) -> str:
        return (self.parent_remuneration_source or self.effective)["business_id"]


@dataclass(frozen=True)
class RemunerationComposition:
    """A cutoff composition, distinct from every exact adopted source it combines."""

    remuneration: ResultSourceRef
    tax_targets: tuple[ResultSourceRef, ...]
    amounts: RemunerationAmounts
    tax_state: str


@dataclass(frozen=True)
class RemunerationBreakdown:
    gross_salary_fen: int = 0
    annual_bonus_fen: int = 0
    employer_social_fen: int = 0
    employer_housing_fen: int = 0
    employee_social_fen: int = 0
    employee_housing_fen: int = 0
    individual_income_tax_fen: int = 0
    net_salary_fen: int = 0
    personal_deduction_fen: int = 0
    employee_cost_fen: int = 0
    labor_gross_fen: int = 0
    labor_tax_fen: int = 0
    labor_net_fen: int = 0
    expensed_labor_fen: int = 0
    capitalized_labor_fen: int = 0
    total_expense_fen: int = 0


@dataclass(frozen=True)
class RemunerationNetSettlement:
    money_outflow_fen: int = 0
    money_inflow_fen: int = 0
    agency_fen: int = 0
    offset_fen: int = 0
    transfer_fen: int = 0
    excess_reclassified_fen: int = 0


@dataclass(frozen=True)
class RemunerationSummary:
    """Monthly facts remain available when a cumulative unpaid balance has no opening basis."""

    amounts: RemunerationBreakdown
    net_settlements: RemunerationNetSettlement
    outstanding_employee_net_fen: int | None
    outstanding_labor_net_fen: int | None
    outstanding_capitalized_labor_net_fen: int | None
    outstanding_remuneration_fen: int | None


@dataclass(frozen=True)
class PersonRemuneration:
    person_id: str
    summary: RemunerationSummary
    payroll_business_ids: tuple[str, ...]
    labor_business_ids: tuple[str, ...]
    payroll_periods: tuple[str, ...]
    has_payroll_activity: bool
    has_labor_activity: bool
    wage_tax_scope: Literal["none", "wage_income", "contributions_only", "mixed"]


@dataclass(frozen=True)
class RemunerationPosition:
    person_id: str
    payroll_period: str
    remuneration_type: RemunerationType
    component: str
    project_id: str | None
    position: ResourcePosition
    recovery: bool = False
    parent_source: BusinessRef | None = None


@dataclass(frozen=True)
class RemunerationSettlement:
    person_id: str
    payroll_period: str
    remuneration_type: RemunerationType
    component: str
    event: SettlementEvent
    recovery: bool = False
    parent_source: BusinessRef | None = None
    tax_reclassification: bool = False


@dataclass(frozen=True)
class RemunerationActivity:
    version: str
    scope: LedgerScope
    summary: RemunerationSummary
    persons: tuple[PersonRemuneration, ...]
    entries: tuple[RemunerationEntry, ...]
    positions: tuple[RemunerationPosition, ...]
    settlements: tuple[RemunerationSettlement, ...]
    sources: tuple[RemunerationSource, ...]
    compositions: tuple[RemunerationComposition, ...]
    roster: tuple[RemunerationRosterPerson, ...]
    issues: tuple[FactIssue, ...]
    inputs: ReadInputs
