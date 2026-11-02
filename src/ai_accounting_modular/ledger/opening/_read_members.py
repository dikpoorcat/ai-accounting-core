"""One exact opening-body check and owner-controlled member location."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_accounting_modular.contracts.public import (
    BusinessRef,
    ContractError,
    EvaluatedSlot,
    validate_contract,
)

from ._members import slot

if TYPE_CHECKING:
    from ai_accounting_modular.ledger.public import AdoptedContent


def validated_members(content: AdoptedContent | EvaluatedSlot):
    # Import at the call boundary: ledger selection itself imports the opening owner.
    from ai_accounting_modular.ledger.public import AdoptedContent

    if not isinstance(content, AdoptedContent | EvaluatedSlot):
        raise ContractError("invalid_input", "期初定位须提供精确正式正文或类型明确的候选结果。")
    validate_contract("business-facts.schema.json", "ResultSourceRef", content.source)
    for definition, value in (("ResultTarget", content.target), ("AdoptedBasis", content.basis)):
        validate_contract("business-facts.schema.json", definition, value)
    source, target, basis = content.source, content.target, content.basis
    business_period = None
    if isinstance(content, AdoptedContent):
        ref = content.reference
        validate_contract("core.schema.json", "ResultRef", ref)
        if any(source[key] != ref[key] for key in ("business_id", "result_id", "result_slot")) or (
            source["result_generation"] != ref["generation"]
        ):
            raise ContractError("invalid_input", "期初正文的采用身份与正式来源不一致。")
        business_period = ref["business_period"]
    if target["kind"] != "opening.package" or basis["kind"] != "opening.package":
        raise ContractError("unsupported_business", "期初定位仅承接正式期初包。")
    members = basis["input"]["members"]
    if (
        any(value["result_slot"] != "main" for value in (source, target, basis))
        or business_period is not None
        and basis["period"] != business_period
        or target["values"]["bookkeeping_start"] != basis["period"]
        or target["values"]["member_count"] != len(members)
        or len({member["member_id"] for member in members}) != len(members)
    ):
        raise ContractError("storage_corrupt", "正式期初包的结果槽、期间或成员目录不一致。")
    return source["business_id"], members


def member_reference(business_id, member) -> BusinessRef:
    """Only single-obligation member kinds use this un-suffixed public resource."""
    return {"business_id": business_id, "slot": slot(member)}
