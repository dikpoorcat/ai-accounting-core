"""Page context must not decode unrelated business bodies before seeking hits."""

from contextlib import contextmanager

import pytest
from test_banking import book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_model_transactions import raw_read
from test_dashboard_open_groups import expenses

from ai_accounting.kernel import dashboard_model_materializer, dashboard_read_model
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_reads import _collection
from ai_accounting.kernel.dashboard_model_runtime import authenticated_model
from ai_accounting.kernel.maintenance import Maintenance


@pytest.fixture(scope="module")
def paged_book(tmp_path_factory):
    records = book.__wrapped__(tmp_path_factory.mktemp("paging-work"))
    engine = records[0]
    original = dashboard_model_materializer.build_model

    def small_blocks(dashboard, period):
        return original(dashboard, period, block_size=2)

    # Exercise the production receipt, loader and consumer with several blocks
    # without constructing hundreds of business facts in this regression.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dashboard_model_materializer, "build_model", small_blocks)
        _publish_filter_funding(engine, ["bank-a"] * 7)
        for index in range(7):
            expenses(records, 1, party=f"supplier-{index}", prefix=f"expense-{index}")
        Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="paging-model")
        yield engine


@contextmanager
def block_reads(monkeypatch):
    original = dashboard_read_model.LoadedModel.read_page_block
    loaded = []

    def observed(model, key, **options):
        loaded.append(key)
        return original(model, key, **options)

    with monkeypatch.context() as patch:
        patch.setattr(dashboard_read_model.LoadedModel, "read_page_block", observed)
        yield loaded


@pytest.mark.parametrize("section", ("vouchers", "activity", "open_items"))
def test_later_page_decodes_only_hit_blocks(paged_book, monkeypatch, section):
    dashboard = Dashboard(paged_book)
    first = dashboard.brief("2026-09", section=section, limit=2)
    cursor = first["data"]["collections"][section]["page"]["next_cursor"]
    assert cursor is not None
    options = {"section": section, "cursor": cursor, "limit": 3,
               "expected_version": first["snapshot_version"]}
    with raw_read():
        expected = dashboard.brief("2026-09", **options)
    with block_reads(monkeypatch) as loaded:
        actual = dashboard.brief("2026-09", **options)
    assert actual == expected
    assert loaded == [f"brief/default/{section}/1",
                      f"brief/default/{section}/2"]


@pytest.mark.parametrize("limit", (1, 3, 20, 500))
def test_first_page_keeps_native_amounts_order_counts_and_each_hit_read(
    paged_book, monkeypatch, limit,
):
    dashboard = Dashboard(paged_book)
    with raw_read():
        expected = dashboard.brief("2026-09", section="vouchers", limit=limit)
    with block_reads(monkeypatch) as loaded:
        actual = dashboard.brief("2026-09", section="vouchers", limit=limit)
    assert actual == expected
    assert loaded[0] == "brief/default/vouchers/0"
    total = expected["data"]["collections"]["vouchers"]["page"]["filtered_count"]
    assert len(loaded) == (min(limit, total) + 1) // 2
    assert len(loaded) == len(set(loaded))


@pytest.mark.parametrize("field", ("total_count", "filtered_count", "collection_version"))
def test_overview_header_and_hit_directory_must_agree(paged_book, field):
    dashboard = Dashboard(paged_book)
    with dashboard._snapshot("2026-09") as snap:
        model = authenticated_model(dashboard, snap)
        page = dict(model.read_page_block("brief/default/vouchers/0").payload[
            "data"]["collections"]["vouchers"]["page"])
        page[field] = page.get(field, 0) + 1
        with pytest.raises(KernelError) as error:
            _collection(model, snap, "brief", "vouchers", cursor=None, limit=1,
                        filters={}, variant="default", original_page=page)
        assert error.value.code == "content_integrity_failed"
