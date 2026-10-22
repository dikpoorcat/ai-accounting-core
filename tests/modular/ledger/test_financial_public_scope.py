"""Public reports consume their exact roles, not a temporary company-wide producer."""

from dataclasses import asdict

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger import _financial_projection_bodies as bodies_module
from ai_accounting_modular.ledger._financial import normalize
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot, summary_values
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_selection import Selection
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_model import Blocks, scope
from tests.modular.ledger.test_financial_projection_ranges import close, corrected


@pytest.fixture(params=MODES)
def book(request, tmp_path, monkeypatch, start_period):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)
    return support.assemble(tmp_path, start_period)


@pytest.fixture
def start_period():
    return "2026-01"


def requests(book, selected_scope, read):
    return tuple(
        book.app.ledger.financial_projection_request(kind, selected_scope, read)
        for kind in ("summary_values", "snapshot")
    )


def original_sources(selection):
    return tuple(
        sorted(
            (selection.source(selection.heads[key]) for key in tuple(selection.used)),
            key=lambda value: (value["business_id"], value["result_slot"], value["result_id"]),
        )
    )


def assert_reports(book, selected_scope, read, blocks, selected):
    ledger = book.app.ledger
    summary = ledger.financial_summary(selected_scope, read)
    snapshot = ledger.financial_snapshot(selected_scope, read)
    small, full = (ledger.read_financial_projection(value, read, blocks) for value in selected)
    assert summary.version == small.version
    assert summary.scope == small.value.scope
    assert {key: value for key, value in asdict(summary).items() if key not in {"sources"}} == {
        "version": small.version,
        "scope": small.value.scope,
        **small.value.values,
        "issues": small.value.issues,
    }
    assert snapshot == full.value
    normalized = normalize(selected_scope, read)

    def classic():
        return Selection(
            read.repository("ledger"),
            normalized["view"],
            normalized["period_to"],
            read.start_period,
            Issues(),
        )

    model = Model(read, blocks)
    expected = model.saved(selected[1])
    if expected is not None:
        assert snapshot == expected
    else:
        assert {key: value for key, value in snapshot.items() if key != "selection"} == (
            calculate_snapshot(normalized, read, classic())
        )
    selection = classic()
    values = summary_values(normalized, selection, saved=model.saved(selected[0]))
    assert {name: getattr(summary, name) for name in values} == values
    assert summary.issues == tuple(selection.issues.values)
    assert summary.sources == original_sources(selection)
    return summary, snapshot


def observe_bodies(monkeypatch):
    original, captured = bodies_module.capture_bodies, []

    def capture(selection, heads):
        heads = tuple(heads)
        captured.extend(head["result_id"] for head in heads)
        return original(selection, heads)

    monkeypatch.setattr(bodies_module, "capture_bodies", capture)
    return captured


@pytest.mark.parametrize("view", ("current", "as_posted"))
def test_future_company_growth_does_not_expand_public_body_capture(book, monkeypatch, view):
    cost = support.expense("public-january", "7")
    book.submit(support.opening(cash="1000"), cost)
    selected_scope = scope(book, view=view)
    with book.app.storage.read_unit(book.company) as read:
        before = (
            asdict(book.app.ledger.financial_summary(selected_scope, read)),
            book.app.ledger.financial_snapshot(selected_scope, read),
        )
    future = [support.expense(f"public-future-{i}", "1", "2026-12") for i in range(32)]
    book.submit(*future)
    with book.app.storage.read_unit(book.company) as read:
        selected, blocks = requests(book, selected_scope, read), Blocks()
        blocks.publish(book.app.ledger.prepare_financial_projection(selected, (), (), read))
        repo = read.repository("ledger")
        future_ids = {
            row["result_id"] for row in repo.current_heads([fact["business_id"] for fact in future])
        }
        captured = observe_bodies(monkeypatch)
        summary, snapshot = assert_reports(book, selected_scope, read, blocks, selected)
        assert not set(captured) & future_ids
        assert asdict(summary) == before[0]
        assert snapshot == before[1] | {"head": read.head}


def test_frozen_and_current_ranges_capture_only_nonzero_selected_legs(book, monkeypatch):
    cost = support.expense("public-closed", "100")
    book.submit(support.opening(cash="1000"), cost)
    close(book, "2026-01")
    with book.app.storage.read_unit(book.company) as read:
        saved = book.app.ledger.get_closed_report(scope(book), read)
    book.replace(corrected(cost, "120"), correction_period="2026-02")
    close(book, "2026-02")
    book.replace(corrected(cost, "80"), seq=2, correction_period="2026-03")
    book.submit(support.expense("public-unrelated-year", "1", "2027-01"))
    with book.app.storage.read_unit(book.company) as read:
        heads = read.repository("ledger").financial_projection_chains(
            ((cost["business_id"], "main"),)
        )
        legs = {head["posting_period"]: head["result_id"] for head in heads}
        cases = (
            (scope(book), {legs["2026-01"]}),
            (scope(book, "2026-02"), {legs["2026-01"], legs["2026-02"]}),
            (scope(book) | {"period_to": "2026-03"}, {legs["2026-03"]}),
            (scope(book, view="current"), {legs["2026-03"]}),
        )
        for selected_scope, needed in cases:
            selected, blocks = requests(book, selected_scope, read), Blocks()
            blocks.publish(book.app.ledger.prepare_financial_projection(selected, (), (), read))
            with monkeypatch.context() as patched:
                captured = observe_bodies(patched)
                assert_reports(book, selected_scope, read, blocks, selected)
            assert set(captured) <= needed
        with monkeypatch.context() as patched:
            captured = observe_bodies(patched)
            exact = book.app.ledger.financial_snapshot(scope(book), read)
        assert captured == []
        assert exact == saved | {"head": read.head}


