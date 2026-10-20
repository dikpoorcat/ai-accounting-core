"""Normalized rows must remain complete without keeping a second body."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    restore_fact_collection,
    split_fact_collection,
)


def example(kind):
    source, account = str(uuid4()), str(uuid4())
    if kind == "opening.package":
        field = "members"
        data = {
            "basis": "existing_ledger",
            "members": [
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [source],
                    "data": {"type": "bank", "funds_account_id": account, "balance_fen": "100"},
                },
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [source],
                    "data": {
                        "type": "equity",
                        "holder_or_basis_id": str(uuid4()),
                        "equity_kind": "retained_earnings",
                        "balance_fen": "100",
                    },
                },
            ],
        }
    else:
        field = "rows"
        data = {
            "rows": [
                {
                    "row_id": str(uuid4()),
                    "source": {"source_id": source},
                    "actual_on": "2026-07-10",
                    "direction": "inflow",
                    "amount_fen": value,
                }
                for value in ("100", "200")
            ]
        }
        if kind == "money.bank_statement":
            data.update(funds_account_id=account, opening_fen="0", closing_fen="300")
        else:
            data["platform_account_id"] = account
    return field, {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": "2026-07",
        "evidence_ids": [source],
        "data": data,
    }


@pytest.mark.parametrize(
    "kind", ["opening.package", "money.bank_statement", "money.platform_activity"]
)
def test_split_keeps_one_body_and_full_export_requires_its_original_order_and_values(kind):
    field, fact = example(kind)
    before = deepcopy(fact)
    parent, items = split_fact_collection(fact, field)
    assert fact == before
    assert field not in parent["data"]
    assert set(parent) == {*fact, "collection_digest"}
    assert restore_fact_collection(parent, field, items) == fact
    for changed in (items[:-1], [], list(reversed(items))):
        with pytest.raises(ContractError) as error:
            restore_fact_collection(parent, field, changed)
        assert error.value.code == "storage_corrupt"
    changed = deepcopy(items)
    location = changed[0]["data"] if field == "members" else changed[0]
    location["balance_fen" if field == "members" else "amount_fen"] = "101"
    with pytest.raises(ContractError) as error:
        restore_fact_collection(parent, field, changed)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("corruption", ["missing_digest", "extra_metadata", "duplicate_body"])
def test_projection_cannot_be_accepted_as_an_unverified_older_shape(corruption):
    field, fact = example("money.bank_statement")
    parent, items = split_fact_collection(fact, field)
    if corruption == "missing_digest":
        del parent["collection_digest"]
    elif corruption == "extra_metadata":
        parent["count"] = len(items)
    else:
        parent["data"][field] = items
    with pytest.raises(ContractError) as error:
        restore_fact_collection(parent, field, items)
    assert error.value.code == "storage_corrupt"
