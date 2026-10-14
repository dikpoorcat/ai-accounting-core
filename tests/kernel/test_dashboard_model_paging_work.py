"""Native continuations preserve seeks after one complete anchored model proof."""

from contextlib import contextmanager

import pytest
from dashboard_model_fixture import derived_connection, produce
from test_banking import book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_model_transactions import raw_read
from test_dashboard_open_groups import expenses

from ai_accounting.kernel import dashboard_model_materializer, dashboard_read_model
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_reads import _collection
from ai_accounting.kernel.dashboard_model_runtime import authenticated_model, stable_payload
from ai_accounting.kernel.maintenance import Maintenance


@pytest.fixture(scope="module")
def paged_book(tmp_path_factory):
    records = book.__wrapped__(tmp_path_factory.mktemp("paging-work"))
    engine = records[0]
    original = dashboard_model_materializer.build_model

    def small_blocks(dashboard, period, **options):
        # Explicitly exercise complete collection blocks in this synthetic
        # producer; resident production ordinarily only prebuilds defaults.
        return original(dashboard, period, block_size=2, default_only=False)

    # Exercise the production receipt, loader and consumer with several blocks
    # without constructing hundreds of business facts in this regression.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dashboard_model_materializer, "build_model", small_blocks)
        _publish_filter_funding(engine, ["bank-a"] * 7)
        for index in range(7):
            expenses(records, 1, party=f"supplier-{index}", prefix=f"expense-{index}")
        Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="paging-model")
        produce(engine, "2026-09")
        yield engine


@contextmanager
def block_reads(monkeypatch):
    original = dashboard_read_model._decode_finite_block_value
    loaded = []

    def observed(value, reference, scope, **options):
        loaded.append(reference.key)
        return original(value, reference, scope, **options)

    with monkeypatch.context() as patch:
        patch.setattr(dashboard_read_model, "_decode_finite_block_value", observed)
        yield loaded


def complete_block_keys(engine):
    with derived_connection(engine) as connection:
        root = connection.execute(
            "SELECT b.payload FROM dashboard_read_model_current x "
            "JOIN dashboard_read_model_commit c ON c.id=x.commit_id "
            "JOIN dashboard_read_model_block b ON b.digest=c.root_digest WHERE x.period=?",
            ("2026-09",),
        ).fetchone()[0]
        return [block["key"] for block in dashboard_read_model.loads_finite_unique(root)["blocks"]]


@pytest.mark.parametrize("section", ("vouchers", "activity", "open_items"))
def test_later_page_uses_native_seek_after_one_complete_content_proof(
    paged_book, monkeypatch, section
):
    dashboard = Dashboard(paged_book)
    first = dashboard.brief("2026-09", section=section, limit=2)
    cursor = first["data"]["collections"][section]["page"]["next_cursor"]
    assert cursor is not None
    options = {
        "section": section,
        "cursor": cursor,
        "limit": 3,
        "expected_version": first["snapshot_version"],
    }
    with raw_read():
        native_first = dashboard.brief("2026-09", section=section, limit=2)
        expected = dashboard.brief(
            "2026-09",
            section=section,
            cursor=native_first["data"]["collections"][section]["page"]["next_cursor"],
            limit=3,
            expected_version=native_first["snapshot_version"],
        )
    with block_reads(monkeypatch) as loaded:
        actual = dashboard.brief("2026-09", **options)
    assert stable_payload(actual) == stable_payload(expected)
    assert loaded == complete_block_keys(paged_book)
    assert len(loaded) == len(set(loaded)), "one complete source-bound proof per read"


@pytest.mark.parametrize("limit", (1, 3, 20, 500))
def test_first_page_keeps_native_amounts_order_counts_and_each_hit_read(
    paged_book,
    monkeypatch,
    limit,
):
    dashboard = Dashboard(paged_book)
    with raw_read():
        expected = dashboard.brief("2026-09", section="vouchers", limit=limit)
    with block_reads(monkeypatch) as loaded:
        actual = dashboard.brief("2026-09", section="vouchers", limit=limit)
    assert stable_payload(actual) == stable_payload(expected)
    assert loaded == complete_block_keys(paged_book)
    total = expected["data"]["collections"]["vouchers"]["page"]["filtered_count"]
    assert len(actual["data"]["collections"]["vouchers"]["items"]) == min(limit, total)
    assert len(loaded) == len(set(loaded))


@pytest.mark.parametrize("field", ("total_count", "filtered_count", "collection_version"))
def test_overview_header_and_hit_directory_must_agree(paged_book, field):
    dashboard = Dashboard(paged_book)
    with dashboard._snapshot("2026-09") as snap:
        model = authenticated_model(dashboard, snap)
        page = dict(
            model.read_page_block("brief/default/vouchers/0").payload["data"]["collections"][
                "vouchers"
            ]["page"]
        )
        page[field] = page.get(field, 0) + 1
        with pytest.raises(KernelError) as error:
            _collection(
                model,
                snap,
                "brief",
                "vouchers",
                cursor=None,
                limit=1,
                filters={},
                variant="default",
                original_page=page,
            )
        assert error.value.code == "content_integrity_failed"
