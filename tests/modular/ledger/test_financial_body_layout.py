"""The thin company catalog owns exact immutable Source bodies in one atomic plan."""

import json
from copy import deepcopy
from uuid import uuid4

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.ledger._financial_projection_bodies import source_atom
from ai_accounting_modular.ledger._financial_projection_indexes import BodyDirectory
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_tree import Entry, digest
from ai_accounting_modular.ledger._financial_tree_store import TreeStore
from ai_accounting_modular.ledger.public import FormalChange
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_candidates import assert_snapshot
from tests.modular.ledger.test_financial_projection_model import Blocks, assert_graph, scope
from tests.modular.ledger.test_financial_projection_ranges import close, corrected


@pytest.fixture(params=MODES)
def book(request, tmp_path, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)
    return support.assemble(tmp_path)


class ObservedBlocks(Blocks):
    def __init__(self):
        super().__init__()
        self.calls = []

    def get_blocks(self, keys):
        keys = tuple(keys)
        self.calls.append(keys)
        return super().get_blocks(keys)


def seed(book):
    costs = tuple(support.expense(f"small-body-{index:03d}", "1") for index in range(65))
    book.submit(support.opening(cash="1000"), *costs)
    return costs


def requests(book, read):
    ledger = book.app.ledger
    return (
        ledger.financial_projection_request("summary_values", scope(book), read),
        ledger.financial_projection_request("summary_values", scope(book, view="current"), read),
        ledger.financial_projection_request(
            "snapshot", scope(book) | {"period_to": "2026-03"}, read
        ),
    )


@pytest.fixture
def saved_body(book):
    book.submit(support.opening(cash="1000"), support.expense("immutable-body", "7"))
    blocks = ObservedBlocks()
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read)
        previous = model.advance(requests(book, read), ())
        blocks.publish(model.plan(previous))
        atom = next(identity for identity, body in model.bodies._bodies.items() if body["profit"])
        body = deepcopy(model.bodies.get_many((atom,))[0])
        return blocks, model.header["bodies"], atom, body


def replace_body_reference(blocks, descriptor, atom, raw):
    directory = BodyDirectory(TreeStore(blocks), descriptor)
    reference = digest(raw)
    blocks.values[BodyDirectory.BODIES, reference] = raw
    changed = directory.tree.patch(directory.root, {atom: Entry(atom, reference)})
    plan = directory.store.finish((directory.root.digest,), (changed.digest,))
    for key in plan.deletes:
        del blocks.values[key]
    for item in plan.writes:
        blocks.values[item.collection, item.key] = item.payload_json
    return changed.wire()


@pytest.mark.parametrize(
    "damage", ["missing", "hash", "noncanonical", "amount", "atom", "duplicate", "float"]
)
def test_actual_body_bytes_and_atom_are_strict_even_with_a_matching_directory_digest(
    saved_body, damage
):
    blocks, descriptor, atom, body = saved_body
    original = canonical_json(body)
    reference = digest(original)
    if damage == "missing":
        del blocks.values[BodyDirectory.BODIES, reference]
    elif damage == "hash":
        blocks.values[BodyDirectory.BODIES, reference] = original + " "
    else:
        if damage == "noncanonical":
            raw = json.dumps(body, ensure_ascii=False, indent=2)
        elif damage == "duplicate":
            raw = original[:-1] + ',"source":' + canonical_json(body["source"]) + "}"
        else:
            if damage == "atom":
                body["source"]["result_id"] = str(uuid4())
            else:
                body["profit"]["14"] = True if damage == "amount" else 1.5
            raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        descriptor = replace_body_reference(blocks, descriptor, atom, raw)
    with pytest.raises(ContractError) as failure:
        BodyDirectory(TreeStore(blocks), descriptor).get_many((atom,))
    assert failure.value.code == "storage_corrupt"


def test_existing_atom_reuses_only_identical_actual_body_and_never_replaces_its_meaning(saved_body):
    blocks, descriptor, atom, body = saved_body
    directory = BodyDirectory(TreeStore(blocks), descriptor)
    directory.prepare({atom: body})
    assert directory.root.wire() == descriptor and directory.changes() == ((), ())
    body["profit"]["14"] += 1
    with pytest.raises(ContractError) as failure:
        directory.prepare({atom: body})
    assert failure.value.code == "storage_corrupt"
    assert directory.root.wire() == descriptor and directory.changes() == ((), ())


