"""Bucket owners bind strict rows once without building an unused flat manifest."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest
from ai_accounting_modular.reading import _brief_activity_index as activity
from ai_accounting_modular.reading import _collections as collections
from ai_accounting_modular.reading._collections import IndexItem, prepare_items, prepare_rows

BUSINESS = "00000000-0000-4000-8000-000000000001"


def test_bound_rows_keep_the_original_wire_bytes_order_and_input_isolation():
    values = [
        IndexItem(
            "z",
            "中文",
            "002",
            {"nested": [None, True, False, {"quote": '"\\\n\u2028', "large": -(2**100)}]},
            business_id=BUSINESS,
        ),
        IndexItem("a", "empty", "001", {}),
    ]
    expected, digests = [], []
    for item in values:
        index = {
            "collection": item.collection,
            "item_id": item.item_id,
            "sort_key": item.sort_key,
            "business_id": item.business_id,
            "object_id": item.object_id,
            "group_key": item.group_key,
            "category": item.category,
        }
        digest = content_digest({"index": index, "value": item.value})
        expected.append((*index.values(), canonical_json({"digest": digest, "value": item.value})))
        digests.append(digest)
    rows, actual_digests = prepare_rows(iter(values))
    assert rows == tuple(expected) and actual_digests == tuple(digests)
    prepared = prepare_items(values, collections=("empty",))
    assert prepared.rows == rows
    assert prepared.manifest == (
        ("a", 1, content_digest([["001", "empty", digests[1]]])),
        ("empty", 0, content_digest([])),
        ("z", 1, content_digest([["002", "中文", digests[0]]])),
    )
    values[0].value["nested"].clear()
    assert rows == tuple(expected)


def test_bucket_preparation_does_not_build_a_discarded_flat_manifest(monkeypatch):
    collection = "activity_members"
    items = [IndexItem(collection, "second", "002", {}), IndexItem(collection, "first", "001", {})]
    expected_rows, expected_digests = prepare_rows(items)

    def unused(*args, **kwargs):
        pytest.fail("bucket owners must not assemble a discarded flat manifest")

    monkeypatch.setattr(collections, "_prepared", unused)
    rows, buckets = activity._prepared(items)
    assert rows == expected_rows
    assert {
        identity: digest for values in buckets.values() for identity, (_, digest) in values.items()
    } == dict(zip((item.item_id for item in items), expected_digests, strict=True))


@pytest.mark.parametrize("damage", ["business", "float", "tuple", "duplicate"])
def test_bucket_binding_retains_strict_index_json_and_duplicate_checks(damage):
    item = IndexItem("activity_members", "member", "001", {"nested": [1]})
    if damage == "business":
        items = [replace(item, business_id="invalid")]
    elif damage == "float":
        items = [replace(item, value={"nested": [1.0]})]
    elif damage == "tuple":
        items = [replace(item, value={"nested": [(1,)]})]
    else:
        items = [item, replace(item, value={"nested": [2]})]
    with pytest.raises((ValueError, ContractError)):
        activity._prepared(items)
