"""The real sparse bodies survive shared physical storage without scope-wide copying."""

from copy import deepcopy

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger._balance_cube import balance_cube
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_bodies import capture_bodies
from ai_accounting_modular.ledger._financial_projection_indexes import (
    BodyDirectory,
    FlowStrategy,
    JournalStrategy,
    dimension_key,
    flow_entries,
    flow_source_used,
    flow_value,
    journal_key,
    order_key,
    prefix_end,
)
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore


class Rows:
    header_json = None

    def __init__(self):
        self.values = {}

    def get_blocks(self, keys):
        return tuple(self.values.get(key) for key in keys)

    def publish(self, plan):
        for key in plan.deletes:
            del self.values[key]
        for item in plan.writes:
            self.values[item.collection, item.key] = item.payload_json

    def publish_bodies(self, directory):
        writes, deletes = directory.changes()
        for key in deletes:
            del self.values[key]
        for collection, key, raw in writes:
            self.values[collection, key] = raw


def records(tmp_path):
    book = support.assemble(tmp_path)
    bill = support.expense("expense", "500")
    book.submit(support.opening(cash="1000"), bill, support.payment("paid", bill, "200"))
    return book


def load_bodies(book, read, rows):
    selection = Selection(read.repository("ledger"), "current", "2026-01", "2026-01", Issues())
    actual = capture_bodies(selection, tuple(selection.stock("2026-01").values()))
    store = TreeStore(rows, namespace="_financial")
    bodies = BodyDirectory(store)
    bodies.prepare(actual)
    return selection, actual, store, bodies


def test_reloaded_roles_match_real_journal_cube_and_do_not_include_the_company_body_root(tmp_path):
    book, rows = records(tmp_path), Rows()
    with book.app.storage.read_unit(book.company) as read:
        selection, actual, store, bodies = load_bodies(book, read, rows)
        flow = Tree("financial-pl-cf/1", FlowStrategy(bodies), store)
        flow_root = flow.full(
            Entry(
                order_key(
                    (
                        body["business_period"],
                        body["source"]["business_id"],
                        body["source"]["result_slot"],
                    )
                ),
                flow_value(identity, body, 1),
            )
            for identity, body in actual.items()
        )
        journal = Tree("financial-journal/1", JournalStrategy(bodies), store)
        journal_root = journal.full(
            Entry(journal_key(body, row), [identity, row[0]])
            for identity, body in actual.items()
            for row in body["journal"]
        )
        assert flow.summary(flow_root)["p14"].finish() == 500
        expected = balance_cube(selection, "2026-01")
        opening = selection.opening_rows()
        dimensions = {tuple(row[1:4]) for body in actual.values() for row in body["journal"]}
        for dimension in dimensions:
            prefix = dimension_key(*dimension)
            key, fold = journal.summary(journal_root, first=prefix, last=prefix_end(prefix))
            assert key == prefix
            initial = sum(
                int(row["debit_fen"]) - int(row["credit_fen"])
                for row in opening
                if (row["account_code"], row.get("party_id"), row.get("object_id")) == dimension
            )
            assert fold.finish(initial) == expected[dimension]
        roots = (bodies.root, flow_root, journal_root)
        rows.publish(store.finish((), tuple(root.digest for root in roots)))
        rows.publish_bodies(bodies)
        reloaded_store = TreeStore(rows, namespace="_financial")
        loaded = BodyDirectory(reloaded_store, bodies.root.wire())
        loaded_flow = Tree("financial-pl-cf/1", FlowStrategy(loaded), reloaded_store)
        saved = loaded_flow.root(flow_root.wire())
        assert loaded_flow.summary(saved) == flow.summary(flow_root)
        previous_summary = loaded_flow.summary(saved)

    # A genuinely new future adoption changes the company catalog, not January's
    # actual role or its consumer guard. It is not an unverified invented body.
    book.submit(support.expense("future", "9", "2026-02"))
    with book.app.storage.read_unit(book.company) as read:
        selection = Selection(read.repository("ledger"), "current", "2026-02", "2026-01", Issues())
        added = capture_bodies(selection, tuple(selection.stock("2026-02").values()))
        changes = {identity: body for identity, body in added.items() if identity not in actual}
        assert len(changes) == 1
        loaded.prepare(changes)
        assert loaded.root.digest != bodies.root.digest
        assert loaded_flow.root(flow_root.wire()).digest == flow_root.digest
        assert loaded_flow.summary(saved) == previous_summary


