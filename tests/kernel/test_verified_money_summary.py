"""Summary consumption preserves proved money semantics without page assembly."""

import copy
import sqlite3
import tracemalloc

import pytest
import test_banking as banking
import test_platforms as platforms
from test_funds_summary_page import _steps

from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.storage import _active_fact_reads

bank_book = banking.book
platform_book = platforms.book
MONTH = "2026-09"


def _summary(read):
    totals = read.account_summary()
    return totals, copy.deepcopy(read.account_rows)


def _original_summary(snap, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(FundsRead, "_verified_money_summary", lambda self: None)
        return _summary(FundsRead(snap))


def test_summary_preserves_three_channels_transfers_zero_accounts_and_work(
    platform_book, monkeypatch,
):
    engine, save, publish, _ = platform_book
    banking.funding(save, publish, amount=1000)
    banking.opening(save, publish)
    save("cash_funding", "cash", {
        "period": MONTH, "actual_date": MONTH + "-01", "owner_id": "owner",
        "amount_fen": 700, "funding_kind": "capital", "cash_account_id": "cash",
    })
    save("platform_funding", "platform", platforms.funding_data(amount=300))
    save("funds_transfer", "banks", {
        "period": MONTH, "actual_date": MONTH + "-02", "amount_fen": 100,
        "source_bank_account_id": "bank-a", "destination_bank_account_id": "bank-b",
    })
    save("cash_bank_transfer", "cash-transfer", {
        "period": MONTH, "actual_date": MONTH + "-02", "amount_fen": 50,
        "direction": "deposit", "cash_account_id": "cash", "bank_account_id": "bank-a",
    })
    save("bank_platform_transfer", "platform-transfer", platforms.transfer_data(amount=25))
    publish("cash", "platform", "banks", "cash-transfer", "platform-transfer")
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        expected, old_steps = _steps(
            snap.connection, lambda: _original_summary(snap, monkeypatch),
        )
        actual, new_steps = _steps(snap.connection, lambda: _summary(FundsRead(snap)))
        assert actual == expected
        assert actual[0] == {
            "inflow_fen": 2000, "outflow_fen": 0,
            "internal_transfer_fen": 175, "movement_count": 9,
        }
        # Brief consumes the same complete money amounts, without building
        # a movement count or latest-date display that it never returns.
        amount_read = FundsRead(snap, amounts_only=True)
        amount_totals = amount_read.account_summary()
        assert amount_totals == {
            key: value for key, value in actual[0].items() if key != "movement_count"
        }
        for account, item in amount_read.account_rows.items():
            assert item == {key: actual[1][account][key] for key in item}
        with monkeypatch.context() as patch:
            patch.setattr(FundsRead, "_verified_money_summary", lambda self: None)
            fallback = FundsRead(snap, amounts_only=True)
            assert fallback.account_summary() == amount_totals
            assert fallback.account_rows == amount_read.account_rows
        assert new_steps < old_steps
        with monkeypatch.context() as patch:
            def no_summary_shortcut(_self):
                raise AssertionError("a paged movement read consumed the summary-only shortcut")

            patch.setattr(FundsRead, "_verified_money_summary", no_summary_shortcut)
            paged = FundsRead(snap)
            assert paged.account_summary(page_request={
                "after": None, "limit": 1, "where": "category=? AND balance_key=?",
                "filters": ("bank", "bank-a"),
            }) == expected[0]
            assert paged.account_rows == expected[1]
            rows, page = paged.shared_pages["movements"]
            assert len(rows) == 1 and rows[0]["balance_key"] == "bank-a"
            assert page["total_count"] == 9 and page["filtered_count"] == 4
            assert page["has_more"]
        # Growing unrelated bodies must not grow the exact consumption scope.
        class ExactBodies(dict):
            def __iter__(self):
                raise AssertionError("unrelated result cache was enumerated")

            def items(self):
                raise AssertionError("unrelated result cache was enumerated")

        snap.reads._verified_source_contents = ExactBodies(snap.reads._verified_source_contents)
        for count in (12, 48, 120):
            snap.reads._verified_source_contents.update(
                (f"unused-{index}", {}) for index in range(count)
            )
            assert _summary(FundsRead(snap)) == expected


@pytest.mark.parametrize("amount", [(1 << 53) + 9, (1 << 63) - 1])
def test_large_safe_summaries_remain_exact(bank_book, monkeypatch, amount):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=amount)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        actual = _summary(FundsRead(snap))
        assert actual == _original_summary(snap, monkeypatch)
        assert actual[0]["inflow_fen"] == amount
        assert type(actual[0]["inflow_fen"]) is int


