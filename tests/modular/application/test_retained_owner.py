"""Keeping a main result also keeps the owner's derived object projection."""

from uuid import uuid4

from ._working_case import case as case


def test_operations_retained_main_preserves_inherited_counterparty_projection(case):
    expense = case.seed()
    bank = str(uuid4())
    paid = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-10",
        "evidence_ids": [case.source_id],
        "data": {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "110000",
            "payment_method": "individual",
            "counterparty_id": case.party,
            "allocations": [
                {
                    "source": {"business_id": expense["business_id"], "slot": "primary"},
                    "amount_fen": "110000",
                }
            ],
        },
    }
    claim = {
        "business_id": str(uuid4()),
        "kind": "operations.overpayment",
        "period": "2026-01",
        "effective_on": "2026-01-20",
        "evidence_ids": [case.source_id],
        "data": {
            "source": {"business_id": expense["business_id"], "slot": "primary"},
            "amount_fen": "10000",
        },
    }
    saved = case.app.accounting_apply(
        case.request(
            paid,
            claim,
            objects=[
                {
                    "object_id": bank,
                    "kind": "bank_account",
                    "name": "合成银行",
                    "expected_seq": 0,
                    "data": {
                        "bank_name": "合成银行",
                        "account_number": "synthetic-001",
                        "currency": "CNY",
                        "owner": "company",
                    },
                }
            ],
        )
    )
    assert saved["completion"] == "complete", saved
    current = case.get(claim["business_id"])["businesses"][0]
    owner = case.app.registry.owner(claim["kind"])
    query = {
        "company_id": case.company,
        "family": "operations",
        "kind": claim["kind"],
        "object_id": case.party,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "limit": 10,
    }
    with case.kernel.storage.read_unit(case.company) as read:
        assert owner.query_current(query, read) == (claim,)
        before = read.head
    # An application description-only visit keeps this exact formal main. The
    # public owner must not replace its inherited party with an absent new plan.
    with case.kernel.storage.write_unit(case.company) as unit:
        owner.apply_current(
            {
                "domain": "operations",
                "businesses": [
                    {
                        "business_id": claim["business_id"],
                        "expected_seq": 1,
                        "op": "recalculate",
                        "fact": claim,
                        "complete_result_slots": ["main"],
                        "natural_key": None,
                    }
                ],
                "plans": [],
                "retained_results": current["state"]["results"],
                "dependencies": [],
                "publications": [],
                "issues": [],
            },
            unit,
        )
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before
        assert owner.query_current(query, read) == (claim,)
    assert case.get(claim["business_id"])["businesses"][0] == current
