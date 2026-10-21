"""Locate evidenced payroll members in an exact adopted opening package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ai_accounting_modular.contracts.public import BusinessRef

from ._read_members import member_reference, validated_members

if TYPE_CHECKING:
    from ai_accounting_modular.ledger.public import AdoptedContent


@dataclass(frozen=True)
class OpeningPayrollReference:
    source: BusinessRef
    person_id: str
    recipient_id: str
    payroll_period: str
    component: Literal[
        "net", "tax", "employee_social", "employee_housing", "employer_social", "employer_housing"
    ]
    tax_period: str | None


def opening_payroll_references(content: AdoptedContent) -> tuple[OpeningPayrollReference, ...]:
    business_id, members = validated_members(content)
    result = []
    for member in members:
        data = member["data"]
        if data["type"] != "payroll_payable":
            continue
        result.append(
            OpeningPayrollReference(
                member_reference(business_id, member),
                data["person_id"],
                data["recipient_id"],
                data["payroll_period"],
                data["component"],
                data.get("tax_period"),
            )
        )
    return tuple(result)
