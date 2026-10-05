"""Owned report source assembly retains exact rows and all physical source proofs."""

import pytest
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel import report_projection
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

JAN = YearMonth("2026-01").ordinal


def _joined_source_rows(connection, selected, parameters):
    """The prior physical SQL, with the same owned selector and later proof."""
    selected_rows = list(connection.execute(
        "WITH selected AS (" + selected + ") "
        "SELECT ?,v.period,v.id,l.line_no,v.voucher_calculation_id AS calculation_id,"
        "v.basis_calculation_id,p.id,v.reverses_id,l.account,l.debit,l.credit,l.cashflow,"
        "c.kind,c.fact_id,c.period,c.subject_id,c.digest,"
        "v.voucher_id,v.voucher_subject_id,v.close_period "
        "FROM selected v JOIN voucher_line l ON l.version_id=v.id "
        "JOIN calculation c ON c.id=v.basis_calculation_id "
        "LEFT JOIN calculation_publication p ON p.calculation_id=v.basis_calculation_id "
        "ORDER BY v.id,l.line_no", (*parameters, "open"),
    ))
    rows = [tuple(row)[:17] for row in selected_rows]
    headers = {row["id"]: row for row in selected_rows}
    return rows, tuple(headers.values())


def _sources(engine, period=JAN):
    with QueryReads.snapshot(engine) as reads:
        rows, absence = report_projection._authoritative_rows(
            reads.connection, "open", period, reads=reads, with_absence=True,
        )
        return rows, absence, set(reads._verified_current_voucher_publications)


@pytest.mark.parametrize("count", [1, 12, 48])
def test_split_sources_match_prior_join_and_record_actual_work(
    engine, monkeypatch, record_property, count,
):
    subjects = [f"selected-{index}" for index in range(count)]
    for index, subject in enumerate(subjects):
        save(engine, subject=subject, amount=index + 1, request=subject)
    publish(engine, subjects)
    actual_work, actual = measure_work(engine, lambda: _sources(engine))
    with monkeypatch.context() as patch:
        patch.setattr(report_projection, "_open_source_rows_from_headers", _joined_source_rows)
        joined_work, joined = measure_work(engine, lambda: _sources(engine))
    assert actual == joined and len(actual[0]) == count * 2
    for key in ("sql_calls", "sqlite_vm_steps", "returned_rows", "returned_value_bytes",
                "calculation_result_rows_loaded", "calculation_result_json_decodes"):
        record_property("split_" + key, actual_work["counters"].get(key, 0))
        record_property("joined_" + key, joined_work["counters"].get(key, 0))
    # Splitting returns one additional physical header per voucher. Do not
    # pretend to reduce returned row count merely because bytes are smaller.
    assert actual_work["counters"]["returned_rows"] == (
        joined_work["counters"]["returned_rows"] + count
    )
    assert actual_work["counters"]["returned_value_bytes"] < (
        joined_work["counters"]["returned_value_bytes"]
    )
    if count == 48:
        assert actual_work["counters"]["sqlite_vm_steps"] < (
            joined_work["counters"]["sqlite_vm_steps"]
        )


def test_split_retains_selected_sql_multiplicity_and_line_major_order(engine, monkeypatch):
    save(engine)
    publish(engine)
    selector = report_projection.selected_voucher_sql

    def duplicated(*args, **kwargs):
        sql, parameters = selector(*args, **kwargs)
        return f"SELECT * FROM ({sql}) UNION ALL SELECT * FROM ({sql})", [
            *parameters, *parameters,
        ]

    monkeypatch.setattr(report_projection, "selected_voucher_sql", duplicated)
    actual = _sources(engine)
    with monkeypatch.context() as patch:
        patch.setattr(report_projection, "_open_source_rows_from_headers", _joined_source_rows)
        joined = _sources(engine)
    assert actual == joined and len(actual[0]) == 4
    assert [row[3] for row in actual[0]] == [1, 1, 2, 2]


