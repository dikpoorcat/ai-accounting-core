"""Unreturned brief overviews have no decoding duty on a collection read."""

from contextlib import contextmanager

import pytest
from test_banking import book as book
from test_banking import funding
from test_dashboard_model_integrity import current_content
from test_dashboard_model_paging_work import paged_book as paged_book
from test_dashboard_model_transactions import raw_read
from test_integrity_content import damage, verify

from ai_accounting.kernel import dashboard_read_model as storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import authenticated_model
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.types import canonical


@contextmanager
def decoded_work(monkeypatch):
    original = storage.loads_finite_unique
    work = []

    def decode(raw):
        value = original(raw)
        if isinstance(value, dict) and "kind" in value and "payload" in value:
            work.append({"bytes": len(raw.encode("utf-8")),
                         "kind": value["kind"],
                         "items": sum(len(collection["items"]) for collection in
                                      value["payload"]["data"].get("collections", {}).values())})
        return value

    with monkeypatch.context() as patch:
        patch.setattr(storage, "loads_finite_unique", decode)
        yield work


@pytest.mark.parametrize("section", ("vouchers", "activity", "open_items"))
@pytest.mark.parametrize("limit", (1, 3))
def test_all_collection_pages_equal_native_without_repeated_overview_work(
    paged_book, monkeypatch, record_property, section, limit,
):
    dashboard = Dashboard(paged_book)
    with decoded_work(monkeypatch) as overview:
        with dashboard._snapshot("2026-09") as snap:
            model = authenticated_model(dashboard, snap)
            model.read_page_block("brief/default/overview/0")
    assert len(overview) == 1 and overview[0]["items"] > 0
    cursor, version = None, None
    pages, saved_bytes, saved_items = 0, 0, 0
    while True:
        with raw_read():
            expected = dashboard.brief("2026-09", section=section, cursor=cursor, limit=limit,
                                       expected_version=version)
        with decoded_work(monkeypatch) as actual_work:
            actual = dashboard.brief("2026-09", section=section, cursor=cursor, limit=limit,
                                     expected_version=version)
        assert actual == expected
        validate_response("dashboard_brief", actual)
        collection = actual["data"]["collections"][section]
        assert actual_work and all(entry["items"] <= 2 for entry in actual_work)
        pages += 1
        saved_bytes += overview[0]["bytes"]
        saved_items += overview[0]["items"]
        cursor = collection["page"]["next_cursor"]
        version = actual["snapshot_version"]
        if cursor is None:
            break
    assert pages > 1
    assert saved_bytes == pages * overview[0]["bytes"]
    assert saved_items == pages * overview[0]["items"]
    record_property("removed_actual_decoding", canonical({
        "pages": pages, "bytes": saved_bytes, "items": saved_items,
        "overview_per_page": overview[0],
    }))


@pytest.mark.parametrize("target", ("overview", "root", "hit", "locator"))
def test_continuation_preserves_its_own_corruption_boundary_and_complete_checks(book, target):
    engine, save, publish, _ = book
    funding(save, publish, amount=17)
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="continuation-model")
    dashboard = Dashboard(engine)
    expected = dashboard.brief("2026-09", section="vouchers", limit=1)
    _, root, blocks = current_content(engine)
    if target == "locator":
        damage(engine, "dashboard_read_model_current",
               "DELETE FROM dashboard_read_model_current WHERE period=?", ("2026-09",))
    else:
        ident = root if target == "root" else blocks[
            "brief/default/overview/0" if target == "overview" else "brief/default/vouchers/0"]
        damage(engine, "dashboard_read_model_block",
               "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?", (ident,))
    if target == "overview":
        actual = dashboard.brief("2026-09", section="vouchers", limit=1)
        assert actual == expected
        validate_response("dashboard_brief", actual)
        with pytest.raises(KernelError) as failure:
            dashboard.brief("2026-09")
        assert failure.value.code == "content_integrity_failed"
    else:
        with pytest.raises(KernelError) as failure:
            dashboard.brief("2026-09", section="vouchers", limit=1)
        assert failure.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.code == "content_integrity_failed"


def test_producer_fallback_preserves_native_collection(book):
    engine, save, publish, _ = book
    funding(save, publish, amount=17)
    dashboard = Dashboard(engine)
    # Native model production must keep its ordinary independent raw read.
    expected = dashboard.brief("2026-09", section="vouchers", limit=1)
    with raw_read():
        actual = dashboard.brief("2026-09", section="vouchers", limit=1)
    assert actual == expected
    validate_response("dashboard_brief", actual)


def test_empty_collection_uses_authenticated_counts_without_a_business_block(book, monkeypatch):
    engine, save, publish, _ = book
    funding(save, publish, amount=17)
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.brief("2026-09", section="open_items")
    with decoded_work(monkeypatch) as work:
        actual = dashboard.brief("2026-09", section="open_items")
    assert actual == expected
    validate_response("dashboard_brief", actual)
    page = actual["data"]["collections"]["open_items"]["page"]
    assert page["total_count"] == page["filtered_count"] == page["returned_count"] == 0
    assert page["has_more"] is False and page["next_cursor"] is None
    assert work == []
