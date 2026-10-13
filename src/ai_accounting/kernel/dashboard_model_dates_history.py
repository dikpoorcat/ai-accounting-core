"""Strict read-only calendar grammar used by retained model content versions 2/3.

Fixed SOURCE24 dashboard_model_dates.py SHA256:
acd913b773589b39b28f44f298d824d62738cc55d0af7bbd8715ca37ac50e786.
These inputs never serve today's native personnel adapter.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActualDate, YearMonth

EmploymentState = Literal["regular", "unpaid_leave", "departed", "unknown"]
EmployeeIdentity = Annotated[str, Field(strict=True, min_length=1, max_length=200)]

class HistoricalEmployeeDateBasis(BaseModel):
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

def _change_day(basis: HistoricalEmployeeDateBasis) -> ActualDate | None:
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

def next_employee_change(employees: tuple[HistoricalEmployeeDateBasis, ...], as_of: str):
    changes = [day for basis in employees
               if (day := _change_day(basis)) is not None and day > as_of]
    return min(changes) if changes else None

class HistoricalModelDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    rule: Literal["current-employment-month-or-day/1"]
    valid_from: ActualDate
    next_change: ActualDate | None
    employees: tuple[HistoricalEmployeeDateBasis, ...]

    @model_validator(mode="after")
    def complete_rule(self):
        identities = [basis.employee_id for basis in self.employees]
        if len(set(identities)) != len(identities) or identities != sorted(identities):
            raise ValueError("employee date directory must have unique sorted identities")
        if self.next_change != next_employee_change(self.employees, self.valid_from):
            raise ValueError("employee date validity does not match its complete inputs")
        return self