def test_missing_body_and_invalid_cached_fen_or_line_identity_are_never_filled_in(tmp_path):
    book, rows = records(tmp_path), Rows()
    with book.app.storage.read_unit(book.company) as read:
        _, actual, store, bodies = load_bodies(book, read, rows)
        identity, body = next((key, value) for key, value in actual.items() if value["profit"])
        empty = BodyDirectory(TreeStore(rows, namespace="_financial"))
        with pytest.raises(ContractError) as missing:
            Tree("flow/1", FlowStrategy(empty), empty.tree.store).full(
                (Entry("one", [identity, 1]),)
            )
        assert missing.value.code == "storage_corrupt"
        wrong = deepcopy(body)
        wrong["profit"]["14"] = True
        with pytest.raises(ContractError) as amount:
            bodies.prepare({identity: wrong})
        assert amount.value.code == "storage_corrupt"
        bodies = BodyDirectory(store)
        bodies.prepare({identity: body})
        line = body["journal"][0]
        with pytest.raises(ContractError) as invalid_line:
            Tree("journal/1", JournalStrategy(bodies), store).full(
                (Entry(journal_key(body, line), [identity, True]),)
            )
        assert invalid_line.value.code == "storage_corrupt"


def test_domain_tuple_keys_preserve_case_unicode_empty_and_clustered_order():
    values = [
        (first, second)
        for first in ("", "a", "a!", "A", "\x00", "甲")
        for second in ("", "0000", "\x00", "乙")
    ]
    assert sorted(values) == sorted(values, key=order_key)
    assert len({order_key(value) for value in values}) == len(values)


def test_non_profit_journal_and_frozen_baseline_keep_real_source_entries(tmp_path):
    book = support.assemble(tmp_path)
    deposit = support.fact(
        "deposit",
        "operations.deposit",
        {"type": "direct", "counterparty_id": support.PARTY_A, "amount_fen": "50"},
    )
    book.submit(support.opening(), deposit)
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-01", unit)
        assert checked.ready
        book.app.ledger.close_period("2026-01", checked.head, unit)
    corrected = deepcopy(deposit)
    corrected["data"]["amount_fen"] = "40"
    book.replace(corrected, correction_period="2026-10")
    with book.app.storage.read_unit(book.company) as read:
        selection = Selection(
            read.repository("ledger"), "as_posted", "2026-10", "2026-01", Issues()
        )
        coefficients = selection.flow("2026-10", "2026-10")
        heads = tuple(selection.heads[identity] for identity in coefficients)
        actual = capture_bodies(selection, heads)
        by_result = {
            body["source"]["result_id"]: (identity, body) for identity, body in actual.items()
        }
        assert len(by_result) == 2
        anchors = {
            head["baseline_result_id"]: head
            for head in heads
            if head["baseline_result_id"] is not None
        }
        values = [
            (*by_result[identity], coefficient) for identity, coefficient in coefficients.items()
        ]
        for _, body, _ in values:
            assert body["journal"] and not body["profit"] and not body["cash"]
            assert flow_source_used(body, "profit") and flow_source_used(body, "pretax")
            assert not flow_source_used(body, "cash")
        entries = flow_entries(
            values,
            view="as_posted",
            first="2026-10",
            last="2026-10",
            anchors=anchors,
            include_cash=False,
        )
        assert len(entries) == 2 and sorted(entry.value[1] for entry in entries) == [-1, 1]
        store = TreeStore(Rows(), namespace="_financial")
        bodies = BodyDirectory(store)
        bodies.prepare(actual)
        tree = Tree("financial-pl-cf/1", FlowStrategy(bodies), store)
        root = tree.full(entries)
        assert root is not None and root.count == 2 and tree.summary(root) == {}
        assert {entry.value[0] for entry in tree.entries(root)} == set(actual)
        assert (
            flow_entries(
                [(identity, body, 0) for identity, body, _ in values],
                view="as_posted",
                first="2026-10",
                last="2026-10",
                anchors=anchors,
                include_cash=False,
            )
            == ()
        )