def test_split_does_not_supply_no_line_voucher_to_publication_proof(engine):
    save(engine)
    publish(engine)
    with engine.store.connection(read_only=True) as connection:
        version = connection.execute("SELECT version_id FROM voucher_current").fetchone()[0]
    damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=?", (version,))
    # This tests physical assembly only; it makes no money/adoption claim about
    # the damaged book. Its caller retains the existing scope/source proofs.
    with QueryReads.snapshot(engine) as reads:
        selected, parameters = report_projection.selected_voucher_sql(
            YearMonth("2026-01"), posting_period=YearMonth("2026-01"),
            no_close_references=True,
        )
        assert report_projection._open_source_rows_from_headers(
            reads.connection, selected, parameters,
        ) == _joined_source_rows(reads.connection, selected, parameters) == ([], ())


def test_split_open_source_lookup_does_not_expand_with_unrelated_months(
    engine, monkeypatch, record_property,
):
    subjects = [f"selected-{index}" for index in range(12)]
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, subjects)
    before_work, before = measure_work(engine, lambda: _sources(engine))
    later = [f"future-{index}" for index in range(48)]
    for subject in later:
        save(engine, subject=subject, period="2026-02", request=subject)
    publish(engine, later, request="future-publish")
    after_work, after = measure_work(engine, lambda: _sources(engine))
    assert before == after
    # Measure the actual two data queries separately from necessary common
    # snapshot/scope guards, whose open-period cardinality has changed.
    def selected_work(work):
        selected = [item for item in work["sql"]
                    if item["statement"].startswith("WITH selected AS (")
                    or item["statement"].startswith("SELECT l.version_id,l.line_no")]
        return {key: sum(item.get(key, 0) for item in selected)
                for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps")}
    first, last = selected_work(before_work), selected_work(after_work)
    for key in ("returned_rows", "returned_value_bytes"):
        assert first[key] == last[key]
    assert last["sqlite_vm_steps"] <= first["sqlite_vm_steps"] + 100
    for key in first:
        record_property("before_" + key, first[key])
        record_property("unrelated_growth_" + key, last[key])


@pytest.mark.parametrize("boundary", ["ordinary", "unowned", "historical", "registry_v1", "closed"])
def test_split_assembly_does_not_enter_existing_fallback_scopes(engine, monkeypatch, boundary):
    save(engine)
    publish(engine)
    if boundary == "closed":
        close(engine)

    def reject_split(*args, **kwargs):
        raise AssertionError("fallback must retain original physical source SQL")

    monkeypatch.setattr(report_projection, "_open_source_rows_from_headers", reject_split)
    with QueryReads.snapshot(engine) as reads:
        selected_reads = reads
        if boundary == "ordinary":
            selected_reads = None
        elif boundary == "unowned":
            selected_reads = QueryReads(engine, reads.connection)
        elif boundary == "registry_v1":
            monkeypatch.setattr(engine.store.registry, "content_version", 1, raising=False)
        if boundary == "historical":
            with historical_content(1):
                rows = report_projection._authoritative_rows(
                    reads.connection, "open", JAN, reads=selected_reads,
                )
        else:
            rows = report_projection._authoritative_rows(
                reads.connection, "open", JAN, reads=selected_reads,
            )
        assert rows and all(len(row) == 17 for row in rows)


def test_all_three_report_statements_equal_prior_joined_source(book, monkeypatch):
    reports = scenario(book)
    with QueryReads.snapshot(book[0]) as reads:
        actual = reports._report(
            2026, 1, source="open", connection=reads.connection, reads=reads,
        )
    with monkeypatch.context() as patch:
        patch.setattr(report_projection, "_open_source_rows_from_headers", _joined_source_rows)
        with QueryReads.snapshot(book[0]) as reads:
            joined = reports._report(
                2026, 1, source="open", connection=reads.connection, reads=reads,
            )
    assert actual == joined
