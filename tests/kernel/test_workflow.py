"""External completion is durable evidence tied to a version, not a remembered tick."""

import json

import pytest
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company, payment

from ai_accounting.kernel import workflow
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


def setup_company(tmp_path):
    registry = default_registry()
    if "external_obligation" not in registry.models:
        workflow.register(registry)
    company = Company.__new__(Company)
    from collections import defaultdict

    company.engine = Engine(
        Store.create(
            tmp_path / "company.sqlite", registry, "company", "91310000123456789A", "database"
        )
    )
    company.sequence, company.materials = 0, defaultdict(list)
    company.owner_confirmation = company.engine.register_evidence(
        b"owner confirmation", "text/plain", "owner", request_id=company.request()
    )["digest"]
    return company


def obligation(kind="individual_income_tax"):
    return workflow.ExternalObligation(
        period="2026-01",
        obligation_kind=kind,
        start_period="2026-01",
        end_period="2026-01",
        due_date="2026-02-20",
        applicability_confirmed=True,
    )


def confirmation_clock(monkeypatch, engine, timestamp):
    """Fix only SQLite's audit clock in synthetic confirmation transactions."""

    def fixed_clock(stage, connection):
        if stage == "published":
            # Test-only UDF defaults require this on the isolated connection;
            # production keeps trusted_schema disabled and SQLite's own clock.
            connection.execute("PRAGMA trusted_schema=ON")
            connection.create_function("strftime", 2, lambda fmt, when: timestamp)

    monkeypatch.setattr(engine, "fault", fixed_clock)


def test_external_unfiled_work_is_a_todo_and_closed_steps_stay_final(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "unfiled")
    service = workflow.Workflow(company.engine)
    before = service.query("2026-01", as_of="2026-02-25")
    assert before["obligations"][0]["status"] == "due"
    company.close("2026-01")
    after = service.query("2026-01", as_of="2026-03-25")
    assert all(
        item["status"] == "closed" and not item["fact_issues"] for item in after["steps"][:6]
    )
    assert not after["fact_issues"]


def test_company_scope_generates_versioned_period_obligations_without_inventing_deadlines(tmp_path):
    company = setup_company(tmp_path)
    company.save(
        workflow.FilingCalendarPolicy(
            period="2026-01",
            version="test-calendar-2026",
            effective_from="2026-01",
            effective_to="2026-12",
            primary_source_url="https://www.chinatax.gov.cn/test-calendar",
            rules=tuple(
                workflow.FilingRule(
                    obligation_kind=kind,
                    cycle="monthly"
                    if kind in workflow.MONTHLY_PAYROLL_OBLIGATIONS
                    else "quarterly"
                    if kind == "quarterly_tax_and_reports"
                    else "annual",
                )
                for kind in workflow.SOURCES
            ),
        ),
        "calendar",
    )
    company.save(
        workflow.CompanyWorkflowScope(
            period="2026-01",
            established_period="2026-01",
            effective_from="2026-01",
            effective_to="2026-12",
            calendar_policy_id="calendar",
            applicability={
                kind: "not_applicable" if kind == "contribution_declaration" else "required"
                for kind in workflow.SOURCES
            },
        ),
        "scope",
    )
    service = workflow.Workflow(company.engine)
    plan = service.prepare_obligations("2026-02")
    assert plan["status"] == "ready"
    assert len(plan["candidates"]) == 5
    assert all(item["data"]["due_date"] is None for item in plan["candidates"])
    quarter = next(
        item["data"]
        for item in plan["candidates"]
        if item["data"]["obligation_kind"] == "quarterly_tax_and_reports"
    )
    assert (quarter["start_period"], quarter["end_period"]) == ("2026-01", "2026-03")
    result = service.confirm_obligations(
        "2026-02", preview_digest=plan["digest"], request_id=company.request()
    )
    assert result["source_fact_ids"] == plan["source_fact_ids"]
    assert len(service.prepare_obligations("2026-02")["reused"]) == 5


