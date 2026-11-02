"""Explicit ordinary and tax opening metadata, separate from monetary resource reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ai_accounting_modular.contracts.public import (
    BusinessRef,
    ContractError,
    EvaluatedSlot,
    parse_fen,
)

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


@dataclass(frozen=True)
class OpeningInvestmentReference:
    source: BusinessRef
    terms_id: str
    income_kind: Literal["dividend", "interest"]
    original_business_reference: str
    product_id: str


@dataclass(frozen=True)
class OpeningCooperationTradeReference:
    source: BusinessRef
    contract_id: str
    side: Literal["revenue", "expense"]
    original_business_reference: str


def opening_obligation_references(
    content: AdoptedContent | EvaluatedSlot,
) -> tuple[
    OpeningOrdinaryReference
    | OpeningTaxReference
    | OpeningLoanReference
    | OpeningInvestmentReference
    | OpeningCooperationTradeReference,
    ...,
]:
    """Return actual obligation slots and declared identities, never a guessed aggregate slot."""
    business_id, members = validated_members(content)
    identities = {}
    for quantity in content.basis["adopted_quantities"]:
        if quantity["type"] != "opening_investment_receivable":
            continue
        key = quantity["source"]["business_id"], quantity["source"]["slot"]
        if key in identities:
            raise ContractError("storage_corrupt", "期初投资收益不能有重复的冻结身份。")
        identities[key] = quantity
    expected = {
        (business_id, member_reference(business_id, member)["slot"])
        for member in members
        if member["data"]["type"] == "investment_receivable"
    }
    if set(identities) != expected:
        raise ContractError("storage_corrupt", "期初投资收益的冻结身份须精确覆盖本包收益成员。")
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
        elif data["type"] == "investment_receivable":
            reference = member_reference(business_id, member)
            identity = identities.get((reference["business_id"], reference["slot"]))
            if (
                identity is None
                or identity["terms_id"] != data["terms_id"]
                or identity["income_kind"] != data["income_kind"]
            ):
                raise ContractError("storage_corrupt", "期初投资收益缺少与成员一致的冻结持仓身份。")
            result.append(
                OpeningInvestmentReference(
                    reference,
                    data["terms_id"],
                    data["income_kind"],
                    data["original_business_reference"],
                    identity["product_id"],
                )
            )
        elif data["type"] == "cooperation":
            result.extend(
                OpeningCooperationTradeReference(
                    {
                        "business_id": business_id,
                        "slot": slot(member, "trade:" + trade["entry_key"]),
                    },
                    data["contract_id"],
                    trade["side"],
                    trade["original_business_reference"],
                )
                for trade in data["open_trades"]
            )
    return tuple(result)
