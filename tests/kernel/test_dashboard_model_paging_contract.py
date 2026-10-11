"""Authenticated paging context, historical bytes and actual cursor consumption."""

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_dashboard_read_model_storage import anchor, produce
from test_dashboard_read_model_storage import database as database
from test_dashboard_read_model_storage import native as native

from ai_accounting.kernel import dashboard_model_reads
from ai_accounting.kernel import dashboard_read_model as model
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_pages import seal_page
from ai_accounting.kernel.types import canonical


def corrupted(operation):
    with pytest.raises(KernelError) as error:
        operation()
    assert error.value.code == "content_integrity_failed"


def reseal(prepared, change):
    """Deliberately malformed producer batch with real hash/audit anchoring."""
    root = json.loads(prepared.root_json)
    change(root)
    raw = canonical(root)
    descriptor = prepared.descriptor
    old_digest = bytes.fromhex(descriptor["root_digest"])
    descriptor["root_digest"] = hashlib.sha256(raw.encode()).hexdigest()
    descriptor["commit_id"] = model._descriptor_id(descriptor)
    rows = tuple((key, value) for key, value in prepared.block_rows if key != old_digest)
    rows += ((bytes.fromhex(descriptor["root_digest"]), raw),)
    return model.PreparedModel(canonical(descriptor), raw, rows)


def two_pages(native, *, version="future-collection-版本"):
    scope, blocks = native
    original = blocks[1]
    item = original.payload["data"]["collections"]["accounts"]["items"][0]
    result = [copy.deepcopy(blocks[0])]
    for ordinal in range(2):
        value = copy.deepcopy(item)
        if ordinal:
            value["account_id"] += "-second"
        payload = copy.deepcopy(original.payload)
        payload["data"]["collections"]["accounts"] = {
            "items": [value],
            "page": {"total_count": 2, "filtered_count": 2, "returned_count": 1,
                     "has_more": ordinal == 0,
                     "next_cursor": value["account_id"] if ordinal == 0 else None,
                     "collection_version": version},
        }
        result.append(replace(original, key=f"funds/default/accounts/{ordinal}",
                              payload=payload, keys=((value["account_id"],),),
                              total_count=2, filtered_count=2))
    return scope, result


@pytest.mark.parametrize("damage", ["missing", "integer", "empty", "different", "overview"])
def test_v3_invalid_directory_is_rejected_before_persist(database, native, damage):
    scope, blocks = two_pages(native)
    prepared = model.prepare_model(scope, blocks, request_id="paging-invalid-directory")

    def change(root):
        ref = root["blocks"][1]
        if damage == "missing":
            del ref["collection_version"]
        elif damage == "integer":
            ref["collection_version"] = 7
        elif damage == "empty":
            ref["collection_version"] = ""
        elif damage == "different":
            root["blocks"][2]["collection_version"] = "other"
        else:
            root["blocks"][0]["collection_version"] = "not-a-collection"

    malformed = reseal(prepared, change)
    database.execute("BEGIN")
    audit = anchor(database, [malformed])
    corrupted(lambda: model.persist_model(database, malformed, audit_id=audit))
    assert database.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0] == 0
    assert database.execute("SELECT count(*) FROM dashboard_read_model_block").fetchone()[0] == 0


def test_v3_reference_and_actual_body_version_must_agree(database, native):
    scope, blocks = native
    prepared = model.prepare_model(scope, blocks, request_id="paging-body-conflict")
    malformed = reseal(prepared, lambda root: root["blocks"][1].update(
        collection_version="body-does-not-have-this-version"))
    database.execute("BEGIN")
    audit = anchor(database, [malformed])
    corrupted(lambda: model.persist_model(database, malformed, audit_id=audit))
    assert database.execute("SELECT count(*) FROM dashboard_read_model_block").fetchone()[0] == 0


