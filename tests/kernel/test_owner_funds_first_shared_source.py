"""First account consumes one money source while preserving the complete company proof."""

import sqlite3
from copy import deepcopy

import pytest
import test_banking as banking
import test_investments as investments
from stage9_metrics import measure_work
from test_dashboard_funds_alignment import _publish_filter_funding
from test_integrity_content import damage
from test_opening_continuation import book as _opening_book

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead, funds
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.storage import _active_fact_reads

bank_book = banking.book
opening_book = _opening_book
MONTH = "2026-09"


def old_first(dashboard, monkeypatch, *, period=MONTH, limit=2):
    """The original wide SQL and Python first selection remain the independent control."""
    with monkeypatch.context() as patch:
        patch.setattr(FundsRead, "_can_share_first_page", lambda self: False)
        return dashboard.funds(period, movement_account_selection="first", limit=limit)


def source_work(work):
    return [row for row in work["sql"]
            if "transfers AS (SELECT event_id" in row["statement"]]


def test_real_first_keeps_complete_output_and_builds_money_source_once(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-b", "bank-a"] * 24)
    board = Dashboard(engine)
    before, expected = measure_work(engine, lambda: old_first(board, monkeypatch))
    after, actual = measure_work(
        engine, lambda: board.funds(MONTH, movement_account_selection="first", limit=2)
    )
    assert actual == expected
    assert sum(row["calls"] for row in source_work(before)) == 2
    assert sum(row["calls"] for row in source_work(after)) == 1
    assert after["counters"]["sqlite_vm_steps"] < before["counters"]["sqlite_vm_steps"]
    assert sum(row["returned_rows"] for row in source_work(after)) < sum(
        row["returned_rows"] for row in source_work(before)
    )
    assert after["counters"]["calculation_result_rows_loaded"] == (
        before["counters"]["calculation_result_rows_loaded"]
    )
    selected = actual["data"]["selected_movement_account"]
    assert selected == {"type": "bank", "account_id": "bank-a"}
    page = actual["data"]["collections"]["movements"]["page"]
    assert page["total_count"] == 48 and page["filtered_count"] == 24
    following = board.funds(
        MONTH, movement_account_type="bank", movement_account_id="bank-a", section="movements",
        cursor=page["next_cursor"], expected_version=actual["snapshot_version"], limit=500,
    )["data"]
    assert len(following["collections"]["movements"]["items"]) == 22
    assert not following["collections"]["movements"]["page"]["has_more"]


@pytest.mark.parametrize("kind", ["zero", "empty", "case_and_prefix"])
def test_first_respects_zero_accounts_empty_source_and_exact_sort(bank_book, monkeypatch, kind):
    engine, save, publish, _ = bank_book
    if kind == "zero":
        banking.opening(save, publish, bank="bank-a")
        _publish_filter_funding(engine, ["bank-b"])
    elif kind == "empty":
        save("bank_opening", "unpublished", {
            "period": MONTH, "bank_account_id": "draft-bank", "opening_fen": 0,
            "basis": "new_account",
        })
    else:
        _publish_filter_funding(engine, ["bank-a", "bank-A", "bank-A.1"])
    board = Dashboard(engine)
    actual = board.funds(MONTH, movement_account_selection="first", limit=2)
    assert actual == old_first(board, monkeypatch)
    if kind == "zero":
        assert actual["data"]["selected_movement_account"]["account_id"] == "bank-a"
        assert actual["data"]["collections"]["movements"]["items"] == []
    elif kind == "empty":
        assert actual["data"]["selected_movement_account"] is None
    else:
        assert actual["data"]["selected_movement_account"]["account_id"] == "bank-A"


def test_first_keeps_transfer_scope_negative_payment_and_refund(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=1000)
    save("funds_transfer", "transfer", {
        "period": MONTH, "actual_date": MONTH + "-02", "amount_fen": 200,
        "source_bank_account_id": "bank-a", "destination_bank_account_id": "bank-b",
    })
    for kind, subject, amount in (
        ("managed_reserve_expense", "paid", 300),
        ("managed_reserve_refund", "refund", 100),
    ):
        save(kind, subject, {
            "period": MONTH, "actual_date": MONTH + "-03", "amount_fen": amount,
            "bank_account_id": "bank-a",
        })
    save("cash_funding", "cash", {
        "period": MONTH, "actual_date": MONTH + "-01", "owner_id": "owner",
        "cash_account_id": "cash", "funding_kind": "capital", "amount_fen": 50,
    })
    publish("transfer", "paid", "refund", "cash")
    board = Dashboard(engine)
    actual = board.funds(MONTH, movement_account_selection="first", limit=500)
    assert actual == old_first(board, monkeypatch, limit=500)
    assert actual["data"]["internal_transfer_fen"] == 200
    assert actual["data"]["inflow_fen"] == 1150 and actual["data"]["outflow_fen"] == 300
    assert {row["signed_amount_fen"] for row in
            actual["data"]["collections"]["movements"]["items"]} == {1000, -200, -300, 100}


def test_closed_receipt_and_current_reversal_keep_original_sources(bank_book, monkeypatch):
    engine, save, publish, proof = bank_book
    fields = dict(period=MONTH, actual_date=MONTH + "-03", cash_account_id="cash", amount_fen=1000)
    save("managed_reserve_refund", "receipt", fields)
    publish("receipt")
    investments.close(engine, MONTH)
    board = Dashboard(engine)
    assert board.funds(MONTH, movement_account_selection="first") == old_first(
        board, monkeypatch, limit=20,
    )
    engine.amend_fact("managed_reserve_refund", "receipt", fields | {"amount_fen": 700},
                      evidence=(proof,), expected_revision=1, recording_error_confirmed=True,
                      request_id="amend-receipt")
    preview = engine.preview(["receipt"], posting_period="2026-10")
    engine.confirm(["receipt"], posting_period="2026-10", preview_digest=preview["digest"],
                   epochs=preview["epochs"], request_id="correct-receipt")
    actual = board.funds("2026-10", movement_account_selection="first", limit=500)
    assert actual == old_first(board, monkeypatch, period="2026-10", limit=500)
    assert actual["data"]["inflow_fen"] == -300 and actual["data"]["outflow_fen"] == 0
    assert any(row["correction"] and row["signed_amount_fen"] == -1000
               for row in actual["data"]["collections"]["movements"]["items"])


def test_first_preserves_unknown_opening_identity_and_amounts(opening_book, monkeypatch):
    engine, _, _, package, _ = opening_book
    package([
        ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 100}),
        ("opening_equity", "equity", {
            "equity_kind": "paid_in_capital", "balance_fen": 100, "holder_or_basis_id": "owner",
        }),
    ])
    original = BusinessQueries._selected_accounting

    def uncertain(*args, **kwargs):
        selected = deepcopy(original(*args, **kwargs))
        through = selected["through_period"]
        candidate = next(row for row in through["state_results"]
                         if row["kind"] == "opening_package")
        through["state_results"].remove(candidate)
        through["unestablished_state_selections"].append({
            "reason": "state_selection_unavailable", "candidates": [candidate],
        })
        return selected

    monkeypatch.setattr(BusinessQueries, "_selected_accounting", uncertain)
    board = Dashboard(engine)
    actual = board.funds("2026-01", movement_account_selection="first", limit=2)
    assert actual == old_first(board, monkeypatch, period="2026-01")
    assert actual["data"]["total_fen"] is None
    assert actual["data"]["selected_movement_account"] == {"type": "cash", "account_id": "cash"}


