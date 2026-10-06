"""Restored owner vouchers retain precise lines and bounded snapshot pagination."""

import pytest
import test_investments as investments
from entity_fixture import seed_entities
from stage9_metrics import measure_work
from test_banking import book as _bank_book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_projection import diagnostic_vouchers
from test_dashboard_provenance import profile
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
    assert response["schema_version"] == 13
    assert data["voucher_count"] == data["activity_count"] == 23
    vouchers = data["collections"]["vouchers"]["items"]
    assert len(vouchers) == 20
    assert [row["voucher_version_id"] for row in vouchers] == [
        row["voucher_version_id"] for row in data["collections"]["activity"]["items"]
    ]
    page = data["collections"]["vouchers"]["page"]
    activity_page = data["collections"]["activity"]["page"]
    assert page["next_cursor"] != activity_page["next_cursor"]
    activity_response = dashboard.brief(
        "2026-09", section="activity", cursor=activity_page["next_cursor"],
        expected_version=response["snapshot_version"], limit=7,
    )
    following = activity_response["data"]["collections"]
    assert set(following) == {"activity", "vouchers"}
    assert [row["voucher_version_id"] for row in following["activity"]["items"]] == [
        row["voucher_version_id"] for row in following["vouchers"]["items"]
    ]
    assert following["activity"]["page"]["returned_count"] == 3
    assert following["vouchers"]["page"]["returned_count"] == 3
    validate_response("dashboard_brief", activity_response)
    next_response = dashboard.brief(
        "2026-09", section="vouchers", cursor=page["next_cursor"],
        expected_version=response["snapshot_version"], limit=7,
    )
    assert set(next_response["data"]["collections"]) == {"vouchers"}
    final = next_response["data"]["collections"]["vouchers"]
    assert final["items"] == following["vouchers"]["items"]
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
    voucher_cursor = response["data"]["collections"]["vouchers"]["page"]["next_cursor"]
    with pytest.raises(KernelError) as rejected:
        dashboard.brief(
            "2026-09", section="vouchers", cursor=activity_cursor,
            expected_version=response["snapshot_version"],
        )
    assert rejected.value.code == "dashboard_snapshot_changed"
    with pytest.raises(KernelError) as rejected:
        dashboard.brief(
            "2026-09", section="activity", cursor=voucher_cursor,
            expected_version=response["snapshot_version"],
        )
    assert rejected.value.code == "dashboard_snapshot_changed"
    following = dashboard.brief(
        "2026-09", section="activity", cursor=activity_cursor,
        expected_version=response["snapshot_version"], limit=1,
    )["data"]["collections"]
    assert following["activity"]["page"]["next_cursor"] != (
        following["vouchers"]["page"]["next_cursor"]
    )
    assert following["activity"]["page"]["has_more"]
    assert following["vouchers"]["page"]["has_more"]
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


def test_activity_owner_page_work_ignores_unrelated_objects_and_display_history(
    bank_book, record_property,
):
    engine, save, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 23)
    dashboard = Dashboard(engine)

    def following(response):
        return dashboard.brief(
            "2026-09", section="activity", limit=2,
            expected_version=response["snapshot_version"],
            cursor=response["data"]["collections"]["activity"]["page"]["next_cursor"],
        )

    default_work, default = measure_work(engine, lambda: dashboard.brief("2026-09", limit=2))
    page_work, before = measure_work(engine, lambda: following(default))
    seed_entities(engine, [(f"unrelated-person-{i}", "person", None) for i in range(12)])
    for index in range(12):
        subject = f"unrelated-expense-{index}"
        save("expense", subject, {
            "period": "2026-08", "counterparty_id": f"unrelated-person-{index}",
            "amount_fen": index + 1, "expense_class": "administration",
            "creditor_kind": "supplier",
        })
        for revision in range(3):
            profile(engine, "business", subject, revision, display_name=f"无关历史 {revision}")
    refreshed = dashboard.brief("2026-09", limit=2)
    grown_work, after = measure_work(engine, lambda: following(refreshed))
    for section in ("activity", "vouchers"):
        assert after["data"]["collections"][section]["items"] == (
            before["data"]["collections"][section]["items"]
        )
        assert len(after["data"]["collections"][section]["items"]) == 2
    assert after["data"]["voucher_count"] == default["data"]["voucher_count"] == 23
    for counter in (
        "calculation_result_rows_loaded", "calculation_result_bytes_loaded",
        "calculation_result_json_decodes", "calculation_result_input_bytes",
    ):
        assert grown_work["counters"].get(counter, 0) == page_work["counters"].get(counter, 0)
    for name, work in (("default", default_work), ("activity", page_work), ("grown", grown_work)):
        record_property(name + "_actual_work", work["counters"])
    # The metrics include necessary month-wide money proofs; only saved result
    # loading above is constrained, not total SQL rows or SQLite instructions.


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
    first = Dashboard(engine).brief("2026-02", limit=1)
    cursor = first["data"]["collections"]["activity"]["page"]["next_cursor"]
    if cursor:
        following = Dashboard(engine).brief(
            "2026-02", section="activity", cursor=cursor,
            expected_version=first["snapshot_version"],
        )
        assert following["data"]["collections"]["vouchers"]["items"] == rows[1:]


def test_activity_vouchers_keep_frozen_saved_lines_and_original_reversal(bank_book):
    engine, save, publish, _ = bank_book
    fields = {
        "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 100,
        "expense_class": "administration", "creditor_kind": "supplier",
    }
    save("expense", "expense", fields)
    publish("expense")
    dashboard = Dashboard(engine)
    original = dashboard.brief("2026-09", section="activity")["data"]["collections"][
        "vouchers"
    ]["items"][0]
    investments.close(engine, "2026-09")
    save("expense", "expense", fields | {"amount_fen": 140}, revision=1)
    preview = engine.preview(["expense"], posting_period="2026-10")
    engine.confirm(
        ["expense"], posting_period="2026-10", preview_digest=preview["digest"],
        epochs=preview["epochs"], request_id="expense-correction",
    )
    frozen = dashboard.brief("2026-09", section="activity")["data"]["collections"][
        "vouchers"
    ]["items"][0]
    assert frozen == original
    correction = dashboard.brief("2026-10", limit=1)
    first = correction["data"]["collections"]["vouchers"]["items"][0]
    following = dashboard.brief(
        "2026-10", section="activity", limit=1,
        cursor=correction["data"]["collections"]["activity"]["page"]["next_cursor"],
        expected_version=correction["snapshot_version"],
    )
    second = following["data"]["collections"]["vouchers"]["items"][0]
    reversal = next(row for row in (first, second) if row["reverses_version_id"] is not None)
    replacement = next(row for row in (first, second) if row["reverses_version_id"] is None)
    assert reversal["reverses_version_id"] == original["voucher_version_id"]
    assert reversal["business_amount_fen"] == -100
    assert replacement["business_amount_fen"] == 140
    for line, saved in zip(reversal["lines"], original["lines"], strict=True):
        assert line["debit_fen"] == saved["credit_fen"]
        assert line["credit_fen"] == saved["debit_fen"]
