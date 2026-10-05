"""Reuse exact month proof without another account locator or gross read."""

from collections import Counter, defaultdict

import pytest
import test_banking as banking
import test_investments as investments
from stage9_metrics import _Connection, measure_work

from ai_accounting.kernel import dashboard_reads
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.storage import _active_fact_reads

book = banking.book
MONTH = "2026-09"


def _effects(read, **scope):
    query, parameters = read.events(**scope)
    return [tuple(row) for row in read.connection.execute(
        query + "SELECT * FROM effects ORDER BY event_id,effect_index", parameters,
    )]


def test_verified_empty_month_sql_stays_empty(book, monkeypatch):
    engine, save, publish, _ = book
    banking.opening(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        assert snap.month_journal.account_amounts() == {}
        read = FundsRead(snap)
        assert _effects(read, current=True) == []
        assert read._money_event_rows == []
        with monkeypatch.context() as patch:
            patch.setattr(FundsRead, "_verified_current_event_rows", lambda self, **kwargs: None)
            assert _effects(FundsRead(snap), current=True) == []


@pytest.mark.parametrize("accounts", [None, set(), {"1002"}, {"1101"}, {"2202"}])
def test_exact_verified_event_sql_matches_original_account_selection(book, monkeypatch, accounts):
    engine, save, publish, _ = book
    banking.funding(save, publish)
    save("expense", "accrual", {
        "period": MONTH, "counterparty_id": "supplier", "amount_fen": 7,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    publish("accrual")
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = FundsRead(snap)
        actual = _effects(read, current=True, accounts=accounts)
        cached_query = read.events(current=True, accounts=accounts)
        assert cached_query == read.events(current=True, accounts=accounts)
        with monkeypatch.context() as patch:
            patch.setattr(FundsRead, "_verified_current_event_rows", lambda self, **kwargs: None)
            expected = _effects(FundsRead(snap), current=True, accounts=accounts)
        assert actual == expected
        if accounts in (set(), {"1101"}):
            assert actual == []


def test_verified_money_events_do_not_repeat_locator_rows_with_unrelated_history(book, monkeypatch):
    engine, save, publish, _ = book
    banking.funding(save, publish)
    original = dashboard_reads._account_voucher_ids
    locator_work = []

    def locate(connection, *args, **kwargs):
        counts, statements, work = Counter(), Counter(), defaultdict(Counter)
        selected = original(
            _Connection(connection, counts, statements, work, set()), *args, **kwargs,
        )
        locator_work.append((dict(counts), selected))
        return selected

    monkeypatch.setattr(dashboard_reads, "_account_voucher_ids", locate)

    def read_summary():
        with Dashboard(engine)._snapshot(MONTH) as snap:
            snap.month_journal.account_amounts()
            read = FundsRead(snap)
            before = len(locator_work)
            actual = read.account_summary()
            assert len(locator_work) == before
            assert read._money_event_rows is not None
            with monkeypatch.context() as patch:
                patch.setattr(
                    FundsRead, "_verified_current_event_rows", lambda self, **kwargs: None,
                )
                expected = FundsRead(snap).account_summary()
            assert actual == expected
            counts, selected = locator_work[-1]
            assert selected and counts["returned_rows"] == len(selected)
            assert counts["returned_value_bytes"] > 0
            return actual

    before = read_summary()
    for period in (MONTH, "2026-08"):
        for index in range(3):
            subject = f"unrelated-{period}-{index}"
            save("expense", subject, {
                "period": period, "counterparty_id": "supplier", "amount_fen": 1,
                "expense_class": "administration", "creditor_kind": "supplier",
            })
            publish(subject)
    assert read_summary() == before


def test_missing_owned_current_proof_and_subject_scope_keep_original_locator(book, monkeypatch):
    engine, save, publish, _ = book
    banking.funding(save, publish)
    calls = []
    original = dashboard_reads._account_voucher_ids

    def locate(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard_reads, "_account_voucher_ids", locate)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        assert _effects(FundsRead(snap), current=True)
        assert len(calls) == 1
        snap.month_journal.account_amounts()
        for subjects in (set(), {"funding"}):
            read = FundsRead(snap)
            assert read._verified_current_event_rows(
                current=True, accounts={"1002"}, subjects=subjects,
            ) is None
            _effects(read, current=True, subjects=subjects)
        assert len(calls) == 3
        read = FundsRead(snap)
        assert read._verified_current_event_rows(
            current=False, accounts={"1002"}, subjects=None,
        ) is None
        _effects(read, current=False)
        assert len(calls) == 4
        token = _active_fact_reads.set(None)
        try:
            assert read._verified_current_event_rows(
                current=True, accounts={"1002"}, subjects=None,
            ) is None
        finally:
            _active_fact_reads.reset(token)
        with historical_content(1):
            assert read._verified_current_event_rows(
                current=True, accounts={"1002"}, subjects=None,
            ) is None


def test_brief_reads_exact_month_gross_projection_once(book):
    engine, save, publish, _ = book
    banking.funding(save, publish)
    result, response = measure_work(engine, lambda: Dashboard(engine).brief(MONTH))
    assert response["data"]["activity_count"] == 1
    statement = "SELECT account,debit,credit FROM monthly_account WHERE period=?"
    matching = [row for row in result["sql"] if row["statement"] == statement]
    assert len(matching) == 1 and matching[0]["calls"] == 1
    assert matching[0]["returned_rows"] == 2


def test_exact_event_sql_keeps_no_impact_frozen_owner_and_current_reversal(book, monkeypatch):
    engine, save, publish, proof = book
    fields = {
        "period": MONTH, "actual_date": MONTH + "-03",
        "cash_account_id": "cash", "amount_fen": 1000,
    }
    save("managed_reserve_refund", "receipt", fields)
    publish("receipt")
    from ai_accounting.kernel import engine as engine_module

    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "PROGRAM_VERSION", "synthetic-event-no-impact")
        assert publish("receipt")["results"][0]["impact"] == "review_no_impact"

    def compare(period, *, frozen=False, subjects=None):
        with Dashboard(engine)._snapshot(period) as snap:
            snap.month_journal.account_amounts()
            read = FundsRead(snap)
            actual = _effects(read, current=True, subjects=subjects)
            if frozen or subjects is not None:
                assert read._money_event_rows is None
            with monkeypatch.context() as patch:
                patch.setattr(
                    FundsRead, "_verified_current_event_rows", lambda self, **kwargs: None,
                )
                expected = _effects(FundsRead(snap), current=True, subjects=subjects)
            assert actual == expected
            return actual

    before = compare(MONTH)
    investments.close(engine, MONTH)
    frozen_before = compare(MONTH, frozen=True)
    # A no-impact review can become the adopted basis at close while the
    # formal voucher remains unchanged. Compare money, then preserve that
    # exact frozen owner after the later correction.
    assert [row[-3:] for row in frozen_before] == [row[-3:] for row in before]
    engine.amend_fact(
        "managed_reserve_refund", "receipt", fields | {"amount_fen": 700},
        evidence=(proof,), expected_revision=1, recording_error_confirmed=True,
        request_id="amend-event-source",
    )
    preview = engine.preview(["receipt"], posting_period="2026-10")
    engine.confirm(
        ["receipt"], preview_digest=preview["digest"], epochs=preview["epochs"],
        posting_period="2026-10", request_id="correct-event-source",
    )
    assert len(compare("2026-10")) == 2
    assert compare("2026-10", subjects={"receipt"}) == compare("2026-10")
    assert compare(MONTH, frozen=True) == frozen_before
