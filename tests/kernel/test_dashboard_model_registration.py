"""Real writes establish page periods without a reader scanning for months."""

import copy

import pytest
from test_banking import book as book
from test_banking import funding, opening

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import _producing, stable_payload
from ai_accounting.kernel.periods import Periods


def registered(engine):
    with engine.store.connection(read_only=True) as connection:
        return [row[0] for row in connection.execute(
            "SELECT period FROM dashboard_read_model_current ORDER BY period",
        )]


def test_new_company_actual_fact_month_works_without_maintenance(book):
    engine, save, publish, _ = book
    # The fixture has an explicitly received January supporting document.
    assert registered(engine) == ["2026-01"]
    opening(save, publish)
    assert registered(engine) == ["2026-01", "2026-09"]
    funding(save, publish, amount=123)
    dashboard = Dashboard(engine)
    actual = stable_payload(dashboard.funds("2026-09", movement_account_selection="first"))
    token = _producing.set(True)
    try:
        expected = stable_payload(dashboard.funds("2026-09", movement_account_selection="first"))
    finally:
        _producing.reset(token)
    assert actual == expected


def test_material_inventory_registers_only_the_recorded_month(book):
    engine, _, _, proof = book
    Periods(engine).inventory(
        "2026-08", "transactions", evidence=[], expected=0, no_business=True,
        confirmation_evidence=proof, request_id="first-inventory-month",
    )
    assert registered(engine) == ["2026-01", "2026-08"]
    response = Dashboard(engine).brief("2026-08")
    assert response["selected_period"]["key"] == "2026-08"


def test_new_month_producer_failure_rolls_back_fact_and_registration(book, monkeypatch):
    from ai_accounting.kernel import dashboard_model_materializer

    engine, save, _, _ = book
    # Entity setup is outside the fact transaction and is intentionally kept.
    from entity_fixture import seed_registration_entities
    data = {"period": "2026-09", "bank_account_id": "bank-a", "opening_fen": 0,
            "basis": "new_account"}
    seed_registration_entities(engine, "bank_opening", data)
    before_months = registered(engine)
    with engine.store.connection(read_only=True) as connection:
        before = copy.copy(tuple(connection.iterdump()))

    def fail(*_):
        raise RuntimeError("synthetic model construction failure")

    monkeypatch.setattr(dashboard_model_materializer, "build_model", fail)
    with pytest.raises(RuntimeError, match="synthetic model construction failure"):
        save("bank_opening", "failed-opening", data)
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == before
    assert registered(engine) == before_months


def test_model_registration_replay_keeps_business_and_model_receipts(book):
    engine, _, _, proof = book
    periods = Periods(engine)
    options = dict(evidence=[], expected=0, no_business=True,
                   confirmation_evidence=proof, request_id="replayed-inventory-month")
    first = periods.inventory("2026-08", "transactions", **options)
    with engine.store.connection(read_only=True) as connection:
        before = tuple(connection.iterdump())
    assert periods.inventory("2026-08", "transactions", **options) == first
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == before