@pytest.mark.parametrize(
    ("boundary", "start_period"),
    (("missing", "2026-01"), ("zero", "2026-01"), ("continuation", "2026-05")),
)
def test_unknown_zero_and_continuation_boundaries_keep_classic_meaning(
    book, boundary, start_period
):
    if boundary != "missing":
        initial = support.opening(
            month=start_period, cash="500000" if boundary == "continuation" else "0"
        )
        book.submit(
            initial,
            *([support.continuation(initial, may=True)] if boundary == "continuation" else []),
        )
    book.submit(support.expense("public-unknown", "19", start_period, details=False))
    for view in ("current", "as_posted"):
        with book.app.storage.read_unit(book.company) as read:
            selected_scope = scope(book, start_period, view=view)
            selected, blocks = requests(book, selected_scope, read), Blocks()
            blocks.publish(book.app.ledger.prepare_financial_projection(selected, (), (), read))
            assert_reports(book, selected_scope, read, blocks, selected)


def test_corrected_opening_uses_the_actual_current_or_frozen_boundary(book):
    initial = support.opening(cash="1000")
    book.submit(initial, support.expense("public-boundary-cost", "7"))
    close(book, "2026-01")
    book.replace(support.opening(cash="2000"), correction_period="2026-02")
    for view, bank in (("current", "2000"), ("as_posted", "1000")):
        with book.app.storage.read_unit(book.company) as read:
            selected_scope = scope(book, view=view)
            selected, blocks = requests(book, selected_scope, read), Blocks()
            blocks.publish(book.app.ledger.prepare_financial_projection(selected, (), (), read))
            summary, snapshot = assert_reports(book, selected_scope, read, blocks, selected)
            assert summary.bank["amount_fen"] == bank
            assert snapshot["balance_sheet"]["1"]["ending"]["amount_fen"] == bank


def test_posting_before_economic_month_keeps_each_actual_selection(book):
    cost = support.expense("public-moved-closed", "100")
    book.submit(support.opening(cash="1000"), cost)
    close(book, "2026-01")
    changed = corrected(cost, "80") | {"period": "2026-07"}
    book.replace(changed, correction_period="2026-02")
    with book.app.storage.read_unit(book.company) as read:
        (head,) = read.repository("ledger").current_heads([cost["business_id"]])
        assert head["business_period"] == "2026-07" and head["posting_period"] == "2026-02"
        for month, view, expense in (
            ("2026-01", "as_posted", "100"),
            ("2026-02", "as_posted", "-20"),
            ("2026-02", "current", "0"),
            ("2026-07", "current", "80"),
        ):
            selected_scope = scope(book, month, view=view)
            selected, blocks = requests(book, selected_scope, read), Blocks()
            blocks.publish(book.app.ledger.prepare_financial_projection(selected, (), (), read))
            summary, _ = assert_reports(book, selected_scope, read, blocks, selected)
            assert summary.expense["amount_fen"] == expense


def test_missing_actual_flow_baseline_is_not_replaced_with_zero(book, monkeypatch):
    cost = support.expense("public-baseline", "100")
    book.submit(support.opening(cash="1000"), cost)
    close(book, "2026-01")
    book.replace(corrected(cost, "120"), correction_period="2026-02")
    with book.app.storage.read_unit(book.company) as read:
        repo = read.repository("ledger")
        result, requested = repo.financial_results, []
        with monkeypatch.context() as observed:
            captured = observe_bodies(observed)
            positive = book.app.ledger.financial_summary(scope(book, "2026-02"), read)
        assert positive.expense["amount_fen"] == "20"
        assert set(captured) == {
            source["result_id"]
            for source in positive.sources
            if source["business_id"] == cost["business_id"]
        }
        assert len(captured) == 2

        def missing(ids, *, metadata_only=False):
            if metadata_only:
                requested.extend(ids)
                return []
            return result(ids)

        monkeypatch.setattr(repo, "financial_results", missing)
        with pytest.raises(ContractError) as error:
            book.app.ledger.financial_summary(scope(book, "2026-02"), read)
        assert error.value.code == "storage_corrupt" and len(requested) == 1


def scope_work(read, view):
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1
        return 0

    read._connection.set_progress_handler(progress, 1)
    try:
        rows = read.repository("ledger").financial_heads("2026-01", view=view, metadata_only=True)
    finally:
        read._connection.set_progress_handler(None, 0)
    return rows, instructions


@pytest.mark.parametrize("view", ("current", "as_posted"))
def test_scope_lookup_does_not_scan_unrelated_future_heads(book, view):
    book.submit(support.opening(), support.expense("public-index-january", "1"))
    with book.app.storage.read_unit(book.company) as read:
        before, initial = scope_work(read, view)
    book.submit(*(support.expense(f"public-index-future-{i}", "1", "2027-01") for i in range(64)))
    with book.app.storage.read_unit(book.company) as read:
        after, grown = scope_work(read, view)
    assert after == before
    # SQL work, not wall time: extra future rows must not become a table scan.
    assert grown <= initial + 64
