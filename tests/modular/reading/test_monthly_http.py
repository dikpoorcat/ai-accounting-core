"""Existing month protocol is reached through the real public composition."""

import pytest

from ai_accounting_modular.dashboard.public import create_dashboard
from tests.modular.application._working_case import case as case

from ._support import NOW


def get(case, *pairs):
    adapter = create_dashboard(case.kernel.reading, now=lambda: NOW)
    with adapter.get("/api/dashboard/funds", [("company_id", case.company), *pairs]) as response:
        return response.status, response.body


def test_empty_month_is_a_valid_ready_page_without_invented_amounts(case):
    status, body = get(case)
    assert status == 200 and body["state"] == "ready"
    assert body["period"] is None and body["model_version"] is None
    response = body["response"]
    assert response["schema_version"] == 10
    assert response["snapshot_version"] is None
    assert response["selected_period"] is None and response["data"] is None
    assert response["read_context"]["company_id"] == case.company
    assert response["read_context"]["as_of"] == "2026-10-21"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.repository("reading").scopes() == ()


def test_real_month_prepares_then_uses_the_frozen_funds_envelope_and_raw_section(case):
    case.seed()
    status, preparing = get(case, ("period", "2026-01"), ("preparation", "deferred"))
    assert status == 200 and preparing["state"] == "preparing"
    assert preparing["period"] == "2026-01" and "response" not in preparing
    assert case.kernel.reading.run_pending(case.company)
    status, ready = get(case, ("period", "2026-01"))
    assert status == 200 and ready["state"] == "ready", ready
    response = ready["response"]
    assert ready["model_version"] == response["snapshot_version"]
    assert response["data"]["total_fen"] == "0"
    assert response["data"]["inflow_fen"] == response["data"]["outflow_fen"] == "0"
    assert set(response["data"]["collections"]) == {
        "accounts",
        "movements",
        "statements",
        "investment_products",
        "investment_events",
    }
    version = ready["model_version"]
    status, raw = get(
        case,
        ("period", "2026-01"),
        ("section", "accounts"),
        ("expected_version", version),
    )
    assert status == 200 and raw["schema_version"] == 10 and "response" not in raw
    assert raw["snapshot_version"] == version
    assert raw["read_semantics"]["business_basis"] == "current_known"


def test_default_month_is_resolved_inside_one_read_snapshot(case, monkeypatch):
    case.seed()
    assert get(case)[1]["period"] == "2026-01"
    assert case.kernel.reading.run_pending(case.company)
    calls = []
    original = case.kernel.storage.read_unit

    def counted(company_id):
        calls.append(company_id)
        return original(company_id)

    monkeypatch.setattr(case.kernel.storage, "read_unit", counted)
    status, result = get(case)
    assert status == 200 and result["state"] == "ready"
    assert calls == [case.company]


@pytest.mark.parametrize(
    "pairs",
    (
        (("company_id", "other-company"),),
        (("period", "2026-13"),),
        (("limit", "0"),),
        (("limit", "501"),),
        (("limit", "1.0"),),
        (("section", "unknown"),),
        (("cursor", "opaque"),),
        (("movement_account_type", "bank"),),
        (("movement_account_selection", "first"), ("section", "movements")),
        (("expected_version", "bad"),),
        (("preparation", "complete"),),
        (("extra", "value"),),
    ),
)
def test_invalid_month_request_does_not_register_work_or_become_an_empty_page(case, pairs):
    status, body = get(case, *pairs)
    assert status == 400 and body["code"] == "invalid_command", body
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.repository("reading").scopes() == ()
