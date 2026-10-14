"""Registered quarterly months cannot disappear behind derived locator damage."""

import pytest
from dashboard_model_fixture import produce
from test_banking import book as book  # noqa: F401
from test_banking import funding, opening
from test_dashboard_model_transactions import state
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard


@pytest.mark.parametrize("target", ["all_missing", "latest_missing", "old_receipt", "wrong_month"])
def test_quarterly_registered_month_damage_refuses_native_fallback(book, monkeypatch, target):
    engine, save, publish, _ = book
    opening(save, publish, month="2026-01")
    january_model = produce(engine, "2026-01")["model_version"]
    produce(engine, "2026-03")
    funding(save, publish, amount=17, day="2026-03-01")
    produce(engine, "2026-03")
    assert Dashboard(engine).quarterly_report(2026, 1)["period"]["quarter"] == 1
    with engine.store.connection(read_only=True) as connection:
        march_receipts = [row[0] for row in connection.execute(
            "SELECT id FROM dashboard_projection_publication WHERE period='2026-03' ORDER BY rowid",
        )]
    assert len(march_receipts) >= 2
    if target == "all_missing":
        damage(engine, "dashboard_projection_current", "DELETE FROM dashboard_projection_current")
    elif target == "latest_missing":
        damage(engine, "dashboard_projection_current",
               "DELETE FROM dashboard_projection_current WHERE period='2026-03'")
    else:
        wrong = march_receipts[0] if target == "old_receipt" else january_model
        damage(engine, "dashboard_projection_current",
               "UPDATE dashboard_projection_current SET publication_id=? WHERE period='2026-03'",
               (wrong,))
    before = state(engine)

    def unexpected(*args, **kwargs):
        raise AssertionError("damaged quarterly registration silently fell back to native output")

    monkeypatch.setattr(Dashboard, "_quarterly_report_in_snapshot", unexpected)
    with pytest.raises(KernelError) as error:
        Dashboard(engine).quarterly_report(2026, 1)
    assert error.value.code == "content_integrity_failed"
    assert state(engine) == before
