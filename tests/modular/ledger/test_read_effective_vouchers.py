"""An inherited journal can be located without fetching historical bodies or lines."""

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense, result, withdrawal
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger


def test_effective_header_follows_formal_only_heads_and_stops_at_withdrawal(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    january = result(store)
    formal = expense(seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [formal], unit, correction_period="2026-02")
        unit.repository("ledger").freeze_heads("2026-02")
    february = result(store)
    assert february["vouchers"] == []
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(BUSINESS, "main", unit)
        adopt_declared(ledger, [withdrawal(baseline)], unit, correction_period="2026-03")
    march = result(store)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        for name in ("results", "financial_results", "vouchers", "children", "read_voucher_lines"):
            monkeypatch.setattr(type(repo), name, lambda *a: pytest.fail("凭证定位读取了历史正文"))
        headers = ledger.get_effective_voucher_directory(
            [january["reference"], february["reference"], march["reference"]], read
        )
    assert headers[0] == headers[1]
    assert headers[0].voucher_id == january["vouchers"][0]["voucher_id"]
    assert headers[0].result == {
        key: value
        for key, value in january["reference"].items()
        if key not in {"voucher_id", "voucher_number"}
    }
    assert headers[0].period == "2026-01" and headers[0].role == "original"
    assert headers[2] is None


def test_effective_header_rejects_an_open_replaced_generation(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    old = result(store)["reference"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit)
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_effective_voucher_directory([old], read)
        assert error.value.code == "snapshot_changed"
