"""The persisted model agrees with the original formulas and has a finite live graph."""

from collections import Counter
from copy import deepcopy
from types import SimpleNamespace

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.ledger._financial import normalize
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot, summary_values
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_bodies import source_atom
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.storage._immutable_tree import structure
from ai_accounting_modular.storage.public import block_digest as digest


class Blocks:
    header_json = None

    def __init__(self):
        self.values = {}

    def get_blocks(self, keys):
        return tuple(self.values.get(key) for key in keys)

    def publish(self, plan):
        for key in plan.deletes:
            del self.values[key]
        self.values.update(((collection, key), raw) for collection, key, raw in plan.writes)
        self.header_json = plan.header_json


def scope(book, month="2026-01", view="as_posted"):
    return {"company_id": book.company, "period_from": month, "period_to": month, "view": view}


def classic(read, normalized):
    return Selection(
        read.repository("ledger"),
        normalized["view"],
        normalized["period_to"],
        read.start_period,
        Issues(),
    )


@pytest.mark.parametrize("kind", ["summary_values", "snapshot"])
@pytest.mark.parametrize("case", ["empty", "zero", "missing", "continuation"])
def test_empty_zero_missing_and_continuation_use_original_values_issues_and_sources(
    tmp_path, kind, case
):
    start = "2026-05" if case == "continuation" else "2026-01"
    book, blocks = support.assemble(tmp_path, start), Blocks()
    if case in ("zero", "continuation"):
        initial = support.opening(month=start, cash="500000" if case == "continuation" else "0")
        book.submit(
            initial, *([support.continuation(initial, may=True)] if case == "continuation" else [])
        )
    if case in ("missing", "continuation"):
        book.submit(support.expense("missing-cost", "19", start, details=False))
    selected_scope = scope(book, start)
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request(kind, selected_scope, read)
        plan = book.app.ledger.prepare_financial_projection((selected,), (), (), read, rebuild=True)
        blocks.publish(plan)
        actual = book.app.ledger.read_financial_projection(selected, read, blocks)
        normalized = normalize(selected_scope, read)
        selection = classic(read, normalized)
        if kind == "snapshot":
            expected = calculate_snapshot(normalized, read, selection)
            assert {
                key: value for key, value in actual.value.items() if key != "selection"
            } == expected
            assert actual.value == book.app.ledger.financial_snapshot(selected_scope, read)
        else:
            expected = summary_values(normalized, selection)
            assert actual.value.values == expected
            assert list(actual.value.issues) == selection.issues.values
            full = book.app.ledger.financial_summary(selected_scope, read)
            assert full.version == actual.version
            sources = tuple(
                sorted(
                    (selection.source(selection.heads[key]) for key in tuple(selection.used)),
                    key=lambda value: (
                        value["business_id"],
                        value["result_slot"],
                        value["result_id"],
                    ),
                )
            )
            assert full.sources == sources


