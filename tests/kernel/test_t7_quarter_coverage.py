"""Quarter batching preserves the original independent historical source rules."""

import pytest
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.read_indexes import sync_close
from ai_accounting.kernel.reports import (
    Reports,
    _applicable_profile,
    _closed_period_issues,
    _periods,
    _report_references,
)
from ai_accounting.kernel.types import YearMonth, canonical, digest


def legacy_coverage(reports, connection, year, quarter):
    """Retain the pre-batch single-quarter selection as an independent oracle."""
    _, end, year_start = _periods(year, quarter)
    references = _report_references(connection, end, "closed")
    profiles = reports._report_profiles(
        connection, references, end, QueryReads(reports.engine, connection)
    )
    profile = _applicable_profile(profiles, end)
    book_start = profile.fact.bookkeeping_start if profile else year_start
    closes = {
        row[0]
        for row in connection.execute(
            "SELECT period FROM period_close WHERE period<=?", (end.ordinal,)
        )
    }
    problems = _closed_period_issues(closes, book_start, year_start, end)
    return {
        "complete": not problems,
        "fact_issues": problems,
        "bookkeeping_start": str(book_start),
    }


def save_profile(book, subject, period, start):
    return book[1](
        "report_profile",
        subject,
        {
            "period": period,
            "company_name": "Synthetic coverage company",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": start,
            "newly_established_zero_opening_confirmed": True,
        },
    )["fact_id"]


def insert_closes(engine, rows):
    """Synthetic immutable manifests isolate source timing from close writes."""
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        for period, facts in rows:
            manifest = {"readiness": {"financial_reports": {"facts": facts}}}
            month = YearMonth(period).ordinal
            connection.execute(
                "INSERT INTO period_close VALUES(?,?,?)",
                (month, canonical(manifest), digest(manifest)),
            )
            sync_close(connection, month)
        connection.commit()


def test_batch_coverage_matches_original_contract_and_verifies_references_once(book, monkeypatch):
    reports = scenario(book)
    close_quarter(book)
    quarters = [(2026, 2), (2025, 4), (2026, 1)]
    with reports.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = {key: legacy_coverage(reports, connection, *key) for key in quarters}
    verified = []
    original = QueryReads.verify_close_references

    def verify(reads, references):
        verified.append(len(references))
        return original(reads, references)

    def no_statements(*args, **kwargs):
        raise AssertionError("quarter coverage must not build financial statements")

    monkeypatch.setattr(QueryReads, "verify_close_references", verify)
    monkeypatch.setattr(reports, "_report", no_statements)
    actual = reports.closed_period_coverages(quarters)
    assert actual == expected
    assert actual[2026, 1]["complete"] is True
    assert actual[2026, 2]["complete"] is False
    assert len(verified) == 1 and verified[0] > 0
    assert reports.closed_period_coverage(2026, 1) == expected[2026, 1]


@pytest.mark.parametrize("conflict", [False, True])
def test_each_quarter_keeps_close_and_fact_cutoffs_and_profile_conflicts(book, conflict):
    engine = book[0]
    first = save_profile(book, "first", "2026-01", "2026-01")
    later_close = save_profile(book, "later-close", "2026-02", "2026-02")
    future_fact = save_profile(book, "future-fact", "2026-07", "2026-07")
    conflict_facts = [save_profile(book, "conflicting", "2026-02", "2026-02")] if conflict else []
    insert_closes(
        engine,
        [
            ("2026-03", [first, future_fact]),
            ("2026-04", [later_close]),
            ("2026-05", conflict_facts),
            ("2026-06", [first, later_close, future_fact]),
            ("2026-09", [future_fact]),
        ],
    )
    reports = Reports(engine)
    quarters = [(2026, 3), (2026, 1), (2026, 2)]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = {key: legacy_coverage(reports, connection, *key) for key in quarters}
    actual = reports.closed_period_coverages(quarters)
    assert actual == expected
    assert actual[2026, 1]["bookkeeping_start"] == "2026-01"
    assert actual[2026, 2]["bookkeeping_start"] == ("2026-01" if conflict else "2026-02")
    assert actual[2026, 3]["bookkeeping_start"] == "2026-07"
    assert actual[2026, 1]["fact_issues"] == [
        {
            "field": "closed_periods",
            "message": "年初或建账月起须逐月关账",
            "semantics": "accounting",
            "period": month,
        }
        for month in ("2026-01", "2026-02")
    ]


def test_coverage_reuses_callers_snapshot_and_new_request_sees_later_sources(book):
    engine = book[0]
    first = save_profile(book, "first", "2026-01", "2026-01")
    insert_closes(engine, [("2026-03", [first])])
    reports = Reports(engine)
    quarters = [(2026, 1), (2026, 2)]
    with QueryReads.snapshot(engine) as reads:
        before = reports.closed_period_coverages(quarters, connection=reads.connection, reads=reads)
        later = save_profile(book, "later", "2026-04", "2026-04")
        insert_closes(
            engine,
            [("2026-04", []), ("2026-05", []), ("2026-06", [later])],
        )
        repeated = reports.closed_period_coverages(
            quarters, connection=reads.connection, reads=reads
        )
        assert repeated == before
    fresh = reports.closed_period_coverages(quarters)
    assert fresh[2026, 1] == before[2026, 1]
    assert fresh[2026, 2]["bookkeeping_start"] == "2026-04"
    assert fresh[2026, 2]["complete"] is True
    assert fresh[2026, 2] != before[2026, 2]


def test_batch_still_checks_nonprofile_reference_leaves(book):
    engine = book[0]
    fact = save_profile(book, "first", "2026-01", "2026-01")
    insert_closes(engine, [("2026-03", [fact, "non-profile-reference"])])
    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_close_reference_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_close_reference_UPDATE")
        connection.execute(
            "UPDATE close_reference SET reference_id='wrong-leaf' "
            "WHERE reference_id='non-profile-reference'"
        )
        connection.execute(trigger)
        connection.commit()
    with pytest.raises(KernelError, match="精确引用目录"):
        Reports(engine).closed_period_coverages([(2026, 1), (2026, 2)])
