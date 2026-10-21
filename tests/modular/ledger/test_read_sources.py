"""Exact frozen sources are distinct from the current selected business head."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError, schema_document
from ai_accounting_modular.ledger.public import create_ledger


def test_exact_source_locates_old_frozen_header_without_loading_any_body(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        old = ledger.get_result_source(BUSINESS, "main", unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
        new = ledger.get_result_source(BUSINESS, "main", unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        for name in ("results", "financial_results", "vouchers", "children", "heads_through"):
            monkeypatch.setattr(type(repo), name, lambda *a: pytest.fail("来源定位读取了正文"))
        entries = ledger.get_adopted_sources((old, new), read)
    assert [entry.source for entry in entries] == [old, new]
    assert [entry.reference["posting_period"] for entry in entries] == ["2026-01", "2026-02"]
    assert entries[0].reference["state"] == "frozen"
    assert all(entry.business_kind == "operations.expense" for entry in entries)


def test_exact_source_rejects_replaced_generation_or_changed_digest(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        old = ledger.get_result_source(BUSINESS, "main", unit)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit)
        new = ledger.get_result_source(BUSINESS, "main", unit)
    altered = deepcopy(new)
    altered["content_digest"] = "0" * 64
    with store.read_unit(COMPANY) as read:
        for value in (old, altered):
            with pytest.raises(ContractError) as error:
                ledger.get_adopted_sources((value,), read)
            assert error.value.code == "snapshot_changed"
        with pytest.raises(ContractError) as error:
            ledger.get_adopted_sources((new, new), read)
        assert error.value.code == "invalid_input"
        with pytest.raises(ContractError) as error:
            ledger.get_adopted_sources((new | {"result_id": str(uuid4())},), read)
        assert error.value.code == "not_found"


def test_public_account_names_cover_exactly_controlled_subjects_without_a_database():
    codes = schema_document("reporting.schema.json")["$defs"]["AccountCode"]["enum"]
    labels = create_ledger().account_names(codes)
    assert set(labels) == set(codes)
    assert all(
        name and any("\u4e00" <= character <= "\u9fff" for character in name)
        for name in labels.values()
    )
    assert labels["221101"] == "应付工资"
    assert labels["560203"] == "管理费用—摊销"
    assert labels["122106"] == "合作代管资金"
    with pytest.raises(ContractError):
        create_ledger().account_names(("9999",))