def test_identity_correction_keeps_original_two_phase_selection(bank_book, monkeypatch):
    engine, save, publish, proof = bank_book
    banking.opening(save, publish)
    banking.opening(save, publish, bank="zero-bank")
    banking.funding(save, publish)
    with engine.store.connection(read_only=True) as connection:
        data = engine.store.current_fact(connection, "funding").fact.model_dump(mode="json")
    command = IdentityCorrections(engine)
    options = dict(changes=[dict(subject_id="funding", expected_revision=1, action="reassign",
                                data=data | {"bank_account_id": "zero-bank"})], evidence=[proof],
                   reason="explicit synthetic bank correction")
    preview = command.preview_identity_correction(**options)
    command.confirm_identity_correction(**options, preview_digest=preview["digest"],
                                        epochs=preview["epochs"], request_id="correct-bank")
    board = Dashboard(engine)
    with board._snapshot(MONTH) as snap:
        assert not FundsRead(snap)._can_share_first_page()
    assert board.funds(MONTH, movement_account_selection="first", limit=2) == old_first(
        board, monkeypatch,
    )


@pytest.mark.parametrize("guard", ["unowned", "v1_context", "v1_registry"])
def test_first_bridge_requires_owned_current_context(bank_book, guard, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        read = FundsRead(snap)
        assert read._can_share_first_page()
        if guard == "unowned":
            token = _active_fact_reads.set(None)
            try:
                assert not read._can_share_first_page()
            finally:
                _active_fact_reads.reset(token)
        elif guard == "v1_context":
            with historical_content(1):
                assert not read._can_share_first_page()
        else:
            with monkeypatch.context() as patch:
                patch.setattr(snap.store.registry, "content_version", 1, raising=False)
                assert not read._can_share_first_page()


def test_first_rejects_damaged_other_account_source(bank_book):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a", "bank-b"])
    damage(engine, "calculation", "UPDATE calculation SET "
           "outcome=json_set(outcome,'$.values.amount_fen',NULL) WHERE subject_id=?",
           ("filter-2026-09-0001",))
    with pytest.raises(KernelError) as rejected:
        Dashboard(engine).funds(MONTH, movement_account_selection="first", limit=1)
    assert rejected.value.code == "content_integrity_failed"


def test_combined_summary_keeps_sqlite_integer_overflow(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    # Private consumer arithmetic probe only; does not attest these synthetic rows as sources.
    source = (
        "SELECT 'bank' category,'bank-a' balance_key,1 sign,"
        "9223372036854775807 signed_amount,0 internal_transfer,NULL actual_date,'a' page_key "
        "UNION ALL SELECT 'bank','bank-a',1,1,0,NULL,'b'"
    )
    monkeypatch.setattr(FundsRead, "movements", lambda self: (source, []))
    with Dashboard(engine)._snapshot(MONTH) as snap:
        with pytest.raises(sqlite3.OperationalError, match="integer overflow"):
            FundsRead(snap).account_summary(first_page_request={"after": None, "limit": 2})


@pytest.mark.parametrize("scope", ["all", "filtered", "accounts_section", "brief_amounts"])
def test_other_consumers_do_not_enter_first_bridge_or_add_work(bank_book, monkeypatch, scope):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-b", "bank-a"] * 3)
    board = Dashboard(engine)

    def operation():
        if scope == "brief_amounts":
            with board._snapshot(MONTH) as snap:
                snap.month_journal.account_amounts()
                return funds(snap, summary_only=True)
        options = {"period": MONTH, "limit": 2}
        if scope == "filtered":
            options |= {"movement_account_type": "bank", "movement_account_id": "bank-a"}
        elif scope == "accounts_section":
            options["section"] = "accounts"
        return board.funds(**options)

    baseline_work, baseline = measure_work(engine, operation)

    def reject_unneeded_guard(self):
        raise AssertionError("this consumer does not need a first-account guard")

    monkeypatch.setattr(FundsRead, "_can_share_first_page", reject_unneeded_guard)
    actual_work, actual = measure_work(engine, operation)
    assert actual == baseline
    for field in ("sql_calls", "sqlite_vm_steps", "returned_rows", "returned_value_bytes",
                  "calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert actual_work["counters"][field] == baseline_work["counters"][field]
