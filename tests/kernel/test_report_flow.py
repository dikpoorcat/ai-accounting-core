"""Root-bound month contributions agree with the original report formulas."""

import pytest
from test_integrity_content import damage
from test_reports import book as _book
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_flow import (
    compare_report_flow,
    read_report_flow,
    repair_report_flow,
    require_report_flow,
)
from ai_accounting.kernel.report_projection import (
    _authoritative_rows,
    _party_delta,
    party_balance_rows,
    require_report_projection,
)
from ai_accounting.kernel.report_semantics import require_report_semantics
from ai_accounting.kernel.reports import (
    _closed_report_fact_sources,
    _report_classifications,
)
from ai_accounting.kernel.types import YearMonth
from ai_accounting.kernel.verified_source_lease import verified_source_lease

book = _book


def test_closed_flow_matches_live_statements_and_authority(book):
    engine = book[0]
    report = scenario(book)
    before = report.report(2026, 1)
    close_quarter(book)
    after = report.report(2026, 1)
    closed = report.report(2026, 1, source="closed")
    assert before["statements"] == after["statements"] == closed["statements"]
    assert before["fact_issues"] == after["fact_issues"] == closed["fact_issues"]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result = require_report_flow(engine, connection)
        assert result["rows"] == 3
        with verified_source_lease(connection):
            source_token = require_report_projection(engine, connection, _return_verified=True)
            semantic_token = require_report_semantics(engine, connection, _return_verified=True)
            assert (
                require_report_flow(
                    engine,
                    connection,
                    _verified_reports=source_token,
                    _verified_semantics=semantic_token,
                )["rows"]
                == 3
            )
        for value in ("2026-01", "2026-02", "2026-03"):
            item = read_report_flow(connection, YearMonth(value).ordinal)
            assert item is not None
            assert item["profit"] and item["cash"]
        connection.commit()


def test_damaged_derived_flow_is_rejected_and_repaired_from_source(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    month = YearMonth("2026-01").ordinal
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE report_period_flow SET content='{}' WHERE posting_period=?", (month,)
        )
        connection.commit()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            read_report_flow(connection, month)
        assert caught.value.code == "content_integrity_failed"
        assert compare_report_flow(engine, connection)["changed"]
        connection.commit()
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert repair_report_flow(engine, connection)["changed"]
        connection.commit()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert not require_report_flow(engine, connection)["changed"]
        connection.commit()


def test_flow_classification_selection_checks_exact_root_references(book, monkeypatch):
    import ai_accounting.kernel.report_flow as flow_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    cutoff = YearMonth("2026-03").ordinal
    original = flow_module._classification_refs

    def no_cumulative_selection(connection, period, source_vouchers, *, closed):
        if closed:
            pytest.fail("ordinary flow read must use its authenticated exact reference IDs")
        return original(connection, period, source_vouchers, closed=closed)

    monkeypatch.setattr(flow_module, "_classification_refs", no_cumulative_selection)
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        rows = _closed_report_fact_sources(connection, YearMonth("2026-03"), reads)
        reads._report_snapshot_cache["closed_report_fact_sources", cutoff] = rows
        for value in ("2026-01", "2026-02", "2026-03"):
            month = YearMonth(value).ordinal
            stored = read_report_flow(connection, month, reads=reads)
            assert stored is not None
            assert tuple(tuple(item) for item in stored["classification_refs"]) == original(
                connection, month, stored["source_vouchers"], closed=True
            )
        assert ("report_flow_classification_candidates", cutoff) not in reads._report_snapshot_cache


def test_party_delta_reuses_authenticated_fact_set_and_checks_selected_leaf(book, monkeypatch):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    month = YearMonth("2026-02").ordinal
    cutoff = YearMonth("2026-03").ordinal
    with QueryReads.snapshot(engine) as reads:
        rows = tuple(_authoritative_rows(reads.connection, "closed", month))
        expected = _party_delta(engine, reads.connection, month, rows, source="closed", reads=reads)
    with QueryReads.snapshot(engine) as reads:
        sources = _closed_report_fact_sources(reads.connection, YearMonth("2026-03"), reads)
        reads._report_snapshot_cache["closed_report_fact_sources", cutoff] = sources

        checked = []
        original = reads.verify_close_references

        def selected_leaf_check(references):
            checked.extend(references)
            return original(references)

        monkeypatch.setattr(reads, "verify_close_references", selected_leaf_check)
        actual = _party_delta(engine, reads.connection, month, rows, source="closed", reads=reads)
        assert actual == expected
        assert checked
        rooted_ids = {item["reference_id"] for item in sources}
        classification_checks = [
            row
            for row in checked
            if row["reference_type"] == "fact"
            and row["path"] == "readiness.financial_reports.facts[*]"
        ]
        assert all(row["reference_id"] in rooted_ids for row in classification_checks)
        assert ("report_classification_directory", month) in reads._report_snapshot_cache


