"""Projection subscriptions are separate artifacts; patches carry only real membership changes."""

from collections import Counter
from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ReadInputs, loads_json
from ai_accounting_modular.reading import _brief_activity_dependencies as activity
from ai_accounting_modular.reading import _brief_activity_index as activity_index
from ai_accounting_modular.reading import _brief_open_index as open_index
from ai_accounting_modular.reading import _brief_open_inputs as open_inputs
from ai_accounting_modular.reading import _brief_open_tree as open_tree
from ai_accounting_modular.reading import _brief_open_update as open_update
from ai_accounting_modular.reading._collections import IndexItem, prepare_model
from ai_accounting_modular.reading._dependencies import bind, difference


def test_full_inputs_do_not_enter_component_bytes_or_content_version():
    model = prepare_model({"amount_fen": "123"})
    selected = ReadInputs(note_scopes=tuple(f"business:{number}" for number in range(5000)))
    prepared = bind(model, selected)
    assert prepared.inputs == selected
    assert prepared.payload_json == model.payload_json
    assert prepared.content_digest == model.content_digest
    assert loads_json(prepared.payload_json) == {"amount_fen": "123"}


def test_input_difference_reports_only_additions_removals_and_flag_transitions():
    before = ReadInputs(business_ids=("same", "old"), all_results=True)
    after = ReadInputs(business_ids=("same", "new"), posted_results=True)
    added, removed = difference(before, after)
    assert added == ReadInputs(business_ids=("new",), posted_results=True)
    assert removed == ReadInputs(business_ids=("old",), all_results=True)


class Owners:
    """A local-key-only directory: deliberately no complete payload or identity list."""

    def __init__(self, values):
        self.values, self.requested = values, []

    def get(self, collection, identities):
        identities = set(identities)
        self.requested.append((collection, identities))
        return {
            identity: {"roots": sorted(self.values[collection][identity])}
            for identity in identities
            if identity in self.values[collection]
        }

    def dependents(self, collection, identities):
        self.requested.append((collection, set(identities)))


def root(identity, *, note=True):
    return IndexItem(
        activity.ROOTS,
        identity,
        identity,
        {
            "business_id": identity,
            "sources": [{"business_id": "source"}],
            "object_ids": ["person"],
            "note_scopes": ["business:source"] if note else [],
        },
    )


def test_activity_shared_input_survives_one_root_losing_note_eligibility():
    directory = Owners(
        {
            activity.BUSINESSES: {"source": {"a", "b"}},
            activity.OBJECTS: {"person": {"a", "b"}},
            activity.NOTES: {"business:source": {"a", "b"}},
        }
    )
    entries, deletes = [], set()
    added, removed = activity.changes(
        directory, [root("a")], [root("a", note=False)], entries, deletes
    )
    assert added == removed == ReadInputs()
    assert entries == [
        IndexItem(activity.NOTES, "business:source", "business:source", {"roots": ["b"]})
    ]
    assert deletes == {(activity.NOTES, "business:source")}
    assert all(identities <= {"business:source"} for _, identities in directory.requested)


def test_activity_last_note_owner_exit_removes_only_that_subscription():
    directory = Owners(
        {
            activity.BUSINESSES: {"source": {"a"}},
            activity.OBJECTS: {"person": {"a"}},
            activity.NOTES: {"business:source": {"a"}},
        }
    )
    added, removed = activity.changes(directory, [root("a")], [root("a", note=False)], [], set())
    assert added == ReadInputs()
    assert removed == ReadInputs(note_scopes=("business:source",))


def test_open_shared_note_counts_remove_subscription_only_when_last_owner_exits():
    class Directory:
        def __init__(self, count):
            self.count = count

        def get(self, collection, identities):
            assert collection == open_index.NOTES
            assert set(identities) == {"business:source"}
            return {"business:source": {"count": self.count}}

    for count in (2, 1):
        entries, deletes = [], set()
        added, removed = open_update._references(
            Directory(count),
            open_index.NOTES,
            Counter({"business:source": 1}),
            Counter(),
            entries,
            deletes,
        )
        assert added == ()
        assert removed == (("business:source",) if count == 1 else ())
        assert deletes == {(open_index.NOTES, "business:source")}


def test_cooperation_archive_roundtrips_per_input_without_a_complete_id_row():
    selected = ReadInputs(
        business_ids=tuple(f"contract-{number}" for number in range(5000)),
        object_ids=("operator",),
        result_kinds=("investment.cooperation_position",),
        all_results=True,
    )
    entries = tuple(open_inputs.provider_items(selected))
    assert len(entries) == 5003
    assert all(set(entry.value) == {"field", "value"} for entry in entries)
    assert (
        open_inputs.provider_inputs((entry.item_id, entry.value) for entry in entries).wire()
        == selected.wire()
    )


@pytest.mark.parametrize("owner", [activity_index, open_index])
def test_private_dependency_layout_does_not_change_public_content_version(owner):
    if owner is open_index:
        before = owner._summary({"open_items": {"count": 0}}, dict.fromkeys(open_tree.ROLES))
        after = deepcopy(before)
        after["_roots"]["rows"] = ["a" * 64, "a", "z", 123, 456]
    else:
        before = {
            "activity_count": 0,
            "_collections": {key: {"count": 0, "digest": "a" * 64} for key in owner.PUBLIC},
        }
        after = deepcopy(before)
        after["_buckets"] = {"notes": {"ff": {"count": 123, "digest": "b" * 64}}}
    after["_item_epoch"] = 999
    assert owner.semantic(before) == owner.semantic(after)


def test_read_inputs_rejects_obsolete_incomplete_contract():
    value = ReadInputs().wire()
    del value["note_scopes"]
    with pytest.raises(ValueError):
        ReadInputs.from_wire(value)


def test_open_reset_removes_stale_dynamic_subscriptions_and_preserves_other_components(tmp_path):
    from ai_accounting_modular.reading import _monthly, _open_store
    from ai_accounting_modular.reading._subscriptions import Directory
    from ai_accounting_modular.reading.public import ReadingImpact

    from ._support import assemble

    book = assemble(tmp_path, established=False)
    scope = _monthly.normalize(
        {
            "company_id": book.company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
    )
    key, note = _monthly.key(scope), "business:shared"
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        directory = Directory(repository)
        for component in (open_index.COMPONENT, activity_index.COMPONENT):
            repository.register(key, component, unit.head, _monthly.version(component))
            assert directory.commit(
                directory.plan(key, component, inputs=ReadInputs(note_scopes=(note,)))
            )
        assert len(directory.candidates(ReadingImpact(note_scopes=(note,)))) == 2
        _open_store.reset(repository, unit.head, open_index.FORMAT_VERSION)
        actual = Directory(repository).candidates(ReadingImpact(note_scopes=(note,)))
        assert actual == ((key, activity_index.COMPONENT),)
        assert Directory(repository).candidates(ReadingImpact(working_period_changed=True)) == (
            (key, open_index.COMPONENT),
        )
