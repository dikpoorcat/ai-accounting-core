"""Owner open reports omit export-only identities while retaining accounting proof."""

import pytest
import test_opening_continuation as opening_cases
import test_reports as report_cases
from test_integrity_content import damage
from test_new_company_reports import profile as opening_profile
from test_new_company_reports import zero_tax

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.types import YearMonth

book = report_cases.book
opening_book = opening_cases.book


def open_plan(engine, report, quarter, *, narrow):
    with QueryReads.snapshot(engine) as reads:
        try:
            plan = report._report(
                2026,
                quarter,
                source="open",
                connection=reads.connection,
                reads=reads,
                _issues_only=narrow,
            )
        except KernelError as error:
            return {"error_code": error.code, "error_details": error.details}
    # Only the unused open export identity list and its digest may differ.
    return {key: value for key, value in plan.items() if key not in {"report_fact_ids", "digest"}}


@pytest.mark.parametrize(
    "case",
    [
        "open",
        "partial_frozen",
        "open_revision",
        "closed_reversal",
        "closed_download",
        "missing_frozen_month",
        "classification_conflict",
        "damaged_necessary_source",
    ],
)
def test_owner_open_selection_retains_full_report_semantics(book, case):
    engine, save, publish, close = book
    report = report_cases.scenario(book)
    quarter = 1
    if case == "partial_frozen":
        close("2026-01")
        close("2026-02")
    if case in {"closed_reversal", "closed_download", "missing_frozen_month"}:
        report_cases.close_quarter(book)
    closed_before = (
        report.report(2026, 1, source="closed")
        if case in {"closed_reversal", "closed_download"}
        else None
    )
    if case in {"open_revision", "closed_reversal"}:
        save(
            "expense",
            "cost",
            {
                "period": "2026-02",
                "counterparty_id": "supplier",
                "amount_fen": 15000,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            revision=1,
        )
        publish("cost", posting_period="2026-04" if case == "closed_reversal" else None)
        if case == "closed_reversal":
            quarter = 2
            report_cases.cit(save, publish, "2026-06")
    if case == "classification_conflict":
        with engine.store.connection(read_only=True) as connection:
            version = connection.execute(
                "SELECT voucher_version_id FROM fact_report_classification LIMIT 1"
            ).fetchone()[0]
        save(
            "report_classification",
            "conflicting",
            {
                "period": "2026-02",
                "voucher_version_id": version,
                "profit_details": [
                    {"line_no": 1, "detail_code": "management_other", "amount_fen": 10000}
                ],
            },
        )
    if case == "missing_frozen_month":
        damage(
            engine,
            "period_close",
            "DELETE FROM period_close WHERE period=?",
            (YearMonth("2026-02").ordinal,),
            foreign_keys=False,
        )
    if case == "damaged_necessary_source":
        with engine.store.connection(read_only=True) as connection:
            identifier = connection.execute(
                "SELECT fact_id FROM fact_current WHERE subject_id='profile'"
            ).fetchone()[0]
        damage(
            engine,
            "fact_report_profile",
            "UPDATE fact_report_profile SET company_name='damaged' WHERE revision_id=?",
            (identifier,),
        )
    full = open_plan(engine, report, quarter, narrow=False)
    assert open_plan(engine, report, quarter, narrow=True) == full
    if case in {"missing_frozen_month", "damaged_necessary_source"}:
        assert full["error_code"] == (
            "read_index_integrity_failed"
            if case == "missing_frozen_month"
            else "content_integrity_failed"
        )
    if case == "classification_conflict":
        assert full["status"] == "needs_information" and full["fact_issues"]
    if closed_before is not None:
        closed_after = report.report(2026, 1, source="closed")
        # A later publication advances current epochs, not frozen report content or digest.
        assert {k: v for k, v in closed_after.items() if k != "epochs"} == {
            k: v for k, v in closed_before.items() if k != "epochs"
        }


def test_owner_open_selection_retains_independent_opening(opening_book):
    engine, save, _, package, _ = opening_book
    package([], period="2026-01")
    opening_profile(save, "2026-01")
    zero_tax(save, 2026, 1)
    report = Reports(engine)
    assert open_plan(engine, report, 1, narrow=True) == open_plan(engine, report, 1, narrow=False)
