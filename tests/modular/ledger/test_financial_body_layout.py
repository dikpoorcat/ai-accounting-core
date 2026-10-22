"""Real Source bodies use bounded leaves without adding another ownership graph."""

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.ledger._financial_projection_indexes import BodyDirectory
from ai_accounting_modular.ledger._financial_projection_model import Model
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


def test_real_body_lookup_decodes_at_most_one_small_leaf_and_roots_own_every_block(book):
    seed(book)
    blocks = ObservedBlocks()
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read)
        selected = requests(book, read)
        previous = model.advance(selected, ())
        blocks.publish(model.plan(previous))
        actual = {entry.key: entry.value for entry in model.bodies.tree.entries(model.bodies.root)}
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
        assert len(leaves) == 1 and 1 <= leaves[0]["count"] <= 16
        assert leaves[0]["count"] < len(actual)
        assert directory.tree.max_items == 16 and directory.tree.max_bytes == 65536
        assert all(node["max_items"] == 16 for node in fetched.values())
        assert_snapshot(book, selected[2], read, blocks)
        assert_graph(Model(read, blocks), blocks)

        # Release the actual complete root multiset: no separate body/blob
        # ownership is needed, and descriptors are outside this tree graph.
        reloaded = Model(read, blocks)
        released = reloaded.store.finish(reloaded.root_ids(), ())
        for key in released.deletes:
            del blocks.values[key]
        for item in released.writes:
            blocks.values[item.collection, item.key] = item.payload_json
        assert not any(
            collection in (TreeStore.NODES, TreeStore.REFS) for collection, _ in blocks.values
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