@pytest.mark.parametrize("version", [1, 2])
def test_archived_contract_preserves_original_body_and_excludes_new_field(
    database, native, monkeypatch, version,
):
    scope, blocks = native
    with monkeypatch.context() as old:
        old.setattr(model, "CONTENT_VERSION", version)
        prepared = produce(database, scope, blocks)
    root = json.loads(prepared.root_json)
    assert all("collection_version" not in ref for ref in root["blocks"])
    assert ("date_basis" in root) == (version >= 2)
    saved = tuple(database.execute(
        "SELECT hex(digest),payload FROM dashboard_read_model_block ORDER BY digest"))
    database.execute("BEGIN")
    loaded = model.load_current_model(database, scope)
    assert model.supports_native_content(loaded.descriptor) is False
    assert model.load_model(database, scope) is None
    assert loaded.read_page_block(blocks[1].key, native_contract=False) == blocks[1]
    live = model.authenticate_current_storage(database)
    assert prepared.descriptor["root_digest"] in live
    assert tuple(database.execute(
        "SELECT hex(digest),payload FROM dashboard_read_model_block ORDER BY digest")) == saved
    assert model._load_bytes(database, prepared.descriptor["root_digest"]) == root


@pytest.mark.parametrize("version", [1, 2])
def test_archived_root_cannot_acquire_even_null_collection_version(
    database, native, monkeypatch, version,
):
    scope, blocks = native
    with monkeypatch.context() as old:
        old.setattr(model, "CONTENT_VERSION", version)
        prepared = model.prepare_model(scope, blocks, request_id="old-paging-context")
    malformed = reseal(prepared, lambda root: root["blocks"][1].update(collection_version=None))
    database.execute("BEGIN")
    audit = anchor(database, [malformed])
    corrupted(lambda: model.persist_model(database, malformed, audit_id=audit))


def test_same_current_build_cannot_disguise_content_two_as_archive(database, native, monkeypatch):
    from ai_accounting.kernel.engine import PROGRAM_VERSION

    scope, blocks = native
    scope = scope.model_copy(update={"rule_build": PROGRAM_VERSION})
    with monkeypatch.context() as old:
        old.setattr(model, "CONTENT_VERSION", 2)
        produce(database, scope, blocks)
    database.execute("BEGIN")
    corrupted(lambda: model.load_current_model(database, scope))
    corrupted(lambda: model.authenticate_current_storage(database))


def test_future_version_cursor_reads_exact_pages_and_rejects_wrong_scope(
    database, native, monkeypatch,
):
    scope, blocks = two_pages(native)
    produce(database, scope, blocks)
    database.execute("BEGIN")
    loaded = model.load_model(database, scope)
    root = model._load_bytes(database, loaded.descriptor.root_digest)
    assert root["content_version"] == 3
    assert root["blocks"][0]["collection_version"] is None
    assert root["blocks"][1]["collection_version"] == "future-collection-版本"
    snap = SimpleNamespace(store=SimpleNamespace(company_id=scope.company_id,
                                                database_id=scope.database_id),
                           period=scope.period, snapshot_version="a" * 64, as_of=scope.as_of)
    calls = []
    monkeypatch.setattr(dashboard_model_reads, "_verify_hits",
                        lambda _snap, sources, facts, vouchers, **kwargs:
                        calls.append((set(sources), set(facts), set(vouchers))))

    def read(cursor):
        return dashboard_model_reads._collection(
            loaded, snap, "funds", "accounts", cursor=cursor, limit=1,
            filters={}, variant="default", original_page=None,
        )

    first = read(None)
    second = read(first["page"]["next_cursor"])
    assert first["items"] == blocks[1].payload["data"]["collections"]["accounts"]["items"]
    assert second["items"] == blocks[2].payload["data"]["collections"]["accounts"]["items"]
    assert first["items"][0]["closing_fen"] == second["items"][0]["closing_fen"]
    assert first["page"]["collection_version"] == second["page"]["collection_version"]
    assert second["page"] == {"total_count": 2, "filtered_count": 2, "returned_count": 1,
                              "has_more": False, "next_cursor": None,
                              "collection_version": "future-collection-版本"}
    assert calls == [(set(blocks[1].sources), set(blocks[1].fact_sources),
                      set(blocks[1].voucher_version_ids))] * 2
    tail_cursor = seal_page(snap, "funds", "accounts", {
        "next_cursor": blocks[2].keys[0][0], "collection_version": "future-collection-版本"}, {})
    assert read(tail_cursor["next_cursor"])["items"] == []
    assert calls[-1] == (set(), set(), set())
    wrong = seal_page(snap, "funds", "accounts", {
        "next_cursor": blocks[1].keys[0][0], "collection_version": "other-version"}, {})
    with pytest.raises(KernelError) as error:
        read(wrong["next_cursor"])
    assert error.value.code == "dashboard_snapshot_changed"
