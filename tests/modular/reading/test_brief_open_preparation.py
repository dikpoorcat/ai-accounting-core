"""Prepared Open roots own their inputs and still authenticate actual published values."""

from copy import deepcopy
from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _monthly, _open_store
from ai_accounting_modular.reading._collections import IndexItem, _read_item, prepare_items
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from tests.modular.application._working_case import case as case

from .test_brief_open_incremental import scope
from .test_brief_open_tree import publish


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


@pytest.mark.parametrize("damage", ["business", "float", "tuple", "duplicate"])
def test_root_factory_keeps_strict_index_json_and_duplicate_checks(case, damage):
    item = IndexItem("open_members", "member", "001", {"nested": [1]})
    if damage == "business":
        selected = [replace(item, business_id="invalid")]
    elif damage == "float":
        selected = [replace(item, value={"nested": [1.0]})]
    elif damage == "tuple":
        selected = [replace(item, value={"nested": [(1,)]})]
    else:
        selected = [item, replace(item, value={"nested": [2]})]
    summary = {}
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        index.complete(summary, (item,), read.head, repository=repository, scope_key=key)
        with pytest.raises((ValueError, ContractError)):
            index.complete(summary, selected, read.head, repository=repository, scope_key=key)


@pytest.mark.parametrize("populated", [False, True])
def test_prepared_full_roots_detach_caller_values_before_real_publication(case, populated):
    case.seed(expense=False)
    values = entries() if populated else []
    expected = deepcopy(values[0].value) if populated else None
    summary = {
        "open_items": {"cutoff_period": "2026-01", "current_cutoff_period": "2026-01"},
        "title": {"text": "中文汇总"},
    }
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository.register(key, index.COMPONENT, unit.head, _monthly.version(index.COMPONENT))
        state = repository.state(key, index.COMPONENT)
        guard = CaptureGuard.capture(state, unit.head)
        prepared = index.complete(summary, values, unit.head, repository=repository, scope_key=key)
        before = deepcopy(prepared)
        summary["title"]["text"] = "caller changed after preparation"
        if populated:
            values[0].value["中文"][3]["text"] = "caller changed after preparation"
        assert prepared == before
        assert repository.publish(
            key,
            index.COMPONENT,
            state["wanted_head"],
            _monthly.version(index.COMPONENT),
            prepared,
            guard=guard,
            current_head=unit.head,
        )
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        directory = index.base(repository, repository.state(key, index.COMPONENT))
        assert directory.payload["title"] == {"text": "中文汇总"}
        actual = directory.get("open_members", ("member-2",), required=populated)
        if not populated:
            assert actual == {} and directory.payload["_collections"]["open_members"]["count"] == 0
        else:
            assert actual == {"member-2": expected}
            actual["member-2"]["中文"][3]["text"] = "changed returned value"
            assert directory.get("open_members", ("member-2",), required=True) == {
                "member-2": expected
            }
    if populated:
        with case.kernel.storage.read_unit(case.company) as read:
            repository = read.repository("reading")
            directory = index.base(repository, repository.state(key, index.COMPONENT))
            assert directory.get("open_members", ("member-2",), required=True) == {
                "member-2": expected
            }


def test_same_content_patch_and_cold_full_keep_roots_manifest_and_base_proof(case):
    case.seed(expense=False)
    original = publish(case, entries())
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        state = repository.state(key, index.COMPONENT)
        directory = index.base(repository, state)
        summary = {
            name: value
            for name, value in directory.payload.items()
            if name not in {"_semantic_digest", "_item_epoch"}
        }
        cold = index.complete(
            summary, entries(), directory.epoch, repository=repository, scope_key=key
        )
        patched = index.patch(summary, entries(), (), directory, state, scope_key=key)
        assert patched.payload_json == cold.payload_json == original.payload_json
        assert patched.content_digest == cold.content_digest == original.content_digest
        assert loads_json(patched.payload_json)["_roots"] == directory.payload["_roots"]
        assert loads_json(patched.payload_json)["_collections"] == directory.payload["_collections"]
        assert cold.tree_plan.writes == cold.tree_plan.deletes == ()
        assert patched.tree_plan.writes == patched.tree_plan.deletes == ()
        assert (
            patched.base_ready,
            patched.base_digest,
            patched.item_epoch,
            patched.base_scope,
        ) == (state["ready_head"], state["content_digest"], directory.epoch, key)


@pytest.mark.parametrize("damage", ["changed_raw", "missing_node"])
def test_prepared_roots_do_not_replace_actual_node_byte_or_existence_checks(case, damage):
    case.seed(expense=False)
    prepared = publish(case, entries())
    key = _monthly.key(scope(case))
    identity = loads_json(prepared.payload_json)["_roots"]["rows"][0]
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        session = unit.repository("reading")._session
        where = "WHERE scope_key=? AND collection_key=? AND item_id=?"
        parameters = (_open_store.SCOPE, _open_store.NODES, identity)
        if damage == "changed_raw":
            # Every declared field stays identical, but these are different actual bytes.
            changed = session.execute(
                "UPDATE read_item SET payload_json=payload_json || ' ' " + where, parameters
            )
        else:
            changed = session.execute("DELETE FROM read_item " + where, parameters)
        assert changed == 1
    with (
        case.kernel.storage.read_unit(case.company) as read,
        pytest.raises(ContractError) as failure,
    ):
        repository = read.repository("reading")
        directory = index.base(repository, repository.state(key, index.COMPONENT))
        directory.get("open_members", ("member-2",), required=True)
    assert failure.value.code == "storage_corrupt"


@pytest.mark.parametrize("damage", ["value", "index"])
def test_a_self_consistent_logical_row_must_still_match_its_actual_published_root(case, damage):
    case.seed(expense=False)
    publish(case, entries())
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        directory = index.base(repository, repository.state(key, index.COMPONENT))
        actual = directory.item_get("open_members", ("member-2",))[0]
        changed = IndexItem(
            "open_members",
            "member-2",
            "changed-order" if damage == "index" else "002",
            {"changed_after_preparation": True} if damage == "value" else entries()[0].value,
            group_key="group",
        )
        raw = prepare_items((changed,)).rows[0][-1]
        forged = dict(actual, sort_key=changed.sort_key, payload_json=raw)
        decoded, _, _ = _read_item(forged)
        assert decoded.value == changed.value  # Its own complete index/body digest is valid.
        with pytest.raises(ContractError) as failure:
            directory.check_many((forged,))
    assert failure.value.code == "storage_corrupt"
