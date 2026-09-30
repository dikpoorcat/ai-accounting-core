"""Quarter batching preserves the original independent historical source rules."""

import pytest
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel.close_contract import CLOSE_FORMAT, CLOSE_FORMAT_VERSION
from ai_accounting.kernel.close_review import build_owner_review
from ai_accounting.kernel.close_storage import write_close
from ai_accounting.kernel.materials import check_completeness
from ai_accounting.kernel.period_balance_freeze import (
    persist_balance_freeze,
    prepare_balance_freeze,
)
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.read_indexes import sync_close
from ai_accounting.kernel.report_classification_directory import (
    DERIVED_ROOT_NAME as REPORT_CLASSIFICATION_ROOT,
)
from ai_accounting.kernel.report_classification_directory import (
    persist_classification_directory,
    prepare_classification_directory,
)
from ai_accounting.kernel.report_flow import persist_report_flow, prepare_report_flow
from ai_accounting.kernel.report_projection import (
    persist_report_projection,
    prepare_report_projection,
)
from ai_accounting.kernel.report_semantics import (
    persist_report_semantics,
    prepare_report_semantics,
)
from ai_accounting.kernel.reports import (
    Reports,
    _applicable_profile,
    _closed_period_issues,
    _periods,
    _report_references,
)
from ai_accounting.kernel.settlement_freeze import (
    persist_freeze_projection,
    prepare_freeze_projection,
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
    """Complete contract envelopes isolate reference cutoffs and deliberate gaps."""
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        previous = connection.execute(
            "SELECT period,digest FROM period_close ORDER BY period DESC LIMIT 1"
        ).fetchone()
        proof = connection.execute("SELECT digest FROM evidence LIMIT 1").fetchone()[0].hex()
        for period, facts in rows:
            manifest = {
                "format": CLOSE_FORMAT,
                "format_version": CLOSE_FORMAT_VERSION,
                "period": period,
                "company_id": engine.store.company_id,
                "database_id": engine.store.database_id,
                "previous_close_period": str(YearMonth.from_ordinal(previous[0]))
                if previous
                else None,
                "previous_close_digest": previous[1].hex() if previous else None,
                "publication_sequence": 0,
                "adopted_results": [],
                "vouchers": [],
                "opening_calculation_id": None,
                "asset_batch_adoptions": [],
                "asset_card_adoptions": [],
                "inventories": {},
                "owner_confirmation": proof,
                "readiness": {"financial_reports": {"facts": facts, "calculations": []}},
                "management_snapshot": {
                    "entity_profiles": [],
                    "employee_entities": [],
                },
                "material_coverage": check_completeness(
                    connection, YearMonth(period).ordinal, engine.store.registry
                ),
                "trial_balance": [],
                "report_classification": {},
                "read_version": {
                    "accounting": 0,
                    "material": 0,
                    "management": 0,
                    "read_repair_revision": 0,
                },
                "approval": None,
            }
            manifest["owner_review"] = build_owner_review(connection, engine, manifest)
            month = YearMonth(period).ordinal
            checksum = digest(manifest)
            settlement = prepare_freeze_projection(connection, month, checksum, 0)
            report = prepare_report_projection(engine, connection, month, manifest, checksum)
            semantics = prepare_report_semantics(engine, connection, month, report.rows, checksum)
            flow = prepare_report_flow(engine, connection, month, report, semantics)
            classifications = prepare_classification_directory(
                connection, month, checksum, manifest, flow.content
            )
            balances = prepare_balance_freeze(connection, month, checksum, 0)
            write_close(
                connection,
                month,
                manifest,
                projection_roots={
                    "settlement": settlement.root_digest,
                    "report": report.root_digest,
                    "report_semantics": semantics.root_digest,
                    "report_flow": flow.root_digest,
                    REPORT_CLASSIFICATION_ROOT: classifications.root_digest,
                    "period_balance": balances.root_digest,
                },
            )
            persist_freeze_projection(connection, settlement)
            sync_close(connection, month)
            persist_report_projection(connection, report)
            persist_report_semantics(connection, semantics)
            persist_report_flow(connection, flow)
            persist_classification_directory(connection, classifications)
            persist_balance_freeze(connection, balances)
            previous = (month, digest(manifest))
        connection.commit()


def test_batch_coverage_matches_original_contract_and_reads_authenticated_closes(book, monkeypatch):
    reports = scenario(book)
    close_quarter(book)
    quarters = [(2026, 2), (2025, 4), (2026, 1)]
    with reports.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = {key: legacy_coverage(reports, connection, *key) for key in quarters}
    verified = []
    original = QueryReads.close_readiness_check

    def verify(reads, row, name):
        if name == "financial_reports":
            verified.append(row["period"])
        return original(reads, row, name)

    def no_statements(*args, **kwargs):
        raise AssertionError("quarter coverage must not build financial statements")

    monkeypatch.setattr(QueryReads, "close_readiness_check", verify)
    monkeypatch.setattr(reports, "_report", no_statements)
    actual = reports.closed_period_coverages(quarters)
    assert actual == expected
    assert actual[2026, 1]["complete"] is True
    assert actual[2026, 2]["complete"] is False
    assert verified == [YearMonth(month).ordinal for month in ("2026-01", "2026-02", "2026-03")]
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


def test_batch_uses_authenticated_fact_ids_when_reverse_directory_is_damaged(book):
    engine = book[0]
    fact = save_profile(book, "first", "2026-01", "2026-01")
    insert_closes(engine, [("2026-03", [fact, "non-profile-reference"])])
    before = Reports(engine).closed_period_coverages([(2026, 1), (2026, 2)])
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
    after = Reports(engine).closed_period_coverages([(2026, 1), (2026, 2)])
    assert after == before


def test_coverage_locates_profile_before_irrelevant_frozen_references(book):
    engine = book[0]
    profile_id = save_profile(book, "first", "2026-01", "2026-01")
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT digest FROM evidence LIMIT 1").fetchone()[0].hex()
    saved = engine.save_facts(
        [
            {
                "kind": "report_income_tax_confirmation",
                "subject_id": f"unrelated-tax-{index}",
                "data": {
                    "period": "2026-03",
                    "treatment": "zero",
                    "cumulative_assessed_fen": 0,
                    "explanation": f"Synthetic unrelated source {index}",
                },
                "evidence": [proof],
                "expected_revision": 0,
            }
            for index in range(1000)
        ],
        request_id="coverage-unrelated-tax-batch",
    )
    unrelated = [row["fact_id"] for row in saved["results"]]
    amended = book[1](
        "report_income_tax_confirmation",
        "unrelated-tax-0",
        {
            "period": "2026-03",
            "treatment": "zero",
            "cumulative_assessed_fen": 0,
            "explanation": "Synthetic revised unrelated source",
        },
        revision=1,
    )["fact_id"]
    unrelated[0] = amended
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM fact_revision f "
            "JOIN subject s ON s.id=f.subject_id "
            "JOIN fact_report_income_tax_confirmation t ON t.revision_id=f.id "
            "WHERE f.id IN (SELECT value FROM json_each(?)) "
            "AND s.kind='report_income_tax_confirmation'",
            (canonical(unrelated),),
        ).fetchone()[0] == 1000
    insert_closes(engine, [("2026-03", [profile_id, *unrelated])])
    reports = Reports(engine)

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = legacy_coverage(reports, connection, 2026, 1)
    with QueryReads.snapshot(engine) as reads:
        steps = 0

        def tick():
            nonlocal steps
            steps += 100
            return 0

        reads.connection.set_progress_handler(tick, 100)
        try:
            actual = reports.closed_period_coverages(
                [(2026, 1)], connection=reads.connection, reads=reads
            )[2026, 1]
        finally:
            reads.connection.set_progress_handler(None, 0)
    assert actual == expected
    assert actual["bookkeeping_start"] == "2026-01"
    assert steps < 10_000, steps
