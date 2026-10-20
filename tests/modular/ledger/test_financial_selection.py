"""Report versions change with formal report inputs, not unrelated formal metadata."""

import pytest
from _financial_snapshot_review_support import (
    assemble,
    expense,
    fact,
    loan_contract,
    opening,
)

from ai_accounting_modular.contracts.public import ContractError


def test_same_month_unfunded_contract_and_policy_do_not_invalidate_financial_scope(tmp_path):
    book = assemble(tmp_path)
    book.submit(opening())
    original = book.snapshot()
    expected = original["selection"]["selection_digest"]
    contract = loan_contract("unused-report-contract", "2027-12-31")
    policy = fact(
        "unused-report-policy",
        "taxes.surtax_policy",
        {
            "policy": {
                "version": "synthetic-1",
                "official_url": "https://www.chinatax.gov.cn/",
                "effective_from": "2026-01-01",
            },
            "urban_rate": "0.07",
            "education_rate": "0.03",
            "local_education_rate": "0.02",
            "payable_fraction": "0.5",
        },
    )
    book.submit(contract, policy)
    assert book.snapshot(expected_head=expected)["selection"]["selection_digest"] == expected
    contract["data"]["matures_on"] = "2028-01-01"
    policy["data"]["urban_rate"] = "0.05"
    book.replace(contract)
    book.replace(policy)
    repeated = book.snapshot(expected_head=expected)
    assert repeated["head"] > original["head"]
    assert repeated["selection"]["selection_digest"] == expected
    assert repeated["pre_tax"] == original["pre_tax"]
    book.submit(expense("new-nonzero-report-business", "10000"))
    with pytest.raises(ContractError) as changed:
        book.snapshot(expected_head=expected)
    assert changed.value.code == "snapshot_changed"
    after = book.snapshot()
    assert after["profit_statement"]["14"]["current"]["amount_fen"] == "10000"
