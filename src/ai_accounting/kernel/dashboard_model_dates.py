"""The dashboard's one calendar-dependent personnel rule and certified inputs.

Money and period membership remain the producer's complete accounting output.
Only current personnel status is evaluated for the reading day. These inputs
belong to an authenticated model root, never an across-request proof cache.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActualDate, YearMonth

EmploymentState = Literal["regular", "unpaid_leave", "departed", "unknown"]
EmployeeIdentity = Annotated[str, Field(strict=True, min_length=1, max_length=200)]


class EmployeeDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    employee_id: EmployeeIdentity
    explicit_state: EmploymentState | None
    employment_start: ActualDate | YearMonth | None
    employment_end: ActualDate | YearMonth | None
    employment_status: str | None


def _conflict(start, end):
    return bool(start and end and (
        start[:7] > end[:7] or len(start) == len(end) == 10 and start > end
    ))


def current_employment_state(basis: EmployeeDateBasis | Mapping, as_of: str) -> EmploymentState:
    """Keep explicit states and the original month/day cutoff exactly distinct."""
    if isinstance(basis, EmployeeDateBasis):
        state, start, end, status = (
            basis.explicit_state, basis.employment_start,
            basis.employment_end, basis.employment_status,
        )
    else:
        state, start, end, status = (
            basis.get("employment_state"), basis.get("employment_start"),
            basis.get("employment_end"), basis.get("employment_status"),
        )
    if state is not None:
        return state
    if _conflict(start, end):
        return "unknown"
    ended = end and (end <= as_of if len(end) == 10 else end < as_of[:7])
    if ended:
        return "departed" if status != "active" else "unknown"
    return "regular" if status == "active" else "unknown"


def employee_date_basis(employee_id: str, profile: Mapping) -> EmployeeDateBasis:
    return EmployeeDateBasis(
        employee_id=employee_id,
        explicit_state=profile.get("employment_state"),
        employment_start=profile.get("employment_start"),
        employment_end=profile.get("employment_end"),
        employment_status=profile.get("employment_status"),
    )


def _change_day(basis: EmployeeDateBasis) -> ActualDate | None:
    if basis.explicit_state is not None or _conflict(
        basis.employment_start, basis.employment_end,
    ) or basis.employment_end is None:
        return None
    end = basis.employment_end
    if len(end) == 10:
        return ActualDate(end)
    year, month = map(int, end.split("-"))
    if year == 9999 and month == 12:
        return None  # No representable reading day crosses this month boundary.
    return ActualDate(f"{year + (month == 12):04d}-{month % 12 + 1:02d}-01")


def next_employee_change(employees: tuple[EmployeeDateBasis, ...], as_of: str):
    changes = [day for basis in employees
               if (day := _change_day(basis)) is not None and day > as_of]
    return min(changes) if changes else None


class ModelDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    rule: Literal["current-employment-month-or-day/1"]
    valid_from: ActualDate
    next_change: ActualDate | None
    employees: tuple[EmployeeDateBasis, ...]

    @model_validator(mode="after")
    def complete_rule(self):
        identities = [basis.employee_id for basis in self.employees]
        if len(set(identities)) != len(identities) or identities != sorted(identities):
            raise ValueError("employee date directory must have unique sorted identities")
        if self.next_change != next_employee_change(self.employees, self.valid_from):
            raise ValueError("employee date validity does not match its complete inputs")
        return self


def make_date_basis(employees: tuple[EmployeeDateBasis, ...], as_of: str) -> ModelDateBasis:
    employees = tuple(sorted(employees, key=lambda basis: basis.employee_id))
    return ModelDateBasis(
        rule="current-employment-month-or-day/1", valid_from=as_of,
        next_change=next_employee_change(employees, as_of), employees=employees,
    )


def employee_states(basis: ModelDateBasis, as_of: str) -> dict[str, EmploymentState]:
    """Evaluate the complete certified roster, including after a real transition."""
    day = str(ActualDate(as_of))
    return {item.employee_id: current_employment_state(item, day) for item in basis.employees}


def original_date_states_valid(basis: ModelDateBasis, as_of: str) -> bool:
    """The certified original default rows remain valid until a real boundary."""
    day = str(ActualDate(as_of))
    return basis.valid_from <= day and (basis.next_change is None or day < basis.next_change)


def date_verification_days(basis: ModelDateBasis):
    """Finite representatives cover every piecewise-constant rule interval."""
    days = {str(basis.valid_from)}
    for item in basis.employees:
        day = _change_day(item)
        if day is None:
            continue
        days.add(str(day))
        parsed = date.fromisoformat(day)
        if parsed > date.min:
            days.add((parsed - timedelta(days=1)).isoformat())
    return tuple(sorted(days))
