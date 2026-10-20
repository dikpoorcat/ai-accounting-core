"""Exact adopted lookup does not require callers to reconstruct historical metadata."""

from uuid import uuid4

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense, result
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger


def test_lookup_preserves_frozen_body_and_rejects_replaced_open_generation(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    before = result(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("90000", seq=1)], unit)
    old = before["reference"]
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_result(old["result_id"], old["generation"], read)
        assert error.value.code == "conflict"
    with store.write_unit(COMPANY) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    frozen = result(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=2)], unit, correction_period="2026-02")
    ref = frozen["reference"]
    with store.read_unit(COMPANY) as read:
        assert ledger.get_result(ref["result_id"], ref["generation"], read) == frozen
        assert ledger.get_results([], read, result_refs=[ref]) == (frozen,)


@pytest.mark.parametrize("generation", [True, 0, -1, "1", 2**63])
def test_lookup_validates_generation_without_a_fake_result_reference(store, generation):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            create_ledger().get_result(str(uuid4()), generation, read)
        assert error.value.code == "invalid_input"


def test_lookup_of_genuinely_absent_result_is_not_found(store):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            create_ledger().get_result(str(uuid4()), 1, read)
        assert error.value.code == "not_found"


def test_reference_only_query_matches_current_and_frozen_without_loading_bodies(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    frozen = result(store)["reference"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    current = result(store)["reference"]
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        for method in ("vouchers", "results", "current_heads", "heads_through"):
            monkeypatch.setattr(
                type(repo), method, lambda *a, **kw: pytest.fail("引用查询加载了正文")
            )
        assert ledger.get_result_refs((BUSINESS,), read) == (current,)
        assert ledger.get_result_refs((), read) == ()
        assert ledger.get_result_refs((str(uuid4()),), read) == ()
    with store.read_unit(COMPANY, view="as_posted", as_of="2026-01") as read:
        assert ledger.get_result_refs((BUSINESS,), read) == (frozen,)
