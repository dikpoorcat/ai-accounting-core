"""Actual range discovery reads complete identity chains, not every company body."""

from copy import deepcopy
from types import SimpleNamespace

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.ledger._financial import normalize
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_chains import stock
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_projection_requests import request
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger._financial_tree import Tree
from ai_accounting_modular.ledger.public import FormalChange
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_model import Blocks
from tests.modular.ledger.test_financial_projection_ranges import close, corrected


@pytest.fixture(params=MODES)
def book(request, tmp_path, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)
    return support.assemble(tmp_path)


def scope(book, first, last=None, view="as_posted"):
    return {
        "company_id": book.company,
        "period_from": first,
        "period_to": last or first,
        "view": view,
    }


def assert_snapshot(book, selected, read, blocks):
    actual = book.app.ledger.read_financial_projection(selected, read, blocks).value
    normalized = normalize(selected.scope, read)
    classic = Selection(
        read.repository("ledger"),
        normalized["view"],
        normalized["period_to"],
        read.start_period,
        Issues(),
    )
    expected = calculate_snapshot(normalized, read, classic)
    # This includes the unchanged FrozenPreTax payload, order and digest.
    assert {key: value for key, value in actual.items() if key != "selection"} == expected
    assert actual == book.app.ledger.financial_snapshot(selected.scope, read)


@pytest.mark.parametrize("view", ("current", "as_posted"))
@pytest.mark.parametrize("initial_kind", ("summary_values", "snapshot"))
def test_forward_backward_and_new_first_or_cash_use_actual_boundary_chains(
    book, monkeypatch, view, initial_kind
):
    january = support.expense("discovery-january", "5")
    february = support.expense("discovery-february", "7", "2026-02")
    april = support.expense("discovery-april", "11", "2026-04")
    december = support.expense("discovery-december", "13", "2026-12")
    book.submit(support.opening(), january, february, april, december)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        initial = request(initial_kind, scope(book, "2026-01", view=view), read)
        blocks.publish(book.app.ledger.prepare_financial_projection((initial,), (), (), read))
        original = book.app.ledger.read_financial_projection(initial, read, blocks)
        # Q2 changes first and, when the initial request was a Summary, cash.
        # A later Q1 request also shrinks the stock cutoff and YTD flow.
        other_view = "current" if view == "as_posted" else "as_posted"
        for first, last, selected_view in (
            ("2026-04", "2026-06", view),
            ("2026-01", "2026-03", view),
            ("2026-04", "2026-06", other_view),
        ):
            selected = request("snapshot", scope(book, first, last, selected_view), read)
            repo = read.repository("ledger")
            query, entries = repo.financial_projection_chains, Tree.entries
            identities = []

            def exact_chains(keys=None, _query=query, _identities=identities):
                assert keys is not None, "new range read the complete company adoption catalog"
                _identities.extend(keys)
                rows = _query(keys)
                assert all("basis_json" not in row and "target_json" not in row for row in rows)
                return rows

            def no_catalog_scan(tree, root, _entries=entries, **options):
                assert tree.domain != "financial-adoption-chains/1"
                return _entries(tree, root, **options)

            with monkeypatch.context() as patch:
                patch.setattr(repo, "financial_projection_chains", exact_chains)
                patch.setattr(Tree, "entries", no_catalog_scan)
                plan = book.app.ledger.prepare_financial_projection(
                    (selected,), (), (), read, base=blocks
                )
            assert (december["business_id"], "main") not in identities
            if first == "2026-04":
                assert (april["business_id"], "main") in identities
            blocks.publish(plan)
            cold = Model(read)
            cold.advance((initial, selected), ())
            assert Model(read, blocks).descriptor(selected) == cold.descriptor(selected)
            assert book.app.ledger.read_financial_projection(initial, read, blocks) == original
            assert_snapshot(book, selected, read, blocks)


