"""Before confirmed wage withholding begins, contributions do not create tax months."""

import pytest
from test_payroll import (
    actual,
    bonus,
    bonus_sources,
    contribution_policy,
    income_tax_policy,
    opening,
    payroll,
    profile,
)
from test_payroll_bounded import bounded
from test_payroll_corrections import Company

from ai_accounting.kernel import tax_import
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.payroll import UNKNOWN_DEDUCTIONS
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.tax_import import TaxImport
from ai_accounting.kernel.workflow import ExternalCompletion, ExternalObligation, Workflow


def company(tmp_path, *, gross=42_500, shortfall="reject", kind="payroll_bounded"):
    instance = Company(tmp_path / "before-withholding.sqlite")
    instance.save(
        profile(
            period="2026-07",
            effective_from="2026-07",
            effective_to="2026-08",
            withholding_start_date="2026-08",
            contribution_shortfall=shortfall,
        ),
        "profile",
    )
    instance.save(contribution_policy(), "contributions")
    instance.save(actual(period="2026-07", employee=42_500, employer=82_500), "july-social")
    make = payroll if kind == "payroll" else bounded
    fact = make(
        period="2026-07",
        accounting_gross_salary_fen=gross,
        tax_reported_salary_fen=0,
        contribution_basis="actual_required",
    )
    instance.save(fact, "july")
    return instance


@pytest.mark.parametrize("kind", ["payroll", "payroll_bounded"])
def test_prestart_zero_net_retains_confirmed_gross_and_contribution_classification(tmp_path, kind):
    instance = company(tmp_path, kind=kind)
    instance.publish("july")
    values = instance.current("july", kind).values
    assert (values["gross_fen"], values["net_fen"], values["tax_fen"]) == (42_500, 0, 0)
    assert values["employee_contributions_fen"] == 42_500
    assert values["employer_contributions_fen"] == 82_500
    assert values["tax_status"] == "not_started"
    assert values["tax_state"] is values["prior_tax_state"] is values["tax_input"] is None
    assert "tax_state_bounds" not in values
    assert values["withholding_start"] == "2026-08"
    trace = instance.engine.trace(instance.current("july", kind).id)
    outcome = trace["calculation"]["outcome"]
    assert sum(row["debit"] for row in outcome["lines"]) == 125_000
    assert {row["kind"] for row in trace["facts"]}.isdisjoint(
        {
            "payroll_income_tax_policy",
            "payroll_opening_state",
            "payroll_first_wage_treatment",
        }
    )
    # An accounting result is neither a tax assessment nor an external filing.
    assert not any(row["step"].startswith("cumulative_") for row in outcome["explanation"])


def test_personal_contribution_shortfall_requires_explicit_company_burden(tmp_path):
    instance = company(tmp_path, gross=0)
    with pytest.raises(KernelError) as error:
        instance.publish("july")
    assert error.value.code == "negative_net_pay"
    instance.save(
        profile(
            period="2026-07",
            effective_from="2026-07",
            effective_to="2026-08",
            withholding_start_date="2026-08",
            contribution_shortfall="employer_borne",
        ),
        "profile",
        revision=1,
    )
    instance.publish("july")
    values = instance.current("july", "payroll_bounded").values
    assert values["gross_fen"] == values["employee_contributions_fen"] == 0
    assert values["employer_contributions_fen"] == 125_000


@pytest.mark.parametrize(
    "change,field",
    [
        ({"tax_reported_salary_fen": 1}, "tax_reported_salary_fen"),
        ({"accounting_gross_salary_fen": 42_501}, "accounting_gross_salary_fen"),
    ],
)
def test_prestart_branch_rejects_nonzero_tax_income_or_unclassified_net_wage(
    tmp_path, change, field
):
    instance = company(tmp_path)
    fact = bounded(
        period="2026-07",
        accounting_gross_salary_fen=42_500,
        tax_reported_salary_fen=0,
        contribution_basis="actual_required",
    ).model_copy(update=change)
    instance.save(fact, "july", revision=1)
    with pytest.raises(NeedsInformation) as error:
        instance.publish("july")
    assert error.value.issues[0]["field"] == field
    assert instance.count("voucher") == 0


def test_august_starts_exactly_one_deduction_month_and_july_social_correction_keeps_cash(tmp_path):
    instance = company(tmp_path)
    instance.publish("july")
    july = instance.current("july", "payroll_bounded")
    instance.save(income_tax_policy(), "income-tax")
    instance.save(opening(period="2026-08"), "opening")
    assert "july" not in instance.pending()  # Unused tax sources are not July dependencies.
    instance.save(actual(period="2026-08", employee=52_500, employer=82_500), "august-social")
    instance.save(
        bounded(
            period="2026-08",
            accounting_gross_salary_fen=52_500,
            tax_reported_salary_fen=52_500,
            contribution_basis="actual_required",
            **dict.fromkeys(UNKNOWN_DEDUCTIONS, 0),
        ),
        "august",
    )
    instance.publish("august")
    august = instance.current("august", "payroll_bounded")
    assert august.values["tax_state"]["cumulative_income_fen"] == 52_500
    assert august.values["tax_state"]["cumulative_standard_deduction_fen"] == 500_000
    assert august.values["tax_state"]["cumulative_employee_contributions_fen"] == 52_500
    assert august.values["prior_tax_state"]["through_period"] is None
    assert instance.current("july", "payroll_bounded") == july
    instance.save(
        Payment(
            period="2026-07",
            actual_date="2026-07-20",
            bank_account_id="bank",
            amount_fen=125_000,
            direction="outflow",
            counterparty_id="social-agency",
            allocations=(
                Allocation(
                    source_kind="payroll_bounded",
                    source_id="july",
                    obligation="employee_social",
                    amount_fen=42_500,
                ),
                Allocation(
                    source_kind="payroll_bounded",
                    source_id="july",
                    obligation="employer_social",
                    amount_fen=82_500,
                ),
            ),
        ),
        "july-paid",
    )
    instance.publish("july-paid")
    with instance.engine.store.connection(read_only=True) as connection:
        paid = instance.engine.store.current_fact(connection, "july-paid")
    instance.save(
        actual(period="2026-07", employee=42_500, employer=83_000), "july-social", revision=1
    )
    assert "august" not in instance.pending()
    instance.publish("july-social")
    with instance.engine.store.connection(read_only=True) as connection:
        assert instance.engine.store.current_fact(connection, "july-paid") == paid
    assert instance.current("july-paid", "payment").values["amount_fen"] == 125_000
    assert instance.current("august", "payroll_bounded") == august


