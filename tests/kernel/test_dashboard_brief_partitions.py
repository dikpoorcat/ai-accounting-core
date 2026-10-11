"""Brief continuations keep their own money proof and avoid overview work."""

import pytest
from stage9_metrics import measure_work
from test_banking import book as book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.response_contracts import http_response, validate_response


@pytest.mark.parametrize("section", ("activity", "open_items", "vouchers"))
def test_partition_has_only_its_collection_and_preserves_overview_selection(book, section):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"] * 4)
    dashboard = Dashboard(engine)
    overview = dashboard.brief("2026-09", limit=2)
    result = dashboard.brief("2026-09", section=section, limit=2,
                             expected_version=overview["snapshot_version"])
    assert overview["scope"] == "overview" and overview["schema_version"] == 19
    assert result["scope"] == section and result["schema_version"] == 19
    assert result["snapshot_version"] == overview["snapshot_version"]
    assert result["read_context"] == overview["read_context"]
    assert set(result["data"]) == {"collections"}
    assert set(result["data"]["collections"]) == {section}
    validate_response("dashboard_brief", result)
    if section != "vouchers":
        assert result["data"]["collections"][section] == (
            overview["data"]["collections"][section]
        )
    else:
        for item in result["data"]["collections"][section]["items"]:
            assert item["amount_fen"] == sum(line["debit_fen"] for line in item["lines"])
            assert item["amount_fen"] == sum(line["credit_fen"] for line in item["lines"])
        wire = http_response("dashboard_brief", result)
        assert wire["data"]["collections"][section]["items"][0]["amount_fen"] == "1"


@pytest.mark.parametrize("section", ("activity", "open_items", "vouchers"))
def test_partition_rejects_exact_locator_and_old_snapshot(book, section):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"])
    dashboard = Dashboard(engine)
    for locator in ({"voucher_number": 1}, {"voucher_version_id": "selected"}):
        with pytest.raises(KernelError) as failure:
            dashboard.brief("2026-09", section=section, **locator)
        assert failure.value.code == "invalid_command"
    overview = dashboard.brief("2026-09")
    _publish_filter_funding(engine, ["bank-b"], period="2026-08")
    with pytest.raises(KernelError) as failure:
        dashboard.brief("2026-09", section=section,
                        expected_version=overview["snapshot_version"])
    assert failure.value.code == "dashboard_snapshot_changed"


def test_voucher_partition_independently_rejects_posted_line_damage(book):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"])
    dashboard = Dashboard(engine)
    result = dashboard.brief("2026-09", section="vouchers")
    version = result["data"]["collections"]["vouchers"]["items"][0]["voucher_version_id"]
    damage(engine, "voucher_line",
           "UPDATE voucher_line SET credit=credit+1 WHERE version_id=? AND credit>0", (version,))
    with pytest.raises(KernelError) as failure:
        dashboard.brief("2026-09", section="vouchers")
    assert failure.value.code == "content_integrity_failed"


def test_voucher_partition_keeps_source_work_bounded_by_returned_page(book, record_property):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"] * 6)
    dashboard = Dashboard(engine)
    before_work, before = measure_work(
        engine, lambda: dashboard.brief("2026-09", section="vouchers", limit=2),
    )
    _publish_filter_funding(engine, ["bank-b"] * 6, period="2026-08")
    after_work, after = measure_work(
        engine, lambda: dashboard.brief("2026-09", section="vouchers", limit=2),
    )
    first = before["data"]["collections"]["vouchers"]
    grown = after["data"]["collections"]["vouchers"]
    assert first["items"] == grown["items"]
    assert {key: value for key, value in first["page"].items() if key != "next_cursor"} == {
        key: value for key, value in grown["page"].items() if key != "next_cursor"
    }
    assert first["page"]["next_cursor"] != grown["page"]["next_cursor"]
    for name in ("calculation_result_rows_loaded", "calculation_result_bytes_loaded",
                 "calculation_result_json_decodes", "calculation_result_input_bytes"):
        assert after_work["counters"].get(name, 0) == before_work["counters"].get(name, 0)
    record_property("before_actual_work", before_work["counters"])
    record_property("after_actual_work", after_work["counters"])
