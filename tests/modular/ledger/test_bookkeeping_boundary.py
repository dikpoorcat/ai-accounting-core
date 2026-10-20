"""The shared ledger protects the book boundary, independent of any domain."""

import pytest
from test_adoption import COMPANY, expense
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger


@pytest.mark.parametrize("store", ["2026-02"], indirect=True)
@pytest.mark.parametrize("entry", ["candidate", "adopt"])
def test_neither_ledger_entry_can_create_a_financial_result_before_book_start(store, entry):
    change, ledger = expense(), create_ledger()
    with pytest.raises(ContractError) as error, store.write_unit(COMPANY) as unit:
        if entry == "candidate":
            candidate = ledger.begin_candidate(
                change["businesses"], unit, publications=change["publications"]
            )
            candidate.stage(change["plans"][0])
        else:
            ledger.adopt([change], unit)
    assert error.value.code == "invalid_input"
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([change["businesses"][0]["business_id"]], read) == ()
