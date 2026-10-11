"""Certified current personnel confirmations, independent of month membership.

Money and period membership remain the producer's complete accounting output.
Current personnel status never follows a date or legacy roster flag. These inputs
belong to an authenticated model root, never an across-request proof cache.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActualDate

EmploymentState = Literal["regular", "unpaid_leave", "departed", "unknown"]
EmployeeIdentity = Annotated[str, Field(strict=True, min_length=1, max_length=200)]


class EmployeeDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    employee_id: EmployeeIdentity
    explicit_state: EmploymentState


def current_employment_state(basis: EmployeeDateBasis | Mapping, as_of: str) -> EmploymentState:
    """Read an explicit current confirmation; missing confirmation stays unknown."""
    if isinstance(basis, EmployeeDateBasis):
        return basis.explicit_state
    else:
        return basis.get("employment_state") or "unknown"


def employee_date_basis(employee_id: str, profile: Mapping) -> EmployeeDateBasis:
    return EmployeeDateBasis(
        employee_id=employee_id,
        explicit_state=profile.get("employment_state") or "unknown",
    )


class ModelDateBasis(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    rule: Literal["explicit-current-employment-state/1"]
    valid_from: ActualDate
    employees: tuple[EmployeeDateBasis, ...]

    @model_validator(mode="after")
    def complete_rule(self):
        identities = [basis.employee_id for basis in self.employees]
        if len(set(identities)) != len(identities) or identities != sorted(identities):
            raise ValueError("employee date directory must have unique sorted identities")
        return self


def make_date_basis(employees: tuple[EmployeeDateBasis, ...], as_of: str) -> ModelDateBasis:
    employees = tuple(sorted(employees, key=lambda basis: basis.employee_id))
    return ModelDateBasis(
        rule="explicit-current-employment-state/1", valid_from=as_of,
        employees=employees,
    )


def employee_states(basis: ModelDateBasis, as_of: str) -> dict[str, EmploymentState]:
    """Read the complete certified roster without inferring date transitions."""
    day = str(ActualDate(as_of))
    return {item.employee_id: current_employment_state(item, day) for item in basis.employees}


def original_date_states_valid(basis: ModelDateBasis, as_of: str) -> bool:
    """Explicit confirmations remain valid within the certified management version."""
    day = str(ActualDate(as_of))
    return basis.valid_from <= day


def date_verification_days(basis: ModelDateBasis):
    """One representative proves the explicit, calendar-independent states."""
    return (str(basis.valid_from),)
