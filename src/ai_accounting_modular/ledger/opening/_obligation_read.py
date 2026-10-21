"""Explicit ordinary and tax opening metadata, separate from monetary resource reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ai_accounting_modular.contracts.public import BusinessRef, parse_fen

from ._members import slot
from ._read_members import member_reference, validated_members

if TYPE_CHECKING:
    from ai_accounting_modular.ledger.public import AdoptedContent


@dataclass(frozen=True)
class OpeningOrdinaryReference:
    source: BusinessRef
    nature: str
    original_business_reference: str


@dataclass(frozen=True)
class OpeningTaxReference:
    source: BusinessRef
    tax_kind: Literal["vat", "surtax", "individual_income_tax", "enterprise_income_tax"]
    tax_period: str
    balance_kind: Literal["payable", "refundable"]


@dataclass(frozen=True)
class OpeningLoanReference:
    source: BusinessRef
    contract_id: str
    lender_id: str
    component: Literal["principal", "interest"]


def opening_obligation_references(
    content: AdoptedContent,
) -> tuple[OpeningOrdinaryReference | OpeningTaxReference | OpeningLoanReference, ...]:
    """Return actual obligation slots and declared identities, never a guessed aggregate slot."""
    business_id, members = validated_members(content)
    result = []
    for member in members:
        data = member["data"]
        if data["type"] == "obligation":
            result.append(
                OpeningOrdinaryReference(
                    member_reference(business_id, member),
                    data["nature"],
                    data["original_business_reference"],
                )
            )
        elif data["type"] == "tax":
            result.append(
                OpeningTaxReference(
                    member_reference(business_id, member),
                    data["tax_kind"],
                    data["tax_period"],
                    data["balance_kind"],
                )
            )
        elif data["type"] == "loan":
            for component, field in (
                ("principal", "principal_fen"),
                ("interest", "accrued_interest_fen"),
            ):
                if parse_fen(data[field]):
                    result.append(
                        OpeningLoanReference(
                            {"business_id": business_id, "slot": slot(member, component)},
                            data["contract_id"],
                            data["lender_id"],
                            component,
                        )
                    )
    return tuple(result)
