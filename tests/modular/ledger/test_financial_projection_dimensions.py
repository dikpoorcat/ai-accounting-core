"""The existing dimension index owns ordered balances, never a second journal."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.ledger._balance_cube import balance_cube
from ai_accounting_modular.ledger._financial_calculation import summary_values
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_fold import Fold
from ai_accounting_modular.ledger._financial_projection_indexes import BodyDirectory, dimension_key
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger._financial_tree import Entry, Tree
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_model import Blocks, scope


@pytest.fixture(params=MODES)
def book(request, tmp_path, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)
    return support.assemble(tmp_path)


def prepare(book, blocks, requests, dirty=()):
    with book.app.storage.read_unit(book.company) as read:
        hot = Model(read, blocks)
        previous = hot.advance(requests, dirty)
        cold = Model(read)
        cold.advance(requests, ())
        assert hot.header == cold.header
        for selected in requests:
            assert hot.descriptor(selected) == cold.descriptor(selected)
        blocks.publish(hot.plan(previous))


def test_reloaded_cube_reads_only_strict_dimension_folds_without_journal_body_hydration(
    book, monkeypatch
):
    bill = support.expense("dimensions-cost", "500")
    book.submit(support.opening(cash="1000"), bill, support.payment("dimensions-paid", bill, "200"))
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    prepare(book, blocks, (selected,))
    with book.app.storage.read_unit(book.company) as read:
        fresh = Model(read, blocks)
        _, role = fresh.stock_role("as_posted", "2026-01")
        opening = Selection(
            read.repository("ledger"), "as_posted", "2026-01", read.start_period, Issues()
        ).opening_rows()

        def forbidden(*args, **kwargs):
            pytest.fail("a ready dimension fold must not query journal ranges again")

        monkeypatch.setattr(role.trees["journal"], "summary", forbidden)
        monkeypatch.setattr(BodyDirectory, "get_many", forbidden)
        assert role.cube(opening) == {
            ("1002", None, support.BANK): 800,
            ("3001", support.PARTY_A, None): -1000,
            ("5602", None, None): 500,
            ("2202", support.PARTY_A, None): -300,
        }


def dimensions(role):
    return {
        entry.key: entry.value
        for entry in role.trees["dimensions"].entries(role.roots["dimensions"])
    }


def dirty(fact):
    return (SimpleNamespace(business_id=fact["business_id"], result_slot="main"),)


def test_same_row_count_amount_change_party_move_and_withdraw_have_one_cold_hot_root(book):
    bill = support.expense("dimension-replaced-cost", "500")
    book.submit(support.opening(cash="1000"), bill)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    prepare(book, blocks, (selected,))
    creditor = dimension_key("2202", support.PARTY_A, None)
    cost = dimension_key("5602", None, None)
    with book.app.storage.read_unit(book.company) as read:
        before = dimensions(Model(read, blocks).stock_role("as_posted", "2026-01")[1])
    changed = deepcopy(bill)
    changed["data"]["amount_fen"] = changed["data"]["report_details"][0]["amount_fen"] = "700"
    book.replace(changed)
    prepare(book, blocks, (selected,), dirty(bill))
    with book.app.storage.read_unit(book.company) as read:
        role = Model(read, blocks).stock_role("as_posted", "2026-01")[1]
        after = dimensions(role)
        assert after[cost][3] == before[cost][3] == after[creditor][3] == before[creditor][3] == 1
        assert Fold.from_record(before[cost][4]).finish() == 500
        assert Fold.from_record(after[cost][4]).finish() == 700
        assert Fold.from_record(after[creditor][4]).finish() == -700
        assert (
            book.app.ledger.read_financial_projection(selected, read, blocks).value.values[
                "expense"
            ]["amount_fen"]
            == "700"
        )
    changed["data"]["creditor_id"] = support.PARTY_B
    book.replace(changed, seq=2)
    prepare(book, blocks, (selected,), dirty(bill))
    with book.app.storage.read_unit(book.company) as read:
        role = Model(read, blocks).stock_role("as_posted", "2026-01")[1]
        moved = dimensions(role)
        assert creditor not in moved
        assert (
            Fold.from_record(moved[dimension_key("2202", support.PARTY_B, None)][4]).finish()
            == -700
        )
    book.submit(
        mutations=[{"op": "withdraw", "business_id": bill["business_id"], "expected_seq": 3}]
    )
    prepare(book, blocks, (selected,), dirty(bill))
    with book.app.storage.read_unit(book.company) as read:
        role = Model(read, blocks).stock_role("as_posted", "2026-01")[1]
        assert role.roots["journal"] is role.roots["dimensions"] is None
        values = book.app.ledger.read_financial_projection(selected, read, blocks).value.values
        assert values["expense"]["amount_fen"] == values["liabilities"]["amount_fen"] == "0"


def test_equal_amount_new_formal_source_changes_version_without_changing_dimension_fold(book):
    bill = support.expense("dimension-new-proof", "500")
    book.submit(support.opening(cash="1000"), bill)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    prepare(book, blocks, (selected,))
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read, blocks)
        original = model.stock_role("as_posted", "2026-01")[1].wire()
        before = book.app.ledger.financial_summary(selected.scope, read)
    replacement_source = support.identity("replacement-proof")
    text = "同额费用的正确合成原件。"
    book.submit(
        sources=[
            {
                "source_id": replacement_source,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ]
    )
    bill["evidence_ids"] = [replacement_source]
    book.replace(bill)
    prepare(book, blocks, (selected,), dirty(bill))
    with book.app.storage.read_unit(book.company) as read:
        current = Model(read, blocks).stock_role("as_posted", "2026-01")[1].wire()
        actual = book.app.ledger.read_financial_projection(selected, read, blocks)
        after = book.app.ledger.financial_summary(selected.scope, read)
        assert current["dimensions"] == original["dimensions"]
        assert current["journal"] != original["journal"]
        assert before.version != after.version == actual.version
        assert before.expense == after.expense == actual.value.values["expense"]
        old_sources = tuple(
            value for value in before.sources if value["business_id"] == bill["business_id"]
        )
        new_sources = tuple(
            value for value in after.sources if value["business_id"] == bill["business_id"]
        )
        assert len(old_sources) == len(new_sources) == 1 and old_sources != new_sources


def test_zero_net_with_real_rows_retains_ordered_prefix_and_max_opening_overflow(book, monkeypatch):
    income = support.fact(
        "dimension-one-in",
        "operations.other_income",
        {"income_kind": "bank_interest", "amount_fen": "1", "funds_account_id": support.BANK},
        on="2026-01-05",
    )
    income["business_id"] = "f0000000-0000-4000-8000-000000000001"
    bill = support.expense("dimension-one-cost", "1")
    bill["business_id"] = "f0000000-0000-4000-8000-000000000003"
    paid = support.payment("dimension-one-out", bill, "1")
    paid["business_id"] = "f0000000-0000-4000-8000-000000000002"
    book.submit(support.opening(cash=str(MAX_FEN)), income, bill, paid)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    # Producer stores an unapplied Fold. It must not consume it with zero or
    # the current opening and reject a structurally valid calculation index.
    with monkeypatch.context() as patch:

        def premature_finish(*args, **kwargs):
            pytest.fail("producer must retain the Fold rather than consume an assumed opening")

        patch.setattr(Fold, "finish", premature_finish)
        prepare(book, blocks, (selected,))
    with book.app.storage.read_unit(book.company) as read:
        role = Model(read, blocks).stock_role("as_posted", "2026-01")[1]
        bank = dimensions(role)[dimension_key("1002", None, support.BANK)]
        assert bank[3] == 2
        fold = Fold.from_record(bank[4])
        assert (fold.total, fold.minimum, fold.maximum) == (0, 0, 1)
        assert fold.finish() == 0
        for consume in (
            lambda: book.app.ledger.read_financial_projection(selected, read, blocks),
            lambda: balance_cube(
                Selection(
                    read.repository("ledger"), "as_posted", "2026-01", read.start_period, Issues()
                ),
                "2026-01",
            ),
        ):
            with pytest.raises(ContractError, match="累计金额超出"):
                consume()


def test_missing_opening_remains_unknown_even_with_known_dimension_folds(book):
    book.submit(support.expense("dimension-without-opening", "70"))
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    prepare(book, blocks, (selected,))
    with book.app.storage.read_unit(book.company) as read:
        actual = book.app.ledger.read_financial_projection(selected, read, blocks).value
        original = Selection(
            read.repository("ledger"), "as_posted", "2026-01", read.start_period, Issues()
        )
        expected = summary_values(selected.scope, original)
        assert actual.values == expected
        assert list(actual.issues) == original.issues.values
        assert actual.values["assets"]["state"] == "unestablished"
        assert actual.values["expense"]["amount_fen"] == "70"


def test_replacement_and_new_month_extension_only_fold_the_actual_touched_dimensions(
    book, monkeypatch
):
    changed = support.expense("dimension-local-a", "5")
    other = support.expense("dimension-local-b", "7")
    other["data"]["creditor_id"] = support.PARTY_B
    book.submit(support.opening(), changed, other)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        january = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    prepare(book, blocks, (january,))
    changed["data"]["amount_fen"] = changed["data"]["report_details"][0]["amount_fen"] = "9"
    book.replace(changed)
    observed = []
    summary = Tree.summary

    def watched(tree, root, *, first=None, last=None):
        if tree.domain == "financial-gl/1":
            observed.append(first)
        return summary(tree, root, first=first, last=last)

    with book.app.storage.read_unit(book.company) as read:
        hot = Model(read, blocks)
        with monkeypatch.context() as patch:
            patch.setattr(Tree, "summary", watched)
            old = hot.advance((january,), dirty(changed))
        assert sorted(observed) == sorted(
            (dimension_key("5602", None, None), dimension_key("2202", support.PARTY_A, None))
        )
        blocks.publish(hot.plan(old))
    future = support.expense("dimension-local-future", "11", "2026-02")
    future["data"]["creditor_id"] = support.PARTY_B
    book.submit(future)
    with book.app.storage.read_unit(book.company) as read:
        february = book.app.ledger.financial_projection_request(
            "summary_values", scope(book, "2026-02"), read
        )
        hot = Model(read, blocks)
        original_january = hot.descriptor(january)
        observed.clear()
        with monkeypatch.context() as patch:
            patch.setattr(Tree, "summary", watched)
            old = hot.advance((february,), ())
        assert sorted(observed) == sorted(
            (dimension_key("5602", None, None), dimension_key("2202", support.PARTY_B, None))
        )
        cold = Model(read)
        cold.advance((january, february), ())
        assert hot.header == cold.header and hot.descriptor(january) == original_january
        blocks.publish(hot.plan(old))
        current = Model(read, blocks).stock_role("as_posted", "2026-02")[1]
        assert (
            Fold.from_record(dimensions(current)[dimension_key("5602", None, None)][4]).finish()
            == 27
        )


def test_old_projection_format_requires_explicit_rebuild(book):
    book.submit(support.opening(), support.expense("dimension-old-format", "5"))
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
    prepare(book, blocks, (selected,))
    old = loads_json(blocks.header_json)
    assert old["format"] == 2
    old["format"] = 1
    blocks.header_json = canonical_json(old)
    with book.app.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as error:
            book.app.ledger.read_financial_projection(selected, read, blocks)
        assert error.value.code == "storage_corrupt"
        rebuilt = book.app.ledger.prepare_financial_projection(
            (selected,), (), (), read, rebuild=True
        )
        blocks = Blocks()
        blocks.publish(rebuilt)
        assert (
            book.app.ledger.read_financial_projection(selected, read, blocks).value.values[
                "expense"
            ]["amount_fen"]
            == "5"
        )


@pytest.mark.parametrize("damage", ["old-four-fields", "bool-count", "bad-fold"])
def test_dimension_rows_reject_old_or_damaged_meaning_without_falling_back(book, damage):
    book.submit(support.opening(), support.expense("dimension-invalid", "5"))
    with book.app.storage.read_unit(book.company) as read:
        selected = book.app.ledger.financial_projection_request("summary_values", scope(book), read)
        model = Model(read)
        model.advance((selected,), ())
        role = model.stock_role("as_posted", "2026-01")[1]
        entries = list(role.trees["dimensions"].entries(role.roots["dimensions"]))
        first = entries[0]
        value = first.value
        if damage == "old-four-fields":
            value.pop()
        elif damage == "bool-count":
            value[3] = True
        else:
            value[4][1] = True
        entries[0] = Entry(first.key, value)
        role.roots["dimensions"] = role.trees["dimensions"].full(entries)
        with pytest.raises(ContractError) as error:
            role.cube([])
        assert error.value.code == "storage_corrupt"
