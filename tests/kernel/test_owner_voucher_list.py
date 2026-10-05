"""Restored owner vouchers retain precise lines and bounded snapshot pagination."""

import pytest
from test_banking import book as _bank_book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_projection import diagnostic_vouchers
from test_reimbursement_assets import accepted_batch, batch_card
from test_reimbursement_assets import book as _asset_book
from test_service_tax_points import company as _tax_company
from test_service_tax_points import receipt, sale

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.response_contracts import http_response, validate_response

bank_book, asset_book, tax_company = _bank_book, _asset_book, _tax_company


def test_default_and_cursor_vouchers_share_selection_and_show_every_saved_line(
    bank_book, monkeypatch
):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"] * 23)

    def forbidden(*_args, **_kwargs):
        pytest.fail("owner voucher reconstructed a diagnostic graph")

    monkeypatch.setattr(_Snapshot, "voucher", forbidden)
    monkeypatch.setattr(_Snapshot, "evidence_details", forbidden)
    dashboard = Dashboard(engine)
    response = dashboard.brief("2026-09")
    data = response["data"]
    assert response["schema_version"] == 11
    assert data["voucher_count"] == data["activity_count"] == 23
    vouchers = data["collections"]["vouchers"]["items"]
    assert len(vouchers) == 20
    assert [row["voucher_version_id"] for row in vouchers] == [
        row["voucher_version_id"] for row in data["collections"]["activity"]["items"]
    ]
    page = data["collections"]["vouchers"]["page"]
    next_response = dashboard.brief(
        "2026-09", section="vouchers", cursor=page["next_cursor"],
        expected_version=response["snapshot_version"], limit=7,
    )
    assert set(next_response["data"]["collections"]) == {"vouchers"}
    final = next_response["data"]["collections"]["vouchers"]
    assert len(final["items"]) == 3 and final["page"]["has_more"] is False
    all_rows = vouchers + final["items"]
    assert len({row["voucher_version_id"] for row in all_rows}) == 23
    assert [int(row["number"]) for row in all_rows] == list(range(1, 24))
    for row in all_rows:
        assert row["amount_fen"] == sum(line["debit_fen"] for line in row["lines"])
        assert row["amount_fen"] == sum(line["credit_fen"] for line in row["lines"])
        assert not {
            "evidence", "evidence_details", "field_sources", "components", "funds", "settlements"
        } & row.keys()
    wire = http_response("dashboard_brief", response)
    assert wire["data"]["collections"]["vouchers"]["items"][0]["amount_fen"] == "1"


def test_voucher_cursor_is_bound_to_its_collection_and_focus_preserves_public_number(bank_book):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"] * 4)
    dashboard = Dashboard(engine)
    response = dashboard.brief("2026-09", limit=2)
    activity_cursor = response["data"]["collections"]["activity"]["page"]["next_cursor"]
    with pytest.raises(KernelError) as rejected:
        dashboard.brief(
            "2026-09", section="vouchers", cursor=activity_cursor,
            expected_version=response["snapshot_version"],
        )
    assert rejected.value.code == "dashboard_snapshot_changed"
    focused = dashboard.brief("2026-09", limit=2, voucher_number=4)
    selected = focused["data"]["focused_voucher"]
    assert selected["number"] == "4"
    assert selected["voucher_version_id"] == (
        focused["data"]["focused_activity"]["voucher_version_id"]
    )
    assert selected["voucher_version_id"] not in {
        row["voucher_version_id"] for row in focused["data"]["collections"]["vouchers"]["items"]
    }
    assert validate_response("dashboard_brief", focused)["data"]["focused_voucher"] == selected


def test_business_amount_remains_separate_from_voucher_total(tax_company):
    sale(tax_company, tax_obligation_date="2026-04-02")
    receipt(tax_company, "first", 40000, "2026-04-02")
    tax_company.publish("first")
    response = Dashboard(tax_company.engine).brief("2026-04")
    row = next(item for item in response["data"]["collections"]["vouchers"]["items"]
               if item["subject_id"] == "first")
    assert row["business_amount_fen"] == 40000
    assert row["amount_fen"] == 41479
    assert sum(line["debit_fen"] for line in row["lines"]) == 41479
    assert sum(line["credit_fen"] for line in row["lines"]) == 41479


def test_asset_members_and_line_parties_match_exact_saved_voucher(asset_book):
    engine, save, publish = asset_book
    save("reimbursed_asset_batch", "package", accepted_batch())
    publish("package")
    save("reimbursed_asset", "card-one", {**batch_card(), "acceptance_id": "package"})
    publish("card-one")
    diagnostic = {
        item["voucher_version_id"]: item for item in diagnostic_vouchers(engine, "2026-02")
    }
    response = Dashboard(engine).brief("2026-02")
    rows = response["data"]["collections"]["vouchers"]["items"]
    validate_response("dashboard_brief", response)
    for row in rows:
        saved = diagnostic[row["voucher_version_id"]]
        assert row["number"] == saved["number"]
        assert row["business_amount_fen"] == saved["business_amount_fen"]
        assert row["asset"] == (
            {key: value for key, value in saved["asset"].items() if key != "field_sources"}
            if saved["asset"] else None
        )
        for line, original in zip(row["lines"], saved["lines"], strict=True):
            assert line["parties"] == original["parties"]
            assert line["party_state"] == original["party_state"]
            assert line["source_label"] == original["source_label"]
