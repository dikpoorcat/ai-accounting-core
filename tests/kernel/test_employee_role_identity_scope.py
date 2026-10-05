"""A closed employee list must prove every role used to assign a wage head."""

import pytest
from test_integrity_content import verify
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard


@pytest.mark.parametrize("gross_fen", [0, 1_000_000], ids=["old-zero-line", "old-posted"])
@pytest.mark.parametrize("subject", ["january-a", "february-a"])
def test_closed_historical_payroll_role_must_be_proved_before_employee_selection(
    tmp_path, gross_fen, subject
):
    company = Company(tmp_path / f"closed-payroll-role-{gross_fen}.sqlite")
    for fact, subject in (
        (
            profile(social_insurance_base_fen=0, social_insurance_participating=False),
            "profile-b",
        ),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening-b"),
        (
            payroll(profile_id="profile-b", accounting_gross_salary_fen=gross_fen,
                    tax_reported_salary_fen=gross_fen),
            "january-b",
        ),
        (
            payroll(period="2026-02", profile_id="profile-b",
                    accounting_gross_salary_fen=gross_fen, tax_reported_salary_fen=gross_fen),
            "february-b",
        ),
        (
            profile(
                employee_id="employee-a", social_insurance_base_fen=0,
                social_insurance_participating=False,
            ),
            "profile-a",
        ),
        (opening(employee_id="employee-a"), "opening-a"),
        (
            payroll(
                employee_id="employee-a", profile_id="profile-a",
                accounting_gross_salary_fen=gross_fen, tax_reported_salary_fen=gross_fen,
            ),
            "january-a",
        ),
        (
            payroll(
                period="2026-02", employee_id="employee-a", profile_id="profile-a",
                accounting_gross_salary_fen=gross_fen, tax_reported_salary_fen=gross_fen,
            ),
            "february-a",
        ),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january-a", "january-b", "february-a", "february-b")
    company.publish("january-a", "january-b", "february-a", "february-b")
    company.close("2026-01")
    company.close("2026-02")
    dashboard = Dashboard(company.engine)
    baseline = dashboard.employees("2026-02", preparation="deferred")
    employees = baseline["data"]["collections"]["employees"]["items"]
    assert {row["employee_id"] for row in employees} == {"employee", "employee-a"}
    baseline_a = dashboard.employees(
        "2026-02", section="employees", employee_id="employee-a", preparation="deferred"
    )["data"]["collections"]["employees"]["items"]
    assert len(baseline_a) == 1
    with company.engine.store.connection() as connection:
        row = connection.execute(
            "SELECT r.* FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
            "JOIN entity_reference_recorded r ON r.fact_id=c.fact_id AND r.role='employee' "
            "WHERE h.subject_id=?",
            (subject,),
        ).fetchone()
        assert row is not None
        jan_lines = connection.execute(
            "SELECT json_array_length(c.outcome,'$.lines') FROM calculation_current h "
            "JOIN calculation c ON c.id=h.calculation_id WHERE h.subject_id='january-a'"
        ).fetchone()[0]
        assert (jan_lines == 0) is (gross_fen == 0)
        original = dict(row)
    changes = {
        "entity_id": "employee",
        "kind": "payroll_bounded",
        "period": original["period"] + 1,
        "source_digest": b"\0" * 32,
    }
    for field, bad in changes.items():
        with company.engine.store.connection() as connection:
            connection.execute(
                f"UPDATE entity_reference_recorded SET {field}=? "
                "WHERE fact_id=? AND role='employee'",
                (bad, original["fact_id"]),
            )
        try:
            if subject == "january-a":
                # Neither list nor focused card adopts this unused historical
                # role. Frozen money comes from its independently sealed basis.
                assert dashboard.employees("2026-02", preparation="deferred")["data"][
                    "employees"
                ] == baseline["data"]["employees"]
                assert dashboard.employees(
                    "2026-02", section="employees", employee_id="employee-a",
                    preparation="deferred",
                )["data"]["collections"]["employees"]["items"] == baseline_a
                with pytest.raises(KernelError) as failure:
                    verify(company.engine)
                assert failure.value.code == "entity_reference_corrupt"
                with pytest.raises(BackupError):
                    verify_file(company.engine.store.path)
            else:
                # The current role is required by both the list and the focused
                # card, and cannot be skipped just because history UI is gone.
                for kwargs in ({}, {"section": "employees", "employee_id": "employee-a"}):
                    with pytest.raises(KernelError) as failure:
                        dashboard.employees("2026-02", preparation="deferred", **kwargs)
                    assert failure.value.code == "entity_reference_corrupt"
        finally:
            with company.engine.store.connection() as connection:
                connection.execute(
                    f"UPDATE entity_reference_recorded SET {field}=? "
                    "WHERE fact_id=? AND role='employee'",
                    (original[field], original["fact_id"]),
                )
    restored_a = dashboard.employees(
        "2026-02", section="employees", employee_id="employee-a", preparation="deferred"
    )["data"]["collections"]["employees"]["items"]
    assert restored_a == baseline_a
