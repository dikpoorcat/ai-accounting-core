"""The shared storage codec does not change real Ledger adoption or frozen history."""

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger.public import FormalChange
from ai_accounting_modular.storage import _immutable_tree as tree_module
from ai_accounting_modular.storage import _immutable_tree_store as module
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_candidates import assert_snapshot
from tests.modular.ledger.test_financial_projection_model import Blocks, scope
from tests.modular.ledger.test_financial_projection_ranges import close, corrected


@pytest.fixture(autouse=True)
def codec(monkeypatch):
    monkeypatch.setattr(module, "_structure_cache", module._StructureCache())


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("frozen", [False, True], ids=["open", "closed"])
def test_real_replacement_keeps_only_actual_sources_and_same_full_statements(
    tmp_path, monkeypatch, mode, frozen
):
    monkeypatch.setattr(support, "ledger_repository", lambda: repository_factory(mode))
    book, blocks = support.assemble(tmp_path), Blocks()
    cost = support.expense("codec-replacement", "7")
    book.submit(support.opening(cash="1000"), cost)
    selected_scope = scope(book) | {"period_to": "2026-03", "view": "current"}
    with book.app.storage.read_unit(book.company) as read:
        request = book.app.ledger.financial_projection_request("snapshot", selected_scope, read)
        model = Model(read)
        previous = model.advance((request,), ())
        old_atom = next(
            head["atom"]
            for chain in model._old_chains().values()
            for head in chain
            if head["business_id"] == cost["business_id"]
        )
        old_body = model.bodies.get_many((old_atom,))[0]
        blocks.publish(model.plan(previous))
    if frozen:
        close(book, "2026-01")
        with book.app.storage.read_unit(book.company) as read:
            report = book.app.ledger.get_closed_report(scope(book), read)
            saved = canonical_json({key: value for key, value in report.items() if key != "head"})
    book.replace(corrected(cost, "9"), correction_period="2026-02" if frozen else None)
    with book.app.storage.read_unit(book.company) as read:
        change = FormalChange(
            cost["business_id"],
            "main",
            ("2026-01",),
            ("2026-02" if frozen else "2026-01",),
            ("operations.expense",),
        )
        blocks.publish(
            book.app.ledger.prepare_financial_projection(
                (request,), (change,), (), read, base=blocks
            )
        )
        current = Model(read, blocks)
        if frozen:
            assert current.bodies.get_many((old_atom,))[0] == old_body
            report = book.app.ledger.get_closed_report(scope(book), read)
            # Only the envelope's live read head advances; all three statements
            # and the complete frozen tax basis retain their actual bytes.
            assert (
                canonical_json({key: value for key, value in report.items() if key != "head"})
                == saved
            )
        else:
            with pytest.raises(ContractError):
                current.bodies.get_many((old_atom,))
        assert_snapshot(book, request, read, blocks)
        cached = book.app.ledger.read_financial_projection(request, read, blocks).value
        assert cached["profit_statement"]["32"]["current"]["amount_fen"] == "-9"
        used = [
            value
            for value in book.app.ledger.financial_summary(request.scope, read).sources
            if value["business_id"] == cost["business_id"]
        ]
        assert len(used) == 1 and used[0] != old_body["source"]
        with monkeypatch.context() as patch:
            patch.setattr(module, "_checked_structure", tree_module._CheckedStructure)
            assert book.app.ledger.read_financial_projection(request, read, blocks).value == cached
