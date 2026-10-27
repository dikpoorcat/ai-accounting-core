"""Same-read component proofs never replace actual bytes, context or owner checks."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    UnitOfWork,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.reading import (
    _brief_activity_index as activity,
)
from ai_accounting_modular.reading import (
    _brief_groups,
    _brief_open,
    _descriptor,
    _item_buckets,
    _monthly,
    _open_store,
)
from ai_accounting_modular.reading import (
    _brief_open_index as open_items,
)
from ai_accounting_modular.reading._collections import (
    CollectionFilter,
    collection_page,
    prepare_model,
)
from ai_accounting_modular.reading._repository import ReadingRepository

from .test_brief_activity import capture as publish_activity
from .test_brief_activity_incremental import book as book
from .test_brief_open import publish as publish_open
from .test_funds import selection


def prepared(book):
    book.seed()
    publish_activity(book)
    publish_open(book)


def snapshot(book, read):
    return _monthly.ready_snapshot(
        book.kernel.reading, selection(book), (activity.COMPONENT, open_items.COMPONENT), read
    )


def count_headers(monkeypatch):
    counts = {activity.COMPONENT: 0, open_items.COMPONENT: 0}
    for owner in (activity, open_items):
        original = owner.validate

        def checked(*args, _owner=owner, _original=original, **kwargs):
            counts[_owner.COMPONENT] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, "validate", checked)
    return counts


def test_month_snapshot_lists_and_group_expansion_check_each_header_once_in_one_read(
    book, monkeypatch
):
    prepared(book)
    counts = count_headers(monkeypatch)
    with book.kernel.storage.read_unit(book.company) as read:
        assert not isinstance(read, UnitOfWork)
        saved = snapshot(book, read)
        assert counts == {activity.COMPONENT: 0, open_items.COMPONENT: 0}
        groups = _brief_groups.activity_page(saved, read, {})["items"]
        assert counts == {activity.COMPONENT: 1, open_items.COMPONENT: 0}
        _brief_open.page(saved, read, {})
        detail = _brief_groups.build(
            book.kernel.reading, saved, read, {"group_key": groups[0]["group_key"]}
        )
        assert detail["data"]["collections"]["members"]["page"]["total_count"] == 1
        assert counts == {activity.COMPONENT: 1, open_items.COMPONENT: 1}


def test_snapshot_mutations_cannot_change_the_private_directory_or_its_manifest(book):
    prepared(book)
    with book.kernel.storage.read_unit(book.company) as read:
        saved = snapshot(book, read)
        original = deepcopy(dict(saved.components))
        assert all(not name.startswith("_") for value in original.values() for name in value)
        saved.components[activity.COMPONENT]["activity_count"] = 999
        saved.components[open_items.COMPONENT]["open_items"]["categories"].clear()
        assert snapshot(book, read).components == original
        assert _brief_groups.activity_page(saved, read, {})["page"]["total_count"] == 1


def test_another_read_rechecks_summary_and_directory_even_if_old_views_are_copied(
    book, monkeypatch
):
    prepared(book)
    counts = count_headers(monkeypatch)
    physical_checks = []
    count_buckets = ReadingRepository.activity_bucket_count

    def counted(repository, scope_key):
        physical_checks.append(repository)
        return count_buckets(repository, scope_key)

    monkeypatch.setattr(ReadingRepository, "activity_bucket_count", counted)
    with book.kernel.storage.read_unit(book.company) as read:
        saved = snapshot(book, read)
        _brief_groups.activity_page(saved, read, {})
        previous_views = dict(read.repository("reading")._component_views)
    with pytest.raises(ContractError) as ended:
        _brief_groups.activity_page(saved, read, {})
    assert ended.value.code == "invalid_operation"
    with book.kernel.storage.read_unit(book.company) as other:
        other.repository("reading")._component_views.update(previous_views)
        collection_page(
            saved.scope_key, activity.COMPONENT, "activity", CollectionFilter(), PageQuery(), other
        )
        _brief_groups.activity_page(saved, other, {})
    assert counts == {activity.COMPONENT: 2, open_items.COMPONENT: 0}
    assert len(physical_checks) == 2
    assert physical_checks[0] is not physical_checks[1]


@pytest.mark.parametrize(
    "change", ["raw_whitespace", "raw_content", "state", "digest", "format", "payload_head"]
)
def test_cached_header_requires_the_actual_raw_and_full_ready_state_again(
    book, monkeypatch, change
):
    prepared(book)
    counts = count_headers(monkeypatch)
    with book.kernel.storage.read_unit(book.company) as read:
        saved = snapshot(book, read)
        _brief_groups.activity_page(saved, read, {})
        repository = read.repository("reading")
        if change in {"state", "digest", "format"}:
            fetch = repository.state

            def changed(scope, component):
                value = fetch(scope, component)
                if component == activity.COMPONENT:
                    if change == "state":
                        value["wanted_head"] += 1
                    elif change == "format":
                        value["format_version"] -= 1
                    else:
                        value["content_digest"] = "0" * 64
                return value

            monkeypatch.setattr(repository, "state", changed)
        else:
            fetch = repository.payload

            def changed(scope, component):
                value = fetch(scope, component)
                if component == activity.COMPONENT:
                    if change == "payload_head":
                        value["head"] += 1
                    else:
                        value["payload_json"] = (
                            "\n" + value["payload_json"] if change == "raw_whitespace" else "{}"
                        )
                return value

            monkeypatch.setattr(repository, "payload", changed)
        if change == "raw_whitespace":
            assert _brief_groups.activity_page(saved, read, {})["page"]["total_count"] == 1
            assert counts[activity.COMPONENT] == 2
        else:
            with pytest.raises(_monthly._CorruptComponent):
                _brief_groups.activity_page(saved, read, {})


@pytest.mark.parametrize(
    "field,value",
    [("payload_json", "{}"), ("head", -1), ("scope_key", "other"), ("category", "wrong")],
)
def test_a_cached_directory_still_checks_each_actual_selected_row(book, monkeypatch, field, value):
    prepared(book)
    with book.kernel.storage.read_unit(book.company) as read:
        saved = snapshot(book, read)
        _brief_groups.activity_page(saved, read, {})
        repository = read.repository("reading")
        fetch = repository.item_page

        def changed(*args):
            rows = [dict(row) for row in fetch(*args)]
            if args[1:3] == (activity.COMPONENT, "activity"):
                rows[0][field] = value
            return rows

        monkeypatch.setattr(repository, "item_page", changed)
        with pytest.raises(_monthly._CorruptComponent):
            _brief_groups.activity_page(saved, read, {})


@pytest.mark.parametrize(
    "component,collection",
    [
        (activity.COMPONENT, "activity"),
        (open_items.COMPONENT, "open_items"),
    ],
)
def test_real_writable_contexts_never_reuse_a_proof_after_a_write(book, component, collection):
    prepared(book)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert isinstance(unit, UnitOfWork)
        first = collection_page(
            scope_key, component, collection, CollectionFilter(), PageQuery(), unit
        )
        identity = first.items[0]["group_key"]
        _damage_index(unit.repository("reading"), scope_key, component, collection, identity)
        with pytest.raises(ContractError) as error:
            collection_page(scope_key, component, collection, CollectionFilter(), PageQuery(), unit)
        assert error.value.code == "storage_corrupt"


def test_a_callers_dictionary_is_never_a_proof_for_the_real_header(book, monkeypatch):
    prepared(book)
    with book.kernel.storage.read_unit(book.company) as read:
        saved = snapshot(book, read)
        repository = read.repository("reading")
        repository._component_views[saved.scope_key, activity.COMPONENT] = {"checked": True}
        fetch = repository.payload

        def broken(scope, component):
            value = fetch(scope, component)
            if component == activity.COMPONENT:
                value["payload_json"] = "{}"
            return value

        monkeypatch.setattr(repository, "payload", broken)
        with pytest.raises(_monthly._CorruptComponent):
            _brief_groups.activity_page(saved, read, {})


def test_normal_view_constructor_checks_descriptor_then_checks_actual_owner_body_on_use(
    book, monkeypatch
):
    from ai_accounting_modular.reading._component_view import _ComponentView

    prepared(book)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        state = repository.state(scope_key, activity.COMPONENT)
        payload = repository.payload(scope_key, activity.COMPONENT)
        _ComponentView(scope_key, activity.COMPONENT, read, state, payload)
        wrong_hash = dict(payload, payload_json="{}")
        with pytest.raises(ContractError) as error:
            _ComponentView(scope_key, activity.COMPONENT, read, state, wrong_hash)
        assert error.value.code == "storage_corrupt"
        descriptor = _descriptor.read(scope_key, activity.COMPONENT, state, payload)
        body = repository.component_body(scope_key, activity.COMPONENT, descriptor)
        value = loads_json(body["payload_json"])
        value["activity_count"] += 1
        value["_semantic_digest"] = activity.semantic(value)
        publication = _descriptor.prepare(
            scope_key,
            activity.COMPONENT,
            state["ready_head"],
            state["format_version"],
            prepare_model(value),
        )
        payload["payload_json"] = publication.descriptor_json
        state["content_digest"] = publication.descriptor_digest
        monkeypatch.setattr(
            repository, "component_body", lambda *_: dict(body, payload_json=canonical_json(value))
        )
        view = _ComponentView(scope_key, activity.COMPONENT, read, state, payload)
        with pytest.raises(ContractError) as error:
            view.monthly_summary()
        assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("owner", [activity, open_items])
def test_base_reads_never_share_decoded_summary_values(book, monkeypatch, owner):
    prepared(book)
    counts = count_headers(monkeypatch)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        directory = owner.base(repository, repository.state(scope_key, owner.COMPONENT))
        expected = deepcopy(directory.payload)
        directory.payload["_roots" if owner is open_items else "_buckets"].clear()
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        directory = owner.base(repository, repository.state(scope_key, owner.COMPONENT))
        assert directory.payload == expected
        assert counts[owner.COMPONENT] == 2


@pytest.mark.parametrize(
    "owner,collection",
    [
        (activity, "activity"),
        (open_items, "open_items"),
    ],
)
def test_new_transaction_rechecks_the_actual_index(book, owner, collection):
    prepared(book)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.read_unit(book.company) as read:
        collection_page(
            scope_key, owner.COMPONENT, collection, CollectionFilter(), PageQuery(), read
        )
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        _damage_index(unit.repository("reading"), scope_key, owner.COMPONENT, collection)
    with book.kernel.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as error:
            collection_page(
                scope_key, owner.COMPONENT, collection, CollectionFilter(), PageQuery(), read
            )
    assert error.value.code == "storage_corrupt"


def _damage_index(repository, scope_key, component, collection, identity=None):
    if component == open_items.COMPONENT:
        descriptor = _descriptor.read(
            scope_key,
            component,
            repository.state(scope_key, component),
            repository.payload(scope_key, component),
        )
        payload = loads_json(
            repository.component_body(scope_key, component, descriptor)["payload_json"]
        )
        node_id = payload["_roots"][collection][0]
        repository._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND collection_key=? "
            "AND item_id=?",
            (_open_store.SCOPE, _open_store.NODES, node_id),
        )
    else:
        sql = (
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND component_key=? "
            "AND collection_key=?"
        )
        params = (scope_key, component, activity.BUCKETS)
        if identity is not None:
            sql += " AND item_id=?"
            params += (_item_buckets.key(collection, identity),)
        repository._session.execute(sql, params)
