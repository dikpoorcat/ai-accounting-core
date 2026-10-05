"""A report source directory must agree with actual selected voucher lines."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_integrity_content import damage
from test_reports import book as _report_book
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads, selected_voucher_sql
from ai_accounting.kernel.read_indexes import CLOSE_REPORT_FACTS
from ai_accounting.kernel.report_projection import (
    _authoritative_rows,
    _party_delta,
    party_balance_rows,
    repair_report_projection,
    require_report_projection,
    verify_report_lines,
)
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.types import YearMonth, canonical

book = _report_book


def test_report_party_direct_relations_match_complete_resolution(book):
    engine = book[0]
    scenario(book)
    for period in ("2026-02", "2026-03"):
        with QueryReads.snapshot(engine) as reads:
            rows = verify_report_lines(reads.connection, "open", period)
            direct = _party_delta(
                engine, reads.connection, YearMonth(period).ordinal, rows, reads=reads
            )
        with QueryReads.snapshot(engine) as reads:
            rows = verify_report_lines(reads.connection, "open", period)
            reads.report_line_relations_many = lambda requests: reads.relations_many(requests)
            complete = _party_delta(
                engine, reads.connection, YearMonth(period).ordinal, rows, reads=reads
            )
        assert direct == complete
        assert direct[1]


def test_report_party_direct_source_damage_and_full_integrity_both_reject(book):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id='cost'"
        ).fetchone()[0]
    damage(
        engine,
        "fact_expense",
        "UPDATE fact_expense SET amount_fen=amount_fen+1 WHERE revision_id=?",
        (fact_id,),
    )
    with QueryReads.snapshot(engine) as reads:
        rows = verify_report_lines(reads.connection, "open", "2026-03")
        with pytest.raises(KernelError, match="完整|摘要|一致|损坏"):
            _party_delta(
                engine, reads.connection, YearMonth("2026-03").ordinal, rows, reads=reads
            )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError):
            verify_integrity(engine, connection)


def test_report_party_rejects_missing_exact_source_dependency(book):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        relation = connection.execute(
            "SELECT d.calculation_id,d.upstream_id FROM dependency_calculation d "
            "JOIN calculation c ON c.id=d.calculation_id "
            "JOIN calculation u ON u.id=d.upstream_id "
            "WHERE c.subject_id='payment' AND u.subject_id='cost'"
        ).fetchone()
    assert relation is not None
    damage(
        engine,
        "dependency_calculation",
        "DELETE FROM dependency_calculation WHERE calculation_id=? AND upstream_id=?",
        tuple(relation),
    )
    with QueryReads.snapshot(engine) as reads:
        rows = verify_report_lines(reads.connection, "open", "2026-03")
        with pytest.raises(KernelError, match="精确核算依赖"):
            _party_delta(
                engine, reads.connection, YearMonth("2026-03").ordinal, rows, reads=reads
            )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError):
            verify_integrity(engine, connection)


def test_open_report_selection_skips_close_lookup_only_after_authoritative_absence(book):
    engine = book[0]
    scenario(book)
    book[3]("2026-01")
    month = YearMonth("2026-02").ordinal

    def journal_query(connection, expected):
        snapshot = SimpleNamespace(
            connection=connection, reads=QueryReads(engine, connection),
            month=month, period=YearMonth.from_ordinal(month),
        )
        with patch(
            "ai_accounting.kernel.dashboard_reads.selected_voucher_sql",
            wraps=selected_voucher_sql,
        ) as selected:
            Journal(snapshot, month=month).sql()
        assert selected.call_args.kwargs["no_close_references"] is expected

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        journal_query(connection, True)
        with patch(
            "ai_accounting.kernel.report_projection.selected_voucher_sql",
            wraps=selected_voucher_sql,
        ) as selected:
            assert _authoritative_rows(connection, "open", month)
        assert selected.call_args.kwargs["no_close_references"] is True
        voucher = connection.execute(
            "SELECT version_id FROM report_line_source WHERE scope='open' "
            "AND posting_period=? LIMIT 1",
            (month,),
        ).fetchone()[0]
        prior_close = connection.execute("SELECT min(period) FROM period_close").fetchone()[0]
    damage(
        engine,
        "close_reference",
        "INSERT INTO close_reference(close_period,path,position,reference_type,reference_id) "
        "VALUES(?,?,?,?,?)",
        (prior_close, "vouchers", "unexpected-open-voucher", "voucher", voucher),
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        journal_query(connection, False)
        with patch(
            "ai_accounting.kernel.report_projection.selected_voucher_sql",
            wraps=selected_voucher_sql,
        ) as selected:
            _authoritative_rows(connection, "open", month)
        assert selected.call_args.kwargs["no_close_references"] is False


def _projection_copy(engine, path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    with engine.store.connection(read_only=True) as source:
        source.backup(connection)
    return connection


def test_open_report_source_is_sealed_and_checks_authoritative_lines(book, tmp_path):
    engine = book[0]
    scenario(book)
    with _projection_copy(engine, tmp_path / "open.sqlite") as connection:
        assert require_report_projection(engine, connection)["periods"] == 3
        rows = verify_report_lines(connection, "open", "2026-02")
        assert any(row[8] == "5602" and row[9] == 10000 for row in rows)
        connection.execute(
            "UPDATE report_line_source SET debit=debit+1 "
            "WHERE scope='open' AND posting_period=? AND account='5602'",
            (rows[0][1],),
        )
        with pytest.raises(KernelError, match="封签"):
            verify_report_lines(connection, "open", "2026-02")
        assert repair_report_projection(engine, connection)["changed"]
        assert require_report_projection(engine, connection)["periods"] == 3


def test_year_end_party_checkpoint_preserves_exact_payable(book, tmp_path, monkeypatch):
    engine, save, publish, close = book
    save(
        "expense",
        "year-end-cost",
        {
            "period": "2026-12",
            "counterparty_id": "supplier",
            "amount_fen": 12345,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("year-end-cost")
    for month in range(1, 13):
        close(f"2026-{month:02d}")
    with _projection_copy(engine, tmp_path / "year-end.sqlite") as connection:
        assert require_report_projection(engine, connection)["periods"] == 12
        row = connection.execute(
            "SELECT usable,row_count FROM report_party_checkpoint_seal"
        ).fetchone()
        assert dict(row) == {"usable": 1, "row_count": 1}
        parties = party_balance_rows(
            engine, connection, YearMonth("2026-12").ordinal, source="closed"
        )
        assert parties == [
            {
                "account": "2202",
                "amount": -12345,
                "party_splits": ((("party", "supplier"), -12345),),
            }
        ]
        connection.execute("UPDATE report_party_checkpoint SET amount=amount+1")
        with pytest.raises(KernelError, match="投影"):
            require_report_projection(engine, connection)
        assert repair_report_projection(engine, connection)["changed"]
        assert require_report_projection(engine, connection)["periods"] == 12
    optimized = Reports(engine).report(2026, 4, source="closed")
    monkeypatch.setattr(
        "ai_accounting.kernel.report_projection._party_balance_position_lines", lambda *_a, **_k: None
    )
    original = Reports(engine).report(2026, 4, source="closed")
    assert optimized["statements"] == original["statements"]
    assert optimized["fact_issues"] == original["fact_issues"]


def test_party_read_after_ten_years_opens_only_latest_checkpoint_and_tail():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE opening_account(period INTEGER,account TEXT,debit INTEGER,credit INTEGER);"
        "CREATE TABLE report_party_checkpoint_seal(posting_period INTEGER,usable INTEGER);"
        "CREATE TABLE calculation_publication(posting_period INTEGER);"
        "CREATE TABLE period_close(period INTEGER);"
    )
    first = YearMonth("2016-01").ordinal
    checkpoint = YearMonth("2024-12").ordinal
    cutoff = YearMonth("2025-11").ordinal
    connection.executemany(
        "INSERT INTO period_close VALUES(?)", ((month,) for month in range(first, cutoff + 1))
    )
    connection.execute("INSERT INTO report_party_checkpoint_seal VALUES(?,1)", (checkpoint,))
    connection.execute("INSERT INTO calculation_publication VALUES(?)", (first,))
    seen = []

    def verified_month(_connection, month):
        seen.append(month)
        return {
            "checkpoint_usable": True,
            "checkpoint_rows": ((checkpoint, "2202", '["party","supplier"]', -100),),
            "party_usable": True,
            "party_rows": (),
        }

    with patch("ai_accounting.kernel.report_projection._stored_month", verified_month):
        rows = party_balance_rows(None, connection, cutoff, source="closed")
    assert seen == [checkpoint, *range(checkpoint + 1, cutoff + 1)]
    assert len(seen) == 12
    assert rows[0]["amount"] == -100


def test_explicit_rebuild_repairs_derived_report_and_advances_read_revision(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection() as connection:
        before = connection.execute("SELECT read_repair_revision FROM state WHERE id=1").fetchone()[
            0
        ]
        connection.execute("UPDATE report_party_delta SET amount=amount+1 WHERE amount<>0")
        connection.commit()
    result = Maintenance(engine).rebuild_projections(request_id="repair-report-projection")
    assert result["changed"] is True
    assert result["read_repair_revision"] == before + 1
    with engine.store.connection(read_only=True) as connection:
        assert require_report_projection(engine, connection)["periods"] == 3


def test_open_and_closed_report_reuse_only_their_shared_snapshot(book, monkeypatch):
    import ai_accounting.kernel.report_projection as projection
    import ai_accounting.kernel.reports as reports_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    original_party = projection._party_balance_position_lines
    original_refs = reports_module._closed_report_fact_sources
    calls = {"party": 0, "references": 0}

    def party(*args, **kwargs):
        calls["party"] += 1
        return original_party(*args, **kwargs)

    def references(*args, **kwargs):
        calls["references"] += 1
        return original_refs(*args, **kwargs)

    monkeypatch.setattr(projection, "_party_balance_position_lines", party)
    monkeypatch.setattr(reports_module, "_closed_report_fact_sources", references)
    reports = Reports(engine)
    for expected in (1, 2):
        with QueryReads.snapshot(engine) as reads:
            reports._report(2026, 1, source="open", connection=reads.connection, reads=reads)
            reports._report(2026, 1, source="closed", connection=reads.connection, reads=reads)
        assert calls == {"party": 2 * expected, "references": expected}


def test_party_balances_reuse_verified_months_only_inside_same_snapshot(book, monkeypatch):
    import ai_accounting.kernel.report_flow as flow_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    original = flow_module.read_report_flow
    checked = []

    def check_month(connection, period, *, reads=None):
        checked.append(period)
        return original(connection, period, reads=reads)

    monkeypatch.setattr(flow_module, "read_report_flow", check_month)
    cutoff = YearMonth("2026-03").ordinal
    with QueryReads.snapshot(engine) as reads:
        first = party_balance_rows(engine, reads.connection, cutoff, source="closed", reads=reads)
        assert first is not None
        before = len(checked)
        assert before > 0
        assert (
            party_balance_rows(engine, reads.connection, cutoff, source="closed", reads=reads)
            == first
        )
        assert len(checked) == before
    with QueryReads.snapshot(engine) as reads:
        assert (
            party_balance_rows(engine, reads.connection, cutoff, source="closed", reads=reads)
            == first
        )
        assert len(checked) > before


def test_unusable_open_party_month_is_not_cached_as_a_verified_balance(book, monkeypatch):
    import ai_accounting.kernel.query_semantics as semantics
    import ai_accounting.kernel.report_projection as projection

    engine = book[0]
    scenario(book, classification=False, tax=False)
    monkeypatch.setattr(
        semantics,
        "report_party_splits",
        lambda *_args, **_kwargs: {"splits": None, "issues": []},
    )
    original = projection._party_delta
    attempted = []

    def party_delta(*args, **kwargs):
        result = original(*args, **kwargs)
        attempted.append((args[2], result[1]))
        return result

    monkeypatch.setattr(projection, "_party_delta", party_delta)
    cutoff = YearMonth("2026-03").ordinal
    with QueryReads.snapshot(engine) as reads:
        assert party_balance_rows(engine, reads.connection, cutoff, reads=reads) is None
        assert ("report_open_source_rows", cutoff) not in reads._report_snapshot_cache
        first = len(attempted)
        assert first > 0 and not attempted[-1][1]
        assert party_balance_rows(engine, reads.connection, cutoff, reads=reads) is None
        assert len(attempted) > first


def test_valid_report_classification_checks_reuse_one_snapshot(book, monkeypatch):
    import ai_accounting.kernel.reports as reports_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    original = reports_module._validated_report_classification_headers
    checked = []

    def validate(*args, **kwargs):
        checked.append(args[4])
        return original(*args, **kwargs)

    monkeypatch.setattr(reports_module, "_validated_report_classification_headers", validate)
    reports = Reports(engine)
    with QueryReads.snapshot(engine) as reads:
        first = reports._report(2026, 1, source="open", connection=reads.connection, reads=reads)
        count = len(checked)
        assert count > 0
        second = reports._report(2026, 1, source="open", connection=reads.connection, reads=reads)
        assert first["statements"] == second["statements"]
        assert first["fact_issues"] == second["fact_issues"]
        assert len(checked) == count


def test_classification_subset_reuse_keeps_cross_subset_conflicts(book):
    from ai_accounting.kernel.reports import _validated_report_classification_headers

    engine, save, _, _ = book
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT revision_id,voucher_version_id FROM fact_report_classification"
        ).fetchone()
        first, voucher = row["revision_id"], row["voucher_version_id"]
    save(
        "report_classification", "another-classification",
        {
            "period": "2026-02", "voucher_version_id": voucher,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
            ],
        },
    )
    with QueryReads.snapshot(engine) as reads:
        all_ids = {
            row[0] for row in reads.connection.execute(
                "SELECT revision_id FROM fact_report_classification"
            )
        }
        statements = []
        reads.connection.set_trace_callback(statements.append)
        problems = []
        def check(ids):
            return _validated_report_classification_headers(
                reads.connection, reads, ids, YearMonth("2026-03"), "open", problems
            )
        assert check({first})[voucher]["revision_id"] == first
        assert not problems
        statements.clear()
        assert check({first})[voucher]["revision_id"] == first
        assert not any("fact_report_classification_profit_details" in sql for sql in statements)
        assert check(all_ids) == {}
        assert problems == [{
            "field": "report_classification",
            "message": "同一凭证版本存在多个分类来源",
            "semantics": "accounting",
            "voucher_version_id": voucher,
        }]
        # The failed combined scope is checked again; a prior successful subset
        # cannot turn the conflicting larger selection into a valid one.
        problems.clear()
        assert check(all_ids) == {}
        assert len(problems) == 1


def test_closed_report_source_freezes_and_rejects_mutation(book, tmp_path):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with _projection_copy(engine, tmp_path / "closed.sqlite") as connection:
        assert require_report_projection(engine, connection)["periods"] == 3
        rows = verify_report_lines(connection, "closed", "2026-02")
        assert rows
        party = connection.execute(
            "SELECT posting_period,account,party_key,amount FROM report_party_delta "
            "WHERE posting_period=?",
            (rows[0][1],),
        ).fetchall()
        assert any(item["account"] == "2202" and item["amount"] == -10000 for item in party)
        connection.execute(
            "DELETE FROM report_line_source WHERE scope='closed' AND posting_period=?",
            (rows[0][1],),
        )
        connection.execute("DELETE FROM report_party_delta WHERE posting_period=?", (rows[0][1],))
        with pytest.raises(KernelError, match="封签"):
            verify_report_lines(connection, "closed", "2026-02")
        assert repair_report_projection(engine, connection)["changed"]
        assert require_report_projection(engine, connection)["periods"] == 3


def test_closed_report_cannot_silently_omit_voucher_when_reverse_directory_row_is_missing(book):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    baseline = report.report(2026, 1, source="closed")
    assert baseline["statements"]["profit_statement"]["14"]["current_fen"] == 10000
    with engine.store.connection(read_only=True) as connection:
        target = connection.execute(
            "SELECT r.close_period,r.path,r.reference_id FROM close_reference r "
            "JOIN voucher_version v ON v.id=r.reference_id "
            "JOIN calculation c ON c.id=v.calculation_id "
            "WHERE r.reference_type='voucher' AND c.subject_id='cost' "
            "AND r.close_period=?",
            (YearMonth("2026-02").ordinal,),
        ).fetchone()
    assert target is not None
    damage(
        engine,
        "close_reference",
        "DELETE FROM close_reference WHERE close_period=? AND path=? AND reference_id=?",
        tuple(target),
    )
    try:
        after = report.report(2026, 1, source="closed")
    except KernelError:
        pass
    else:
        assert canonical(after) == canonical(baseline)
    with pytest.raises(KernelError):
        Maintenance(engine).verify_integrity()
    assert Maintenance(engine).repair_read_indexes(request_id="repair-report-close-reference")[
        "changed"
    ]
    assert canonical(report.report(2026, 1, source="closed")) == canonical(baseline)


def test_closed_report_fact_ids_come_from_authenticated_close_not_reverse_directory(book):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    baseline = report.report(2026, 1, source="closed")
    with engine.store.connection(read_only=True) as connection:
        target = connection.execute(
            "SELECT close_period,path,reference_id FROM close_reference "
            "WHERE path=? AND close_period=? LIMIT 1",
            (CLOSE_REPORT_FACTS, YearMonth("2026-02").ordinal),
        ).fetchone()
    assert target is not None
    damage(
        engine,
        "close_reference",
        "DELETE FROM close_reference WHERE close_period=? AND path=? AND reference_id=?",
        tuple(target),
    )
    with QueryReads.snapshot(engine) as reads:
        actual = report._report(2026, 1, source="closed", connection=reads.connection, reads=reads)
    assert canonical(actual) == canonical(baseline)
    with pytest.raises(KernelError):
        Maintenance(engine).verify_integrity()


@pytest.mark.parametrize(
    "table,sql",
    [
        (
            "close_storage_subroot",
            "UPDATE close_storage_subroot SET content='{}' "
            "WHERE period=? AND family='management'",
        ),
        (
            "close_storage_root",
            "UPDATE close_storage_root SET storage_digest=zeroblob(32) WHERE period=?",
        ),
        (
            "read_index_source",
            "DELETE FROM read_index_source WHERE source_kind='close' AND source_id=CAST(? AS TEXT)",
        ),
    ],
)
def test_closed_report_rejects_damaged_authenticated_readiness(book, table, sql):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    month = YearMonth("2026-02").ordinal
    damage(engine, table, sql, (month,), foreign_keys=table != "read_index_source")
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as caught:
            report._report(2026, 1, source="closed", connection=reads.connection, reads=reads)
    assert caught.value.code in {"content_integrity_failed", "read_index_integrity_failed"}
