"""Sparse persisted meanings come from real formal journals and the same owner formulas."""

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_bodies import capture_bodies, source_atom
from ai_accounting_modular.ledger._financial_projection_fold import Notice
from ai_accounting_modular.ledger._financial_selection import Selection


@pytest.mark.parametrize("details", [True, False])
def test_real_expense_payment_keeps_sparse_formal_amounts_and_missing_meanings(tmp_path, details):
    book = support.assemble(tmp_path)
    expense = support.expense("expense", "500", details=details)
    payment = support.payment("paid", expense, "200")
    book.submit(support.opening(cash="1000"), expense, payment)
    with book.app.storage.read_unit(book.company) as read:
        selection = Selection(read.repository("ledger"), "current", "2026-01", "2026-01", Issues())
        bodies = capture_bodies(selection, tuple(selection.stock("2026-01").values()))
        assert len(bodies) == 2
        assert all(source_atom(body["source"]) == key for key, body in bodies.items())
        by_business = {body["source"]["business_id"]: body for body in bodies.values()}
        bill, paid = by_business[expense["business_id"]], by_business[payment["business_id"]]
        assert bill["profit"]["14"] == 500
        assert bill["pretax"] == [0, 0, -500]
        assert bill["cash"] == []
        assert paid["profit"] == {} and paid["pretax"] is None
        assert sum(part[1] for movement in paid["cash"] for part in movement) == 200
        # Explicit management_other needs no supplemental row; its absence instead
        # creates a real unknown-detail contribution in the formal result.
        assert bill["has_report_details"] is (not details)
        assert bool(bill["notices"]) is (not details)
        for note in bill["notices"]:
            assert Notice.from_record(note).field.endswith("/@column")
        # No full Target, adopted Basis, source document, or 32 zero cells is copied.
        assert not {"target", "basis", "report_details", "evidence_ids"} & bill.keys()
        assert len(bill["profit"]) < 32


def test_empty_formal_selection_has_no_fake_source_or_contribution(tmp_path):
    book = support.assemble(tmp_path)
    with book.app.storage.read_unit(book.company) as read:
        selection = Selection(read.repository("ledger"), "current", "2026-01", "2026-01", Issues())
        assert capture_bodies(selection, ()) == {}
