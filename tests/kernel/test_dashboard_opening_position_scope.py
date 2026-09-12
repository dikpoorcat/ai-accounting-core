"""Bank continuity controls and true opening adoption have separate consumers."""

from pathlib import Path

import pytest
import test_banking as banking
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as _opening_book

import ai_accounting
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard

opening_book = _opening_book


@pytest.mark.parametrize("opening_kind", ["opening_cash", "opening_bank"])
def test_bank_control_does_not_gate_position_but_true_opening_still_does(
    opening_book, opening_kind
):
    candidate = Path(__file__).resolve().parents[2]
    assert Path(ai_accounting.__file__).resolve().is_relative_to(candidate / "src")
    engine, save, publish, package, proof = opening_book
    fields = (
        {"cash_account_id": "cash", "balance_fen": 100}
        if opening_kind == "opening_cash"
        else {"bank_account_id": "bank-a", "balance_fen": 100}
    )
    package(
        [
            (opening_kind, "real-opening", fields),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 100,
                    "holder_or_basis_id": "owner",
                },
            ),
        ]
    )

    def bank_save(kind, subject, data, revision=0):
        return save(kind, subject, data, revision=revision)

    initial = 100 if opening_kind == "opening_bank" else 0
    banking.opening(
        bank_save,
        publish,
        month="2026-01",
        amount=initial,
        basis="existing_ledger" if initial else "new_account",
    )
    dashboard = Dashboard(engine)
    missing = dashboard.brief("2026-01")["data"]
    assert missing["cash"]["missing_account_count"] == 1
    position = missing["position"]
    assert position["assets_fen"] == 100
    assert position["complete"] and position["equation_valid"] is True

    # Neither a missing statement nor an unpublished reconciliation gates the
    # exact position; these facts do not add accounting amounts.
    banking.statement(bank_save, publish, [], month="2026-01", initial=initial)
    banking.reconciliation(bank_save, publish, [], month="2026-01", posted=False)
    waiting = dashboard.brief("2026-01")["data"]
    assert waiting["cash"]["missing_account_count"] == 0
    assert waiting["position"] == position
    publish("reconciliation")
    assert dashboard.brief("2026-01")["data"]["position"] == position
    _close_without_current_business(engine, "2026-01", proof)

    selected = BusinessQueries(engine).business_status(
        "opening-bank-a", "2026-01", as_of="2026-02-01"
    )["selected_accounting"]["through_period"]
    assert selected["state_results"] == []
    unresolved = selected["unestablished_state_selections"]
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "manifest_state_adoption_not_proven"
    assert {item["kind"] for item in unresolved[0]["candidates"]} == {"bank_opening"}
    assert all(item["trace_only"] for item in unresolved[0]["candidates"])

    historical = dashboard.brief("2026-01")["data"]["position"]
    if opening_kind == "opening_cash":
        assert historical == position
    else:
        # A true opening-bank detail is also a no-entry/opening=False result,
        # but losing its independent adoption must remain conservative. The
        # published bank control uses that exact detail as a dependency.
        opening = BusinessQueries(engine).business_status(
            "real-opening", "2026-01", as_of="2026-02-01"
        )["selected_accounting"]["through_period"]
        assert opening["state_results"] == []
        assert opening["unestablished_state_selections"]
        assert historical["complete"] is False
        assert historical["assets_fen"] is None
        assert historical["liabilities_fen"] is None
        assert historical["equation_valid"] is None
        assert historical["other_assets_fen"] is None
        assert any(item["field"] == "opening.selection" for item in historical["issues"])
