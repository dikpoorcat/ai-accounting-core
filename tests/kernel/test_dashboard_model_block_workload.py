"""Real typed storage/consumer work on synthetic cards, not accounting qualification."""

import copy
import inspect
import json
from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_banking import funding, opening
from test_dashboard_read_model_storage import database as database
from test_dashboard_read_model_storage import produce

from ai_accounting.kernel import dashboard_model_materializer as producer
from ai_accounting.kernel import dashboard_model_reads as consumer
from ai_accounting.kernel import dashboard_read_model as storage
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.types import canonical


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    records = book.__wrapped__(tmp_path_factory.mktemp("block-work-native"))
    engine, save, publish, _ = records
    opening(save, publish)
    funding(save, publish, amount=17)
    response = Dashboard(engine).brief("2026-09", section="vouchers", limit=1)
    context = response["read_context"]
    with engine.store.connection(read_only=True) as connection:
        epochs = engine.store.epochs(connection)
    scope = storage.ModelScope(
        company_id=context["company_id"], database_id=context["database_id"],
        period="2026-09", epochs=storage.ModelEpochs(**epochs), read_repair_revision=0,
        rule_build="synthetic-block-work", as_of=context["as_of"],
    )
    return records, scope, response


def test_actual_default_producer_requests_one_hundred_item_blocks(sample, monkeypatch):
    records, _, _ = sample
    requested = []
    page = Journal.page

    def observed(self, after_number, limit, **options):
        requested.append(limit)
        return page(self, after_number, limit, **options)

    monkeypatch.setattr(Journal, "page", observed)
    result = producer.build_model(Dashboard(records[0]), "2026-09")
    assert requested and 100 in requested
    assert max(requested) == 100
    assert all(len(block.keys) <= 100 for block in result.blocks)
    vouchers = [item for block in result.blocks if block.key.startswith("brief/default/vouchers/")
                for item in block.payload["data"]["collections"]["vouchers"]["items"]]
    assert len(vouchers) == 1 and vouchers[0]["business_amount_fen"] == 17


def card_blocks(response, size):
    template = response["data"]["collections"]["vouchers"]["items"][0]
    result = []
    for start in range(0, 1005, size):
        items = []
        for index in range(start, min(start + size, 1005)):
            item = copy.deepcopy(template)
            item["number"] = str(index + 1)
            item["voucher_version_id"] = f"synthetic-voucher-{index:04}"
            items.append(item)
        payload = copy.deepcopy(response)
        payload["data"]["collections"]["vouchers"] = {
            "items": items,
            "page": {"total_count": 1005, "filtered_count": 1005,
                     "returned_count": len(items), "has_more": start + len(items) < 1005,
                     "next_cursor": str(items[-1]["number"])
                     if start + len(items) < 1005 else None},
        }
        versions = tuple(item["voucher_version_id"] for item in items)
        result.append(storage.PageBlock(
            f"brief/default/vouchers/{len(result)}", "dashboard_brief", payload,
            keys=tuple((int(item["number"]), item["voucher_version_id"]) for item in items),
            total_count=1005, filtered_count=1005, voucher_version_ids=versions,
            item_sources=tuple(() for _ in items),
            item_fact_sources=tuple(() for _ in items),
            item_voucher_version_ids=tuple((ident,) for ident in versions),
        ))
    return result


def test_default_block_work_reduces_padding_and_bytes_with_exact_twenty_five_hundred_tail(
    sample, database, monkeypatch, record_property,
):
    _, scope, response = sample
    size = inspect.signature(producer.build_model).parameters["block_size"].default
    work, results = {}, {}
    reads = storage.LoadedModel.read_page_block
    hit_versions = []

    def hits(_snap, _sources, _facts, vouchers, *, voucher_items):
        assert vouchers == {item["voucher_version_id"] for item in voucher_items}
        hit_versions.append(tuple(sorted(vouchers)))

    monkeypatch.setattr(consumer, "_verify_hits", hits)
    parent = None
    for selected_size in (500, size):
        blocks = card_blocks(response, selected_size)
        prepared = produce(
            database, scope, blocks, key=f"block-size-{selected_size}", parent=parent,
        )
        parent = prepared.descriptor["commit_id"]
        database.execute("BEGIN")
        loaded = storage.load_model(database, scope)
        snap = SimpleNamespace(
            store=SimpleNamespace(company_id=scope.company_id, database_id=scope.database_id),
            period=scope.period, snapshot_version=response["snapshot_version"], as_of=scope.as_of,
        )
        observed, decoded, bytes_read = [], [], []

        def read(model, key, *, decoded=decoded, bytes_read=bytes_read, **options):
            ref = model._blocks_by_key[key]
            decoded.append(ref.item_count)
            raw = database.execute("SELECT payload FROM dashboard_read_model_block WHERE digest=?",
                                   (bytes.fromhex(ref.digest),)).fetchone()[0]
            bytes_read.append(len(raw.encode("utf-8")))
            return reads(model, key, **options)

        cursor = None
        with monkeypatch.context() as patch:
            patch.setattr(storage.LoadedModel, "read_page_block", read)
            for limit in (20, 500, 500):
                offset = len(decoded)
                page = consumer._collection(
                    loaded, snap, "brief", "vouchers", cursor=cursor, limit=limit,
                    filters={}, variant="default", original_page=None,
                )
                observed.append(page)
                cursor = page["page"]["next_cursor"]
                assert len(page["items"]) in (20, 500, 485)
                results.setdefault(selected_size, []).extend(page["items"])
                work.setdefault(selected_size, {"decoded_items": [], "read_bytes": []})[
                    "decoded_items"].append(sum(decoded[offset:]))
                work[selected_size]["read_bytes"].append(sum(bytes_read[offset:]))
        assert cursor is None
        assert [page["page"]["returned_count"] for page in observed] == [20, 500, 485]
        work[selected_size]["root_bytes"] = len(prepared.root_json.encode("utf-8"))
        work[selected_size]["block_count"] = len(blocks)
        database.rollback()
    assert results[500] == results[size]
    assert [int(item["number"]) for item in results[size]] == list(range(1, 1006))
    assert hit_versions[:3] == hit_versions[3:]
    assert work[500]["decoded_items"] == [500, 1000, 505]
    assert work[size]["decoded_items"] == [100, 600, 505]
    assert work[size]["block_count"] == 11 > work[500]["block_count"] == 3
    assert work[size]["root_bytes"] > work[500]["root_bytes"]
    for selected in ((0,), (1, 2), (0, 1, 2)):
        def total(size, selected=selected):
            return sum(work[size]["read_bytes"][index] + work[size]["root_bytes"]
                       for index in selected)
        assert total(size) < total(500), "include the larger root for every real request"
    record_property("actual_storage_work", canonical(work))
    assert json.loads(canonical(work))[str(size)]["decoded_items"] == [100, 600, 505]
