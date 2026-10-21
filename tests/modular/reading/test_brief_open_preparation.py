"""Carry each already checked digest through full and incremental publication unchanged."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.reading import _brief_open_buckets as buckets
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._collections import IndexItem
from tests.modular.application._working_case import case as case

from .test_brief_open_buckets import publish
from .test_brief_open_incremental import scope


def entries():
    return [
        IndexItem(
            "open_members",
            "member-2",
            "002",
            {"中文": [None, True, False, {"text": '引号"\\换行\n\u2028', "int": -(2**100)}]},
            group_key="group",
        ),
        IndexItem("open_items", "group", "001", {"amount_fen": "9007199254740993"}),
        index.item(index.VOUCHERS, "display", {"count": 2}),
    ]


def legacy_digest_extraction(monkeypatch):
    """The previous path got each digest by decoding its freshly serialized body."""
    prepare = index.prepare_items

    def from_serialized_rows(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        return replace(
            prepared, row_digests=tuple(loads_json(row[-1])["digest"] for row in prepared.rows)
        )

    monkeypatch.setattr(index, "prepare_items", from_serialized_rows)


def no_new_body_decodes(monkeypatch):
    prepare, decode = index.prepare_items, index.loads_json
    new_bodies = set()

    def capture(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        new_bodies.update(row[-1] for row in prepared.rows)
        return prepared

    def checked(raw):
        assert raw not in new_bodies, "new rows must reuse their factory binding digests"
        return decode(raw)

    monkeypatch.setattr(index, "prepare_items", capture)
    monkeypatch.setattr(index, "loads_json", checked)


@pytest.mark.parametrize("values", [(), entries()])
def test_full_packet_matches_previous_digest_extraction_bytes_without_decoding_new_rows(
    values, monkeypatch
):
    summary = {"title": "中文汇总", "amount_fen": "9007199254740993"}
    with monkeypatch.context() as hook:
        legacy_digest_extraction(hook)
        expected = index.complete(summary, values, 17)
    with monkeypatch.context() as hook:
        no_new_body_decodes(hook)
        actual = index.complete(summary, values, 17)
    assert actual == expected  # All payload bytes, hash, ordinary rows and physical buckets.


def test_patch_packet_matches_previous_digest_extraction_bytes_and_keeps_base_proof(
    case, monkeypatch
):
    case.seed(expense=False)
    publish(case, entries())
    with case.kernel.storage.read_unit(case.company) as read:
        key = _monthly.key(scope(case))
        repository = read.repository("reading")
        state = repository.state(key, index.COMPONENT)
        payload = loads_json(repository.payload(key, index.COMPONENT)["payload_json"])
        directory = index.Directory(repository, key, payload)
        summary = {k: v for k, v in payload.items() if k not in {"_semantic_digest", "_item_epoch"}}
        changed = [
            IndexItem("open_members", "member-2", "002", {"amount_fen": "-1"}),
            index.item(index.VOUCHERS, "new-display", {"count": 1}),
        ]
        deletes = [(index.VOUCHERS, "display")]
        with monkeypatch.context() as hook:
            legacy_digest_extraction(hook)
            expected = index.patch(summary, changed, deletes, directory, state)
        with monkeypatch.context() as hook:
            no_new_body_decodes(hook)
            actual = index.patch(summary, changed, deletes, directory, state)
    assert actual == expected  # Includes the exact deletes, base version and item epoch.


@pytest.mark.parametrize("damage", ["value", "index", "bucket"])
def test_prepared_metadata_never_replaces_persisted_body_index_or_bucket_verification(case, damage):
    case.seed(expense=False)
    model = publish(case, entries())
    key = _monthly.key(scope(case))
    payload = loads_json(model.payload_json)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        session = unit.repository("reading")._session
        if damage == "value":
            raw = next(row[-1] for row in model.rows if row[:2] == ("open_members", "member-2"))
            body = loads_json(raw)
            body["value"] = {"changed_after_preparation": True}
            session.execute(
                "UPDATE read_item SET payload_json=? WHERE scope_key=? AND collection_key=? "
                "AND item_id=?",
                (canonical_json(body), key, "open_members", "member-2"),
            )
        elif damage == "index":
            session.execute(
                "UPDATE read_item SET sort_key='changed-after-preparation' WHERE scope_key=? "
                "AND collection_key=? AND item_id=?",
                (key, "open_members", "member-2"),
            )
        else:
            session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
                (key, index.BUCKETS, buckets.key("open_members", "member-2")),
            )
    with case.kernel.storage.read_unit(case.company) as read:
        with pytest.raises(ContractError) as failure:
            directory = index.Directory(read.repository("reading"), key, payload)
            directory.get("open_members", ("member-2",), required=True)
    assert failure.value.code == "storage_corrupt"
