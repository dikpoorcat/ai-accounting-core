"""Invalid owner requests cannot bypass native checks through preparing delivery."""

import pytest
from test_dashboard_async import _publish_snapshot_loans
from test_dashboard_model_transactions import state
from test_replay_close_range import replay_book as replay_book  # noqa: F401

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import delivery_state


def test_invalid_requests_share_native_errors_and_never_queue_while_preparing(replay_book):
    engine = _publish_snapshot_loans(replay_book)
    assert delivery_state(engine, "2026-01")["state"] == "preparing"
    before = state(engine)
    with engine.store.connection(read_only=True) as connection:
        queued_periods = {row[0] for row in connection.execute(
            "SELECT period FROM dashboard_projection_request"
        )}
    cases = [
        (page, {"period": month}, "invalid_command")
        for page in ("brief", "funds", "employees", "assets")
        for month in ("bad", "", "2026-13", "0000-01")
    ] + [
        (page, {"period": "2027-12"}, "dashboard_period_not_found")
        for page in ("brief", "funds", "employees", "assets")
    ] + [
        ("brief", {"period": "2026-01", **options}, "invalid_command")
        for options in (
            {"limit": 0}, {"limit": 501}, {"limit": True}, {"limit": 20.0},
            {"preparation": "skip"}, {"cursor": "unbound"}, {"section": "missing"},
            {"voucher_number": 0}, {"voucher_number": True},
            {"voucher_number": 1, "voucher_version_id": "other"},
            {"section": "activity", "voucher_number": 1},
        )
    ] + [
        ("funds", {"period": "2026-01", **options}, "invalid_command")
        for options in (
            {"movement_account_type": "bank"}, {"movement_account_id": "account"},
            {"movement_account_type": "invalid", "movement_account_id": "account"},
            {"movement_account_type": "bank", "movement_account_id": ""},
            {"statement_account_id": ""}, {"movement_account_selection": "invalid"},
            {"movement_account_selection": "first", "section": "movements"},
            {"movement_account_selection": "first", "movement_account_type": "bank",
             "movement_account_id": "account"},
        )
    ] + [
        ("employees", {"period": "2026-01", **options}, "invalid_command")
        for options in ({"employee_id": ""}, {"employee_filter": "invalid"})
    ] + [
        ("assets", {"period": "2026-01", **options}, "invalid_command")
        for options in ({"asset_id": ""}, {"project_id": ""}, {"asset_filter": "invalid"})
    ] + [
        ("quarterly_report", options, "invalid_command")
        for options in (
            {"year": 0, "quarter": 1}, {"year": 10000, "quarter": 1},
            {"year": True, "quarter": 1}, {"year": "2026", "quarter": 1},
            {"year": 2026, "quarter": 0}, {"year": 2026, "quarter": 5},
            {"year": 2026, "quarter": True},
            {"year": 2026, "quarter": 1, "preparation": "skip"},
        )
    ]
    dashboard = Dashboard(engine)
    for page, options, expected_code in cases:
        with pytest.raises(KernelError) as native:
            getattr(dashboard, page)(**options)
        with pytest.raises(KernelError) as delivered:
            replay_book.app.dispatch(
                "dashboard_" + page, {"company_id": replay_book.company, **options},
                session_token=replay_book.token,
            )
        assert native.value.code == delivered.value.code == expected_code, (page, options)
        assert state(engine) == before, (page, options)
    with engine.store.connection(read_only=True) as connection:
        assert {row[0] for row in connection.execute(
            "SELECT period FROM dashboard_projection_request"
        )} == queued_periods