def test_completion_reopens_on_new_calculation_and_does_not_invent_date(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (obligation(), "obligation"),
    ):
        company.save(fact, subject)
    company.publish("january")
    company.save(payment(), "actual-pay")
    company.publish("actual-pay")
    original_payment = company.current("actual-pay", "payment")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    assert len(basis["accepted_calculations"]) == 1
    completion = workflow.ExternalCompletion.model_validate_json(
        __import__("json").dumps(
            {
                **basis,
                "period": "2026-02",
                "completion_status": "confirmed_complete",
                "date_status": "not_established",
                "completion_date": None,
            }
        )
    )
    with monkeypatch.context() as clock:
        confirmation_clock(clock, company.engine, "2026-02-20T10:00:00.000Z")
        company.save(completion, "completion")
        company.publish("completion")
    assert service.query("2026-02", as_of="2026-03-01")["obligations"][0]["status"] == "completed"
    assert company.current("completion", "external_completion").values["completion_date"] is None
    changed = payroll().model_copy(update={"accounting_gross_salary_fen": 1100000})
    company.save(changed, "january", revision=1)
    assert "completion" in company.pending()
    company.publish("january")
    revised_payment = company.current("actual-pay", "payment")
    assert revised_payment.fact_id == original_payment.fact_id
    assert revised_payment.values["amount_fen"] == original_payment.values["amount_fen"]
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM subject WHERE kind='payment'").fetchone()[0]
            == 1
        )
    assert not company.current("completion", "external_completion").values["basis_current"]
    assert service.query("2026-02", as_of="2026-03-01")["obligations"][0]["status"] == "due"
    revised = workflow.ExternalCompletion.model_validate_json(
        __import__("json").dumps(
            {
                **service.obligation_basis("obligation"),
                "period": "2026-02",
                "completion_status": "submitted",
                "date_status": "known",
                "completion_date": "2026-02-21",
            }
        )
    )
    with pytest.raises(KernelError, match="不可|不"):
        company.save(revised, "completion", revision=1)
    company.save(revised, "resubmission")
    company.publish("resubmission")
    assert service.query("2026-02", as_of="2026-03-01")["obligations"][0]["status"] == "completed"
    assert company.current("completion", "external_completion").values["completion_date"] is None


def test_typed_direct_completion_cannot_bypass_required_close(tmp_path):
    company = setup_company(tmp_path)
    saved = company.save(obligation("quarterly_tax_and_reports"), "obligation")
    completion = workflow.ExternalCompletion(
        period="2026-02",
        obligation_id="obligation",
        obligation_fact_id=saved["fact_id"],
        obligation_kind="quarterly_tax_and_reports",
        start_period="2026-01",
        end_period="2026-01",
        accepted_calculations=(),
        completion_status="submitted",
        date_status="known",
        completion_date="2026-02-10",
    )
    company.save(completion, "completion")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["completion"])
    assert error.value.code == "awaiting_close"


def completion_from_basis(basis, **changes):
    return workflow.ExternalCompletion.model_validate_json(
        json.dumps(
            dict(
                **basis,
                period="2026-02",
                completion_status="confirmed_complete",
                date_status="known",
                completion_date="2026-02-10",
            )
            | changes
        )
    )


def test_empty_basis_cannot_hide_a_known_unpublished_payroll(tmp_path):
    company = setup_company(tmp_path)
    saved = company.save(obligation(), "obligation")
    service = workflow.Workflow(company.engine)
    empty = service.obligation_basis("obligation")
    company.save(payroll(), "unpublished-payroll")
    with pytest.raises(KernelError) as error:
        service.obligation_basis("obligation")
    assert error.value.code == "basis_unpublished"
    company.save(completion_from_basis(empty, no_reportable_activity_confirmed=True), "completion")
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["completion"])
    assert error.value.issues[0]["field"] == "accepted_calculations"
    assert saved["fact_id"] == empty["obligation_fact_id"]
    assert service.query("2026-01", as_of="2026-02-28")["obligations"][0]["status"] != "completed"


def test_empty_source_requires_explicit_no_activity_and_a_real_completion_evidence(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    company.save(completion_from_basis(basis), "unconfirmed-empty")
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["unconfirmed-empty"])
    assert error.value.issues[0]["field"] == "no_reportable_activity_confirmed"
    complete = completion_from_basis(basis, no_reportable_activity_confirmed=True)
    with pytest.raises(NeedsInformation) as error:
        company.engine.save_fact(
            complete.kind,
            "without-proof",
            complete.model_dump(mode="json"),
            evidence=(),
            expected_revision=0,
            request_id=company.request(),
        )
    assert error.value.issues[0]["field"] == "evidence"


