"""Position consumes exact proved month lines and complete frozen party bases."""

from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_payroll_corrections import company as company  # noqa: F401
from test_report_party_summary import _MeasuredPartyBase, checkpoint_net_book
from test_reports import book as book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel import report_projection
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.cash import CashFunding
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical


@pytest.mark.parametrize("history_months", [12, 120])
def test_checkpoint_summary_keeps_exact_parties_without_unused_display_tuples(
    monkeypatch, history_months, record_property,
):
    obligations = history_months * 50
    connection, reads, december, _months, _seen, _traversed = checkpoint_net_book(
        monkeypatch, historic_obligations=obligations,
    )
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    decoded = []
    original = report_projection._freeze_party_key

    def display_key(value):
        decoded.append(value)
        return original(value)

    monkeypatch.setattr(report_projection, "_freeze_party_key", display_key)
    try:
        assert report_projection._party_balance_position_lines(
            object(), connection, december + 2, reads=reads,
            _validate_party_keys=True,
        ) == {33: 30, 39: obligations * 10}
        record_property("discarded_display_key_nodes", len(decoded))
        assert decoded == []
        # Detail consumers still receive the exact historical identities.
        rows = report_projection.party_balance_rows(
            object(), connection, december + 2, reads=reads,
        )
        assert len(rows) == obligations + 1
        assert any(row["party_splits"][0][0] == ("party", "supplier") for row in rows)
        assert decoded
    finally:
        connection.close()


def test_public_overview_party_work_ignores_non_reclass_line_growth(company, record_property):
    engine = company.engine
    company.publish("january", "february")
    actual_reader = report_projection._verified_party_source_rows
    full_rows, party_rows = [], []
    full_reader = report_projection._authoritative_rows

    def full(*args, **kwargs):
        value = full_reader(*args, **kwargs)
        if args[2] == YearMonth("2026-02").ordinal:
            full_rows.append(len(value[0] if kwargs.get("with_absence") else value))
        return value

    def proved(*args):
        value = actual_reader(*args)
        if value is not None:
            party_rows.append(len(value))
        return value

    counts = []
    for size in (0, 6):
        if size:
            for index in range(size):
                company.save(CashFunding(**{
                    "period": "2026-02", "actual_date": f"2026-02-{index + 3:02d}",
                    "owner_id": "owner", "funding_kind": "capital",
                    "amount_fen": 100 + index, "cash_account_id": "cash",
                }), f"position-unrelated-{index}")
            company.publish(*(f"position-unrelated-{index}" for index in range(size)))
        full_rows.clear()
        party_rows.clear()
        with patch.object(report_projection, "_verified_party_source_rows", return_value=None):
            with patch.object(report_projection, "_authoritative_rows", full):
                before, expected = measure_work(engine, lambda: Dashboard(engine).brief("2026-02"))
        with patch.object(report_projection, "_verified_party_source_rows", proved):
            after, actual = measure_work(engine, lambda: Dashboard(engine).brief("2026-02"))
        actual["data"].pop("generated_at")
        expected["data"].pop("generated_at")
        assert actual == expected
        assert party_rows and full_rows and party_rows[0] < full_rows[0]
        assert after["counters"]["returned_rows"] < before["counters"]["returned_rows"]
        assert after["counters"]["returned_value_bytes"] < (
            before["counters"]["returned_value_bytes"]
        )
        counts.append((full_rows[0], party_rows[0]))
        record_property(f"before_{size}", before["counters"])
        record_property(f"after_{size}", after["counters"])
    assert counts[1][0] > counts[0][0]
    assert counts[1][1] == counts[0][1]


@pytest.mark.parametrize("broken", ["non_reclass_line", "outcome", "fact"])
def test_public_month_proof_reuse_still_rejects_source_damage(book, broken):
    engine, _, _, _ = book
    scenario(book, classification=False, tax=False)
    assert Dashboard(engine).brief("2026-03")["data"]["financial_position"]["equation_valid"]
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.id,c.fact_id,v.id version_id FROM calculation c "
            "JOIN voucher_version v ON v.calculation_id=c.id WHERE c.subject_id='payment'",
        ).fetchone()
    if broken == "non_reclass_line":
        damage(engine, "voucher_line", "UPDATE voucher_line SET credit=credit+1 "
               "WHERE version_id=? AND account='1001'", (row["version_id"],))
    elif broken == "outcome":
        damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?",
               (row["id"],))
    else:
        damage(engine, "fact_revision", "UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
               (row["fact_id"],))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief("2026-03")
    assert failure.value.code == "content_integrity_failed"


def test_partial_header_or_line_scope_cannot_certify_party_tail(book):
    engine = book[0]
    scenario(book, classification=False, tax=False)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        selected_month = snap.month
        helper = report_projection._verified_party_source_rows
        assert helper(snap.reads, snap.connection, snap.month, snap.month) is None
        snap.month_journal.authenticated_headers()
        assert helper(snap.reads, snap.connection, snap.month, snap.month) is None
        snap.month_journal.account_amounts()
        assert helper(snap.reads, snap.connection, snap.month, snap.month)
        with historical_content(1):
            assert helper(snap.reads, snap.connection, snap.month, snap.month) is None
        assert helper(snap.reads, snap.connection, snap.month - 1, snap.month) is None
        row = snap.month_journal.verified_rows()[0]
        key = ("authenticated_voucher_lines", row["id"], row["voucher_calculation_id"],
               row["basis_calculation_id"])
        snap.reads._report_snapshot_cache.pop(key)
        assert helper(snap.reads, snap.connection, snap.month, snap.month) is None
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        assert helper(reads, connection, selected_month, selected_month) is None


