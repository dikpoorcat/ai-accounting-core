"""Owner reads avoid unrelated ledger history while preserving business amounts."""

import pytest
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.dashboard import Dashboard, _Snapshot

engine = engine_fixture


@pytest.mark.parametrize("page", ["funds", "employees"])
def test_unused_ledger_history_is_not_loaded(engine, monkeypatch, page):
    save(engine, subject="january", amount=100, request="january")
    publish(engine, ["january"], request="post-january")
    close(engine)
    save(engine, subject="february", period="2026-02", amount=200, request="february")
    publish(engine, ["february"], request="post-february")
    def read():
        return getattr(Dashboard(engine), page)("2026-02", limit=1)
    narrowed_work, narrowed = measure_work(engine, read)

    # Reproduce the prior eager ledger read within the same real snapshot.
    # Both paths execute real storage and content checks, without fake counters.
    original = _Snapshot.__init__

    def eager(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _ = self.accounts

    monkeypatch.setattr(_Snapshot, "__init__", eager)
    eager_work, eager_result = measure_work(engine, read)
    for result in (narrowed, eager_result):
        result["data"].pop("generated_at", None)
    assert narrowed == eager_result
    assert narrowed_work["counters"]["returned_value_bytes"] < (
        eager_work["counters"]["returned_value_bytes"]
    )
    assert narrowed_work["counters"]["sqlite_vm_steps"] < (
        eager_work["counters"]["sqlite_vm_steps"]
    )


def test_ledger_consumers_still_read_exact_frozen_amounts(engine):
    save(engine, subject="january", amount=100, request="january")
    publish(engine, ["january"], request="post-january")
    close(engine)
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        assert snapshot.accounts["5602"] == 100
        assert snapshot.accounts["2202"] == -100
