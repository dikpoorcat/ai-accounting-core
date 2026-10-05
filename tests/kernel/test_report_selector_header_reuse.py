"""Report selectors reuse only their own exact physical open-month headers."""

import pytest
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel import query_reads, report_projection
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

JAN = YearMonth("2026-01").ordinal


def source_rows(engine, period=JAN):
    with QueryReads.snapshot(engine) as reads:
        return report_projection._authoritative_rows(
            reads.connection, "open", period, reads=reads
        )


@pytest.mark.parametrize("history", ["original", "review", "closed", "correction"])
def test_report_source_tuples_equal_independent_reader(engine, history, monkeypatch):
    save(engine)
    publish(engine)
    period = JAN
    if history == "review":
        save(engine, revision=1, request="review")
        publish(engine, request="review-publish")
    elif history in {"closed", "correction"}:
        close(engine)
        if history == "correction":
            save(engine, amount=150, revision=1, request="correction")
            publish(engine, request="correct-publish", posting_period="2026-02")
            period += 1
    actual = source_rows(engine, period)
    with monkeypatch.context() as patch:
        patch.setattr(report_projection, "_selected_current_voucher_publications",
                      lambda *a, **k: None)
        expected = source_rows(engine, period)
    assert actual == expected
    assert actual and all(len(row) == 17 for row in actual)
    if history == "correction":
        assert any(row[7] is not None for row in actual)


@pytest.mark.parametrize("boundary", ["ordinary", "unowned", "historical", "registry_v1"])
def test_nonowned_and_fixed_content_scopes_keep_independent_lookup(
    engine, monkeypatch, boundary
):
    save(engine)
    publish(engine)
    observed = []
    original = report_projection.verify_current_voucher_publications

    def independent(connection, identifiers):
        observed.append(set(identifiers))
        return original(connection, identifiers)

    monkeypatch.setattr(report_projection, "verify_current_voucher_publications", independent)
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
                    reads.connection, "open", JAN, reads=selected_reads
                )
        else:
            rows = report_projection._authoritative_rows(
                reads.connection, "open", JAN, reads=selected_reads
            )
        assert rows and all(len(row) == 17 for row in rows)
    assert len(observed) == 1 and len(observed[0]) == 1


@pytest.mark.parametrize("corruption", [
    "original_missing", "head_missing", "head_subject", "head_period",
    "original_voucher", "head_voucher", "current_missing", "current_redirect",
    "successor", "digest",
])
def test_report_selected_publication_relationship_damage_never_publishes_batch_success(
    engine, corruption
):
    for subject in ("first", "second"):
        save(engine, subject=subject, request=subject)
    publish(engine, ["first", "second"])
    save(engine, subject="second", revision=1, request="review")
    publish(engine, ["second"], request="review-publish")
    source_rows(engine)  # An earlier successful request cannot hide later damage.
    with engine.store.connection(read_only=True) as connection:
        heads = dict(connection.execute(
            "SELECT subject_id,calculation_id FROM calculation_current"
        ))
        original = connection.execute(
            "SELECT v.calculation_id FROM voucher_current h JOIN voucher_version v "
            "ON v.id=h.version_id JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='second'"
        ).fetchone()[0]
    if corruption.endswith("missing") and corruption != "current_missing":
        target = original if corruption == "original_missing" else heads["second"]
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?",
               (target,), foreign_keys=False)
    elif corruption.startswith("current_") or corruption == "successor":
        target = heads["first"] if corruption == "current_redirect" else original
        if corruption == "current_redirect":
            damage(engine, "calculation_current",
                   "DELETE FROM calculation_current WHERE subject_id='first'")
        sql = (
            "DELETE FROM calculation_current WHERE subject_id='second'"
            if corruption == "current_missing" else
            "UPDATE calculation_current SET calculation_id=? WHERE subject_id='second'"
        )
        damage(engine, "calculation_current", sql,
               () if corruption == "current_missing" else (target,))
    else:
        changes = {
            "head_subject": "subject_id='first'",
            "head_period": "posting_period=posting_period+1",
            "original_voucher": "voucher_id=NULL",
            "head_voucher": "voucher_id=NULL",
            "digest": "mode='open_replace'",
        }
        target = original if corruption == "original_voucher" else heads["second"]
        damage(engine, "calculation_publication",
               "UPDATE calculation_publication SET " + changes[corruption]
               + " WHERE calculation_id=?", (target,))
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                report_projection._authoritative_rows(
                    reads.connection, "open", JAN, reads=reads
                )
            assert failure.value.code == "content_integrity_failed"
            assert not reads._verified_publication_ids


