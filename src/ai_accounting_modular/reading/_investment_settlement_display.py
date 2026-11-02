"""Read a collector only from the payment's frozen typed authorization proof."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.ledger.public import (
    investment_settlement_identity,
    settlement_purpose,
    validate_settlement_counterparty,
)


def authorized_sources(content, cache, actual_party):
    basis = content.basis
    data, authorized = basis["input"], set()
    subjects = {}
    for allocation in data["allocations"]:
        if "authorization" not in allocation:
            continue
        proofs = [
            item
            for item in basis["adopted_quantities"]
            if item["type"] == "investment_settlement_authorization"
            and item["obligation_source"] == allocation["source"]
        ]
        obligations = [
            item
            for item in basis["adopted_quantities"]
            if item["type"] == "obligation" and item["source"] == allocation["source"]
        ]
        if len(proofs) != 1 or len(obligations) != 1:
            raise ContractError("storage_corrupt", "投资实际收付缺少唯一的冻结授权及原义务。")
        proof, obligation = proofs[0], obligations[0]
        validate_contract(
            "business-facts.schema.json", "AdoptedInvestmentSettlementAuthorization", proof
        )
        source, authorization = proof["source"], proof["authorization"]
        adopted = cache.source(content, source["business_id"], result_slot=source["result_slot"])
        producer = cache.source(content, allocation["source"]["business_id"])
        terms_id, product_id = investment_settlement_identity(
            producer, allocation["source"], subjects=subjects
        )
        purpose = settlement_purpose(
            producer.target["kind"], data["direction"], account_code=obligation["account_code"]
        )
        if (
            source["business_id"] != allocation["authorization"]["business_id"]
            or source["result_slot"] != allocation["authorization"]["slot"]
            or source not in basis["source_results"]
            or adopted.source != source
            or adopted.target["kind"] != "investment.terms"
            or adopted.target["values"].get("authorization") != authorization
            or proof["amount_fen"] != allocation["amount_fen"]
            or proof["purpose"] != purpose
        ):
            raise ContractError("storage_corrupt", "投资实际收付的冻结授权来源、金额或方向不一致。")
        validate_settlement_counterparty(
            authorization,
            terms_id=terms_id,
            product_id=product_id,
            principal_id=obligation.get("party_id"),
            actual_party_id=actual_party,
            purpose=proof["purpose"],
            actual_on=basis["effective_on"],
        )
        authorized.add((allocation["source"]["business_id"], allocation["source"]["slot"]))
    return authorized