def test_prestart_is_social_filing_basis_but_not_wage_tax_basis_or_tax_export(tmp_path):
    instance = company(tmp_path)
    for kind in ("contribution_declaration", "individual_income_tax"):
        instance.save(
            ExternalObligation(
                period="2026-07",
                obligation_kind=kind,
                start_period="2026-07",
                end_period="2026-07",
                applicability_confirmed=True,
            ),
            kind,
        )
    workflow = Workflow(instance.engine)
    with pytest.raises(KernelError) as error:
        workflow.obligation_basis("individual_income_tax")
    assert error.value.code == "basis_unpublished"
    instance.publish("july")
    social = workflow.obligation_basis("contribution_declaration")
    tax = workflow.obligation_basis("individual_income_tax")
    assert len(social["accepted_calculations"]) == 1
    assert not tax["accepted_calculations"]
    exported = TaxImport(instance.engine).preview("2026-07")
    assert not exported["rows_fen"]
    assert exported["excluded_sources"][0]["reason"] == "withholding_not_started"
    assert {row["field"] for row in exported["fact_issues"]} == {"tax_income_period"}
    # A caller may not label the contribution calculation as accepted wage-tax income.
    invalid = ExternalCompletion.model_validate_json(
        __import__("json").dumps(
            {
                **tax,
                "accepted_calculations": social["accepted_calculations"],
                "period": "2026-08",
                "completion_status": "confirmed_complete",
                "date_status": "not_established",
            }
        )
    )
    instance.save(invalid, "invalid-completion")
    with pytest.raises(KernelError) as error:
        instance.publish("invalid-completion")
    assert error.value.code == "invalid_accepted_calculation"


def test_bonus_cannot_use_prestart_contribution_as_regular_tax_state(tmp_path):
    instance = company(tmp_path)
    instance.save(income_tax_policy(), "income-tax")
    for source in bonus_sources():
        if source.subject_id in {"bonus-policy", "bonus-usage"}:
            instance.save(source.fact.model_copy(update={"period": "2026-07"}), source.subject_id)
    instance.save(
        bonus(
            period="2026-07",
            income_date="2026-07-31",
            regular_payroll_id="july",
            tax_method="combined",
        ),
        "bonus",
    )
    with pytest.raises(NeedsInformation) as error:
        instance.publish("july", "bonus")
    assert error.value.issues[0]["field"] == "regular_payroll_id"


def test_mixed_month_exports_only_started_wage_and_retains_contribution_acceptance_boundary(
    tmp_path,
):
    instance = company(tmp_path)
    instance.save(income_tax_policy(), "income-tax")
    instance.save(
        profile(
            period="2026-07",
            employee_id="other",
            effective_from="2026-07",
            effective_to="2026-07",
            withholding_start_date="2026-07",
        ),
        "other-profile",
    )
    instance.save(opening(period="2026-07", employee_id="other"), "other-opening")
    instance.save(
        payroll(
            period="2026-07",
            employee_id="other",
            profile_id="other-profile",
            tax_reported_salary_fen=500_000,
        ),
        "other-wage",
    )
    instance.publish("july", "other-wage")
    other = instance.current("other-wage")
    instance.save(
        tax_import.TaxImportDetails(
            period="2026-07",
            employee_id="other",
            payroll_result_digest=other.result_digest,
            cumulative_special_fen=dict.fromkeys(tax_import.SPECIAL_COLUMNS, 0),
            current_other_fen=dict.fromkeys(tax_import.OTHER_COLUMNS, 0),
            cumulative_personal_pension_fen=0,
            tax_relief_fen=0,
            treaty_relief_fen=0,
        ),
        "details",
    )
    instance.save(
        tax_import.TaxImportIdentity(
            period="2026-07",
            employee_id="other",
            employee_code="00001",
            name="合成员工",
            document_type="居民身份证",
            document_number="001234567890123456",
        ),
        "identity",
    )
    instance.save(
        tax_import.TaxImportMapping(
            period="2026-07",
            pension_code="pension",
            medical_code=None,
            unemployment_code=None,
        ),
        "mapping",
    )
    exported = TaxImport(instance.engine).preview("2026-07")
    assert exported["status"] == "ready", exported["fact_issues"]
    assert exported["row_count"] == len(exported["excluded_sources"]) == 1
    assert exported["excluded_sources"][0]["employee_id"] == "employee"
    for calc in (other, instance.current("july", "payroll_bounded")):
        marked = {
            item["name"]
            for item in calc.values["obligations"]
            if item.get("reimbursement_acceptance_basis") == "company_confirmation_month"
        }
        assert marked == {
            "employee_social",
            "employee_housing",
            "employer_social",
            "employer_housing",
        }
    instance.close("2026-07")
    with instance.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
