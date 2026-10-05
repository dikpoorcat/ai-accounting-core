"""Automatic owner selection keeps the company proof and explicit query contract."""

import pytest
import test_banking as banking
from stage9_metrics import measure_work
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_transport import authenticated
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store

bank_book = banking.book
resident = resident_fixture


def test_first_account_has_one_company_summary_and_explicit_pagination(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-b", "bank-a", "bank-a", "bank-b"])
    dashboard = Dashboard(engine)
    with monkeypatch.context() as patch:
        patch.setattr(FundsRead, "_can_share_first_page", lambda self: False)
        old_work, expected = measure_work(engine, lambda: dashboard.funds(
            "2026-09", movement_account_selection="first", limit=1,
        ))
    work, first = measure_work(engine, lambda: dashboard.funds(
        "2026-09", movement_account_selection="first", limit=1,
    ))
    assert first == expected
    # The former private empty-summary call is no longer a product contract.
    # Check real source execution and complete business output instead.
    source_marker = "transfers AS (SELECT event_id"
    assert sum(row["calls"] for row in old_work["sql"] if source_marker in row["statement"]) == 2
    assert sum(row["calls"] for row in work["sql"] if source_marker in row["statement"]) == 1
    assert work["counters"]["sqlite_vm_steps"] < old_work["counters"]["sqlite_vm_steps"]
    validate_response("dashboard_funds", first)
    data = first["data"]
    assert first["schema_version"] == 9
    assert data["selected_movement_account"] == {"type": "bank", "account_id": "bank-a"}
    assert data["total_fen"] == data["inflow_fen"] == 4
    assert data["movement_count"] == 4
    movements = data["collections"]["movements"]
    assert movements["page"]["filtered_count"] == 2
    assert [row["account_id"] for row in movements["items"]] == ["bank-a"]
    following = dashboard.funds(
        "2026-09", movement_account_type="bank", movement_account_id="bank-a",
        section="movements", limit=1, cursor=movements["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )["data"]["collections"]["movements"]
    assert following["items"][0]["id"] != movements["items"][0]["id"]
    assert not following["page"]["has_more"]
    all_accounts = dashboard.funds("2026-09", limit=20)["data"]
    assert all_accounts["selected_movement_account"] is None
    assert {row["account_id"] for row in all_accounts["collections"]["movements"]["items"]} == {
        "bank-a", "bank-b",
    }


def test_first_account_can_have_no_movements(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish, bank="bank-a")
    _publish_filter_funding(engine, ["bank-b", "bank-b"])
    data = Dashboard(engine).funds("2026-09", movement_account_selection="first")["data"]
    assert data["selected_movement_account"] == {"type": "bank", "account_id": "bank-a"}
    assert data["total_fen"] == 2
    assert data["collections"]["movements"]["items"] == []
    assert data["collections"]["movements"]["page"]["filtered_count"] == 0


def test_first_account_without_accounts_is_empty(bank_book):
    engine, save, _, _ = bank_book
    save("bank_opening", "draft-opening", {
        "period": "2026-09", "bank_account_id": "draft-bank", "opening_fen": 0,
        "basis": "new_account",
    })
    response = Dashboard(engine).funds("2026-09", movement_account_selection="first")
    validate_response("dashboard_funds", response)
    assert response["data"]["selected_movement_account"] is None
    assert response["data"]["collections"]["accounts"]["items"] == []
    assert response["data"]["collections"]["movements"]["items"] == []


def test_first_account_without_a_period_has_versioned_empty_response(tmp_path):
    engine = Engine(Store.create(
        tmp_path / "empty.sqlite", production_bundle(), "company",
        "911100000000000001", "empty-database",
    ))
    response = Dashboard(engine).funds(movement_account_selection="first")
    validate_response("dashboard_funds", response)
    assert response["schema_version"] == 9
    assert response["data"] is None


@pytest.mark.parametrize("options", [
    {"movement_account_selection": "invalid"},
    {"movement_account_selection": "first", "section": "movements"},
    {"movement_account_selection": "first", "movement_account_type": "bank",
     "movement_account_id": "bank-a"},
])
def test_first_account_rejects_conflicting_selection(bank_book, options):
    with pytest.raises(KernelError) as rejected:
        Dashboard(bank_book[0]).funds("2026-09", **options)
    assert rejected.value.code == "invalid_command"


def test_http_first_account_and_default_full_company_query(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "自动账户测试")["id"]
    _publish_filter_funding(service.engine(company), ["bank-b", "bank-a", "bank-a"])
    query = f"/api/dashboard/funds?company_id={company}&period=2026-09"
    status, _, _, response = http.request(
        query + "&movement_account_selection=first", headers=headers
    )
    assert status == 200
    assert response["data"]["selected_movement_account"] == {"type": "bank", "account_id": "bank-a"}
    assert response["data"]["collections"]["movements"]["page"]["filtered_count"] == 2
    status, _, _, response = http.request(query, headers=headers)
    assert status == 200
    assert response["data"]["selected_movement_account"] is None
    assert response["data"]["collections"]["movements"]["page"]["filtered_count"] == 3
    status, _, _, response = http.request(
        query + "&movement_account_selection=invalid", headers=headers
    )
    assert status == 400 and response["code"] == "invalid_command"