def test_summary_requires_existing_effect_proof_and_current_owned_snapshot(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        read = FundsRead(snap)
        assert read._verified_money_summary() is None
        read.events(current=True)
        assert read._verified_money_summary() is None
        snap.month_journal.account_amounts()
        read = FundsRead(snap)
        read.events(current=True)
        assert read._verified_money_summary() is not None
        token = _active_fact_reads.set(None)
        try:
            assert read._verified_money_summary() is None
        finally:
            _active_fact_reads.reset(token)
        with historical_content(1):
            assert read._verified_money_summary() is None


def test_summary_unsafe_arithmetic_and_encoding_retain_original_consumer(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = FundsRead(snap)
        read.events(current=True)
        original = read._verified_money_effects()[0]
        # Probe only this private consumer; these are not stored source proofs.
        for amounts in (((1 << 63) - 1, 1), (-(1 << 63),), ((1 << 62), -(1 << 62))):
            effects = [list(original) for _ in amounts]
            for index, amount in enumerate(amounts):
                effects[index][9] = amount
            with monkeypatch.context() as patch:
                patch.setattr(read, "_verified_money_effects", lambda effects=effects: effects)
                assert read._verified_money_summary() is None
                if amounts == ((1 << 63) - 1, 1):
                    with pytest.raises(sqlite3.OperationalError, match="integer overflow"):
                        read.account_summary()
        for position in (8, 10):
            effects = [list(original)]
            effects[0][position] = "\ud800"
            with monkeypatch.context() as patch:
                patch.setattr(read, "_verified_money_effects", lambda effects=effects: effects)
                assert read._verified_money_summary() is None


def test_summary_empty_dates_and_reversal_sign_consume_verified_effects(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = FundsRead(snap)
        read.events(current=True)
        original = read._verified_money_effects()[0]
        effects = [list(original), list(original)]
        effects[0][5], effects[0][9], effects[0][10] = -1, 1000, None
        effects[1][1], effects[1][9], effects[1][10] = "replacement", 700, MONTH + "-03"
        with monkeypatch.context() as patch:
            patch.setattr(read, "_verified_money_effects", lambda: effects)
            assert read._verified_money_summary() == [{
                "category": "bank", "balance_key": "bank-a",
                "inflow_fen": -300, "outflow_fen": 0, "movement_count": 2,
                "last_activity_date": MONTH + "-03", "external_inflow_fen": -300,
                "external_outflow_fen": 0, "internal_outflow_fen": 0,
            }]
            patch.setattr(read, "_verified_money_effects", lambda: [])
            assert read._verified_money_summary() == []


def test_receipt_summary_does_not_allocate_unused_per_event_state(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = FundsRead(snap, amounts_only=True)
        read.events(current=True)
        original = read._verified_money_effects()[0]
        peaks = []
        for count in (12, 120, 1200):
            # Isolate consumption of already proved effects. Creation of the
            # source list is outside this measurement, not a source-proof bypass.
            effects = [list(original) for _ in range(count)]
            for index, row in enumerate(effects):
                row[1] = f"receipt-{index}"
            with monkeypatch.context() as patch:
                patch.setattr(read, "_verified_money_effects", lambda effects=effects: effects)
                tracemalloc.start()
                try:
                    actual = read._verified_money_summary()
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            assert actual == [{
                "category": "bank", "balance_key": "bank-a",
                "inflow_fen": count * original[9], "outflow_fen": 0,
                "external_inflow_fen": count * original[9],
                "external_outflow_fen": 0, "internal_outflow_fen": 0,
            }]
            peaks.append(peak)
        # Only one account summary is needed for ordinary receipts, even as
        # their count grows. Allow interpreter noise, not per-event containers.
        assert peaks[-1] <= peaks[0] + 16_384
