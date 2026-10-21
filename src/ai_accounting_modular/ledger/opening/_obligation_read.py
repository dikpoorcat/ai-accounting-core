"""Explicit ordinary and tax opening metadata, separate from monetary resource reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ai_accounting_modular.contracts.public import BusinessRef

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


def opening_obligation_references(
    content: AdoptedContent,
) -> tuple[OpeningOrdinaryReference | OpeningTaxReference, ...]:
    """These positive, single-slot members need no amount calculation or inferred identity."""
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
    return tuple(result)
