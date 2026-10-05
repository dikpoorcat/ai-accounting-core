"""Exact classification header reuse keeps rooted month and combined scope checks."""

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_reports import book as _book
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import _validated_report_classification_headers
from ai_accounting.kernel.types import YearMonth

book = _book


def _header_work(work, prefix):
    selected = [item for item in work["sql"] if item["statement"].startswith(prefix)]
    return (
        sum(item["returned_rows"] for item in selected),
        sum(item["returned_value_bytes"] for item in selected),
    )


def test_actual_quarter_page_reuses_party_and_statement_scalar_headers(
    book, monkeypatch, record_property
):
    import ai_accounting.kernel.reports as reports_module

    engine = book[0]
    scenario(book)
    dashboard = Dashboard(engine)
    validate = reports_module._validated_report_classification_headers

    def no_scalar_reuse(connection, reads, *args, **kwargs):
        # A/B only the new exact header reuse. All normal report selection,
        # anchored content, line checks and other snapshot proofs still run.
        reads._report_snapshot_cache.pop("report_classification_scalar_headers", None)
        return validate(connection, reads, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(reports_module, "_validated_report_classification_headers", no_scalar_reuse)
        original_work, original = measure_work(engine, lambda: dashboard.quarterly_report(2026, 1))
    reused_work, reused = measure_work(engine, lambda: dashboard.quarterly_report(2026, 1))
    assert {key: value for key, value in original.items() if key != "checked_at"} == {
        key: value for key, value in reused.items() if key != "checked_at"
    }
    prefix = "SELECT c.revision_id,c.period,c.voucher_version_id,v.period AS voucher_period,"
    original_rows, original_bytes = _header_work(original_work, prefix)
    reused_rows, reused_bytes = _header_work(reused_work, prefix)
    assert original_rows > reused_rows > 0
    assert 0 < reused_bytes < original_bytes
    record_property("classification_header_rows_before", original_rows)
    record_property("classification_header_rows_after", reused_rows)
    record_property("classification_header_bytes_before", original_bytes)
    record_property("classification_header_bytes_after", reused_bytes)


def test_reused_scalar_subset_still_reports_actual_combined_conflict(book, record_property):
    engine, save, _, _ = book
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT revision_id,voucher_version_id FROM fact_report_classification"
        ).fetchone()
    duplicate = save("report_classification", "duplicate-classification", {
        "period": "2026-02", "voucher_version_id": original["voucher_version_id"],
        "profit_details": [{
            "line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000,
        }],
    })["fact_id"]

    def check(reuse):
        with QueryReads.snapshot(engine) as owned:
            reads = owned if reuse else QueryReads(engine, owned.connection)
            problems = []
            first = {original["revision_id"]}
            for identifiers in (first, first, first | {duplicate}):
                headers = _validated_report_classification_headers(
                    owned.connection, reads, identifiers, YearMonth("2026-03"), "open", problems
                )
                if len(identifiers) == 1:
                    assert headers[original["voucher_version_id"]]["revision_id"] in first
                    assert not problems
                else:
                    assert headers == {}
            assert [item["field"] for item in problems] == ["report_classification"]
            return problems

    original_work, original_result = measure_work(engine, lambda: check(False))
    reused_work, reused_result = measure_work(engine, lambda: check(True))
    assert original_result == reused_result
    prefix = "SELECT c.revision_id,c.period,c.voucher_version_id,v.period AS voucher_period,"
    original_rows, original_bytes = _header_work(original_work, prefix)
    reused_rows, reused_bytes = _header_work(reused_work, prefix)
    assert original_rows == 4 and reused_rows == 2
    assert 0 < reused_bytes < original_bytes
    record_property("scalar_header_rows_before", original_rows)
    record_property("scalar_header_rows_after", reused_rows)


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("corruption", ["digest", "child_body"])
def test_successful_header_reuse_never_hides_damage_in_a_new_quarter_snapshot(
    book, closed, corruption
):
    engine = book[0]
    scenario(book)
    if closed:
        close_quarter(book)
    dashboard = Dashboard(engine)
    baseline = dashboard.quarterly_report(2026, 1)
    next_read = dashboard.quarterly_report(2026, 1)
    assert {key: value for key, value in next_read.items() if key != "checked_at"} == {
        key: value for key, value in baseline.items() if key != "checked_at"
    }
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT revision_id FROM fact_report_classification"
        ).fetchone()[0]
    if corruption == "digest":
        damage(engine, "fact_revision", "UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
               (ident,))
    else:
        damage(engine, "fact_report_classification_profit_details",
               "UPDATE fact_report_classification_profit_details SET amount_fen=amount_fen+1 "
               "WHERE revision_id=?", (ident,))
    for _ in range(2):
        if closed and corruption == "child_body":
            # The quarter uses independently frozen flow amounts, not this
            # unused normalized child. Its complete source checker still
            # rejects the damaged fact when that body is consumed.
            response = dashboard.quarterly_report(2026, 1)
            assert {key: value for key, value in response.items() if key != "checked_at"} == {
                key: value for key, value in baseline.items() if key != "checked_at"
            }
            from ai_accounting.kernel.integrity import verify_sources

            with engine.store.connection(read_only=True) as connection:
                with pytest.raises(KernelError) as failure:
                    verify_sources(engine, connection, fact_ids={ident})
            assert failure.value.code == "content_integrity_failed"
            continue
        with pytest.raises(KernelError) as failure:
            dashboard.quarterly_report(2026, 1)
        assert failure.value.code == "content_integrity_failed"
