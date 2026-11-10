"""A withdrawn target cannot create a new effective voucher group."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.ledger._values import check_plan
from tests.modular.ledger.test_adoption import BUSINESS, expense, withdrawal


def test_withdrawal_rejects_nonempty_journals_before_adoption():
    baseline = {
        "business_id": BUSINESS,
        "result_slot": "main",
        "result_id": "70000000-0000-4000-8000-000000000001",
        "result_generation": 1,
        "content_digest": "a" * 64,
    }
    plan = withdrawal(baseline)["plans"][0]
    check_plan(plan)
    plan["journals"] = expense()["plans"][0]["journals"]
    with pytest.raises(ContractError) as error:
        check_plan(plan)
    assert error.value.code == "invalid_input"


def test_expanded_batch_journal_has_the_same_write_and_read_contract():
    plan = expense()["plans"][0]
    journal = plan["journals"][0]
    journal["lines"] = [
        dict(line, line_no=index) for index, line in enumerate(journal["lines"] * 5001, 1)
    ]
    check_plan(plan)
    voucher = {
        **journal,
        "voucher_id": "80000000-0000-4000-8000-000000000001",
        "result": {
            "business_id": BUSINESS,
            "result_slot": "main",
            "result_id": "70000000-0000-4000-8000-000000000001",
            "generation": 1,
            "business_period": "2026-01",
            "posting_period": "2026-01",
            "state": "open",
        },
        "period": "2026-01",
        "number": 1,
        "role": "original",
    }
    assert len(validate_contract("core.schema.json", "Voucher", voucher)["lines"]) == 10002