def test_frozen_classification_digest_damage_is_not_selection_fallback(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    feb = YearMonth("2026-02").ordinal
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='report_classification' AND f.period=?",
            (feb,),
        ).fetchone()[0]
    damage(
        engine,
        "fact_revision",
        "UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
        (ident,),
    )
    with QueryReads.snapshot(engine) as reads:
        sources = _closed_report_fact_sources(reads.connection, YearMonth("2026-03"), reads)
        reads._report_snapshot_cache["closed_report_fact_sources", YearMonth("2026-03").ordinal] = (
            sources
        )
        with pytest.raises(KernelError) as caught:
            read_report_flow(reads.connection, feb, reads=reads)
        assert caught.value.code == "content_integrity_failed"


def test_missing_repairable_reference_does_not_change_bound_flow_but_full_check_rejects(book):
    from ai_accounting.kernel.integrity import verify_integrity

    engine = book[0]
    scenario(book)
    close_quarter(book)
    feb = YearMonth("2026-02").ordinal
    with QueryReads.snapshot(engine) as reads:
        frozen = read_report_flow(reads.connection, feb, reads=reads)
        assert frozen is not None and frozen["classification_refs"]
        ident = frozen["classification_refs"][0][0]
    damage(
        engine,
        "close_reference",
        "DELETE FROM close_reference WHERE close_period=? "
        "AND reference_type='fact' AND reference_id=? "
        "AND path='readiness.financial_reports.facts[*]'",
        (feb, ident),
    )
    with QueryReads.snapshot(engine) as reads:
        assert read_report_flow(reads.connection, feb, reads=reads) == frozen
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            verify_integrity(engine, connection)
        assert caught.value.code == "read_index_integrity_failed"


def _old_classification_scope(engine, reads):
    """Exercise a frozen 2026 classification as old history at a 2027 cutoff."""
    end = YearMonth("2027-01")
    sources = _closed_report_fact_sources(reads.connection, end, reads)
    reads._report_snapshot_cache["closed_report_fact_sources", end.ordinal] = sources
    row = reads.connection.execute(
        "SELECT c.revision_id,c.voucher_version_id FROM fact_report_classification c "
        "WHERE c.period=? LIMIT 1",
        (YearMonth("2026-02").ordinal,),
    ).fetchone()
    assert row is not None
    return end, row["revision_id"], row["voucher_version_id"]


def test_old_unused_classification_reuses_exact_frozen_flow_only(book, monkeypatch):
    import ai_accounting.kernel.reports as reports_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        end, ident, voucher = _old_classification_scope(engine, reads)
        selected = []
        original = reports_module._report_vouchers

        def track(*args, **kwargs):
            selected.append(kwargs.get("voucher_ids"))
            return original(*args, **kwargs)

        monkeypatch.setattr(reports_module, "_report_vouchers", track)
        problems = []
        assert _report_classifications(
            reads.connection, reads, {ident}, set(), end, "open", problems
        ) == {}
        assert not problems and not selected
        # The earlier unused proof cannot suppress later exact line use.
        assert voucher in _report_classifications(
            reads.connection, reads, {ident}, {voucher}, end, "open", problems
        )
        assert not problems and len(selected) == 1


