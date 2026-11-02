"""Exact investment payment authorizations; no change to the underlying creditor."""

import json
from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    parse_fen,
    validate_contract,
)

from ._planning import stored_source_reference
from ._values import fail

_PURPOSES = {
    "investment.acquire": "acquisition_payment",
    "investment.dispose": "redemption_receipt",
    "investment.capital_return": "redemption_receipt",
    "investment.income": "distribution_receipt",
    "investment.debt_interest": "distribution_receipt",
    "investment.loss": "redemption_receipt",
}


def investment_settlement_identity(producer, source, *, subjects=None):
    """Resolve a holding from its typed result, including sourced opening income."""
    if producer.target["kind"] != "opening.package":
        if producer.target["kind"] not in _PURPOSES:
            fail("invalid_input", "投资授权不能用于其他业务的收付。")
        values = producer.target["values"]
        return values["terms_id"], values["product_id"]
    from ai_accounting_modular.ledger.opening.public import (
        OpeningInvestmentReference,
        opening_obligation_references,
    )

    key = producer.source["result_id"], producer.source["content_digest"]
    positions = subjects.get(key) if subjects is not None else None
    if positions is None:
        positions = {
            (item.source["business_id"], item.source["slot"]): (item.terms_id, item.product_id)
            for item in opening_obligation_references(producer)
            if isinstance(item, OpeningInvestmentReference)
        }
        if subjects is not None:
            subjects[key] = positions
    found = positions.get((source["business_id"], source["slot"]))
    if found is None:
        fail("invalid_input", "期初投资授权只能清偿有明确持仓来源的投资收益成员。")
    return found


def settlement_purpose(kind, direction, *, account_code=None):
    purpose = _PURPOSES.get(kind)
    if (purpose is not None or kind == "opening.package") and account_code in {"1131", "1132"}:
        purpose = "distribution_receipt"
    expected = "outflow" if purpose == "acquisition_payment" else "inflow"
    if purpose is None or direction != expected:
        fail("invalid_input", "授权不适用于该投资收付用途或方向。")
    return purpose


def validate_settlement_counterparty(
    authorization, *, terms_id, product_id, principal_id, actual_party_id, purpose, actual_on
):
    """The same pure invariant is checked before calculation and at ledger adoption."""
    validate_contract(
        "business-facts.schema.json", "InvestmentSettlementAuthorization", authorization
    )
    if (
        authorization["terms_id"] != terms_id
        or authorization["product_id"] != product_id
        or authorization["principal_party_id"] != principal_id
        or authorization["agent_id"] != actual_party_id
        or purpose not in authorization["purposes"]
    ):
        fail("dependency_conflict", "投资代收付授权的持仓、用途或实际双方不一致。")
    start, end = authorization["valid_from"], authorization.get("valid_through")
    first = actual_on[: len(start)]
    last = actual_on[: len(end)] if end is not None else None
    if first < start or end is not None and last > end:
        fail("dependency_conflict", "实际收付不在有来源的授权有效范围内。")


def adopted_authorization(basis, source):
    found = [
        item
        for item in basis["adopted_quantities"]
        if item["type"] == "investment_settlement_authorization"
        and item["obligation_source"] == source
    ]
    if len(found) > 1:
        fail("storage_corrupt", "同一实际清偿重复采用投资代收付授权。")
    return found[0] if found else None


def validate_authorized_money(repo, settlement, obligation, movement, *, subjects=None):
    consumer = repo.result(settlement["result_id"])
    basis = json.loads(consumer["basis_json"])
    source = {
        "business_id": settlement["source_business_id"],
        "slot": settlement["source_public_slot"],
    }
    proof = adopted_authorization(basis, source)
    if proof is None:
        return None
    validate_contract(
        "business-facts.schema.json", "AdoptedInvestmentSettlementAuthorization", proof
    )
    if parse_fen(proof["amount_fen"]) != settlement["amount_fen"]:
        fail("dependency_conflict", "授权采用金额与实际清偿不一致。")
    auth_source = proof["source"]
    adopted = repo.result(auth_source["result_id"])
    if adopted is None or stored_source_reference(adopted) != auth_source:
        fail("dependency_conflict", "投资代收付授权的精确采用来源已失效。")
    target = json.loads(adopted["target_json"])
    if (
        target["kind"] != "investment.terms"
        or target["values"].get("authorization") != proof["authorization"]
        or auth_source not in basis["source_results"]
    ):
        fail("dependency_conflict", "代收付证明与正式投资授权不一致。")
    producer = repo.result(obligation["result_id"])
    producer_target = json.loads(producer["target_json"])
    from ._read_contents import reference
    from ._types import AdoptedContent

    producer_content = AdoptedContent(
        reference(producer),
        stored_source_reference(producer),
        producer_target,
        json.loads(producer["basis_json"]),
    )
    terms_id, product_id = investment_settlement_identity(
        producer_content,
        source,
        subjects=subjects,
    )
    purpose = settlement_purpose(
        producer_target["kind"],
        "outflow" if movement["direction"] == "out" else "inflow",
        account_code=obligation["account_code"],
    )
    if proof["purpose"] != purpose:
        fail("dependency_conflict", "实际投资收付与授权用途不一致。")
    actual_party = movement["party_id"]
    if actual_party is None:
        allocation = next(
            (item for item in basis["input"].get("allocations", []) if item["source"] == source),
            None,
        )
        actual_party = allocation.get("recipient_id") if allocation else None
    validate_settlement_counterparty(
        proof["authorization"],
        terms_id=terms_id,
        product_id=product_id,
        principal_id=obligation["party_id"],
        actual_party_id=actual_party,
        purpose=purpose,
        actual_on=movement["actual_on"],
    )
    return proof["authorization"]["terms_id"]


def validate_authorization_limits(repo, terms_ids):
    if not terms_ids:
        return
    totals, grants = defaultdict(int), {}
    current = {
        (row["business_id"], row["result_slot"]): row
        for row in repo.current_heads(tuple(terms_ids))
    }
    for row in repo.authorization_uses(tuple(terms_ids)):
        for proof in json.loads(row["basis_json"])["adopted_quantities"]:
            if proof["type"] != "investment_settlement_authorization":
                continue
            auth = proof["authorization"]
            if auth["terms_id"] not in terms_ids:
                continue
            key = auth["terms_id"], auth["entry_key"]
            totals[key] += parse_fen(proof["amount_fen"])
            if key in grants:
                continue
            head = current.get((key[0], "authorization:" + key[1]))
            target = (
                json.loads(repo.result(head["result_id"])["target_json"])
                if head is not None
                else None
            )
            grants[key] = (
                target["values"].get("authorization", auth)
                if target and target["kind"] == "investment.terms"
                else auth
            )
    for key, amount in totals.items():
        if "limit_fen" in grants[key] and amount > parse_fen(grants[key]["limit_fen"]):
            fail("dependency_conflict", "同一投资代收付授权的累计真实清偿超过有据限额。")
