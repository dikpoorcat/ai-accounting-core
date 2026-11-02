"""An actual collector clears only the opening investment income named by the grant."""

from copy import deepcopy

import pytest

from ai_accounting_modular.domains.financing.public import create_investment
from tests.modular.ledger.opening._support import PARTY, PERSON, SOURCE, member
from tests.modular.reading.test_brief_activity import capture, vouchers

from ._investment_portability_case import invested as invested
from ._investment_portability_case import movement, seed


@pytest.mark.parametrize("mode", ["allowed", "cross_terms", "ordinary"])
def test_opening_income_agent_uses_its_frozen_identity_and_rejects_other_claims(invested, mode):
    case, inputs = invested, seed(invested, settle=False)
    terms = deepcopy(inputs["terms"][1 if mode == "cross_terms" else 0])
    terms["data"]["settlement_authorizations"] = [
        {
            "entry_key": "opening_agent",
            "principal_party_id": PARTY,
            "agent_id": PERSON,
            "purposes": ["distribution_receipt"],
            "valid_from": "2026-07",
            "valid_through": "2026-07",
            "source": {"source_id": SOURCE},
            "limit_fen": "17",
        }
    ]
    mutations = [{"op": "replace", "expected_seq": 1, "fact": terms}]
    package = deepcopy(inputs["package"])
    source_member, amount_ = inputs["receivables"][0], "7"
    if mode == "ordinary":
        source_member = member(
            {
                "type": "obligation",
                "nature": "customer_receivable",
                "counterparty_id": PARTY,
                "amount_fen": "17",
                "original_business_reference": "普通期初客户款",
            }
        )
        package["data"]["members"].append(source_member)
        for item in package["data"]["members"]:
            if item["data"]["type"] == "equity":
                item["data"]["balance_fen"] = "10105"
        mutations.append({"op": "replace", "expected_seq": 1, "fact": package})
        amount_ = "17"
    received = movement(package, "member:" + source_member["member_id"], amount_)
    received["data"]["counterparty_id"] = PERSON
    received["data"]["allocations"][0]["authorization"] = {
        "business_id": terms["business_id"],
        "slot": "authorization:opening_agent",
    }
    mutations.append({"op": "create", "expected_seq": 0, "fact": received})
    response = case.app.accounting_apply(case.request(mutations=mutations))
    if mode != "allowed":
        assert response.get("completion") == "not_saved", str(response)
        assert case.get(received["business_id"])["businesses"] == []
        return
    assert response.get("completion") == "complete", response.get("message", response)
    capture(case, "2026-07")
    actual_voucher = next(
        item
        for item in vouchers(case, "2026-07")["items"]
        if item["subject_id"] == received["business_id"]
    )
    assert {party["id"] for line in actual_voucher["lines"] for party in line["parties"]} == {PARTY}
    owner = create_investment(ledger=case.app.ledger, identity=case.app.identity)
    with case.kernel.storage.read_unit(case.company) as read:
        actual = owner.investment_activity(
            {
                "company_id": case.company,
                "period_from": "2026-07",
                "period_to": "2026-07",
                "view": "current",
            },
            read,
        )
        assert actual.actual_receipts_fen == 7
        assert actual.actual_payments_fen == 0
        matched = [item for item in actual.products if item.terms_id == terms["business_id"]]
        assert len(matched) == 1
        product = matched[0]
        assert product.actual_receipts_fen == 7
        assert product.actual_payments_fen == 0
        assert (
            sum(
                item.actual_receipts_fen
                for item in actual.products
                if item.terms_id != terms["business_id"]
            )
            == 0
        )
        (receipt,) = [
            item
            for item in actual.events
            if item.business_id == received["business_id"] and item.settlement_fen is not None
        ]
        assert receipt.terms_id == terms["business_id"]
        assert receipt.fund_id == product.fund_id
        (body,) = case.app.ledger.get_adopted_contents((receipt.adopted,), read)
        assert receipt.source == body.source
        (proof,) = [
            item
            for item in body.basis["adopted_quantities"]
            if item["type"] == "investment_settlement_authorization"
        ]
        assert proof["obligation_source"] == received["data"]["allocations"][0]["source"]
        assert proof["authorization"]["terms_id"] == terms["business_id"]
        assert proof["authorization"]["principal_party_id"] == PARTY
        assert proof["authorization"]["agent_id"] == PERSON
        assert proof["source"]["business_id"] == terms["business_id"]
        assert proof["source"]["result_slot"] == "authorization:opening_agent"
        assert proof["source"] in body.basis["source_results"]
        (grant_entry,) = case.app.ledger.get_adopted_sources((proof["source"],), read)
        (grant,) = case.app.ledger.get_adopted_contents((grant_entry.reference,), read)
        assert grant.source == proof["source"]
        assert grant.target["values"]["authorization"] == proof["authorization"]
