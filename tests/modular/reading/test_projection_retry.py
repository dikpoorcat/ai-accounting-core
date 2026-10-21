"""A user can repair derived reads without repeating any accounting command."""

import pytest

from ai_accounting_modular.dashboard.public import create_dashboard
from ai_accounting_modular.reading import _funds
from tests.modular.application._working_case import case as case


def parameters(case):
    return {"company_id": case.company, "period": "2026-01"}


def get(adapter, case):
    with adapter.get("/api/dashboard/funds", parameters(case)) as response:
        return response


def failed(case, monkeypatch):
    case.seed()
    adapter = create_dashboard(case.kernel.reading)
    assert get(adapter, case).body["state"] == "preparing"
    original = _funds.prepare

    def broken(*args):
        raise OSError("synthetic producer failure")

    monkeypatch.setattr(_funds, "prepare", broken)
    assert case.kernel.reading.run_pending(case.company) == ()
    monkeypatch.setattr(_funds, "prepare", original)
    assert get(adapter, case).body["state"] == "failed"
    return adapter


def test_explicit_http_retry_repairs_only_failed_read_and_get_does_not_retry(case, monkeypatch):
    adapter = failed(case, monkeypatch)
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    assert get(adapter, case).body["state"] == "failed"
    assert case.kernel.reading.run_pending(case.company) == ()
    pending = adapter.post("/api/dashboard/funds/retry", parameters(case))
    assert pending.status == 200 and pending.body["state"] == "preparing"
    # Duplicate clicks neither rerun a completed command nor replace a target.
    assert adapter.post("/api/dashboard/funds/retry", parameters(case)).body == pending.body
    assert case.kernel.reading.run_pending(case.company)
    ready = get(adapter, case)
    assert ready.status == 200 and ready.body["state"] == "ready"
    assert adapter.post("/api/dashboard/funds/retry", parameters(case)).body == ready.body
    assert case.kernel.reading.run_pending(case.company) == ()
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head
        states = read.repository("reading").scopes()
        assert len(states) == 1 and states[0]["component_key"] == "funds"


@pytest.mark.parametrize(
    "extra",
    (
        {"section": "accounts"},
        {"cursor": "bad"},
        {"expected_version": "bad"},
        {"movement_account_selection": "bad"},
        {"extra": "bad"},
        {"period": ""},
        {"limit": 20},
    ),
)
def test_invalid_retry_does_not_restart_failed_projection(case, monkeypatch, extra):
    adapter = failed(case, monkeypatch)
    result = adapter.post("/api/dashboard/funds/retry", parameters(case) | extra)
    assert result.status == 400 and result.body["code"] == "invalid_command"
    assert get(adapter, case).body["state"] == "failed"
    assert case.kernel.reading.run_pending(case.company) == ()


def test_retry_requires_exact_supported_route_and_month(case, monkeypatch):
    adapter = failed(case, monkeypatch)
    assert adapter.post("/api/dashboard/funds/retry", {"company_id": case.company}).status == 400
    assert adapter.post("/api/dashboard/quarterly_report/retry", parameters(case)).status == 404
    assert get(adapter, case).body["state"] == "failed"
