import pytest
from test_payroll import actual, contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel import payroll_preparation as preparation
from ai_accounting.kernel.contracts import NeedsInformation, Read
from ai_accounting.kernel.payroll_confirmation import revision_reference


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
    result.confirm_payroll("january")
    result.publish("january")
    return result


def confirm_no_change(company):
    service = preparation.PayrollPreparation(company.engine)
    basis = service.reuse_basis("2026-02")
    company.save(
        preparation.PayrollNoChange(
            period="2026-02",
            prior_period="2026-01",
            employees=tuple(basis["employees"]),
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
    assert plan["tax_import_mapping"]["status"] == "pending_publication"
    assert plan["tax_import_mapping"]["blocking_scope"] == "tax_import_file"
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
    after = service.prepare("2026-02")
    assert not after["candidates"]
    assert after["status"] == "ready" and not after["fact_issues"]
    assert after["tax_import_mapping"]["status"] == "needs_information"
    assert after["tax_import_mapping"]["blocking_scope"] == "tax_import_file"


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
            **plan_references(instance),
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
    with pytest.raises(NeedsInformation):
        instance.publish("january")
    instance.confirm_payroll("january")
    instance.publish("january")
    assert service.prepare("2026-02")["status"] == "needs_information"


def test_notice_recorded_after_plan_invalidates_that_plan(tmp_path):
    instance = company(tmp_path)
    instance.save(
        preparation.PayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=payroll(period="2026-02"),
            **plan_references(instance),
        ),
        "february-plan",
    )
    service = preparation.PayrollPreparation(instance.engine)
    assert service.prepare("2026-02")["status"] == "ready"
    instance.save(
        preparation.PayrollChangeNotice(
            period="2026-02",
            employee_id="employee",
            changed_fields=("salary",),
        ),
        "late-notice",
    )
    result = service.prepare("2026-02")
    assert result["status"] == "needs_information"
    assert any(
        issue["field"] == "payroll_plan.change_notice_revisions" for issue in result["fact_issues"]
    )


def test_no_change_rechecks_complete_active_roster(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    instance.save(
        profile(employee_id="new-employee", effective_from="2026-02"),
        "new-profile",
    )
    result = service.prepare("2026-02")
    assert result["status"] == "needs_information"
    assert any(issue["field"] == "payroll_no_change.employees" for issue in result["fact_issues"])


def test_no_change_calculation_tracks_empty_full_roster_scope(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    proposal = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    subject = saved["publish_subjects"][0]
    instance.publish(subject)
    instance.save(
        profile(employee_id="later-employee", effective_from="2026-02"),
        "later-profile",
    )
    assert subject in instance.pending()


def test_new_plan_prepares_revision_of_existing_wage_without_publishing(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    first = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=first["digest"], request_id=instance.request()
    )
    february_subject = saved["publish_subjects"][0]
    instance.publish(february_subject)
    before = instance.count("voucher")
    instance.save(
        preparation.PayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=payroll(period="2026-02", accounting_gross_salary_fen=1_100_000),
            **plan_references(instance),
        ),
        "revised-february-plan",
    )
    proposal = service.prepare("2026-02")
    assert proposal["status"] == "ready"
    assert len(proposal["candidates"]) == 1
    assert proposal["candidates"][0]["subject_id"] == february_subject
    assert proposal["candidates"][0]["expected_revision"] == 1
    result = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    assert result["publish_subjects"] == [february_subject]
    assert instance.count("voucher") == before
    with instance.engine.store.connection(read_only=True) as connection:
        assert instance.engine.store.current_fact(connection, february_subject).revision == 2


def test_no_change_cannot_hide_changed_wage_input(tmp_path):
    instance = company(tmp_path)
    service = preparation.PayrollPreparation(instance.engine)
    basis = service.reuse_basis("2026-02")
    employee = basis["employees"][0]
    employee["payroll"]["accounting_gross_salary_fen"] += 1
    instance.save(
        preparation.PayrollNoChange(
            period="2026-02",
            prior_period="2026-01",
            employees=(employee,),
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "false-no-change",
    )
    result = service.prepare("2026-02")
    assert result["status"] == "needs_information"
    assert any(
        issue["field"] == "payroll_no_change.employees.payroll" for issue in result["fact_issues"]
    )


def test_not_started_wage_needs_no_unadopted_income_tax_policy_for_plan_or_reuse(tmp_path):
    instance = Company(tmp_path / "not-started.sqlite")
    future_profile = profile(withholding_start_date="2026-06-01")
    january = payroll(
        accounting_gross_salary_fen=80_000,
        tax_reported_salary_fen=0,
    )
    instance.save(future_profile, "profile")
    instance.save(contribution_policy(), "contributions")
    instance.save(january, "january")
    with instance.engine.store.connection(read_only=True) as connection:
        profile_version = instance.engine.store.current_fact(connection, "profile")
        policy_version = instance.engine.store.current_fact(connection, "contributions")
    instance.save(
        preparation.PayrollPlan(
            period="2026-01",
            employee_id="employee",
            payroll=january,
            profile_revision=revision_reference(profile_version),
            contribution_policy_revision=revision_reference(policy_version),
            income_tax_policy_revision=None,
        ),
        "january-plan",
    )
    instance.publish("january")
    assert instance.current("january").values["tax_status"] == "not_started"

    service = preparation.PayrollPreparation(instance.engine)
    basis = service.reuse_basis("2026-02")
    assert not basis["fact_issues"]
    assert basis["employees"][0]["income_tax_policy_revision"] is None
    instance.save(
        preparation.PayrollNoChange(
            period="2026-02",
            prior_period="2026-01",
            employees=tuple(basis["employees"]),
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "february-no-change",
    )
    proposal = service.prepare("2026-02")
    assert proposal["status"] == "ready"
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    instance.publish(saved["publish_subjects"][0])


def test_prepare_does_not_turn_undeclared_read_into_owner_question(tmp_path, monkeypatch):
    instance = company(tmp_path)
    instance.save(
        preparation.PayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=payroll(period="2026-02"),
            **plan_references(instance),
        ),
        "february-plan",
    )

    def broken_resolver(*args, **kwargs):
        raise preparation.KernelError("undeclared_read", "injected calculator defect")

    monkeypatch.setattr(preparation, "resolve_payroll_confirmation", broken_resolver)
    with pytest.raises(preparation.KernelError) as error:
        preparation.PayrollPreparation(instance.engine).prepare("2026-02")
    assert error.value.code == "undeclared_read"


def test_prior_recalculation_wait_does_not_require_new_no_change_confirmation(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    proposal = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    february_subject = saved["publish_subjects"][0]
    instance.publish(february_subject)
    with instance.engine.store.connection(read_only=True) as connection:
        original_confirmation = instance.engine.store.current_fact(connection, "no-change")
    instance.save(actual(), "january-actual")

    waiting = service.prepare("2026-02")
    assert waiting["status"] == "needs_information"
    assert any(
        issue.get("code") == "pending_publication" and issue["field"] == "prior_payroll"
        for issue in waiting["fact_issues"]
    )
    assert not any(
        issue["field"].startswith("payroll_no_change") for issue in waiting["fact_issues"]
    )
    with pytest.raises(preparation.KernelError) as blocked:
        instance.publish(february_subject)
    assert blocked.value.code == "pending_upstream"

    instance.publish("january")
    ready = service.prepare("2026-02")
    assert ready["status"] == "ready"
    assert ready["existing"][0]["source_fact_id"] == original_confirmation.id
    instance.publish(february_subject)
    with instance.engine.store.connection(read_only=True) as connection:
        confirmation = instance.engine.store.current_fact(connection, "no-change")
    assert confirmation.id == original_confirmation.id
    assert confirmation.revision == original_confirmation.revision == 1


def test_program_build_recalculation_keeps_no_change_confirmation(tmp_path, monkeypatch):
    from ai_accounting.kernel import engine as engine_module

    instance = company(tmp_path)
    service = confirm_no_change(instance)
    proposal = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    february_subject = saved["publish_subjects"][0]
    instance.publish(february_subject)
    january_before = instance.current("january")
    february_before = instance.current(february_subject)
    with instance.engine.store.connection(read_only=True) as connection:
        confirmation_before = instance.engine.store.current_fact(connection, "no-change")

    monkeypatch.setattr(engine_module, "PROGRAM_VERSION", "stage6-build-only-change")
    instance.publish("january")
    january_after = instance.current("january")
    assert january_after.id != january_before.id
    assert january_after.result_digest == january_before.result_digest

    ready = service.prepare("2026-02")
    assert ready["status"] == "ready"
    assert ready["existing"][0]["source_fact_id"] == confirmation_before.id
    instance.publish(february_subject)
    february_after = instance.current(february_subject)
    assert february_after.id != february_before.id
    assert february_after.result_digest == february_before.result_digest
    with instance.engine.store.connection(read_only=True) as connection:
        confirmation_after = instance.engine.store.current_fact(connection, "no-change")
    assert confirmation_after.id == confirmation_before.id
    assert confirmation_after.revision == confirmation_before.revision == 1


def plan_references(instance):
    with instance.engine.store.connection(read_only=True) as connection:
        return {
            "profile_revision": revision_reference(
                instance.engine.store.select(
                    connection, Read("fact", "payroll_profile", "@profile")
                )[0]
            ),
            "contribution_policy_revision": revision_reference(
                instance.engine.store.select(
                    connection, Read("fact", "payroll_contribution_policy", "@contributions")
                )[0]
            ),
            "income_tax_policy_revision": revision_reference(
                instance.engine.store.select(
                    connection, Read("fact", "payroll_income_tax_policy", "@income-tax")
                )[0]
            ),
            "change_notice_revisions": tuple(
                revision_reference(item)
                for item in instance.engine.store.select(
                    connection,
                    Read(
                        "fact",
                        preparation.PayrollChangeNotice.kind,
                        "employee:employee:month:2026-02",
                    ),
                )
            ),
        }