def test_report_selector_work_decreases_without_source_scope_or_body_change(
    engine, monkeypatch, record_property
):
    subjects = [f"charge-{index}" for index in range(24)]
    for index, subject in enumerate(subjects):
        save(engine, subject=subject, amount=index + 1, request=subject)
    publish(engine, subjects)
    actual_work, actual = measure_work(engine, lambda: source_rows(engine))
    independent = report_projection.verify_current_voucher_publications

    def unowned_lookup(connection, identifiers):
        # Both owned public and selector entries now share the same proof.
        # The unowned entry remains an independent physical source comparison.
        token = query_reads._active_fact_reads.set(None)
        try:
            return independent(connection, identifiers)
        finally:
            query_reads._active_fact_reads.reset(token)

    with monkeypatch.context() as patch:
        patch.setattr(report_projection, "_owns_current_selector_snapshot",
                      lambda *a, **k: False)
        patch.setattr(report_projection, "verify_current_voucher_publications", unowned_lookup)
        fallback_work, fallback = measure_work(engine, lambda: source_rows(engine))
    assert actual == fallback and len(actual) == 48
    for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        assert actual_work["counters"][key] < fallback_work["counters"][key]
        record_property("selected_" + key, actual_work["counters"][key])
        record_property("independent_" + key, fallback_work["counters"][key])
    for key in ("calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert actual_work["counters"][key] == fallback_work["counters"][key] == 0
    # An unrelated future voucher and broken publication are outside January.
    save(engine, subject="future", period="2026-02", request="future")
    publish(engine, ["future"], request="future-publish")
    damage(engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE subject_id='future'",
           foreign_keys=False)
    assert source_rows(engine) == actual


def test_three_public_reports_equal_independent_header_resolution(book, monkeypatch):
    reports = scenario(book)
    with QueryReads.snapshot(book[0]) as reads:
        actual = reports._report(
            2026, 1, source="open", connection=reads.connection, reads=reads
        )
    with monkeypatch.context() as patch:
        patch.setattr(report_projection, "_selected_current_voucher_publications",
                      lambda *a, **k: None)
        with QueryReads.snapshot(book[0]) as reads:
            expected = reports._report(
                2026, 1, source="open", connection=reads.connection, reads=reads
            )
    assert actual == expected
    assert reports.report(2026, 1) == actual


def test_unexpected_frozen_reference_keeps_independent_lookup_even_with_other_guard(
    engine, monkeypatch
):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, subject="february", period="2026-02", request="february")
    publish(engine, ["february"], request="february-publish")
    with engine.store.connection(read_only=True) as connection:
        voucher = connection.execute(
            "SELECT id FROM voucher_version WHERE period=?", (JAN + 1,)
        ).fetchone()[0]
    damage(engine, "close_reference",
           "INSERT INTO close_reference(close_period,path,position,reference_type,reference_id) "
           "VALUES(?,?,?,?,?)", (JAN, "vouchers", "unexpected", "voucher", voucher))
    independent = report_projection.verify_current_voucher_publications
    observed = []

    def lookup(connection, identifiers):
        observed.append(set(identifiers))
        return independent(connection, identifiers)

    monkeypatch.setattr(report_projection, "verify_current_voucher_publications", lookup)
    with QueryReads.snapshot(engine) as reads:
        # A different month's successful absence is not this month's proof.
        reads._report_snapshot_cache["open_voucher_period", JAN + 1, JAN] = True
        rows, absence = report_projection._authoritative_rows(
            reads.connection, "open", JAN + 1, reads=reads, with_absence=True
        )
        assert rows and not absence
        assert ("open_voucher_period", JAN + 1, JAN + 1) not in reads._report_snapshot_cache
    assert observed == [{voucher}]
