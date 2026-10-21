"""Exact and covering close identities need no financial statement hydration."""

import pytest
from test_adoption import COMPANY, expense
from test_adoption import store as store
from test_closing import close
from test_financial_core import origin, save

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger import _closing, _financial
from ai_accounting_modular.ledger.public import create_ledger


def test_close_headers_preserve_exact_and_covering_heads_without_statements(store, monkeypatch):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        assert ledger.period_closure("2026-01", read).state == "open"
    save(store, ledger, [origin("0"), expense()])
    checked = close(store, ledger, "2026-03")

    def forbidden(*args, **kwargs):
        raise AssertionError("关账头不得读取完整三表或重新核算公司")

    monkeypatch.setattr(_financial, "snapshot", forbidden)
    monkeypatch.setattr(_closing, "_record", forbidden)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        monkeypatch.setattr(type(repo), "close_record", forbidden)
        january = ledger.period_closure("2026-01", read)
        march = ledger.period_closure("2026-03", read)
        april = ledger.period_closure("2026-04", read)
    assert january.period == "2026-01" and january.closing_period == "2026-03"
    assert january.period_from == "2026-01" and january.head == checked.head
    assert march.head == january.head and march.closing_period == march.period
    assert april.state == "open" and april.head is None and april.closing_period is None


def test_close_header_rejects_changed_digest_instead_of_guessing_covered_state(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    close(store, ledger, "2026-01")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original = type(repo).close_header

        def changed(self, period):
            return original(self, period) | {"result_head": "f" * 64}

        monkeypatch.setattr(type(repo), "close_header", changed)
        with pytest.raises(ContractError) as failure:
            ledger.period_closure("2026-01", read)
        assert failure.value.code == "storage_corrupt"
