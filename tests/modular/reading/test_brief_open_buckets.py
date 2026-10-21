"""The dedicated two-level directory changes only actual affected collections and buckets."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery, ReadInputs, loads_json
from ai_accounting_modular.reading import _brief_open_buckets as buckets
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._collections import CollectionFilter, IndexItem, collection_page
from ai_accounting_modular.reading._repository import ReadingRepository
from tests.modular.application._working_case import case as case

from .test_brief_open_incremental import scope


def publish(case, entries):
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repo = unit.repository("reading")
        repo.register(key, index.COMPONENT, unit.head, _monthly.FORMAT_VERSION)
        state = repo.state(key, index.COMPONENT)
        model = index.complete({"_inputs": ReadInputs().wire()}, entries, unit.head)
        assert repo.publish(
            key, index.COMPONENT, state["wanted_head"], _monthly.FORMAT_VERSION, model
        )
    return model


def patch(case, entries, deletes):
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        repo = read.repository("reading")
        state = repo.state(key, index.COMPONENT)
        payload = loads_json(repo.payload(key, index.COMPONENT)["payload_json"])
        directory = index.Directory(repo, key, payload)
        summary = {k: v for k, v in payload.items() if k not in {"_semantic_digest", "_item_epoch"}}
        prepared = index.patch(summary, entries, deletes, directory, state)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert unit.repository("reading").publish(
            key, index.COMPONENT, state["wanted_head"], _monthly.FORMAT_VERSION, prepared
        )
    return prepared.model


def test_cross_bucket_add_delete_and_content_change_match_full_and_preserve_other_cursor(
    case, monkeypatch
):
    case.seed(expense=False)
    identities, seen = [], set()
    for number in range(1000):
        identity = f"member-{number}"
        selected = buckets.key("open_members", identity)
        if selected not in seen:
            identities.append(identity)
            seen.add(selected)
        if len(identities) == 4:
            break
    members = [
        IndexItem("open_members", ident, ident, {"amount": n})
        for n, ident in enumerate(identities[:3])
    ]
    groups = [IndexItem("open_items", str(n), str(n), {"label": str(n)}) for n in range(2)]
    original = publish(case, (*members, *groups))
    with case.kernel.storage.read_unit(case.company) as read:
        before = collection_page(
            _monthly.key(scope(case)),
            index.COMPONENT,
            "open_items",
            CollectionFilter(),
            PageQuery(1),
            read,
        )
    calls = []
    method = ReadingRepository.open_digest_buckets

    def observed(repo, key, selected):
        calls.extend(selected)
        return method(repo, key, selected)

    changed = IndexItem("open_members", identities[0], identities[0], {"amount": 99})
    added = IndexItem("open_members", identities[3], identities[3], {"amount": 10})
    with monkeypatch.context() as hook:
        hook.setattr(ReadingRepository, "open_digest_buckets", observed)
        result = patch(case, [changed, added], [("open_members", identities[1])])
    assert len(calls) == len(set(calls)) == 3
    assert set(calls) == {
        buckets.key("open_members", identity)
        for identity in (identities[0], identities[1], identities[3])
    }
    actual = loads_json(result.payload_json)
    expected = index.complete(
        {"_inputs": ReadInputs().wire()},
        [changed, members[2], added, *groups],
        actual["_item_epoch"],
    )
    assert result.payload_json == expected.payload_json
    assert (
        actual["_collections"]["open_items"]
        == loads_json(original.payload_json)["_collections"]["open_items"]
    )
    with case.kernel.storage.read_unit(case.company) as read:
        after = collection_page(
            _monthly.key(scope(case)),
            index.COMPONENT,
            "open_items",
            CollectionFilter(),
            PageQuery(1, before.next_cursor, before.version),
            read,
        )
    assert after.version == before.version and after.items == ({"label": "1"},)


def test_display_set_digest_ignores_reference_count_but_changes_when_identity_leaves(case):
    case.seed(expense=False)
    before = loads_json(
        publish(case, [index.item(index.VOUCHERS, "precise-display", {"count": 2})]).payload_json
    )
    after = loads_json(
        patch(case, [index.item(index.VOUCHERS, "precise-display", {"count": 1})], []).payload_json
    )
    assert before["_voucher_display_head"] == after["_voucher_display_head"]
    assert before["_collections"][index.VOUCHERS] != after["_collections"][index.VOUCHERS]
    empty = loads_json(patch(case, [], [(index.VOUCHERS, "precise-display")]).payload_json)
    assert empty["_voucher_display_head"] != after["_voucher_display_head"]
    assert empty["_buckets"][index.VOUCHERS] == {}


def test_same_bucket_collision_keeps_other_members_and_is_read_only_once(case, monkeypatch):
    case.seed(expense=False)
    candidates = {}
    for number in range(10000):
        identity = f"collision-{number}"
        selected = buckets.key("open_members", identity)
        values = candidates.setdefault(selected, [])
        values.append(identity)
        if len(values) == 3:
            first, second, added = values
            break
    original = [
        IndexItem("open_members", identity, identity, {"label": identity})
        for identity in (first, second)
    ]
    publish(case, original)
    new = IndexItem("open_members", added, added, {"label": "new"})
    seen = []
    method = ReadingRepository.open_digest_buckets

    def observed(repository, key, selected):
        seen.extend(selected)
        return method(repository, key, selected)

    with monkeypatch.context() as hook:
        hook.setattr(ReadingRepository, "open_digest_buckets", observed)
        actual = patch(case, [new], [("open_members", first)])
    assert seen == [buckets.key("open_members", first)]
    epoch = loads_json(actual.payload_json)["_item_epoch"]
    expected = index.complete({"_inputs": ReadInputs().wire()}, [original[1], new], epoch)
    assert actual.payload_json == expected.payload_json


def test_a_declared_empty_bucket_is_really_queried_and_rejects_extra_body_and_leaf(case):
    case.seed(expense=False)
    first = IndexItem("open_members", "original", "original", {"label": "original"})
    original = publish(case, [first])
    identity = next(
        f"extra-{n}"
        for n in range(1000)
        if buckets.key("open_members", f"extra-{n}") != buckets.key("open_members", first.item_id)
    )
    extra = IndexItem("open_members", identity, identity, {"label": "unexpected"})
    payload = loads_json(original.payload_json)
    forged = index.complete({"_inputs": ReadInputs().wire()}, [extra], payload["_item_epoch"])
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
            "object_id,group_key,category,payload_json,component_key,head) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(key, *row, index.COMPONENT, payload["_item_epoch"]) for row in forged.rows],
        )
    with case.kernel.storage.read_unit(case.company) as read:
        directory = index.Directory(read.repository("reading"), key, payload)
        with pytest.raises(ContractError) as error:
            directory.get("open_members", [identity])
    assert error.value.code == "storage_corrupt"