@pytest.mark.parametrize("existing_locator", [False, True])
def test_prepare_rejects_wrong_actual_raw_and_does_not_heal_an_existing_missing_body(
    saved_body, existing_locator
):
    blocks, descriptor, atom, body = saved_body
    reference = digest(canonical_json(body))
    if existing_locator:
        del blocks.values[BodyDirectory.BODIES, reference]
    else:
        blocks.values[BodyDirectory.BODIES, reference] += " "
        descriptor = None
    before = dict(blocks.values)
    directory = BodyDirectory(TreeStore(blocks), descriptor)
    with pytest.raises(ContractError) as failure:
        directory.prepare({atom: body})
    assert failure.value.code == "storage_corrupt"
    assert blocks.values == before and directory.changes() == ((), ())


@pytest.mark.parametrize("damage", ["missing", "wrong_raw"])
def test_failed_body_batch_keeps_root_changes_and_old_legs(saved_body, damage):
    blocks, descriptor, atom, body = saved_body
    directory = BodyDirectory(TreeStore(blocks), descriptor)
    damaged = next(entry for entry in directory.tree.entries(directory.root) if entry.key != atom)
    damaged_key = BodyDirectory.BODIES, damaged.value
    damaged_body = loads_json(blocks.values[damaged_key])
    if damage == "missing":
        del blocks.values[damaged_key]
    else:
        blocks.values[damaged_key] += " "
    retained = directory.get_many((atom,))[0]
    retained_line = directory.line(atom, retained["journal"][0][0])
    before = deepcopy((directory._bodies, directory._lines))
    root, changes = directory.root, directory.changes()
    new_body = deepcopy(body)
    new_body["source"]["result_id"] = str(uuid4())
    new_atom = source_atom(new_body["source"])

    with pytest.raises(ContractError) as failure:
        directory.prepare({new_atom: new_body, damaged.key: damaged_body})

    assert failure.value.code == "storage_corrupt"
    assert directory.root == root and directory.changes() == changes
    assert (directory._bodies, directory._lines) == before
    assert directory.get_many((atom,))[0] is retained
    assert directory.line(atom, retained_line[0]) is retained_line
    tree_plan = directory.store.finish((root.digest,), (root.digest,))
    assert tree_plan.writes == tree_plan.deletes == ()


def test_new_body_owns_its_input_and_unpublished_retirement_leaves_no_orphan(saved_body):
    _, _, atom, body = saved_body
    blocks = ObservedBlocks()
    directory = BodyDirectory(TreeStore(blocks))
    directory.prepare({atom: body})
    original = deepcopy(body)
    body["profit"]["14"] += 1
    assert directory.get_many((atom,))[0] == original
    writes, _ = directory.changes()
    assert len(writes) == 1 and loads_json(writes[0][2]) == original
    directory.prepare({}, (atom, atom))
    assert directory.root is None and directory.changes() == ((), ())
    assert directory.store.finish((), ()).writes == () and blocks.values == {}


def test_retired_atom_can_only_reattach_its_original_body_without_changing_old_legs(saved_body):
    blocks, descriptor, atom, body = saved_body
    directory = BodyDirectory(TreeStore(blocks), descriptor)
    directory.prepare({}, (atom,))
    retired_root = directory.root
    retired = directory.get_many((atom,))[0]
    changed = deepcopy(body)
    changed["profit"]["14"] += 1
    with pytest.raises(ContractError) as failure:
        directory.prepare({atom: changed})
    assert failure.value.code == "storage_corrupt"
    assert directory.root == retired_root and directory.get_many((atom,))[0] == retired == body
    directory.prepare({atom: body})
    assert directory.root.wire() == descriptor and directory.changes() == ((), ())


