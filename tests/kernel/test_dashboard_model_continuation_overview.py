"""Complete anchored content is authenticated once inside an owned collection read."""

from contextlib import contextmanager

import pytest
from dashboard_model_fixture import derived_connection, produce
from test_banking import book as book
from test_banking import funding
from test_dashboard_model_paging_work import paged_book as paged_book
from test_dashboard_model_transactions import raw_read
from test_integrity_content import damage, verify

from ai_accounting.kernel import dashboard_read_model as storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import delivery_state
from ai_accounting.kernel.dashboard_model_runtime import authenticated_model, stable_payload
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.types import canonical


@contextmanager
def decoded_work(monkeypatch):
    original = storage.loads_finite_unique
    work = []

    def decode(raw):
        value = original(raw)
        if isinstance(value, dict) and "kind" in value and "payload" in value:
            work.append(
                {
                    "bytes": len(raw.encode("utf-8")),
                    "kind": value["kind"],
                    "items": sum(
                        len(collection["items"])
                        for collection in value["payload"]
                        .get("data", {})
                        .get("collections", {})
                        .values()
                    ),
                }
            )
        return value

    with monkeypatch.context() as patch:
        patch.setattr(storage, "loads_finite_unique", decode)
        yield work


@contextmanager
def same_snapshot(dashboard, monkeypatch, snap):
    @contextmanager
    def selected(period):
        assert period == snap.period
        yield snap

    with monkeypatch.context() as patch:
        patch.setattr(dashboard, "_snapshot", selected)
        yield


@pytest.mark.parametrize("section", ("vouchers", "activity", "open_items"))
@pytest.mark.parametrize("limit", (1, 3))
def test_all_collection_pages_equal_native_without_repeated_owned_model_decoding(
    paged_book,
    monkeypatch,
    record_property,
    section,
    limit,
):
    dashboard = Dashboard(paged_book)
    with raw_read():
        expected = dashboard.brief("2026-09", section=section, limit=500)
    expected_collection = expected["data"]["collections"][section]
    cursor, version, pages, items = None, None, 0, []
    with dashboard._snapshot("2026-09") as snap:
        with decoded_work(monkeypatch) as proof:
            model = authenticated_model(dashboard, snap)
        assert len(proof) == len(model.root.blocks)
        assert any(entry["items"] > 2 for entry in proof)
        with same_snapshot(dashboard, monkeypatch, snap):
            while True:
                with decoded_work(monkeypatch) as actual_work:
                    actual = dashboard.brief(
                        "2026-09",
                        section=section,
                        cursor=cursor,
                        limit=limit,
                        expected_version=version,
                    )
                validate_response("dashboard_brief", actual)
                collection = actual["data"]["collections"][section]
                assert actual_work == [], "complete proof is reusable only in this owned snapshot"
                items.extend(collection["items"])
                pages += 1
                cursor = collection["page"]["next_cursor"]
                version = actual["snapshot_version"]
                assert (
                    collection["page"]["total_count"] == expected_collection["page"]["total_count"]
                )
                if cursor is None:
                    break
    assert items == expected_collection["items"]
    assert pages > 1
    record_property(
        "authenticated_model_decoding",
        canonical(
            {
                "pages": pages,
                "bytes": sum(entry["bytes"] for entry in proof),
                "items": sum(entry["items"] for entry in proof),
                "proof_batches": 1,
            }
        ),
    )


@pytest.mark.parametrize("target", ("overview", "root", "hit", "locator"))
def test_continuation_authenticates_returned_and_unreturned_content(book, target):
    engine, save, publish, _ = book
    funding(save, publish, amount=17)
    produce(engine, "2026-09")
    dashboard = Dashboard(engine)
    with derived_connection(engine) as connection:
        row = connection.execute(
            "SELECT c.root_digest,b.payload FROM dashboard_read_model_current x "
            "JOIN dashboard_read_model_commit c ON c.id=x.commit_id "
            "JOIN dashboard_read_model_block b ON b.digest=c.root_digest WHERE x.period=?",
            ("2026-09",),
        ).fetchone()
        root = bytes(row[0])
        blocks = {
            block["key"]: bytes.fromhex(block["digest"])
            for block in storage.loads_finite_unique(row[1])["blocks"]
        }
    if target == "locator":
        damage(
            engine,
            "dashboard_projection_current",
            "DELETE FROM dashboard_projection_current WHERE period=?",
            ("2026-09",),
        )
    else:
        ident = (
            root
            if target == "root"
            else blocks[
                "funds/default/overview/0" if target == "overview" else "brief/default/overview/0"
            ]
        )
        with derived_connection(engine, read_only=False) as connection:
            connection.execute(
                "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
                (ident,),
            )
    with pytest.raises(KernelError) as failure:
        dashboard.brief("2026-09", section="vouchers", limit=1)
    assert failure.value.code == "content_integrity_failed"
    assert delivery_state(engine, "2026-09")["reason"] == "corrupt"
    assert verify(engine)["status"] == "verified", (
        "display corruption cannot change business proofs"
    )


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
    produce(engine, "2026-09")
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.brief("2026-09", section="open_items")
    with dashboard._snapshot("2026-09") as snap:
        assert authenticated_model(dashboard, snap) is not None
        with same_snapshot(dashboard, monkeypatch, snap), decoded_work(monkeypatch) as work:
            actual = dashboard.brief("2026-09", section="open_items")
    assert stable_payload(actual) == stable_payload(expected)
    validate_response("dashboard_brief", actual)
    page = actual["data"]["collections"]["open_items"]["page"]
    assert page["total_count"] == page["filtered_count"] == page["returned_count"] == 0
    assert page["has_more"] is False and page["next_cursor"] is None
    assert work == []
