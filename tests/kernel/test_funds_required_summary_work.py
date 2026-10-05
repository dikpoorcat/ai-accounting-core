"""Summary reads reuse successful money scope and retain full detail semantics."""

import json

import pytest
import test_banking as banking
import test_investments as investments
from test_dashboard_funds_alignment import _publish_filter_funding
from test_funds_source_integrity import _reviewed_closed_investment
from test_funds_summary_page import _steps
from test_integrity_content import damage

from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.types import canonical, digest

bank_book = banking.book
investment_book = investments.book
MONTH = "2026-09"
ACCOUNTS = {"1001", "1002", "1012", "1101"}


def _effects(read, *, accounts=ACCOUNTS, subjects=None):
    source, parameters = read.events(current=True, accounts=accounts, subjects=subjects)
    return [dict(row) for row in read.connection.execute(
        source + "SELECT event_id,calculation_id,kind,sign,category,balance_key,amount "
        "FROM effects ORDER BY event_id,effect_index", parameters,
    )]


def test_bank_summary_matches_full_detail_source_without_detail_windows(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.statement(save, publish, [
        banking.entry(f"row-{index}", amount=1 if index % 2 else -1)
        for index in range(40)
    ])
    with Dashboard(engine)._snapshot(MONTH) as snap:
        read = FundsRead(snap)
        read.account_summary()
        statements = []
        snap.connection.set_trace_callback(statements.append)
        totals = read.bank_summary()
        snap.connection.set_trace_callback(None)
        narrow = next(query for query in statements if query.startswith(
            "SELECT account_id,count(*) transaction_count"
        ))
        prefix = narrow.split(" FROM (", 1)[0]
        full = prefix + " FROM (" + read.bank_source + ") GROUP BY account_id"
        old, old_steps = _steps(snap.connection, lambda: [dict(row) for row in
            snap.connection.execute(full, read.bank_parameters)])
        new, new_steps = _steps(snap.connection, lambda: [dict(row) for row in
            snap.connection.execute(narrow)])
        assert new == old
        assert old[0]["transaction_count"] == totals["transaction_count"] == 40
        assert old[0]["inflow_fen"] == old[0]["outflow_fen"] == 20
        assert new_steps < old_steps
        # The source used by displayed rows still contains complete batch data.
        detail = dict(snap.connection.execute(read.bank_source, read.bank_parameters).fetchone())
        assert detail["source_row_count"] == 40 and detail["source_rows_total_fen"] == 40


def test_current_event_proof_reuse_keeps_actual_account_subject_scope_and_transfer(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, subject="capital", amount=1000)
    save("funds_transfer", "transfer", {
        "period": MONTH, "actual_date": MONTH + "-02",
        "source_bank_account_id": "bank-a", "destination_bank_account_id": "bank-b",
        "amount_fen": 200,
    })
    publish("transfer")
    dashboard = Dashboard(engine)
    with dashboard._snapshot(MONTH) as snap:
        baseline = _effects(FundsRead(snap))
        filtered = _effects(FundsRead(snap), subjects={"transfer"})
        excluded = _effects(FundsRead(snap), accounts={"1001"})
        assert snap.month_journal.verified_rows() is None
    with dashboard._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = FundsRead(snap)
        assert _effects(read) == baseline
        assert _effects(FundsRead(snap), subjects={"transfer"}) == filtered
        assert _effects(FundsRead(snap), accounts={"1001"}) == excluded == []
        totals = read.account_summary()
        assert totals["inflow_fen"] == 1000 and totals["outflow_fen"] == 0
        assert totals["internal_transfer_fen"] == 200
        assert len(read._verified_current_event_rows(
            current=True, accounts=ACCOUNTS, subjects={"transfer"},
        )) == 1


def test_existing_proof_reuse_reduces_real_selector_work_without_loading_new_bodies(
    bank_book, monkeypatch,
):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    _publish_filter_funding(engine, ["bank-a"] * 15 + ["bank-b"] * 15)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        # Warm necessary SQL metadata equally; only source discovery differs.
        snap.month_journal.select(accounts=ACCOUNTS).sql()
        selected = snap.month_journal.verified_rows()
        FundsRead(snap)._verify_event_sources({row["basis_calculation_id"] for row in selected}, {})
        old_read, new_read = FundsRead(snap), FundsRead(snap)
        before = set(snap.reads._verified_source_contents)
        with monkeypatch.context() as patch:
            patch.setattr(FundsRead, "_verified_current_event_rows", lambda *_args, **_kwargs: None)
            old, old_steps = _steps(snap.connection, lambda: _effects(old_read))
        new, new_steps = _steps(snap.connection, lambda: _effects(new_read))
        assert new == old and len(new) == 30
        assert sum(row["amount"] for row in new) == 30
        assert new_steps < old_steps
        assert before == snap.reads._verified_source_contents.keys()


def test_headers_are_not_reused_before_complete_proof_or_for_other_scope(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    dashboard = Dashboard(engine)
    with dashboard._snapshot(MONTH) as snap:
        read = FundsRead(snap)
        args = {"current": True, "accounts": ACCOUNTS, "subjects": None}
        assert read._verified_current_event_rows(**args) is None
        snap.month_journal.account_amounts()
        assert len(read._verified_current_event_rows(**args)) == 1
        assert read._verified_current_event_rows(**(args | {"current": False})) is None
        assert snap.month_journal.select(subjects={"funding"}).verified_rows() is None
        with historical_content(1):
            assert read._verified_current_event_rows(**args) is None
    with dashboard._snapshot(MONTH) as fresh:
        assert fresh.month_journal.verified_rows() is None
        assert FundsRead(fresh)._verified_current_event_rows(**args) is None


@pytest.mark.parametrize("corruption", ["line", "source"])
def test_failed_off_page_money_proof_cannot_supply_event_headers(bank_book, corruption):
    engine, save, publish, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 21)
    with engine.store.connection(read_only=True) as connection:
        row = dict(connection.execute(
            "SELECT v.id,c.id calculation_id FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='filter-2026-09-0020'",
        ).fetchone())
    if corruption == "line":
        damage(engine, "voucher_line",
               "DELETE FROM voucher_line WHERE version_id=? AND line_no=1", (row["id"],))
    else:
        damage(engine, "calculation",
               "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount_fen',2) "
               "WHERE id=?",
               (row["calculation_id"],))
    with Dashboard(engine)._snapshot(MONTH) as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
        assert FundsRead(snap)._verified_current_event_rows(
            current=True, accounts=ACCOUNTS, subjects=None,
        ) is None
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(MONTH, limit=20)
    assert failure.value.code == "content_integrity_failed"


def test_verified_events_reuse_keeps_frozen_no_impact_voucher_and_adopted_basis(
    investment_book, monkeypatch,
):
    engine, original = _reviewed_closed_investment(investment_book, monkeypatch)
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-01") as snap:
        expected = _effects(FundsRead(snap))
    with dashboard._snapshot("2026-01") as snap:
        snap.month_journal.account_amounts()
        rows = snap.month_journal.verified_rows()
        assert any(row["voucher_calculation_id"] == original["id"]
                   and row["basis_calculation_id"] == original["id"] for row in rows)
        close = snap.reads.authoritative_close_rows(periods={snap.month})[0]
        adopted = snap.reads.close_section(close, "adopted_results")
        assert any(item["subject_id"] == original["subject_id"]
                   and item["calculation_id"] != original["id"] for item in adopted)
        reader = FundsRead(snap)
        assert _effects(reader) == expected
        assert original["id"] in snap.reads._verified_saved_input_identities
        totals = reader.account_summary()
        assert totals["inflow_fen"] == 12000 and totals["outflow_fen"] == 10100


@pytest.mark.parametrize("corrupt_original", [False, True])
def test_verified_reversal_events_preserve_original_frozen_month_anchor(
    bank_book, corrupt_original,
):
    engine, save, publish, proof = bank_book
    fields = {
        "period": MONTH, "actual_date": MONTH + "-03", "cash_account_id": "cash",
        "amount_fen": 1000,
    }
    save("managed_reserve_refund", "receipt", fields)
    publish("receipt")
    with engine.store.connection(read_only=True) as connection:
        original = dict(connection.execute(
            "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
            "WHERE h.subject_id='receipt'",
        ).fetchone())
    investments.close(engine, MONTH)
    engine.amend_fact(
        "managed_reserve_refund", "receipt", fields | {"amount_fen": 700}, evidence=(proof,),
        expected_revision=1, recording_error_confirmed=True, request_id="amend-receipt",
    )
    preview = engine.preview(["receipt"], posting_period="2026-10")
    engine.confirm(
        ["receipt"], preview_digest=preview["digest"], epochs=preview["epochs"],
        posting_period="2026-10", request_id="receipt-correction",
    )
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-10") as snap:
        expected = _effects(FundsRead(snap))
    with dashboard._snapshot("2026-10") as snap:
        snap.month_journal.account_amounts()
        reader = FundsRead(snap)
        rows = reader._verified_current_event_rows(
            current=True, accounts=ACCOUNTS, subjects=None,
        )
        assert len(rows) == 2 and any(row["reverses_id"] for row in rows)
        actual = _effects(reader)
        assert actual == expected
        assert sum(row["sign"] * row["amount"] for row in actual) == -300
        assert next(row for row in actual if row["sign"] == -1)["calculation_id"] == original["id"]
    if corrupt_original:
        outcome = json.loads(original["outcome"])
        outcome["values"]["actual_date"] = MONTH + "-04"
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(outcome), digest(outcome), original["id"]))
        with dashboard._snapshot("2026-10") as snap:
            with pytest.raises(KernelError) as failure:
                snap.month_journal.account_amounts()
            assert failure.value.code == "content_integrity_failed"
            assert FundsRead(snap)._verified_current_event_rows(
                current=True, accounts=ACCOUNTS, subjects=None,
            ) is None
        with engine.store.connection(read_only=True) as connection:
            with pytest.raises(KernelError) as failure:
                verify_integrity(engine, connection)
        assert failure.value.code == "content_integrity_failed"