def test_split_replacement_reclaims_old_open_bodies_and_matches_full_roots(tmp_path, monkeypatch):
    book, blocks = support.assemble(tmp_path), Blocks()
    costs = tuple(support.expense(f"bounded-{i:03d}", "1") for i in range(130))
    february = support.expense("bounded-february", "7", "2026-02")
    book.submit(support.opening(cash="1000"), *costs, february)
    selected_scope = scope(book)
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request(
            "summary_values", selected_scope, read
        )
        second = book.app.ledger.financial_projection_request(
            "summary_values", scope(book, "2026-02"), read
        )
        quarter = book.app.ledger.financial_projection_request(
            "snapshot", selected_scope | {"period_to": "2026-03"}, read
        )
        requests = selected, second, quarter
        model = Model(read)
        previous = model.advance(requests, ())
        assert model.bodies.root.count > 128
        _, stock_role = model.stock_role("as_posted", "2026-01")
        _, flow_role = model.flow_role("as_posted", "2026-01", "2026-01", include_cash=False)
        assert stock_role.roots["journal"].count == 260 and flow_role.root.count == 130
        old_atom = next(
            head["atom"]
            for heads in model._old_chains().values()
            for head in heads
            if head["business_id"] == costs[0]["business_id"]
        )
        blocks.publish(model.plan(previous))
    changed = deepcopy(costs[0])
    changed["data"]["amount_fen"] = "2"
    changed["data"]["report_details"][0]["amount_fen"] = "2"
    book.replace(changed)
    with book.app.storage.read_unit(book.company) as read:
        hot = Model(read, blocks)
        previous = hot.advance(
            requests, (SimpleNamespace(business_id=changed["business_id"], result_slot="main"),)
        )
        cold = Model(read)
        cold.advance(requests, ())
        assert hot.header == cold.header
        assert hot.descriptor(selected) == cold.descriptor(selected)
        plan = hot.plan(previous)
        blocks.publish(plan)
        reloaded = Model(read, blocks)
        assert reloaded.header == cold.header
        assert reloaded.bodies.tree.get_many(reloaded.bodies.root, (old_atom,)) == (None,)
        actual = book.app.ledger.read_financial_projection(selected, read, blocks)
        assert actual.value.values["expense"]["amount_fen"] == "131"
        assert all(
            loads_json(raw)["count"] >= 1
            for (collection, _), raw in blocks.values.items()
            if collection == "_financial_node_refs"
        )
        assert len(plan.writes) < len(blocks.values)  # A single change is not a full rewrite.
        assert_graph(reloaded, blocks)

    april = support.expense("bounded-april", "11", "2026-04")
    book.submit(april)
    with book.app.storage.read_unit(book.company) as read:
        from ai_accounting_modular.ledger import (
            _financial_projection_bodies,
            _financial_projection_indexes,
        )

        before = Model(read, blocks)
        old_atoms = {head["atom"] for chain in before._old_chains().values() for head in chain}
        april_request = book.app.ledger.financial_projection_request(
            "summary_values", scope(book, "2026-04"), read
        )
        quarter_request = book.app.ledger.financial_projection_request(
            "snapshot", scope(book, "2026-04") | {"period_to": "2026-06"}, read
        )
        previous_requests, requests = requests, (*requests, april_request, quarter_request)
        get_many = _financial_projection_indexes.BodyDirectory.get_many
        capture = _financial_projection_bodies.capture_bodies
        captured = []

        def actual_bodies(selection, heads):
            captured.extend(head["business_id"] for head in heads)
            return capture(selection, heads)

        def new_bodies_only(directory, identities):
            identities = tuple(identities)
            assert not old_atoms.intersection(identities), "new range expanded historical bodies"
            return get_many(directory, identities)

        with monkeypatch.context() as patch:
            patch.setattr(_financial_projection_bodies, "capture_bodies", actual_bodies)
            patch.setattr(_financial_projection_indexes.BodyDirectory, "get_many", new_bodies_only)
            previous = before.advance(requests, ())  # No dirty signal was sent for future April.
            plan = before.plan(previous)
        assert captured == [april["business_id"]]
        cold = Model(read)
        cold.advance(requests, ())
        assert before.header == cold.header
        for value in previous_requests:
            assert before.descriptor(value) == Model(read, blocks).descriptor(value)
        blocks.publish(plan)

    removed = costs[1:101]
    book.submit(
        mutations=[
            {"op": "withdraw", "business_id": value["business_id"], "expected_seq": 1}
            for value in removed
        ]
    )
    with book.app.storage.read_unit(book.company) as read:
        hot = Model(read, blocks)
        previous = hot.advance(
            requests,
            tuple(
                SimpleNamespace(business_id=value["business_id"], result_slot="main")
                for value in removed
            ),
        )
        cold = Model(read)
        cold_previous = cold.advance(requests, ())
        assert hot.header == cold.header
        plan = hot.plan(previous)
        assert plan.deletes
        blocks.publish(plan)
        fresh = Blocks()
        fresh.publish(cold.plan(cold_previous))
        reloaded = Model(read, blocks)
        assert reloaded.bodies.root.count == 33
        for value in requests:
            assert book.app.ledger.read_financial_projection(
                value, read, blocks
            ) == book.app.ledger.read_financial_projection(value, read, fresh)
        assert (
            book.app.ledger.read_financial_projection(selected, read, blocks).value.values[
                "expense"
            ]["amount_fen"]
            == "31"
        )
        assert (
            book.app.ledger.read_financial_projection(second, read, blocks).value.values["expense"][
                "amount_fen"
            ]
            == "7"
        )
        assert_graph(reloaded, blocks)