def test_new_unpublished_source_invalidates_previously_empty_completion(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    service = workflow.Workflow(company.engine)
    company.save(
        completion_from_basis(
            service.obligation_basis("obligation"), no_reportable_activity_confirmed=True
        ),
        "completion",
    )
    company.publish("completion")
    assert service.query("2026-01", as_of="2026-02-28")["obligations"][0]["status"] == "completed"
    company.save(payroll(), "new-payroll")
    result = service.query("2026-01", as_of="2026-02-28")
    assert result["obligations"][0]["status"] == "due"
    assert result["obligations"][0]["basis_issues"][0]["field"] == "unpublished_basis"
    assert (
        company.current("completion", "external_completion").values["completion_date"]
        == "2026-02-10"
    )


def test_material_complete_does_not_complete_payroll_or_declarations(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
    ):
        company.save(fact, subject)
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        evidence = sorted({ev for _, ev in company.materials[category]})
        periods.inventory(
            "2026-01",
            category,
            evidence=evidence,
            expected=len(evidence),
            no_business=not evidence,
            confirmation_evidence=company.owner_confirmation,
            request_id=company.request(),
        )
    result = workflow.Workflow(company.engine).query("2026-01", as_of="2026-02-28")
    steps = {item["number"]: item for item in result["steps"]}
    assert all(steps[number]["status"] == "needs_information" for number in (2, 3, 4))
    assert any(issue["field"] == "january" for issue in steps[2]["fact_issues"])


def test_monthly_obligation_recorded_later_still_requires_actual_completion(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation().model_copy(update={"period": "2026-02"}), "late-obligation")
    registry = company.engine.store.registry
    reads, evaluate = workflow.required_reads, workflow.required_work
    assert "external_monthly_declarations" not in registry.readiness
    from ai_accounting.kernel.contracts import Context
    from ai_accounting.kernel.types import YearMonth

    with company.engine.store.connection(read_only=True) as connection:
        ctx = Context(
            {
                read: company.engine.store.select(connection, read)
                for read in reads(YearMonth("2026-01"))
            }
        )
    issues = evaluate(YearMonth("2026-01"), ctx)
    assert issues[0]["obligation_id"] == "late-obligation"


def test_quarterly_completion_waits_for_close_without_blocking_that_close(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation("quarterly_tax_and_reports"), "quarter")
    fact_id = company.engine.store.current_fact
    with company.engine.store.connection(read_only=True) as connection:
        obligation_id = fact_id(connection, "quarter").id
    company.save(
        completion_from_basis(
            dict(
                obligation_id="quarter",
                obligation_fact_id=obligation_id,
                obligation_kind="quarterly_tax_and_reports",
                start_period="2026-01",
                end_period="2026-01",
                accepted_calculations=[],
            ),
            no_reportable_activity_confirmed=True,
        ),
        "quarter-completion",
    )
    with pytest.raises(KernelError) as error:
        company.engine.preview(["quarter-completion"])
    assert error.value.code == "awaiting_close"
    company.close("2026-01")
    company.publish("quarter-completion")
    assert (
        workflow.Workflow(company.engine).query("2026-01", as_of="2026-02-28")["obligations"][0][
            "status"
        ]
        == "completed"
    )


def test_future_completion_does_not_count_before_its_actual_date(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    service = workflow.Workflow(company.engine)
    company.save(
        completion_from_basis(
            service.obligation_basis("obligation"),
            completion_date="2026-02-25",
            no_reportable_activity_confirmed=True,
        ),
        "completion",
    )
    company.publish("completion")
    assert service.query("2026-01", as_of="2026-02-21")["obligations"][0]["status"] == "due"
    assert service.query("2026-01", as_of="2026-02-25")["obligations"][0]["status"] == "completed"


def test_active_employee_cannot_disappear_from_empty_basis_or_month_close(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    service = workflow.Workflow(company.engine)
    empty = service.obligation_basis("obligation")
    company.save(profile(), "active-employee")
    with pytest.raises(KernelError) as error:
        service.obligation_basis("obligation")
    assert error.value.details["fact_issues"][0]["field"] == "missing_payroll"
    company.save(completion_from_basis(empty, no_reportable_activity_confirmed=True), "completion")
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["completion"])
    assert error.value.issues[0]["field"] == "accepted_calculations"
    with pytest.raises(KernelError) as error:
        company.close("2026-01")
    assert any(item["field"] == "missing_payroll" for item in error.value.details["fact_issues"])
    result = service.query("2026-01", as_of="2026-02-28")
    assert result["steps"][1]["status"] == "needs_information"
    assert any(item["field"] == "missing_payroll" for item in result["steps"][1]["fact_issues"])


def test_material_no_business_does_not_infer_external_non_applicability(tmp_path):
    company = setup_company(tmp_path)
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-01",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=company.owner_confirmation,
            request_id=company.request(),
        )
    service = workflow.Workflow(company.engine)
    assert all(
        item["status"] == "needs_information"
        for item in service.query("2026-01", as_of="2026-02-28")["steps"][2:4]
    )
    for kind in ("contribution_declaration", "individual_income_tax"):
        company.save(
            obligation(kind).model_copy(
                update={"applicability": "not_applicable", "due_date": None}
            ),
            kind,
        )
    result = service.query("2026-01", as_of="2026-02-28")
    assert all(item["status"] == "not_applicable" for item in result["steps"][2:4])
    company.close("2026-01")


@pytest.mark.parametrize("invalid_basis", ["subset", "duplicate", "two_versions"])
def test_completion_accepts_only_one_exact_calculation_set(tmp_path, invalid_basis):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (obligation(), "obligation"),
    ):
        company.save(fact, subject)
    company.publish("january")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    accepted = basis["accepted_calculations"]
    changed = (
        []
        if invalid_basis == "subset"
        else [
            *accepted,
            accepted[0]
            if invalid_basis == "duplicate"
            else dict(accepted[0], calculation_id="other"),
        ]
    )
    company.save(completion_from_basis(basis, accepted_calculations=changed), "completion")
    if invalid_basis == "subset":
        company.publish("completion")
        assert service.query("2026-01", as_of="2026-02-28")["obligations"][0]["status"] == "due"
    else:
        with pytest.raises(KernelError) as error:
            company.engine.preview(["completion"])
        assert error.value.code == "duplicate_basis"


