"""Publication versions stay small while owner bodies and exact counts load on demand."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    UnitOfWork,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.reading import _component_view, _descriptor, _monthly
from ai_accounting_modular.reading._collections import CollectionFilter, prepare_model


def packet(value=None):
    model = prepare_model(value or {"data": {"amount_fen": "17"}, "first_account": None})
    publication = _descriptor.prepare("scope", "funds", 7, 11, model)
    state = {
        "scope_key": "scope",
        "component_key": "funds",
        "format_version": 11,
        "state": "ready",
        "ready_head": 7,
        "wanted_head": 7,
        "content_digest": publication.descriptor_digest,
    }
    descriptor = {
        "scope_key": "scope",
        "component_key": "funds",
        "head": 7,
        "payload_json": publication.descriptor_json,
    }
    body = {
        **descriptor,
        "collection_key": _descriptor.BODY_COLLECTION,
        "item_id": "funds",
        "sort_key": "funds",
        "business_id": None,
        "object_id": None,
        "group_key": None,
        "category": None,
        "payload_json": publication.body_json,
    }
    return state, descriptor, body


class Repository:
    def __init__(self, saved):
        self.saved = saved
        self._component_views = {}
        self.body_reads, self.count_queries = 0, []

    def state(self, *_):
        return dict(self.saved[0])

    def payload(self, *_):
        return dict(self.saved[1])

    def component_body(self, scope, component, descriptor):
        self.body_reads += 1
        return _descriptor.body(scope, component, descriptor, self.saved[2])

    def item_counts(self, scope, component, collection, query, *, total=None):
        self.count_queries.append((collection, query, total))
        return 20 if total is None else total, 3 if query.categories else 20


def read_context(saved):
    repository = Repository(saved)
    read = SimpleNamespace(
        company_id="company",
        database_id="database",
        repository=lambda owner: repository,
    )
    return read, repository


def test_descriptor_size_and_fields_do_not_follow_owner_body_growth():
    small = packet({"data": {"members": ["a"]}})
    large = packet({"data": {"members": ["a"] * 10000}})
    first, second = (loads_json(value[1]["payload_json"]) for value in (small, large))
    assert set(first) == set(second)
    assert "data" not in first and "members" not in first
    assert len(small[1]["payload_json"]) == len(large[1]["payload_json"])
    assert first["body_digest"] != second["body_digest"]


@pytest.mark.parametrize(
    "part,field,value",
    [
        (0, "content_digest", "0" * 64),
        (0, "format_version", 10),
        (1, "head", 8),
        (1, "scope_key", "other"),
        (1, "component_key", "other"),
        (1, "payload_json", "{}"),
    ],
)
def test_descriptor_checks_actual_binding_before_exposing_a_version(part, field, value):
    saved = packet()
    saved[part][field] = value
    with pytest.raises(ContractError) as error:
        _descriptor.read("scope", "funds", saved[0], saved[1])
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_key", "other"),
        ("component_key", "other"),
        ("head", 6),
        ("collection_key", "public_collection"),
        ("item_id", "other"),
        ("sort_key", "other"),
        ("business_id", "wrong"),
    ],
)
def test_owner_body_requires_its_actual_private_row_identity(field, value):
    saved = packet()
    saved[2][field] = value
    read, _ = read_context(saved)
    view = _component_view.ready("scope", "funds", read)
    with pytest.raises(ContractError) as error:
        view.monthly_summary()
    assert error.value.code == "storage_corrupt"


def test_version_construction_does_not_read_body_and_summary_checks_it_once(monkeypatch):
    saved = packet()
    read, repository = read_context(saved)
    parses = []
    original = _component_view.loads_json

    def parsed(raw):
        parses.append(raw)
        return original(raw)

    monkeypatch.setattr(_component_view, "loads_json", parsed)
    view = _component_view.ready("scope", "funds", read)
    assert view.semantic_digest == loads_json(saved[1]["payload_json"])["semantic_digest"]
    assert repository.body_reads == 0 and parses == []
    value = view.monthly_summary()
    value["data"]["amount_fen"] = "999"
    assert view.monthly_summary()["data"]["amount_fen"] == "17"
    assert repository.body_reads == 1 and parses == [saved[2]["payload_json"]]
    assert _component_view.ready("scope", "funds", read) is view


def test_a_valid_descriptor_never_substitutes_for_the_actual_body_digest():
    saved = packet()
    saved[2]["payload_json"] = '{"data":{"amount_fen":"999"}}'
    read, _ = read_context(saved)
    view = _component_view.ready("scope", "funds", read)
    assert view.semantic_digest
    with pytest.raises(ContractError) as error:
        view.monthly_summary()
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "component", ["financial", "funds", "assets", "employees", "brief_context"]
)
def test_ordinary_descriptor_rejects_a_resealed_independent_semantic_digest(component):
    saved = packet()
    state, row, _ = saved
    descriptor = loads_json(row["payload_json"])
    descriptor.update(component_key=component, semantic_digest="0" * 64)
    state.update(component_key=component, content_digest=content_digest(descriptor))
    row.update(component_key=component, payload_json=canonical_json(descriptor))
    with pytest.raises(ContractError) as error:
        _descriptor.read("scope", component, state, row)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("component", ["brief_activity", "brief_open_items"])
def test_owner_descriptor_preserves_a_semantic_version_separate_from_private_layout(component):
    semantic = "b" * 64
    model = prepare_model({"_semantic_digest": semantic, "_private_layout": "first"})
    publication = _descriptor.prepare("scope", component, 7, 1, model)
    state = {
        "scope_key": "scope",
        "component_key": component,
        "ready_head": 7,
        "format_version": 1,
        "content_digest": publication.descriptor_digest,
    }
    row = {
        "scope_key": "scope",
        "component_key": component,
        "head": 7,
        "payload_json": publication.descriptor_json,
    }
    descriptor = _descriptor.read("scope", component, state, row)
    assert descriptor["semantic_digest"] == semantic != descriptor["body_digest"]
    changed = prepare_model({"_semantic_digest": semantic, "_private_layout": "second"})
    replacement = loads_json(_descriptor.prepare("scope", component, 7, 1, changed).descriptor_json)
    assert replacement["body_digest"] != descriptor["body_digest"]
    assert replacement["semantic_digest"] == descriptor["semantic_digest"]


def test_body_state_preserves_descriptor_identity_and_uses_owner_digest():
    state, row, _ = packet()
    descriptor = _descriptor.read("scope", "funds", state, row)
    adapted = _descriptor.body_state(state, descriptor)
    assert adapted["content_digest"] == descriptor["body_digest"]
    assert state["content_digest"] == content_digest(descriptor)
    assert adapted["descriptor_semantic_digest"] == descriptor["semantic_digest"]


def test_exact_counts_share_one_total_within_a_view_and_recheck_in_a_new_read():
    saved = packet()
    read, repository = read_context(saved)
    view = _component_view.ready("scope", "funds", read)
    empty = CollectionFilter()
    first, second = CollectionFilter(categories=("bank",)), CollectionFilter(categories=("cash",))
    assert view.item_counts("entries", first) == (20, 3)
    assert view.item_counts("entries", first) == (20, 3)
    assert view.item_counts("entries", empty) == (20, 20)
    assert view.item_counts("entries", second) == (20, 3)
    assert repository.count_queries == [("entries", first, None), ("entries", second, 20)]
    other, _ = read_context(saved)
    other.repository = lambda owner: repository
    assert _component_view.ready("scope", "funds", other).item_counts("entries", first) == (20, 3)
    assert repository.count_queries[-1] == ("entries", first, None)


def test_writable_contexts_recount_after_changes_and_never_reuse_views():
    saved = packet()
    read, repository = read_context(saved)
    read.start_period, read.head, read.view, read.as_of = "2026-01", 7, "current", None
    read.next_head, read.check_before_commit = 8, lambda callback: None
    assert isinstance(read, UnitOfWork)
    actual = [20]

    def counts(*_, **__):
        return actual[0], actual[0]

    repository.item_counts = counts
    first = _component_view.ready("scope", "funds", read)
    assert first.item_counts("entries", CollectionFilter()) == (20, 20)
    actual[0] = 19
    assert first.item_counts("entries", CollectionFilter()) == (19, 19)
    assert _component_view.ready("scope", "funds", read) is not first


def test_components_keep_membership_and_iteration_lazy_and_detach_only_loaded_values(monkeypatch):
    loaded = []

    def load(scope, component, state, read):
        loaded.append(component)
        return {"value": component}

    monkeypatch.setattr(_monthly, "_load", load)
    components = _monthly._Components("scope", {"funds": {}, "assets": {}}, object())
    assert list(components) == ["funds", "assets"]
    assert "funds" in components and "other" not in components and loaded == []
    assert components["funds"] == {"value": "funds"}
    assert components["funds"] == {"value": "funds"} and loaded == ["funds"]
    detached = deepcopy(dict(components))
    assert detached == {"funds": {"value": "funds"}, "assets": {"value": "assets"}}
    assert loaded == ["funds", "assets"]