def test_new_quarter_candidates_keep_complete_twice_frozen_baseline_chain(book, monkeypatch):
    cost = support.expense("discovery-closed-cost", "100")
    book.submit(support.opening(), cost)
    close(book, "2026-01")
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        first = request("snapshot", scope(book, "2026-01", "2026-03"), read)
        blocks.publish(book.app.ledger.prepare_financial_projection((first,), (), (), read))
    book.replace(corrected(cost, "120"), correction_period="2026-02")
    close(book, "2026-02")
    book.replace(corrected(cost, "80"), seq=2, correction_period="2026-03")
    book.submit(support.expense("discovery-next-quarter", "3", "2026-04"))
    with book.app.storage.read_unit(book.company) as read:
        second = request("snapshot", scope(book, "2026-04", "2026-06"), read)
        repo, observed = read.repository("ledger"), []
        query = repo.financial_projection_chains

        def chains(identities=None):
            assert identities is not None
            rows = query(identities)
            observed.extend(
                row["posting_period"] for row in rows if row["business_id"] == cost["business_id"]
            )
            return rows

        with monkeypatch.context() as patch:
            patch.setattr(repo, "financial_projection_chains", chains)
            plan = book.app.ledger.prepare_financial_projection(
                (second,),
                # All intermediate adoptions are discovered from this identity;
                # a single merged pending entry never substitutes just the latest head.
                (
                    FormalChange(
                        cost["business_id"],
                        "main",
                        ("2026-01",),
                        ("2026-02", "2026-03"),
                        ("operations.expense",),
                    ),
                ),
                (),
                read,
                base=blocks,
            )
        assert sorted(observed) == ["2026-01", "2026-02", "2026-03"]
        blocks.publish(plan)
        cold = Model(read)
        cold.advance((first, second), ())
        assert Model(read, blocks).header == cold.header
        assert_snapshot(book, first, read, blocks)
        assert_snapshot(book, second, read, blocks)


@pytest.mark.parametrize("view", ("current", "as_posted"))
def test_open_targets_move_in_both_directions_and_physically_retire(book, view):
    future = support.expense("discovery-moving", "17", "2026-08")
    book.submit(support.opening(), future)
    blocks, requests = Blocks(), []
    with book.app.storage.read_unit(book.company) as read:
        selected = request("summary_values", scope(book, "2026-01", view=view), read)
        requests.append(selected)
        blocks.publish(book.app.ledger.prepare_financial_projection(requests, (), (), read))
        unchanged = book.app.ledger.read_financial_projection(selected, read, blocks)
    for seq, month in ((1, "2026-09"), (2, "2026-02")):
        changed = deepcopy(future)
        changed["period"] = month
        book.replace(changed, seq=seq)
        with book.app.storage.read_unit(book.company) as read:
            selected = request("snapshot", scope(book, "2026-01", "2026-03", view), read)
            if selected not in requests:
                requests.append(selected)
            model = Model(read, blocks)
            roots = model.advance(
                requests, (SimpleNamespace(business_id=future["business_id"], result_slot="main"),)
            )
            blocks.publish(model.plan(roots))
            cold = Model(read)
            cold.advance(requests, ())
            assert model.header == cold.header
            assert book.app.ledger.read_financial_projection(requests[0], read, blocks) == unchanged
            assert_snapshot(book, selected, read, blocks)
    book.submit(
        mutations=[{"op": "withdraw", "business_id": future["business_id"], "expected_seq": 3}]
    )
    with book.app.storage.read_unit(book.company) as read:
        assert (
            read.repository("ledger").financial_projection_chains(
                ((future["business_id"], "main"),)
            )
            == []
        )
        model = Model(read, blocks)
        roots = model.advance(
            (), (SimpleNamespace(business_id=future["business_id"], result_slot="main"),)
        )
        blocks.publish(model.plan(roots))
        cold = Model(read)
        cold.advance(requests, ())
        assert model.header == cold.header
        assert_snapshot(book, requests[-1], read, blocks)
    book.submit(mutations=[{"op": "restore", "expected_seq": 4, "fact": changed}])
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read, blocks)
        roots = model.advance(
            (), (SimpleNamespace(business_id=future["business_id"], result_slot="main"),)
        )
        blocks.publish(model.plan(roots))
        cold = Model(read)
        cold.advance(requests, ())
        assert model.header == cold.header
        assert_snapshot(book, requests[-1], read, blocks)


def test_as_posted_stock_does_not_add_an_economic_cutoff_to_the_original_selection():
    # No posting>=business assumption is needed: this target is already selected
    # at P, so the same target remains in a root extended from P to Q.
    head = {
        "business_kind": "operations.expense",
        "posting_period": "2026-01",
        "business_period": "2026-07",
        "is_current": 1,
    }
    assert stock((head,), "as_posted", "2026-01") is head
    assert stock((head,), "as_posted", "2026-07") is head
    assert stock((head,), "current", "2026-01") is None