def test_quarterly_basis_is_explicit_current_calculations_not_old_close_manifest(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (obligation("quarterly_tax_and_reports"), "quarter"),
    ):
        company.save(fact, subject)
    company.publish("january")
    frozen = company.close("2026-01")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("quarter")
    assert {item["subject_id"] for item in basis["accepted_calculations"]} == {"january"}
    company.save(completion_from_basis(basis), "completion")
    company.publish("completion")
    assert service.query("2026-01", as_of="2026-02-28")["obligations"][0]["status"] == "completed"
    company.save(
        payroll().model_copy(update={"accounting_gross_salary_fen": 1100000}), "january", 1
    )
    company.publish("january", correction_period="2026-02")
    assert Periods(company.engine).closed_report("2026-01") == frozen
    assert company.current("completion", "external_completion").values["accepted_calculations"] == (
        basis["accepted_calculations"][0],
    )
    assert service.query("2026-01", as_of="2026-02-28")["obligations"][0]["status"] == "due"
    assert (
        service.obligation_basis("quarter")["accepted_calculations"]
        != basis["accepted_calculations"]
    )


def test_explicit_zero_payroll_and_effective_end_satisfy_population_without_inferred_salary(
    tmp_path,
):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(effective_to="2026-01", social_insurance_participating=False), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0), "january"),
    ):
        company.save(fact, subject)
    company.publish("january")
    assert company.current("january").values["net_fen"] == 0
    company.close("2026-01")
    company.close("2026-02")


def test_confirmed_submission_preserves_source_and_cannot_silently_lose_it(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (obligation(), "obligation"),
    ):
        company.save(fact, subject)
    company.publish("january")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    company.save(completion_from_basis(basis), "completion")
    company.publish("completion")
    with pytest.raises(KernelError) as error:
        company.engine.preview_delete("january")
    assert error.value.code == "has_dependents"
    company.save(obligation().model_copy(update={"due_date": "2026-02-25"}), "obligation", 1)
    company.publish("obligation")
    old = company.current("completion", "external_completion")
    assert old.values["obligation_fact_id"] == basis["obligation_fact_id"]
    assert old.values["completion_date"] == "2026-02-10"
    assert old.values["basis_current"]
    assert service.query("2026-01", as_of="2026-02-28")["obligations"][0]["status"] == "completed"
    assert "completion" not in company.pending()


@pytest.fixture
def submitted_payroll(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(effective_to="2026-01"), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (obligation(), "obligation"),
    ):
        company.save(fact, subject)
    company.publish("january")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    with monkeypatch.context() as clock:
        confirmation_clock(clock, company.engine, "2026-02-20T10:00:00.000Z")
        company.save(
            completion_from_basis(basis, completion_date=None, date_status="not_established"),
            "completion",
        )
        company.publish("completion")
    return company


