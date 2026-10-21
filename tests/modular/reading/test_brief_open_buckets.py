"""The dedicated two-level directory changes only actual affected collections and buckets."""

from copy import deepcopy
from hashlib import sha256

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    ReadInputs,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.reading import _brief_open_buckets as buckets
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._collections import (
    CollectionFilter,
    IndexItem,
    collection_items,
    collection_page,
    prepare_items,
)
from ai_accounting_modular.reading._repository import ReadingRepository
from tests.modular.application._working_case import case as case

from .test_brief_open_incremental import scope


def summary():
    return {"_inputs": ReadInputs().wire(), "_cooperation_inputs": ReadInputs().wire()}


def publish(case, entries):
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repo = unit.repository("reading")
        repo.register(key, index.COMPONENT, unit.head, _monthly.version(index.COMPONENT))
        state = repo.state(key, index.COMPONENT)
        model = index.complete(summary(), entries, unit.head)
        assert repo.publish(
            key, index.COMPONENT, state["wanted_head"], _monthly.version(index.COMPONENT), model
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
            key, index.COMPONENT, state["wanted_head"], _monthly.version(index.COMPONENT), prepared
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
    method = ReadingRepository.open_bucket_rows

    def observed(repo, key, selected):
        calls.extend(selected)
        return method(repo, key, selected)

    changed = IndexItem("open_members", identities[0], identities[0], {"amount": 99})
    added = IndexItem("open_members", identities[3], identities[3], {"amount": 10})
    with monkeypatch.context() as hook:
        hook.setattr(ReadingRepository, "open_bucket_rows", observed)
        result = patch(case, [changed, added], [("open_members", identities[1])])
    assert len(calls) == len(set(calls)) == 3
    assert set(calls) == {
        buckets.key("open_members", identity)
        for identity in (identities[0], identities[1], identities[3])
    }
    actual = loads_json(result.payload_json)
    expected = index.complete(
        summary(),
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
    method = ReadingRepository.open_bucket_rows

    def observed(repository, key, selected):
        seen.extend(selected)
        return method(repository, key, selected)

    with monkeypatch.context() as hook:
        hook.setattr(ReadingRepository, "open_bucket_rows", observed)
        actual = patch(case, [new], [("open_members", first)])
    assert seen == [buckets.key("open_members", first)]
    epoch = loads_json(actual.payload_json)["_item_epoch"]
    expected = index.complete(summary(), [original[1], new], epoch)
    assert actual.payload_json == expected.payload_json


def test_an_undeclared_bucket_rejects_extra_physical_content(case):
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
    forged = index.complete(summary(), [extra], payload["_item_epoch"])
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        session = unit.repository("reading")._session
        session.execute(
            "DELETE FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
            (key, index.BUCKETS, buckets.key("open_members", first.item_id)),
        )
        session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
            "object_id,group_key,category,payload_json,component_key,head) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(key, *row, index.COMPONENT, payload["_item_epoch"]) for row in forged.rows],
        )
    with case.kernel.storage.read_unit(case.company) as read:
        with pytest.raises(ContractError) as error:
            directory = index.Directory(read.repository("reading"), key, payload)
            directory.get("open_members", [identity])
    assert error.value.code == "storage_corrupt"


def test_private_buckets_have_one_body_no_item_mirrors_and_get_returns_isolated_values(case):
    case.seed(expense=False)
    values = [index.item(index.FOLDS, "group", {"count": 500000, "nested": {"paid": "0"}})]
    values += [
        index.item(index.BUSINESSES, f"business-{n}", {"members": [str(n)]}) for n in range(30)
    ]
    model = publish(case, values)
    payload, key = loads_json(model.payload_json), _monthly.key(scope(case))
    assert all(row[0] == index.BUCKETS for row in model.rows)
    assert len(model.rows) == sum(len(root) for root in payload["_buckets"].values())
    assert all(all(field is None for field in row[3:7]) for row in model.rows)
    with case.kernel.storage.read_unit(case.company) as read:
        directory = index.Directory(read.repository("reading"), key, payload)
        first = directory.get(index.FOLDS, ["group"], required=True)["group"]
        first["nested"]["paid"] = "999"
        first["count"] = 0
        original = directory.get(index.FOLDS, ["group"], required=True)["group"]
        assert original == {"count": 500000, "nested": {"paid": "0"}}
        with pytest.raises(ContractError, match="未声明"):
            directory.get("undeclared", ())
    changed = index.item(index.BUSINESSES, "business-new", {"members": ["new"]})
    actual = patch(case, [changed], [(index.BUSINESSES, "business-0")])
    expected = index.complete(
        summary(),
        [value for value in values if value.item_id != "business-0"] + [changed],
        payload["_item_epoch"],
    )
    assert actual.payload_json == expected.payload_json
    assert actual.content_digest == expected.content_digest
    empty = patch(
        case,
        [],
        [(value.collection, value.item_id) for value in values[1:]]
        + [(index.BUSINESSES, "business-new"), (index.FOLDS, "group")],
    )
    assert empty.payload_json == index.complete(summary(), (), payload["_item_epoch"]).payload_json


def test_full_factory_rejects_duplicate_private_identity_and_does_not_make_binding_rows(
    monkeypatch,
):
    value = index.item(index.FOLDS, "fold", {"count": 1})
    with pytest.raises(ContractError):
        index.complete({}, [value, value], 1)
    prepare = index.prepare_items

    def public_only(items):
        assert all(item.collection in index.PUBLIC for item in items)
        return prepare(items)

    monkeypatch.setattr(index, "prepare_items", public_only)
    model = index.complete({}, [value], 1)
    assert len(model.rows) == 1 and model.rows[0][0] == index.BUCKETS


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize(
    "damage", ["format", "kind", "collection", "bucket", "duplicate", "order", "identity", "count"]
)
def test_bucket_structure_cannot_be_forged_by_rehashing_raw_bytes(private, damage):
    collection = index.FOLDS if private else "open_members"
    identities = {}
    for n in range(10000):
        identity = f"same-{n}"
        selected = buckets.key(collection, identity)[-2:]
        identities.setdefault(selected, []).append(identity)
        if len(identities[selected]) == 2:
            first, second = sorted(identities[selected])
            break
    values = (
        {first: {"n": 1}, second: {"n": 2}}
        if private
        else {first: (first, "1" * 64), second: (second, "2" * 64)}
    )
    expected, raw = buckets.prepare(collection, selected, values, private=private)
    body, field = loads_json(raw), "values" if private else "triples"
    if damage in {"format", "kind", "collection", "bucket"}:
        body[damage] = "wrong"
    elif damage == "duplicate":
        body[field].append(deepcopy(body[field][0]))
    elif damage == "order":
        body[field].reverse()
    elif damage == "identity":
        identity = next(
            f"other-{n}"
            for n in range(1000)
            if buckets.key(collection, f"other-{n}")[-2:] != selected
        )
        body[field][0][0 if private else 1] = identity
    else:
        expected["count"] += 1
    raw = canonical_json(body)
    expected["digest" if private else "body_digest"] = sha256(raw.encode()).hexdigest()
    with pytest.raises(ContractError) as error:
        buckets.read(raw, collection, selected, expected, private=private)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "field",
    [
        "sort_key",
        "head",
        "business_id",
        "object_id",
        "group_key",
        "category",
        "scope_key",
        "component_key",
    ],
)
def test_physical_bucket_metadata_is_exact_and_never_used_as_a_semantic_bucket(field, case):
    case.seed(expense=False)
    model = publish(case, [index.item(index.FOLDS, "group", {"count": 1})])
    payload, key = loads_json(model.payload_json), _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            f"UPDATE read_item SET {field}=? WHERE scope_key=? AND collection_key=?",
            (0 if field == "head" else "unexpected", key, index.BUCKETS),
        )
    with case.kernel.storage.read_unit(case.company) as read, pytest.raises(ContractError) as error:
        index.Directory(read.repository("reading"), key, payload).get(index.FOLDS, ["group"])
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("collection", index.PUBLIC)
@pytest.mark.parametrize("access", ["page", "batch", "lookahead"])
def test_public_reads_anchor_even_a_self_consistent_replacement_to_the_header(
    case, collection, access
):
    case.seed(expense=False)
    values = [
        IndexItem(collection, identity, identity, {"amount": "100"}) for identity in ("a", "b")
    ]
    model = publish(case, values)
    selected = values[1] if access == "lookahead" else values[0]
    replacement = prepare_items(
        [IndexItem(collection, selected.item_id, selected.sort_key, {"amount": "999"})]
    ).rows[0]
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        original_head = read.head
        original_state = read.repository("reading").state(key, index.COMPONENT)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json=? WHERE scope_key=? AND component_key=? "
            "AND collection_key=? AND item_id=?",
            (replacement[-1], key, index.COMPONENT, collection, selected.item_id),
        )
    with case.kernel.storage.read_unit(case.company) as read, pytest.raises(ContractError) as error:
        assert read.head == original_head
        repo = read.repository("reading")
        assert repo.state(key, index.COMPONENT) == original_state
        assert repo.payload(key, index.COMPONENT)["payload_json"] == model.payload_json
        if access == "batch":
            collection_items(key, index.COMPONENT, collection, (selected.item_id,), read)
        else:
            collection_page(
                key, index.COMPONENT, collection, CollectionFilter(), PageQuery(1), read
            )
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("collection", index.PUBLIC)
@pytest.mark.parametrize("access", ["page", "batch"])
def test_public_batch_preloads_each_actual_bucket_once_including_lookahead(
    case, collection, access, monkeypatch
):
    case.seed(expense=False)
    values = [IndexItem(collection, f"id-{n:03}", f"id-{n:03}", {"n": n}) for n in range(25)]
    publish(case, values)
    original, calls = ReadingRepository.open_bucket_rows, []

    def observed(repo, scope_key, keys):
        calls.append(keys)
        return original(repo, scope_key, keys)

    monkeypatch.setattr(ReadingRepository, "open_bucket_rows", observed)
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        if access == "page":
            result = collection_page(
                key, index.COMPONENT, collection, CollectionFilter(), PageQuery(20), read
            )
            assert len(result.items) == 20 and result.next_cursor is not None
            expected = values[:21]
        else:
            result = collection_items(
                key, index.COMPONENT, collection, tuple(value.item_id for value in values), read
            )
            assert len(result) == 25
            expected = values
    assert len(calls) == 1
    assert set(calls[0]) == {buckets.key(collection, value.item_id) for value in expected}