def test_old_unused_frozen_classification_still_detects_new_voucher_conflict(book):
    engine, save, _, _ = book
    scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        _, first, voucher = _old_classification_scope(engine, reads)
    save(
        "report_classification",
        "later-conflicting-classification",
        {
            "period": "2026-02",
            "voucher_version_id": voucher,
            "profit_details": [
                {
                    "line_no": 1,
                    "detail_code": "management_entertainment",
                    "amount_fen": 10000,
                }
            ],
        },
    )
    with QueryReads.snapshot(engine) as reads:
        end, _, _ = _old_classification_scope(engine, reads)
        second = reads.connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "WHERE c.subject_id='later-conflicting-classification'"
        ).fetchone()[0]
        problems = []
        assert _report_classifications(
            reads.connection, reads, {first, second}, set(), end, "open", problems
        ) == {}
        assert [item["field"] for item in problems] == ["report_classification"]


def test_old_unused_classification_without_flow_coverage_uses_full_selection(book, monkeypatch):
    import ai_accounting.kernel.report_flow as flow_module
    import ai_accounting.kernel.reports as reports_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    selected = []
    original = reports_module._report_vouchers

    def track(*args, **kwargs):
        selected.append(kwargs.get("voucher_ids"))
        return original(*args, **kwargs)

    monkeypatch.setattr(flow_module, "read_report_flow", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reports_module, "_report_vouchers", track)
    with QueryReads.snapshot(engine) as reads:
        end, ident, _ = _old_classification_scope(engine, reads)
        problems = []
        assert _report_classifications(
            reads.connection, reads, {ident}, set(), end, "open", problems
        ) == {}
        assert not problems and len(selected) == 1


def test_old_unused_classification_frozen_root_damage_is_rejected(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        _, ident, _ = _old_classification_scope(engine, reads)
    damage(
        engine,
        "report_period_flow",
        "UPDATE report_period_flow SET content='{}' WHERE posting_period=?",
        (YearMonth("2026-02").ordinal,),
    )
    with QueryReads.snapshot(engine) as reads:
        end, _, _ = _old_classification_scope(engine, reads)
        with pytest.raises(KernelError) as caught:
            _report_classifications(
                reads.connection, reads, {ident}, set(), end, "open", []
            )
        assert caught.value.code == "content_integrity_failed"


def test_old_unused_child_is_deferred_to_full_integrity_but_hit_child_is_checked(book):
    from ai_accounting.kernel.integrity import verify_integrity

    engine = book[0]
    scenario(book)
    close_quarter(book)
    damage(
        engine,
        "fact_report_classification_profit_details",
        "UPDATE fact_report_classification_profit_details SET line_no=99",
    )
    with QueryReads.snapshot(engine) as reads:
        end, ident, voucher = _old_classification_scope(engine, reads)
        problems = []
        assert _report_classifications(
            reads.connection, reads, {ident}, set(), end, "open", problems
        ) == {}
        assert not problems
        with pytest.raises(KernelError) as caught:
            _report_classifications(
                reads.connection, reads, {ident}, {voucher}, end, "open", problems
            )
        assert caught.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            verify_integrity(engine, connection)
        assert caught.value.code == "content_integrity_failed"


@pytest.mark.parametrize("used", [False, True])
def test_old_classification_digest_damage_is_rejected(book, used):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        end, ident, voucher = _old_classification_scope(engine, reads)
    damage(
        engine,
        "fact_revision",
        "UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
        (ident,),
    )
    with QueryReads.snapshot(engine) as reads:
        end, _, _ = _old_classification_scope(engine, reads)
        with pytest.raises(KernelError) as caught:
            _report_classifications(
                reads.connection, reads, {ident}, {voucher} if used else set(), end, "open", []
            )
        assert caught.value.code == "content_integrity_failed"


def test_missing_frozen_typed_classification_rejects_flow_and_party(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    feb = YearMonth("2026-02").ordinal
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT c.revision_id FROM fact_report_classification c "
            "JOIN fact_revision f ON f.id=c.revision_id WHERE f.period=?", (feb,)
        ).fetchone()[0]
    damage(
        engine,
        "fact_report_classification",
        "DELETE FROM fact_report_classification WHERE revision_id=?",
        (ident,),
        foreign_keys=False,
    )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as caught:
            read_report_flow(reads.connection, feb, reads=reads)
        assert caught.value.code == "content_integrity_failed"
        with pytest.raises(KernelError) as caught:
            party_balance_rows(
                engine,
                reads.connection,
                YearMonth("2026-03").ordinal,
                source="closed",
                reads=reads,
            )
        assert caught.value.code == "content_integrity_failed"
