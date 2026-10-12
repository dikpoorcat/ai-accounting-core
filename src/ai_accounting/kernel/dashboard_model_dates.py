"""Certified monthly personnel states derived from effective event history.

The selected business month and management version determine membership.
Changing the observation day never substitutes today's state for a past month.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .employee_employment import WorkforceState
from .types import ActualDate, EvidenceDigest, YearMonth

EmploymentState = WorkforceState
EmployeeIdentity = Annotated[str, Field(strict=True, min_length=1, max_length=200)]


class EmployeeDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    employee_id: EmployeeIdentity
    period_states: tuple[WorkforceState, ...]
    possible_period_states: tuple[WorkforceState, ...]
    period_end_state: WorkforceState
    event_version: EvidenceDigest


def employee_date_basis(employee_id, monthly, timeline):
    return EmployeeDateBasis(
        employee_id=employee_id,
        period_states=tuple(monthly["period_states"]),
        possible_period_states=tuple(monthly["possible_period_states"]),
        period_end_state=monthly["period_end_state"],
        event_version=timeline["event_version"],
    )


class ModelDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    rule: Literal["effective-workforce-month/1"]
    period: YearMonth
    valid_from: ActualDate
    employees: tuple[EmployeeDateBasis, ...]

    @model_validator(mode="after")
    def complete_rule(self):
        identities = [basis.employee_id for basis in self.employees]
        if len(set(identities)) != len(identities) or identities != sorted(identities):
            raise ValueError("employee date directory must have unique sorted identities")
        return self


def make_date_basis(employees, as_of, period):
    return ModelDateBasis(
        rule="effective-workforce-month/1",
        period=YearMonth(period),
        valid_from=ActualDate(as_of),
        employees=tuple(sorted(employees, key=lambda basis: basis.employee_id)),
    )


def employee_states(basis: ModelDateBasis, as_of: str):
    ActualDate(as_of)
    return {item.employee_id: item.period_end_state for item in basis.employees}


def original_date_states_valid(basis: ModelDateBasis, as_of: str) -> bool:
    return basis.valid_from <= ActualDate(as_of)


def date_verification_days(basis: ModelDateBasis):
    return (str(basis.valid_from),)
