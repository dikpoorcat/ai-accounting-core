import pytest
from test_payroll import actual, contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel import payroll_preparation as preparation
from ai_accounting.kernel.contracts import NeedsInformation


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
    save_monthly_plan(
        instance, payroll(period="2026-02", special_additional_deduction_fen=120000), "february"
    )
    proposal = service.prepare("2026-02")
    assert proposal["status"] == "ready"
    assert not proposal["candidates"]
    with instance.engine.store.connection(read_only=True) as connection:
        assert (
            instance.engine.store.current_fact(
                connection, "february"
            ).fact.special_additional_deduction_fen
            == 120000
        )


def test_employee_profile_revision_prevents_silent_reuse(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    instance.save(profile(social_insurance_base_fen=700000), "profile", revision=1)
    assert service.prepare("2026-02")["status"] == "needs_information"
    with pytest.raises(NeedsInformation):
        instance.publish("january")
    instance.save(
        payroll(profile_revision={"subject_id": "profile", "revision": 2}), "january", revision=1
    )
    instance.confirm_payroll("january")
    instance.publish("january")
    assert service.prepare("2026-02")["status"] == "needs_information"


def test_notice_recorded_after_plan_invalidates_that_plan(tmp_path):
    instance = company(tmp_path)
    save_monthly_plan(instance, payroll(period="2026-02"), "february")
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
    assert any(issue["field"] == "employee_roster" for issue in result["fact_issues"])


def test_direct_publication_cannot_hide_a_departed_prior_month_employee(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    instance.save(payroll(period="2026-02"), "february")
    reviewed = instance.engine.preview(["february"])

    # Only A remains effective in February, but January actually paid A and B.
    instance.save(profile(employee_id="departed", effective_to="2026-01"), "departed-profile")
    instance.save(opening(employee_id="departed"), "departed-opening")
    instance.save(
        payroll(
            employee_id="departed",
            profile_revision={"subject_id": "departed-profile", "revision": 1},
        ),
        "departed-january",
    )
    instance.confirm_payroll("departed-january")
    instance.publish("departed-january")
    assert any(
        issue["field"] == "employee_roster" for issue in service.prepare("2026-02")["fact_issues"]
    )
    adopted = {subject: instance.current(subject) for subject in ("january", "departed-january")}
    with instance.engine.store.connection(read_only=True) as connection:
        before = tuple(connection.iterdump())

    with pytest.raises(NeedsInformation) as preview_error:
        instance.engine.preview(["february"])
    assert preview_error.value.details["fact_issues"][0]["field"] == "payroll_no_change.employees"
    with pytest.raises(NeedsInformation) as publication_error:
        instance.engine.confirm(
            ["february"],
            preview_digest=reviewed["digest"],
            epochs=reviewed["epochs"],
            request_id=instance.request(),
        )
    assert publication_error.value.details["fact_issues"][0]["field"] == (
        "payroll_no_change.employees"
    )
    with instance.engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == before
    assert {subject: instance.current(subject) for subject in adopted} == adopted


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
    save_monthly_plan(
        instance,
        payroll(period="2026-02", accounting_gross_salary_fen=1_100_000),
        february_subject,
        revision=1,
    )
    proposal = service.prepare("2026-02")
    assert proposal["status"] == "ready"
    assert not proposal["candidates"]
    assert proposal["existing"][0]["subject_id"] == february_subject
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
    instance.save(payroll(period="2026-02", accounting_gross_salary_fen=1_000_001), "february")
    instance.save(
        preparation.PayrollNoChange(
            period="2026-02",
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
        income_tax_policy_revision=None,
    )
    instance.save(future_profile, "profile")
    instance.save(contribution_policy(), "contributions")
    instance.save(january, "january")
    instance.confirm_payroll("january")
    instance.publish("january")
    assert instance.current("january").values["tax_status"] == "not_started"

    service = preparation.PayrollPreparation(instance.engine)
    basis = service.reuse_basis("2026-02")
    assert not basis["fact_issues"]
    assert set(basis["employees"][0]) == {"prior_payroll_revision"}
    instance.save(
        preparation.PayrollNoChange(
            period="2026-02",
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
    save_monthly_plan(instance, payroll(period="2026-02"), "february")

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


def save_monthly_plan(instance, wage, subject, *, revision=0):
    instance.save(wage, subject, revision=revision)
    instance.confirm_payroll(subject)


def test_payroll_confirmation_schema_stores_each_fact_once():
    from pydantic import ValidationError

    from ai_accounting.kernel.domains.payroll import PayrollBounded

    wage = payroll()
    reference = {"subject_id": "january", "revision": 1}
    plan = preparation.PayrollPlan(period="2026-01", payroll_revision=reference)
    assert set(plan.model_dump()) == {"period", "payroll_revision", "change_notice_revisions"}
    no_change = preparation.PayrollNoChange(
        period="2026-02",
        employees=({"prior_payroll_revision": reference},),
        employee_roster_unchanged=True,
        salary_and_deductions_unchanged=True,
    )
    assert set(no_change.employees[0].model_dump()) == {"prior_payroll_revision"}
    with pytest.raises(ValidationError):
        preparation.PayrollPlan(**plan.model_dump(), payroll=wage)
    with pytest.raises(ValidationError):
        preparation.PayrollNoChange(**no_change.model_dump(), prior_period="2026-01")
    for model in (type(wage), PayrollBounded):
        data = wage.model_dump() | {"profile_id": "profile"}
        with pytest.raises(ValidationError):
            model(**data)


def test_saved_wage_revision_invalidates_plan_until_reconfirmed(tmp_path):
    instance = company(tmp_path)
    service = preparation.PayrollPreparation(instance.engine)
    save_monthly_plan(instance, payroll(period="2026-02"), "february")
    original = service.prepare("2026-02")
    instance.save(
        payroll(period="2026-02", accounting_gross_salary_fen=1_000_001), "february", revision=1
    )
    changed = service.prepare("2026-02")
    assert changed["status"] == "needs_information"
    assert any(
        issue["field"] == "payroll_plan.payroll_revision" for issue in changed["fact_issues"]
    )
    with pytest.raises(preparation.KernelError) as error:
        service.confirm("2026-02", preview_digest=original["digest"], request_id=instance.request())
    assert error.value.code == "preview_expired"
    instance.confirm_payroll("february")
    assert service.prepare("2026-02")["status"] == "ready"


def test_no_change_prior_wage_revision_requires_new_owner_confirmation(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    instance.save(payroll(), "january", revision=1)
    instance.confirm_payroll("january")
    instance.publish("january")
    result = service.prepare("2026-02")
    assert result["status"] == "needs_information"
    assert any(
        issue["field"] == "payroll_no_change.prior_payroll_revision"
        for issue in result["fact_issues"]
    )


def test_no_change_generation_is_atomic_and_retryable(tmp_path, monkeypatch):
    instance = company(tmp_path)
    instance.save(profile(employee_id="other"), "other-profile")
    instance.save(opening(employee_id="other"), "other-opening")
    instance.save(payroll(employee_id="other", profile_id="other-profile"), "other-january")
    instance.confirm_payroll("other-january")
    instance.publish("other-january")
    service = confirm_no_change(instance)
    preview = service.prepare("2026-02")
    assert preview["status"] == "ready" and len(preview["candidates"]) == 2
    before = {table: instance.count(table) for table in ("fact_revision", "audit", "request")}
    original = instance.engine._registration
    calls = 0

    def failing_registration(*args, **kwargs):
        lane, save = original(*args, **kwargs)

        def operation(connection):
            nonlocal calls
            calls += 1
            result = save(connection)
            if calls == 2:
                raise RuntimeError("synthetic second-wage failure")
            return result

        return lane, operation

    request = instance.request()
    with monkeypatch.context() as patch:
        patch.setattr(instance.engine, "_registration", failing_registration)
        with pytest.raises(RuntimeError, match="second-wage failure"):
            service.confirm("2026-02", preview_digest=preview["digest"], request_id=request)
    assert {table: instance.count(table) for table in before} == before
    result = service.confirm("2026-02", preview_digest=preview["digest"], request_id=request)
    assert len(result["results"]) == 2
    assert (
        service.confirm("2026-02", preview_digest=preview["digest"], request_id=request) == result
    )


def test_no_change_identity_companion_updates_only_exact_corrected_prior_wage():
    from types import SimpleNamespace

    from test_payroll import version

    from ai_accounting.kernel.payroll_confirmation import identity_confirmation_companion

    original = version(payroll(), "january")
    destination = version(profile(employee_id="other"), "other-profile")
    corrected = payroll(employee_id="other", profile_id="other-profile")
    change = SimpleNamespace(
        subject_id="january",
        expected_revision=1,
        action="reassign",
        data=corrected.model_dump(mode="json"),
    )
    versions = {"january": original, "other-profile": destination}
    store = SimpleNamespace(current_fact=lambda connection, subject: versions[subject])
    before = preparation.PayrollNoChange(
        period="2026-02",
        employees=({"prior_payroll_revision": {"subject_id": "january", "revision": 1}},),
        employee_roster_unchanged=True,
        salary_and_deductions_unchanged=True,
    )
    after = preparation.PayrollNoChange(
        **(
            before.model_dump(mode="json")
            | {"employees": ({"prior_payroll_revision": {"subject_id": "january", "revision": 2}},)}
        )
    )
    assert identity_confirmation_companion(store, None, before, after, [change])
    assert not identity_confirmation_companion(store, None, before, after, [])
    change.data["accounting_gross_salary_fen"] += 1
    assert not identity_confirmation_companion(store, None, before, after, [change])