def test_checkpoint_summary_fuses_first_complete_base_pass(monkeypatch):
    connection, reads, december, _months, seen, traversed = checkpoint_net_book(
        monkeypatch, historic_obligations=24,
    )
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    inputs = report_projection._party_balance_inputs

    def control(*args, **kwargs):
        kwargs["_checkpoint_summary"] = None
        checkpoint, totals, tail = inputs(*args, **kwargs)
        measured = _MeasuredPartyBase(totals)
        reads._report_snapshot_cache["report_party_checkpoint_totals", checkpoint] = measured
        return checkpoint, measured, tail

    with patch.object(report_projection, "_party_balance_inputs", control):
        expected = report_projection._party_balance_position_lines(
            object(), connection, december + 1, reads=reads, _validate_party_keys=True,
        )
    before = reads._report_snapshot_cache["report_party_checkpoint_totals", december].work
    assert before["items_rows"] == 75
    reads._report_snapshot_cache.clear()
    seen.clear()
    traversed.clear()
    summary = report_projection._checkpoint_party_summary

    def measured_summary(rows):
        totals, result = summary(rows)
        return _MeasuredPartyBase(totals), result

    monkeypatch.setattr(report_projection, "_checkpoint_party_summary", measured_summary)
    original = report_projection._party_key_shape_problems
    shape_sizes = []

    def shapes(values):
        shape_sizes.append(len(values))
        return original(values)

    monkeypatch.setattr(report_projection, "_party_key_shape_problems", shapes)
    try:
        actual = report_projection._party_balance_position_lines(
            object(), connection, december + 1, reads=reads, _validate_party_keys=True,
        )
        assert actual == expected == {5: 50, 39: 240}
        assert len(traversed) == 25
        assert seen == [december, december + 1]
        # Shape validation of the complete frozen base was fused into the
        # materialization; only the one changed tail key requires a later walk.
        assert shape_sizes == [1]
        base = reads._report_snapshot_cache["report_party_checkpoint_totals", december]
        assert len(base) == 25
        assert base["2202", canonical(("party", "supplier"))] == -100
        assert base.work == {"copy_rows": 0, "iter_rows": 0, "items_rows": 0, "gets": 1}
    finally:
        connection.close()


@pytest.mark.parametrize("last_amount", [-30, 0, 60, "bad"])
def test_fused_checkpoint_keeps_exact_last_key_and_amount_shape(last_amount):
    key = canonical(("party", "supplier"))
    rows = [(10, "2202", key, -100), (10, "2202", key, last_amount)]
    totals, (supported, lines, shapes) = report_projection._checkpoint_party_summary(rows)
    assert totals == {("2202", key): last_amount}
    assert supported == (type(last_amount) is int)
    assert shapes == report_projection._party_key_shape_problems(totals)
    if supported:
        expected = report_projection._party_position_totals(
            ((account, raw, amount) for (account, raw), amount in totals.items() if amount),
            by_account=True,
        )
        assert {key: amount for key, amount in lines.items() if amount} == expected


def test_fused_checkpoint_retains_invalid_key_and_boolean_amount_problems():
    rows = [
        (10, "2202", '["party","supplier-12"]', -7),
        (10, "2202", '"中文"', 3),
        (10, "2202", '[1]', 1),
        (10, "2202", 'invalid', 1),
        (10, "2202", '"supplier-0"', True),
    ]
    totals, (supported, _lines, shapes) = report_projection._checkpoint_party_summary(rows)
    assert totals == {(row[1], row[2]): row[3] for row in rows}
    assert supported is False
    assert shapes == (
        frozenset({("2202", "invalid")}),
        frozenset({("2202", "[1]"), ("2202", '"supplier-0"')}),
    )


def test_fused_bad_key_that_tail_cancels_keeps_original_full_scope(monkeypatch):
    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    raw = "{malformed"
    months[december]["checkpoint_rows"] = ((december, "224101", raw, -30),)
    months[december + 1]["party_rows"] = ((december + 1, "224101", raw, 30),)
    try:
        assert report_projection._party_balance_position_lines(
            object(), connection, december + 1, reads=reads,
            _accounts={"2202"}, _validate_party_keys=True,
        ) == {}
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ["unknown", "exception"])
def test_fused_checkpoint_failure_does_not_publish_summary_or_shapes(monkeypatch, failure):
    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    months[december + 1] = (
        {**months[december + 1], "party_usable": False} if failure == "unknown"
        else KernelError("content_integrity_failed", "synthetic frozen tail damaged")
    )
    try:
        if failure == "exception":
            with pytest.raises(KernelError):
                report_projection._party_balance_position_lines(
                    object(), connection, december + 1, reads=reads, _validate_party_keys=True,
                )
        else:
            assert report_projection._party_balance_position_lines(
                object(), connection, december + 1, reads=reads, _validate_party_keys=True,
            ) is None
        assert ("report_party_checkpoint_position_lines", december) not in (
            reads._report_snapshot_cache
        )
        assert ("report_party_checkpoint_party_key_shapes", december) not in (
            reads._report_snapshot_cache
        )
    finally:
        connection.close()