def assert_graph(model, blocks):
    # Count every distinct parent edge once and every actual root reference.
    expected, pending, visited = Counter(model.root_ids()), list(model.root_ids()), set()
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        visited.add(identity)
        raw = blocks.values["_financial_nodes", identity]
        _, _, children = structure(raw, identity)
        for child in children:
            expected[child.digest] += 1
            pending.append(child.digest)
    assert visited == {
        identity for (collection, identity) in blocks.values if collection == "_financial_nodes"
    }
    actual = {
        identity: loads_json(raw)["count"]
        for (collection, identity), raw in blocks.values.items()
        if collection == "_financial_node_refs"
    }
    assert actual == expected
    directory = {entry.key: entry.value for entry in model.bodies.tree.entries(model.bodies.root)}
    atoms = {head["atom"] for chain in model._old_chains().values() for head in chain}
    assert set(directory) == atoms
    body_keys = {
        identity for collection, identity in blocks.values if collection == model.bodies.BODIES
    }
    assert set(directory.values()) == body_keys and len(body_keys) == len(atoms)
    for atom, reference in directory.items():
        raw = blocks.values[model.bodies.BODIES, reference]
        body = loads_json(raw)
        assert digest(raw) == reference and canonical_json(body) == raw
        assert source_atom(body["source"]) == atom


def test_request_mutation_and_missing_declared_descriptor_do_not_turn_into_empty_values(tmp_path):
    book, blocks = support.assemble(tmp_path), Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
        assert book.app.ledger.read_financial_projection(selected, read, blocks) is None
        changed = deepcopy(selected)
        changed.scope["period_to"] = "2026-02"
        with pytest.raises(ContractError):
            book.app.ledger.prepare_financial_projection((changed,), (), (), read)
        blocks.publish(book.app.ledger.prepare_financial_projection((selected,), (), (), read))
        del blocks.values[selected.descriptor_key]
        with pytest.raises(ContractError) as error:
            book.app.ledger.read_financial_projection(selected, read, blocks)
        assert error.value.code == "storage_corrupt"


def test_consumer_reads_its_roles_while_publication_checks_the_complete_graph(tmp_path):
    book, blocks = support.assemble(tmp_path), Blocks()
    book.submit(
        support.opening(cash="1000"),
        support.expense("january-cost", "11"),
        support.expense("february-cost", "17", "2026-02"),
    )
    with book.app.storage.read_unit(book.company) as read:
        january, february = (
            book.app.ledger.financial_projection_request("summary_values", scope(book, month), read)
            for month in ("2026-01", "2026-02")
        )
        blocks.publish(
            book.app.ledger.prepare_financial_projection((january, february), (), (), read)
        )
        model = Model(read, blocks)
        _, role = model.flow_role("as_posted", "2026-01", "2026-01", include_cash=False)
        january_node = ("_financial_nodes", role.root.digest)
        before = book.app.ledger.read_financial_projection(february, read, blocks)
        assert before.value.values["expense"]["amount_fen"] == "17"
        assert (
            book.app.ledger.read_financial_projection(january, read, blocks).value.values[
                "expense"
            ]["amount_fen"]
            == "11"
        )
        # An unrelated month's broken role cannot block a valid consumer. It
        # still blocks both its own consumer and a complete graph publication.
        blocks.values[january_node] = "{}"
        assert book.app.ledger.read_financial_projection(february, read, blocks) == before
        with pytest.raises(ContractError, match="不可变树") as failure:
            book.app.ledger.read_financial_projection(january, read, blocks)
        assert failure.value.code == "storage_corrupt"
        with pytest.raises(ContractError) as failure:
            book.app.ledger.prepare_financial_projection((february,), (), (), read, base=blocks)
        assert failure.value.code == "storage_corrupt"