def test_real_lookup_reads_only_one_body_and_company_directory_owns_every_body(book):
    seed(book)
    blocks = ObservedBlocks()
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read)
        selected = requests(book, read)
        previous = model.advance(selected, ())
        blocks.publish(model.plan(previous))
        references = {
            entry.key: entry.value for entry in model.bodies.tree.entries(model.bodies.root)
        }
        actual = dict(zip(references, model.bodies.get_many(references), strict=True))
        assert len(actual) == 66
        wanted = sorted(actual)[len(actual) // 2]
        blocks.calls.clear()
        directory = BodyDirectory(TreeStore(blocks), model.header["bodies"])
        assert directory.get_many((wanted, wanted)) == (actual[wanted], actual[wanted])
        fetched = {
            identity: loads_json(blocks.values[collection, identity])
            for batch in blocks.calls
            for collection, identity in batch
            if collection == TreeStore.NODES
        }
        leaves = [node for node in fetched.values() if node["kind"] == "leaf"]
        assert len(leaves) == 1
        assert all(type(entry[1]) is str and len(entry[1]) == 64 for entry in leaves[0]["entries"])
        assert [
            identity
            for batch in blocks.calls
            for collection, identity in batch
            if collection == BodyDirectory.BODIES
        ] == [references[wanted]]
        assert_snapshot(book, selected[2], read, blocks)
        assert_graph(Model(read, blocks), blocks)

        # Trees and the uniquely owned bodies retire together. Descriptors are
        # outside this graph; a bare TreeStore root release cannot delete bodies.
        reloaded = Model(read, blocks)
        old_roots = reloaded.root_ids()
        reloaded.bodies.prepare({}, tuple(references))
        released = reloaded.store.finish(old_roots, ())
        for key in released.deletes:
            del blocks.values[key]
        for item in released.writes:
            blocks.values[item.collection, item.key] = item.payload_json
        writes, deletes = reloaded.bodies.changes()
        assert not writes
        for key in deletes:
            del blocks.values[key]
        assert not any(
            collection in (TreeStore.NODES, TreeStore.REFS, BodyDirectory.BODIES)
            for collection, _ in blocks.values
        )


@pytest.mark.parametrize("frozen", [False, True], ids=["open", "closed"])
def test_actual_replacement_reclaims_only_retired_sources_and_keeps_frozen_legs(book, frozen):
    costs = seed(book)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
        model = Model(read)
        previous = model.advance(selected, ())
        old_atom = next(
            head["atom"]
            for chain in model._old_chains().values()
            for head in chain
            if head["business_id"] == costs[0]["business_id"]
        )
        old_body = model.bodies.get_many((old_atom,))[0]
        blocks.publish(model.plan(previous))
    if frozen:
        close(book, "2026-01")
    book.replace(corrected(costs[0], "2"), correction_period="2026-02" if frozen else None)
    with book.app.storage.read_unit(book.company) as read:
        change = FormalChange(
            costs[0]["business_id"],
            "main",
            ("2026-01",),
            ("2026-02" if frozen else "2026-01",),
            ("operations.expense",),
        )
        plan = book.app.ledger.prepare_financial_projection(
            selected, (change,), (), read, base=blocks
        )
        blocks.publish(plan)
        fresh = Blocks()
        fresh.publish(
            book.app.ledger.prepare_financial_projection(selected, (), (), read, rebuild=True)
        )
        assert blocks.header_json == fresh.header_json
        assert blocks.values == fresh.values
        reloaded = Model(read, blocks)
        assert reloaded.bodies.root.count == (67 if frozen else 66)
        if frozen:
            assert reloaded.bodies.get_many((old_atom,))[0] == old_body
        else:
            with pytest.raises(ContractError) as retired:
                reloaded.bodies.get_many((old_atom,))
            assert retired.value.code == "storage_corrupt"
        current = book.app.ledger.read_financial_projection(selected[1], read, blocks)
        assert current.value.values["expense"]["amount_fen"] == "66"
        posted = book.app.ledger.read_financial_projection(selected[0], read, blocks)
        assert posted.value.values["expense"]["amount_fen"] == ("65" if frozen else "66")
        sources = book.app.ledger.financial_summary(selected[1].scope, read).sources
        used = [value for value in sources if value["business_id"] == costs[0]["business_id"]]
        assert len(used) == 1 and used[0] != old_body["source"]
        assert_snapshot(book, selected[2], read, blocks)
        assert_graph(reloaded, blocks)