def test_equivalent_recalculation_preserves_actual_submission_after_published_review(
    submitted_payroll,
):
    company = submitted_payroll
    service = workflow.Workflow(company.engine)
    original_payroll = company.current("january")
    original_completion = company.current("completion", "external_completion")
    ledger = company.engine.ledger("2026-01")
    # New confirmed evidence and fact version, with exactly the same business data.
    company.save(payroll(), "january", revision=1)
    assert service.query("2026-01", as_of="2026-03-01")["obligations"][0]["status"] == "due"
    company.publish("january")
    current_payroll = company.current("january")
    reviewed = company.current("completion", "external_completion")
    assert current_payroll.id != original_payroll.id
    assert current_payroll.result_digest == original_payroll.result_digest
    assert company.engine.ledger("2026-01") == ledger
    assert reviewed.fact_id == original_completion.fact_id
    assert (
        reviewed.values["accepted_calculations"]
        == original_completion.values["accepted_calculations"]
    )
    assert reviewed.values["reviewed_calculations"][0]["calculation_id"] == current_payroll.id
    assert reviewed.values["basis_current"]
    assert reviewed.values["completion_date"] is None
    assert service.query("2026-01", as_of="2026-03-01")["obligations"][0]["status"] == "completed"
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind='external_completion'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_revision WHERE subject_id='completion'"
            ).fetchone()[0]
            == 1
        )
    # The period readiness uses the same published review, including an unknown date.
    company.close("2026-01")


@pytest.mark.parametrize(
    "forgery",
    [
        "missing_calculation",
        "another_subject",
        "wrong_kind",
        "wrong_period",
        "missing_obligation",
        "another_obligation",
    ],
)
def test_external_completion_rejects_unverifiable_or_foreign_history(submitted_payroll, forgery):
    company = submitted_payroll
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    if forgery in {"missing_calculation", "another_subject", "wrong_kind", "wrong_period"}:
        if forgery == "missing_calculation":
            accepted_id = "not-a-published-calculation"
        elif forgery == "another_subject":
            # Preserve the real ID but falsely attribute it to another stable subject.
            basis["accepted_calculations"][0]["subject_id"] = "someone-else"
            accepted_id = basis["accepted_calculations"][0]["calculation_id"]
        elif forgery == "wrong_kind":
            accepted_id = company.current("completion", "external_completion").id
            basis["accepted_calculations"][0]["subject_id"] = "completion"
        else:
            company.save(profile(), "profile", revision=1)
            company.publish("profile")
            company.save(payroll(period="2026-02"), "february")
            company.publish("february")
            accepted_id = company.current("february").id
            basis["accepted_calculations"][0]["subject_id"] = "february"
        basis["accepted_calculations"][0]["calculation_id"] = accepted_id
    elif forgery == "missing_obligation":
        basis["obligation_fact_id"] = "not-an-obligation-version"
    else:
        other = company.save(obligation(), "other-obligation")
        basis["obligation_fact_id"] = other["fact_id"]
    company.save(completion_from_basis(basis), "forged-completion")
    before = company.count("calculation")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["forged-completion"])
    assert error.value.code in {
        "needs_information",
        "invalid_accepted_calculation",
        "invalid_obligation_history",
    }
    assert company.count("calculation") == before


def test_quarter_review_excludes_monthly_submission_revisions_from_accounting_basis(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(effective_to="2026-01"), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (obligation(), "monthly"),
        (obligation("quarterly_tax_and_reports"), "quarter"),
    ):
        company.save(fact, subject)
    company.publish("january")
    service = workflow.Workflow(company.engine)
    company.save(
        completion_from_basis(
            service.obligation_basis("monthly"), period="2026-01", completion_date="2026-01-31"
        ),
        "monthly-completion",
    )
    company.publish("monthly-completion")
    company.close("2026-01")
    basis = service.obligation_basis("quarter")
    assert [item["subject_id"] for item in basis["accepted_calculations"]] == ["january"]
    company.save(completion_from_basis(basis), "quarter-completion")
    company.publish("quarter-completion")
    old_monthly = company.current("monthly-completion", "external_completion")
    company.save(payroll(), "january", revision=1)
    company.publish("january", correction_period="2026-02")
    assert (
        company.current("monthly-completion", "external_completion").result_digest
        != old_monthly.result_digest
    )
    assert all(
        item["status"] in {"completed", "closed"}
        for item in service.query("2026-01", as_of="2026-03-01")["obligations"]
    )
    assert company.current("quarter-completion", "external_completion").values[
        "accepted_calculations"
    ] == (basis["accepted_calculations"][0],)
    invalid_basis = dict(
        basis,
        accepted_calculations=[
            {
                "subject_id": "monthly-completion",
                "calculation_id": old_monthly.id,
            }
        ],
    )
    company.save(completion_from_basis(invalid_basis), "invalid-quarter")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["invalid-quarter"])
    assert error.value.code == "invalid_accepted_calculation"
