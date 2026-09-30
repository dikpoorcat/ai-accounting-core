"""First-wage deduction clocks apply prospectively from their confirmed month."""

import pytest
from test_payroll import (
    as_calculation,
    calculate_payroll,
    context_for,
    contribution_policy,
    income_tax_policy,
    opening,
    payroll,
    payroll_sources,
    profile,
    version,
    with_payroll_plan,
)
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import Read
from ai_accounting.kernel.domains import payroll as domain
from ai_accounting.kernel.domains.payroll import (
    PayrollBounded,
    PayrollFirstWageTreatment,
    first_wage_read,
)
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.settlement_projection import settlement_dashboard_open
from ai_accounting.kernel.types import YearMonth


def treatment(period="2026-07"):
    return PayrollFirstWageTreatment(
        period=period,
        employee_id="employee",
        standard_deduction_start_month=1,
    )


def sources():
    return [item for item in payroll_sources() if item.subject_id != "profile"] + [
        version(profile(withholding_start_date="2026-06-01"), "profile")
    ]


@pytest.mark.parametrize("bounded", [False, True])
def test_future_treatment_does_not_change_june_but_applies_july_and_august(bounded):
    def wage(period):
        fact = payroll(period=period)
        return PayrollBounded.model_validate(fact.model_dump(mode="json")) if bounded else fact

    june = version(wage("2026-06"), "june")
    confirmed = version(treatment(), "first-wage")
    original = calculate_payroll(june, context_for(june, with_payroll_plan(june, sources())))
    ctx = context_for(june, with_payroll_plan(june, [*sources(), confirmed]))
    checked = calculate_payroll(june, ctx)
    assert checked == original
    assert confirmed.id not in ctx.versions
    july = version(wage("2026-07"), "july")
    july_ctx = context_for(
        july, with_payroll_plan(july, [*sources(), confirmed]), [as_calculation(june, checked)]
    )
    july_result = calculate_payroll(july, july_ctx)
    assert confirmed.id in july_ctx.versions
    assert july_result.values["tax_state"]["cumulative_standard_deduction_fen"] == 3_500_000
    august = version(wage("2026-08"), "august")
    august_ctx = context_for(
        august,
        with_payroll_plan(august, [*sources(), confirmed]),
        [as_calculation(june, checked), as_calculation(july, july_result)],
    )
    august_result = calculate_payroll(august, august_ctx)
    assert confirmed.id in august_ctx.versions
    assert august_result.values["tax_state"]["cumulative_standard_deduction_fen"] == 4_000_000


@pytest.mark.parametrize("period,cutoff", [("2026-12", "2027-01"), ("9999-12", None)])
def test_december_exclusive_bound_and_last_representable_month(period, cutoff):
    read = first_wage_read("employee", YearMonth(period))
    assert read.before_period == (YearMonth(cutoff) if cutoff else None)
    assert read in payroll(period=period).reads()


def frozen(company):
    with company.engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT period,digest,manifest FROM period_close ORDER BY period"
            )
        ]


def test_new_june_publication_does_not_become_pending_for_future_marker(tmp_path):
    company = Company(tmp_path / "bounded-first-wage-read.sqlite")
    company.save(
        profile(
            period="2026-06",
            effective_from="2026-06",
            effective_to="2026-09",
            withholding_start_date="2026-06-01",
        ),
        "profile",
    )
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    company.save(opening(period="2026-06"), "opening")
    company.save(payroll(period="2026-06"), "june")
    company.confirm_payroll("june")
    company.publish("june")
    old = company.current("june")
    saved = company.save(treatment(), "first-wage-july")
    assert "june" not in saved["pending"]
    assert "june" not in company.pending()
    assert company.current("june") == old


def test_saved_unbounded_dependency_can_be_reviewed_without_rewriting_closed_payroll(
    tmp_path, monkeypatch
):
    company = Company(tmp_path / "first-wage.sqlite")
    company.save(
        profile(
            period="2026-06",
            effective_from="2026-06",
            effective_to="2026-09",
            withholding_start_date="2026-06-01",
        ),
        "profile",
    )
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    company.save(opening(period="2026-06"), "opening")
    company.save(payroll(period="2026-06"), "june")
    company.confirm_payroll("june")
    # Reproduce the historical implementation through production publication;
    # no dependency rows or pending flags are inserted directly.
    with monkeypatch.context() as old:
        old.setattr(
            domain,
            "first_wage_read",
            lambda employee, period: Read(
                "fact", PayrollFirstWageTreatment.kind, domain.employee_year(employee, period)
            ),
        )
        company.publish("june")
        company.close("2026-06")
    original = company.current("june")
    original_ledger = company.engine.ledger("2026-06")
    original_close = frozen(company)
    with company.engine.store.connection(read_only=True) as connection:
        original_settlements = settlement_dashboard_open(connection, "2026-06", current=True)
    saved = company.save(treatment(), "first-wage-july")
    assert "june" in saved["pending"]
    assert "june" in company.pending()
    preview, published = company.publish("june")
    assert published["june"]["impact"] == "review_no_impact"
    assert company.current("june").values == original.values
    assert company.engine.ledger("2026-06") == original_ledger
    assert frozen(company) == original_close
    assert "june" not in company.pending()
    with company.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(company.engine, connection)["status"] == "verified"
        assert settlement_dashboard_open(connection, "2026-06", current=True) == (
            original_settlements
        )
    trace = company.engine.trace(company.current("june").id)
    assert saved["fact_id"] not in {row["id"] for row in trace["facts"]}
    company.save(payroll(period="2026-07"), "july")
    company.confirm_payroll("july")
    company.publish("july")
    assert (
        company.current("july").values["tax_state"]["cumulative_standard_deduction_fen"]
        == 3_500_000
    )
    future = company.save(treatment("2026-09"), "first-wage-september")
    assert {"june", "july"}.isdisjoint(future["pending"])
    assert {"june", "july"}.isdisjoint(company.pending())
    assert frozen(company) == original_close
    with company.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(company.engine, connection)["status"] == "verified"
