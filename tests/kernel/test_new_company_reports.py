"""A verified empty formation boundary is different from taking over an existing ledger."""

import pytest
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as book  # noqa: F401

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.types import YearMonth


def profile(save, start, *, continuation=False, effective=None):
    kind = "continuation_report_profile" if continuation else "report_profile"
    data = {
        "period": effective or start,
        "company_name": "Synthetic newly established company",
        "accounting_standard": "small_enterprise",
        "bookkeeping_start": start,
    }
    data.update(
        {"opening_package_id": "opening"}
        if continuation
        else {"newly_established_zero_opening_confirmed": True}
    )
    return save(kind, "profile", data)


def zero_tax(save, year, quarter):
    return save(
        "report_income_tax_confirmation",
        "income-tax",
        {
            "period": f"{year:04d}-{quarter * 3:02d}",
            "treatment": "zero",
            "cumulative_assessed_fen": 0,
            "explanation": "Explicit synthetic zero tax confirmation",
        },
    )


def fields(result):
    return {issue["field"] for issue in result["fact_issues"]}


@pytest.mark.parametrize("start,quarter", [("2025-12", 4), ("2026-08", 3)])
def test_empty_formation_opening_allows_new_company_report_and_freezes_its_source(
    book, start, quarter
):
    engine, save, _, package, proof = book
    package([], period=start)
    profile(save, start)
    year = int(start[:4])
    zero_tax(save, year, quarter)
    reports = Reports(engine)
    preview = reports.report(year, quarter)
    assert preview["status"] == "ready", preview["fact_issues"]
    assert all(
        value == 0
        for statement in preview["statements"].values()
        for row in statement.values()
        for name, value in row.items()
        if name.endswith("_fen")
    )
    opening = preview["opening_source"]
    assert opening["period"] == start and opening["subject_id"] == "opening"
    assert opening["fact_id"] in preview["report_fact_ids"]
    end = YearMonth(f"{year:04d}-{quarter * 3:02d}")
    for ordinal in range(YearMonth(start).ordinal, end.ordinal + 1):
        _close_without_current_business(engine, str(YearMonth.from_ordinal(ordinal)), proof)
    manifest = Periods(engine).closed_report(start)
    assert opening["calculation_id"] in manifest["calculations"]
    assert opening["fact_id"] in manifest["facts"]
    frozen = reports.preview_export(year, quarter)
    assert frozen["opening_source"] == opening
    assert all(check["passed"] for check in frozen["checks"])


def test_closed_new_company_report_uses_exact_opening_fact_even_after_a_later_correction(book):
    engine, save, _, package, proof = book
    data = package([], period="2025-12")
    profile(save, "2025-12")
    zero_tax(save, 2025, 4)
    _close_without_current_business(engine, "2025-12", proof)
    reports = Reports(engine)
    before = reports.preview_export(2025, 4)
    engine.amend_fact(
        "opening_package",
        "opening",
        data | {"period": "2026-01"},
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="synthetic-later-opening-correction",
    )
    after = reports.preview_export(2025, 4)
    for name in ("statements", "opening_source", "report_fact_ids", "source_closes", "digest"):
        assert after[name] == before[name]


@pytest.mark.parametrize(
    "members",
    [
        [("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 0})],
        [
            ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 1000}),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 1000,
                    "holder_or_basis_id": "owner",
                },
            ),
        ],
    ],
)
def test_nonempty_or_nonzero_opening_cannot_be_declared_a_new_company_zero_boundary(book, members):
    engine, save, _, package, proof = book
    package(members, period="2026-08")
    profile(save, "2026-08")
    zero_tax(save, 2026, 3)
    result = Reports(engine).report(2026, 3)
    assert "continuation_report_profile" in fields(result)
    assert "report_carry_forward" in fields(result)
    with pytest.raises(KernelError) as rejected:
        _close_without_current_business(engine, "2026-08", proof)
    assert rejected.value.code == "period_not_ready"
    assert any(
        item["field"] == "continuation_report_profile"
        for item in rejected.value.details["fact_issues"]
    )


def test_zero_opening_at_a_different_boundary_still_requires_continuation(book):
    engine, save, _, package, _ = book
    package([], period="2026-07")
    profile(save, "2026-08")
    zero_tax(save, 2026, 3)
    result = Reports(engine).report(2026, 3)
    assert "continuation_report_profile" in fields(result)
    assert "report_carry_forward" in fields(result)


def test_explicit_midyear_continuation_needs_real_prior_flows_even_when_opening_is_zero(book):
    engine, save, _, package, proof = book
    package([], period="2026-08")
    profile(save, "2026-08", continuation=True)
    zero_tax(save, 2026, 3)
    result = Reports(engine).report(2026, 3)
    assert "continuation_report_profile" not in fields(result)
    assert "report_carry_forward" in fields(result)
    # Existing policy permits statement comparatives to follow close; they still
    # cannot be omitted from the actual quarterly report/export.
    _close_without_current_business(engine, "2026-08", proof)
    _close_without_current_business(engine, "2026-09", proof)
    with pytest.raises(KernelError) as rejected:
        Reports(engine).preview_export(2026, 3)
    assert rejected.value.code == "needs_information"


def test_new_company_profile_can_confirm_formation_month_later_without_changing_boundary(book):
    engine, save, _, package, _ = book
    package([], period="2026-08")
    profile(save, "2026-08", effective="2026-09")
    zero_tax(save, 2026, 3)
    assert Reports(engine).report(2026, 3)["status"] == "ready"
