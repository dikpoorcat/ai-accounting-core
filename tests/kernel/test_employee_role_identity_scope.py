"""A closed employee list must prove every role used to assign a wage head."""

import pytest
from test_dashboard_model_transactions import raw_read
from test_integrity_content import verify
from test_payroll import contribution_policy, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import _produce
from ai_accounting.kernel.entities import Entities


@pytest.mark.parametrize("gross_fen", [0, 1_000_000], ids=["old-zero-line", "old-posted"])
@pytest.mark.parametrize("subject", ["january-a", "february-a"])
def test_closed_historical_payroll_role_must_be_proved_before_employee_selection(
    tmp_path, gross_fen, subject
):
    company = Company(tmp_path / f"closed-payroll-role-{gross_fen}.sqlite")
    for fact, fact_subject in (
        (
            profile(social_insurance_base_fen=0, social_insurance_participating=False),
            "profile-b",
        ),
        (contribution_policy(), "contributions"),
        (
            payroll(profile_id="profile-b", accounting_gross_salary_fen=gross_fen),
            "january-b",
        ),
        (
            payroll(
                period="2026-02", profile_id="profile-b", accounting_gross_salary_fen=gross_fen
            ),
            "february-b",
        ),
        (
            profile(
                employee_id="employee-a",
                social_insurance_base_fen=0,
                social_insurance_participating=False,
            ),
            "profile-a",
        ),
        (
            payroll(
                employee_id="employee-a",
                profile_id="profile-a",
                accounting_gross_salary_fen=gross_fen,
            ),
            "january-a",
        ),
        (
            payroll(
                period="2026-02",
                employee_id="employee-a",
                profile_id="profile-a",
                accounting_gross_salary_fen=gross_fen,
            ),
            "february-a",
        ),
    ):
        company.save(fact, fact_subject)
    for employee in ("employee", "employee-a"):
        proof = company.engine.register_evidence(
            (f"Synthetic confirmation: {employee} was hired on 2026-01-01 "
             "and remains employed.").encode(),
            "text/plain", "synthetic workforce confirmation", request_id="hire-proof-" + employee,
        )["digest"]
        Entities(company.engine).register_worker(
            {"display_name": employee, "action": "hire", "effective_date": "2026-01-01"},
            entity_id=employee, expected_revision=1, evidence_digest=proof,
            source="explicit synthetic workforce timeline", request_id="hire-" + employee,
        )
    company.confirm_payroll("january-a", "january-b", "february-a", "february-b")
    for source in ("january-a", "january-b", "february-a", "february-b"):
        company.tax_basis(source, 0 if gross_fen == 0 else 12_600)
    company.publish("january-a", "january-b", "february-a", "february-b")
    company.close("2026-01")
    company.close("2026-02")
    # Authoritative writes queue the asynchronous card; make its certified
    # February overview available before testing that independent consumer.
    produced = _produce(company.engine, "2026-02")
    assert produced["status"] == "ready"
    dashboard = Dashboard(company.engine)
    assert dashboard.employees("2026-02")["snapshot_version"] == produced["model_version"]

    def read(period="2026-02", **kwargs):
        response = dashboard.employees(period, preparation="deferred", **kwargs)
        # Native details and the certified default carry different version IDs.
        response.pop("snapshot_version", None)
        return response

    baseline = read()
    employees = baseline["data"]["collections"]["employees"]["items"]
    assert {row["employee_id"] for row in employees} == {"employee", "employee-a"}
    assert baseline["data"]["employees"]["gross_salary_fen"] == 2 * gross_fen
    assert {
        row["employee_id"]: (row["gross_salary_fen"], row["wage_tax_scope"]) for row in employees
    } == {
        "employee": (gross_fen, "wage_income"),
        "employee-a": (gross_fen, "wage_income"),
    }
    focused = {"section": "employees", "employee_id": "employee-a"}
    baseline_a = read(**focused)
    assert len(baseline_a["data"]["collections"]["employees"]["items"]) == 1
    baseline_all = read(employee_filter="all")
    with raw_read():
        assert read() == baseline
        assert read(**focused) == baseline_a
        assert read(employee_filter="all") == baseline_all
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
            # The produced overview retains its exact original payroll identity
            # and money; the mutable role directory is not a returned raw hit.
            assert read() == baseline
            if subject == "january-a":
                # February's native cohort does not use January's old role.
                assert read(**focused) == baseline_a
                assert read(employee_filter="all") == baseline_all
                with raw_read():
                    assert read() == baseline
                    assert read(**focused) == baseline_a
                selected_period = "2026-01"
            else:
                selected_period = "2026-02"
                # Focused cards remain bounded native reads and must authenticate
                # the current month's role even beside an available overview.
                with pytest.raises(KernelError) as failure:
                    read(**focused)
                assert failure.value.code == "entity_reference_corrupt"
            # Native selection of the damaged role's actual frozen month must
            # prove that role before assigning either list or focused card.
            for kwargs in ({}, focused, {"employee_filter": "all"}):
                with raw_read(), pytest.raises(KernelError) as failure:
                    read(selected_period, **kwargs)
                assert failure.value.code == "entity_reference_corrupt"
            with pytest.raises(KernelError) as failure:
                verify(company.engine)
            assert failure.value.code == "entity_reference_corrupt"
            with pytest.raises(BackupError):
                verify_file(company.engine.store.path)
        finally:
            with company.engine.store.connection() as connection:
                connection.execute(
                    f"UPDATE entity_reference_recorded SET {field}=? "
                    "WHERE fact_id=? AND role='employee'",
                    (original[field], original["fact_id"]),
                )
    restored_a = read(**focused)
    assert restored_a == baseline_a
