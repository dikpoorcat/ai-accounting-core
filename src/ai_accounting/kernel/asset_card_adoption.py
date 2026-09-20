"""Frozen adoption proof for card rows backed by an accepted asset batch.

Batch-backed ``reimbursed_asset`` calculations deliberately have no journal
lines: the accepted batch owns the voucher while each card owns its stable
asset identity.  A card can also be a dependency of activation in the same
month, so a flat historical calculation list cannot prove that the card was
independently adopted. This module validates the explicit role saved by the
single direct-adoption close contract.
"""

from __future__ import annotations

from .contracts import KernelError
from .types import YearMonth

_CARD_KIND = "reimbursed_asset"
_BATCH_KIND = "reimbursed_asset_batch"
_CONTRACT_VERSION = 1


def _card_shape(card, batch):
    data, outcome = card["fact_data"], card["outcome"]
    batch_data, batch_outcome = batch["fact_data"], batch["outcome"]
    if (
        card["kind"] != _CARD_KIND
        or batch["kind"] != _BATCH_KIND
        or data.get("acceptance_id") != batch["subject_id"]
        or data.get("period") != batch_data.get("period")
        or data.get("acquisition_date") != batch_data.get("acquisition_date")
        or data.get("company_acceptance_confirmed") is not True
        or batch_data.get("company_acceptance_confirmed") is not True
        or outcome.get("lines") != []
        or outcome.get("balances") != []
        or outcome.get("explanation") != []
        or outcome.get("opening_lines") != []
        or outcome.get("opening") is not False
    ):
        return False
    members = [
        item
        for item in batch_data.get("assets", ())
        if isinstance(item, dict) and item.get("asset_id") == card["subject_id"]
    ]
    if len(members) != 1:
        return False
    member = members[0]
    values = outcome.get("values")
    if values != {
        "asset_type": data.get("asset_type"),
        "cost_fen": data.get("cost_fen"),
        "acceptance_fact_id": batch["fact_id"],
        "obligations": [],
    } or (member.get("asset_type"), member.get("cost_fen")) != (
        data.get("asset_type"),
        data.get("cost_fen"),
    ):
        return False
    carrying = [
        item
        for item in batch_outcome.get("balances", ())
        if isinstance(item, dict) and item.get("key") == f"asset:{card['subject_id']}:carrying"
    ]
    return carrying == [
        {
            "key": f"asset:{card['subject_id']}:carrying",
            "amount": data.get("cost_fen"),
            "category": "asset",
        }
    ]


def _relationships(reads, metadata, members, close_period, proven_batches):
    month = str(YearMonth.from_ordinal(close_period))
    card_ids = {
        ident
        for ident in members
        if metadata[ident]["kind"] == _CARD_KIND and metadata[ident]["posting_period"] == month
    }
    if not card_ids:
        return {}
    cards = reads.calculations(card_ids)
    eligible = {
        ident for ident, card in cards.items() if card["fact_data"].get("acceptance_id") is not None
    }
    reads.prime_parents(eligible)
    parents = {
        ident: [
            parent
            for parent in reads.parents(ident)
            if parent in members and metadata[parent]["kind"] == _BATCH_KIND
        ]
        for ident in eligible
    }
    batch_ids = {parent for values in parents.values() for parent in values}
    batches = reads.calculations(batch_ids)
    result = {}
    for ident in sorted(eligible):
        matches = parents[ident]
        valid = (
            len(matches) == 1
            and matches[0] in proven_batches
            and _card_shape(cards[ident], batches[matches[0]])
            and set(reads.parents(ident)) == {matches[0]}
        )
        if not valid:
            raise KernelError(
                "asset_card_adoption_unproven",
                "整批验收资产卡片缺少唯一、完整的关账采用关系",
                asset_id=cards[ident]["subject_id"],
                calculation_id=ident,
            )
        result[ident] = matches[0]
    return result


def build_asset_card_adoptions(reads, *, close_period, calculation_ids, voucher_calculation_ids):
    """Build explicit card roles for a new manifest, failing before an omission."""
    members = set(calculation_ids)
    metadata = reads.metadata(members)
    relationships = _relationships(
        reads,
        metadata,
        members,
        close_period,
        set(voucher_calculation_ids),
    )
    calculations = reads.calculations(set(relationships) | set(relationships.values()))
    return [
        {
            "contract_version": _CONTRACT_VERSION,
            "asset_id": calculations[card_id]["subject_id"],
            "calculation_id": card_id,
            "result_digest": calculations[card_id]["result_digest"],
            "acceptance_calculation_id": batch_id,
            "acceptance_result_digest": calculations[batch_id]["result_digest"],
        }
        for card_id, batch_id in sorted(relationships.items())
    ]


def prove_asset_card_adoptions(reads, *, close_period, manifest, metadata, independent_proofs):
    """Validate explicitly declared card roles; absent declarations are corruption."""
    from .close_contract import direct_calculation_ids

    members = direct_calculation_ids(manifest)
    proven_batches = {
        ident
        for ident, proof in independent_proofs.items()
        if proof.get("basis") == "manifest_voucher_root"
    }
    declared = manifest.get("asset_card_adoptions")
    if not isinstance(declared, list):
        raise KernelError("asset_card_adoption", "关账采用的资产卡片清单格式不正确")
    card_ids = {
        item.get("calculation_id")
        for item in declared
        if isinstance(item, dict) and isinstance(item.get("calculation_id"), str)
    }
    batch_ids = {
        item.get("acceptance_calculation_id")
        for item in declared
        if isinstance(item, dict) and isinstance(item.get("acceptance_calculation_id"), str)
    }
    known_ids = (card_ids | batch_ids) & members
    calculations = reads.calculations(known_ids)
    proofs = {}
    seen = set()
    month = str(YearMonth.from_ordinal(close_period))
    for item in declared:
        card_id = item.get("calculation_id") if isinstance(item, dict) else None
        batch_id = item.get("acceptance_calculation_id") if isinstance(item, dict) else None
        valid = (
            isinstance(item, dict)
            and set(item)
            == {
                "contract_version",
                "asset_id",
                "calculation_id",
                "result_digest",
                "acceptance_calculation_id",
                "acceptance_result_digest",
            }
            and item.get("contract_version") == _CONTRACT_VERSION
            and card_id not in seen
            and card_id in calculations
            and batch_id in calculations
            and card_id in members
            and batch_id in members
            and batch_id in proven_batches
            and metadata[card_id]["posting_period"] == month
            and calculations[card_id]["subject_id"] == item.get("asset_id")
            and calculations[card_id]["result_digest"] == item.get("result_digest")
            and calculations[batch_id]["result_digest"] == item.get("acceptance_result_digest")
            and set(reads.parents(card_id)) == {batch_id}
            and _card_shape(calculations[card_id], calculations[batch_id])
        )
        if not valid:
            raise KernelError("asset_card_adoption", "关账采用的资产卡片关系不匹配")
        seen.add(card_id)
        proofs[card_id] = {
            "basis": "manifest_asset_card_adoption",
            "contract_version": _CONTRACT_VERSION,
            "acceptance_calculation_id": batch_id,
            "result_digest": item["result_digest"],
        }
    expected = _relationships(reads, metadata, members, close_period, proven_batches)
    if seen != set(expected):
        raise KernelError("asset_card_adoption", "关账采用的资产卡片清单不完整")
    return proofs
