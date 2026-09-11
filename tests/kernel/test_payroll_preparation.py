from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel import payroll_preparation as preparation


def company(tmp_path):
    result = Company(tmp_path / "prepare.sqlite")
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
    ):
        result.save(fact, subject)
    result.publish("january")
    return result


def confirm_no_change(company):
    service = preparation.PayrollPreparation(company.engine)
    basis = service.reuse_basis("2026-02")
    company.save(
        preparation.PayrollNoChange(
            period="2026-02",
            prior_period="2026-01",
            basis_digest=basis["basis_digest"],
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "no-change",
    )
    return service


def test_previous_payroll_is_not_an_implicit_monthly_salary(tmp_path):
    instance = company(tmp_path)
    service = preparation.PayrollPreparation(instance.engine)
    assert service.prepare("2026-02")["status"] == "needs_information"
    service = confirm_no_change(instance)
    plan = service.prepare("2026-02")
    assert plan["status"] == "ready"
    assert plan["candidates"][0]["source"] == "explicit_no_change"
    before = instance.count("voucher")
    saved = service.confirm("2026-02", preview_digest=plan["digest"], request_id=instance.request())
    assert instance.count("voucher") == before
    subject = saved["results"][0]["subject_id"]
    instance.publish(subject)
    assert (
        instance.current(subject).values["gross_fen"]
        == instance.current("january").values["gross_fen"]
    )
    assert not service.prepare("2026-02")["candidates"]


def test_known_deduction_change_invalidates_no_change_and_monthly_plan_resolves_it(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    instance.save(
        preparation.PayrollChangeNotice(
            period="2026-02",
            employee_id="employee",
            changed_fields=("special_additional_deduction",),
        ),
        "change",
    )
    assert service.prepare("2026-02")["status"] == "needs_information"
    instance.save(
        preparation.PayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=payroll(period="2026-02", special_additional_deduction_fen=120000),
        ),
        "plan",
    )
    proposal = service.prepare("2026-02")
    assert proposal["status"] == "ready"
    assert proposal["candidates"][0]["data"]["special_additional_deduction_fen"] == 120000


def test_employee_profile_revision_prevents_silent_reuse(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    instance.save(profile(social_insurance_base_fen=700000), "profile", revision=1)
    assert service.prepare("2026-02")["status"] == "needs_information"
    instance.publish("january")
    assert service.prepare("2026-02")["status"] == "needs_information"
