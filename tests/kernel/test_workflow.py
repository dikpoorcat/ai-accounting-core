"""External submissions remain real events while accounting reviews follow their own basis."""

import json
from collections import defaultdict

import pytest
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel import workflow
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


def setup_company(tmp_path):
    company = Company.__new__(Company)
    company.engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            production_bundle(),
            "company",
            "91310000123456789A",
            "database",
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
    def fixed_clock(stage, connection):
        if stage == "published":
            connection.execute("PRAGMA trusted_schema=ON")
            connection.create_function("strftime", 2, lambda fmt, when: timestamp)

    monkeypatch.setattr(engine, "fault", fixed_clock)


def completion_from_basis(basis, **changes):
    data = {
        "period": "2026-02",
        "obligation_id": basis["obligation_id"],
        "obligation_fact_id": basis["obligation_fact_id"],
        "obligation_kind": basis["obligation_kind"],
        "start_period": basis["start_period"],
        "end_period": basis["end_period"],
        "accepted_calculations": basis.get("candidate_calculations", []),
        "completion_status": "confirmed_complete",
        "date_status": "known",
        "completion_date": "2026-02-10",
    }
    data.update(changes)
    return workflow.ExternalCompletion.model_validate_json(json.dumps(data))


def save_completion(company, fact, subject="completion", *, adopted_basis=False):
    receipt = company.engine.register_evidence(
        b"external receipt",
        "text/plain",
        "external receipt",
        request_id=company.request(),
    )["digest"]
    evidence = [receipt]
    if adopted_basis:
        source = company.engine.register_evidence(
            b"actual submitted return",
            "text/plain",
            "submitted return",
            request_id=company.request(),
        )["digest"]
        evidence.append(source)
        fact = fact.model_copy(update={"adopted_evidence_digests": (source,)})
    return company.engine.save_fact(
        fact.kind,
        subject,
        fact.model_dump(mode="json"),
        evidence=evidence,
        expected_revision=0,
        request_id=company.request(),
    )


def external_item(company, day="2026-02-28", period="2026-01"):
    result = workflow.Workflow(company.engine).query(period, as_of=day)
    return next(
        item for item in result["sections"]["external"]["obligations"] if item["id"] == "obligation"
    )


def review_from_completion(completion, completion_fact_id, reviewed, result):
    return workflow.ExternalBasisReview(
        period="2026-02",
        completion_id="completion",
        completion_fact_id=completion_fact_id,
        obligation_id=completion.obligation_id,
        obligation_fact_id=completion.obligation_fact_id,
        obligation_kind=completion.obligation_kind,
        start_period=completion.start_period,
        end_period=completion.end_period,
        source_facts=completion.source_facts,
        adopted_calculations=completion.accepted_calculations,
        reviewed_calculations=tuple(workflow.AcceptedCalculation(**item) for item in reviewed),
        review_result=result,
    )


def test_actual_filing_before_payroll_remains_complete_and_review_is_separate(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    assert basis["candidate_calculations"] == []
    completion = completion_from_basis(
        basis, accepted_calculations=(), adopted_evidence_digests=("placeholder",)
    )
    # The exact source digest must be among the saved evidence.
    original = save_completion(company, completion)
    with pytest.raises(KernelError) as error:
        company.publish("completion")
    assert error.value.code == "invalid_adopted_evidence"
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation_current WHERE subject_id='completion'"
            ).fetchone()[0]
            == 0
        )

    submitted = company.engine.register_evidence(
        b"actual submitted wage tax return",
        "text/plain",
        "submitted return",
        request_id=company.request(),
    )["digest"]
    receipt = company.engine.register_evidence(
        b"external filing receipt",
        "text/plain",
        "filing receipt",
        request_id=company.request(),
    )["digest"]
    corrected = completion.model_copy(update={"adopted_evidence_digests": (submitted,)})
    saved = company.engine.amend_fact(
        corrected.kind,
        "completion",
        corrected.model_dump(mode="json"),
        evidence=(submitted, receipt),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    assert saved["fact_id"] != original["fact_id"]
    company.publish("completion")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "not_reviewed"
    assert item["recorded_completions"][0]["fact_id"] == saved["fact_id"]
    assert item["recorded_completions"][0]["adopted_evidence_digests"]
    assert len(item["recorded_completions"]) == 1


def test_bad_source_recording_is_amended_before_real_completion_can_publish(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    incorrect = completion_from_basis(
        basis,
        accepted_calculations=(),
        source_facts=(
            {
                "subject_id": "missing-source",
                "fact_id": "f_missing",
                "kind": "payroll_tax_declaration_actual",
            },
        ),
    )
    original = save_completion(company, incorrect)
    with pytest.raises(NeedsInformation) as error:
        company.publish("completion")
    assert error.value.issues[0]["field"] == "source_facts"
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation_current WHERE subject_id='completion'"
            ).fetchone()[0]
            == 0
        )

    corrected = incorrect.model_copy(
        update={"source_facts": (), "no_reportable_activity_confirmed": True}
    )
    receipt = company.engine.register_evidence(
        b"confirmed no activity receipt",
        "text/plain",
        "no activity filing receipt",
        request_id=company.request(),
    )["digest"]
    amended = company.engine.amend_fact(
        corrected.kind,
        "completion",
        corrected.model_dump(mode="json"),
        evidence=(receipt,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    assert amended["fact_id"] != original["fact_id"]
    company.publish("completion")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert [record["fact_id"] for record in item["recorded_completions"]] == [
        amended["fact_id"]
    ]
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_revision WHERE subject_id='completion'"
            ).fetchone()[0]
            == 2
        )


def test_saved_rejected_completion_cannot_be_bypassed_by_another_root(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    incorrect = completion_from_basis(
        basis, accepted_calculations=(), adopted_evidence_digests=("missing-adoption",)
    )
    save_completion(company, incorrect, "recorded-error")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["recorded-error"])
    assert error.value.code == "invalid_adopted_evidence"

    later = completion_from_basis(
        basis, accepted_calculations=(), no_reportable_activity_confirmed=True
    )
    save_completion(company, later, "separate-root")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["separate-root"])
    assert error.value.code == "completion_continuation_required"


def test_published_review_needs_exact_adopted_accounting_basis(tmp_path):
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
    company.confirm_payroll("january")
    company.publish("january")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    assert len(basis["candidate_calculations"]) == 1
    completion = completion_from_basis(basis)
    saved = save_completion(company, completion)
    company.publish("completion")
    reviewed = basis["candidate_calculations"]
    review = review_from_completion(completion, saved["fact_id"], reviewed, "matched")
    company.save(review, "review")
    company.publish("review")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "reviewed"

    changed = payroll().model_copy(update={"accounting_gross_salary_fen": 1100000})
    company.save(changed, "january", revision=1)
    company.confirm_payroll("january")
    company.publish("january")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "outdated"


def test_raw_submitted_document_cannot_be_rubber_stamped_as_matched(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation("quarterly_tax"), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(
        basis,
        obligation_kind="quarterly_tax",
        accepted_calculations=(),
        adopted_evidence_digests=("placeholder",),
    )
    saved = save_completion(company, completion, adopted_basis=True)
    company.publish("completion")
    review = review_from_completion(completion, saved["fact_id"], [], "matched")
    company.save(review, "review")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["review"])
    assert error.value.code == "review_not_proven"


def test_financial_report_requires_close_but_quarterly_tax_does_not(tmp_path):
    company = setup_company(tmp_path)
    tax = company.save(obligation("quarterly_tax"), "tax-obligation")
    report = company.save(obligation("quarterly_financial_report"), "report-obligation")
    for subject, kind, source in (
        ("tax-completion", "quarterly_tax", tax),
        ("report-completion", "quarterly_financial_report", report),
    ):
        fact = workflow.ExternalCompletion(
            period="2026-02",
            obligation_id=subject.replace("-completion", "-obligation"),
            obligation_fact_id=source["fact_id"],
            obligation_kind=kind,
            start_period="2026-01",
            end_period="2026-01",
            no_reportable_activity_confirmed=True,
            completion_status="submitted",
            date_status="known",
            completion_date="2026-02-10",
        )
        save_completion(company, fact, subject)
    company.publish("tax-completion")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["report-completion"])
    assert error.value.code == "awaiting_close"


def test_real_supplemental_submission_links_previous_exact_fact(tmp_path):
    company = setup_company(tmp_path)
    first_obligation = company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    original = completion_from_basis(
        basis,
        no_reportable_activity_confirmed=True,
        accepted_calculations=(),
    )
    first = save_completion(company, original, "z-original")
    company.publish("z-original")
    followup = original.model_copy(
        update={
            "period": "2026-03",
            "completion_date": "2026-03-02",
            "previous_completion_fact_id": first["fact_id"],
        }
    )
    save_completion(company, followup, "a-supplement")
    company.publish("a-supplement")
    assert first_obligation["fact_id"] == basis["obligation_fact_id"]
    item = external_item(company, day="2026-03-10")
    assert item["actual_completion_status"] == "completed"
    assert len(item["recorded_completions"]) == 2
    assert item["recorded_completions"][1]["previous_completion_fact_id"] == first["fact_id"]


def test_supplement_cannot_precede_original_actual_completion(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    original = completion_from_basis(
        basis, accepted_calculations=(), no_reportable_activity_confirmed=True
    )
    first = save_completion(company, original)
    company.publish("completion")
    earlier = original.model_copy(
        update={
            "period": "2026-03",
            "completion_date": "2026-02-01",
            "previous_completion_fact_id": first["fact_id"],
        }
    )
    save_completion(company, earlier, "invalid-supplement")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["invalid-supplement"])
    assert error.value.code == "invalid_completion_continuation"


def test_company_scope_generates_six_versioned_obligations_without_deadlines(tmp_path):
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
                    if kind in {"quarterly_tax", "quarterly_financial_report"}
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
    assert len(plan["candidates"]) == 6
    assert all(item["data"]["due_date"] is None for item in plan["candidates"])
    for kind in ("quarterly_tax", "quarterly_financial_report"):
        quarter = next(
            item["data"] for item in plan["candidates"] if item["data"]["obligation_kind"] == kind
        )
        assert (quarter["start_period"], quarter["end_period"]) == ("2026-01", "2026-03")
    confirmed = service.confirm_obligations(
        "2026-02", preview_digest=plan["digest"], request_id=company.request()
    )
    assert confirmed["source_fact_ids"] == plan["source_fact_ids"]
    assert len(service.prepare_obligations("2026-02")["reused"]) == 6


def test_later_recorded_monthly_obligation_still_requires_actual_completion(tmp_path):
    from ai_accounting.kernel.contracts import Context
    from ai_accounting.kernel.types import YearMonth

    company = setup_company(tmp_path)
    company.save(obligation().model_copy(update={"period": "2026-02"}), "late")
    with company.engine.store.connection(read_only=True) as connection:
        month = YearMonth("2026-01")
        context = Context(
            {
                read: company.engine.store.select(connection, read)
                for read in workflow.required_reads(month)
            }
        )
    issues = workflow.required_work(month, context)
    assert issues[0]["obligation_id"] == "late"


def test_empty_current_basis_is_not_a_claim_of_no_reportable_activity(tmp_path):
    from pydantic import ValidationError

    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    assert basis["candidate_calculations"] == []
    with pytest.raises(ValidationError):
        completion_from_basis(basis, accepted_calculations=())
    explicit = completion_from_basis(
        basis, accepted_calculations=(), no_reportable_activity_confirmed=True
    )
    save_completion(company, explicit)
    company.publish("completion")
    assert external_item(company)["actual_completion_status"] == "completed"


def test_unpublished_payroll_and_active_profile_are_visible_without_blocking_real_filing(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    company.save(profile(), "active-employee")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    assert any(issue["field"] == "missing_payroll" for issue in basis["fact_issues"])
    completion = completion_from_basis(
        basis, accepted_calculations=(), adopted_evidence_digests=("placeholder",)
    )
    save_completion(company, completion, adopted_basis=True)
    company.publish("completion")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "not_reviewed"
    assert any(issue["field"] == "missing_payroll" for issue in item["basis_issues"])
    with pytest.raises(KernelError):
        company.close("2026-01")


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
    assert (
        workflow.Workflow(company.engine).query("2026-01", as_of="2026-02-28")["sections"][
            "external"
        ]["obligations"]
        == []
    )
    for kind in ("contribution_declaration", "individual_income_tax"):
        company.save(
            obligation(kind).model_copy(
                update={"applicability": "not_applicable", "due_date": None}
            ),
            kind,
        )
    obligations = workflow.Workflow(company.engine).query("2026-01", as_of="2026-02-28")[
        "sections"
    ]["external"]["obligations"]
    assert {item["actual_completion_status"] for item in obligations} == {"not_applicable"}


def test_actual_completion_date_is_not_inferred_from_recording_period(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    fact = completion_from_basis(
        basis,
        accepted_calculations=(),
        no_reportable_activity_confirmed=True,
        completion_date="2026-02-25",
    )
    save_completion(company, fact)
    company.publish("completion")
    assert external_item(company, day="2026-02-21")["actual_completion_status"] == "due"
    assert external_item(company, day="2026-02-25")["actual_completion_status"] == "completed"


def test_social_actual_source_can_precede_payroll_and_cannot_prove_matched_review(tmp_path):
    from test_payroll import actual as contribution_actual

    company = setup_company(tmp_path)
    saved = company.save(contribution_actual(), "actual-contribution")
    company.save(obligation("contribution_declaration"), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(
        basis,
        accepted_calculations=(),
        source_facts=(
            {
                "subject_id": "actual-contribution",
                "fact_id": saved["fact_id"],
                "kind": "payroll_contribution_actual",
            },
        ),
    )
    saved_completion = save_completion(company, completion)
    company.publish("completion")
    assert external_item(company)["actual_completion_status"] == "completed"
    review = review_from_completion(completion, saved_completion["fact_id"], [], "matched")
    company.save(review, "review")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["review"])
    assert error.value.code == "review_not_proven"


def test_individual_tax_scope_includes_bonus_and_labor_facts(tmp_path):
    from test_payroll import bonus, labor

    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    company.save(bonus(), "bonus")
    company.save(labor(), "labor")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    issues = {
        item["subject_id"] for item in basis["fact_issues"] if item["field"] == "unpublished_basis"
    }
    assert {"bonus", "labor"} <= issues


@pytest.mark.parametrize(
    "forgery",
    [
        "duplicate",
        "missing_calculation",
        "foreign_subject",
        "wrong_kind",
        "wrong_period",
        "missing_obligation",
        "foreign_obligation",
    ],
)
def test_completion_rejects_foreign_or_unverifiable_exact_history(tmp_path, forgery):
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
    company.confirm_payroll("january")
    company.publish("january")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    accepted = [dict(item) for item in basis["candidate_calculations"]]
    if forgery == "duplicate":
        accepted.append(dict(accepted[0]))
    elif forgery == "missing_calculation":
        accepted[0]["calculation_id"] = "missing-version"
    elif forgery == "foreign_subject":
        accepted[0]["subject_id"] = "someone-else"
    elif forgery == "wrong_kind":
        accepted[0] = {
            "subject_id": "completion-other",
            "calculation_id": "missing-version",
        }
    elif forgery == "wrong_period":
        accepted[0]["subject_id"] = "someone-else"
    if forgery == "missing_obligation":
        basis["obligation_fact_id"] = "missing-version"
    if forgery == "foreign_obligation":
        other = company.save(obligation(), "other")
        basis["obligation_fact_id"] = other["fact_id"]
    fact = completion_from_basis(basis, accepted_calculations=accepted)
    save_completion(company, fact, "forged")
    before = company.count("calculation")
    with pytest.raises(KernelError):
        company.engine.preview(["forged"])
    assert company.count("calculation") == before


def test_quarterly_basis_excludes_external_completion_calculations(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation("individual_income_tax"), "monthly")
    monthly_basis = workflow.Workflow(company.engine).obligation_basis("monthly")
    monthly = completion_from_basis(
        monthly_basis,
        accepted_calculations=(),
        no_reportable_activity_confirmed=True,
    )
    save_completion(company, monthly, "monthly-completion")
    company.publish("monthly-completion")
    company.save(obligation("quarterly_financial_report"), "quarter")
    company.close("2026-01")
    quarter_basis = workflow.Workflow(company.engine).obligation_basis("quarter")
    assert not any(
        item["subject_id"] == "monthly-completion"
        for item in quarter_basis["candidate_calculations"]
    )


def test_explicit_zero_payroll_and_effective_end_leave_no_inferred_next_month_salary(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(effective_to="2026-01", social_insurance_participating=False), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0), "january"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january")
    company.publish("january")
    assert company.current("january").values["net_fen"] == 0
    company.close("2026-01")
    company.close("2026-02")


def test_completion_preserves_exact_adopted_source_after_obligation_update(tmp_path):
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
    company.confirm_payroll("january")
    company.publish("january")
    service = workflow.Workflow(company.engine)
    basis = service.obligation_basis("obligation")
    completion = completion_from_basis(basis)
    saved = save_completion(company, completion)
    company.publish("completion")
    with pytest.raises(KernelError) as error:
        company.engine.preview_delete("january")
    assert error.value.code == "has_dependents"
    company.save(obligation().model_copy(update={"due_date": "2026-02-25"}), "obligation", 1)
    original = company.current("completion", "external_completion")
    assert original.values["obligation_fact_id"] == basis["obligation_fact_id"]
    assert external_item(company)["actual_completion_status"] == "completed"
    assert saved["fact_id"] == original.fact_id


def test_equivalent_new_calculation_keeps_real_submission_and_requires_new_review(tmp_path):
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
    company.confirm_payroll("january")
    company.publish("january")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(basis)
    saved = save_completion(company, completion)
    company.publish("completion")
    review = review_from_completion(
        completion, saved["fact_id"], basis["candidate_calculations"], "matched"
    )
    company.save(review, "review")
    company.publish("review")
    old_payroll = company.current("january")
    old_completion = company.current("completion", "external_completion")
    ledger = company.engine.ledger("2026-01")
    company.save(payroll(), "january", revision=1)
    company.confirm_payroll("january")
    company.publish("january")
    assert company.current("january").id != old_payroll.id
    assert company.current("january").result_digest == old_payroll.result_digest
    assert company.engine.ledger("2026-01") == ledger
    assert company.current("completion", "external_completion").fact_id == old_completion.fact_id
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "outdated"


def test_review_must_cover_all_current_calculations_even_if_filing_adopted_subset(tmp_path):
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
    company.confirm_payroll("january")
    company.publish("january")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(
        basis, accepted_calculations=(), adopted_evidence_digests=("placeholder",)
    )
    saved = save_completion(company, completion, adopted_basis=True)
    company.publish("completion")
    review = review_from_completion(completion, saved["fact_id"], [], "matched")
    company.save(review, "review")
    company.publish("review")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "outdated"


def test_unlinked_second_submission_is_rejected_and_new_link_needs_its_own_review(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    original = completion_from_basis(
        basis, accepted_calculations=(), no_reportable_activity_confirmed=True
    )
    first = save_completion(company, original)
    company.publish("completion")
    review = review_from_completion(original, first["fact_id"], [], "matched")
    company.save(review, "old-review")
    company.publish("old-review")
    assert external_item(company)["basis_review_status"] == "reviewed"

    supplement = original.model_copy(
        update={
            "period": "2026-03",
            "completion_date": "2026-03-02",
            "previous_completion_fact_id": first["fact_id"],
        }
    )
    save_completion(company, supplement, "supplement")
    company.publish("supplement")
    company.publish("completion")
    item = external_item(company, day="2026-03-10")
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "not_reviewed"
    assert item["basis_review_calculation_id"] is None
    assert len(item["recorded_completions"]) == 2
    assert (
        company.current("old-review", "external_basis_review").values["review_result"] == "matched"
    )

    save_completion(company, original, "independent")
    with pytest.raises(KernelError) as error:
        company.engine.preview(["independent"])
    assert error.value.code == "completion_continuation_required"


def test_latest_published_review_uses_sequence_not_subject_name(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(
        basis, accepted_calculations=(), no_reportable_activity_confirmed=True
    )
    saved = save_completion(company, completion)
    company.publish("completion")
    review = review_from_completion(completion, saved["fact_id"], [], "matched")
    company.save(review, "z-earlier")
    company.publish("z-earlier")
    company.save(review, "a-later")
    company.publish("a-later")
    assert (
        external_item(company)["basis_review_calculation_id"]
        == company.current("a-later", "external_basis_review").id
    )


def test_social_actual_amounts_are_compared_with_current_payroll_and_changes_show_difference(
    tmp_path,
):
    from test_payroll import actual as contribution_actual

    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (contribution_actual(), "actual-contribution"),
        (payroll(), "january"),
        (obligation("contribution_declaration"), "obligation"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january")
    company.publish("january")
    values = company.current("january").values
    assert (values["employee_contributions_fen"], values["employer_contributions_fen"]) == (
        100_000,
        200_000,
    )
    with company.engine.store.connection(read_only=True) as connection:
        actual_version = company.engine.store.current_fact(connection, "actual-contribution")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(
        basis,
        accepted_calculations=(),
        source_facts=(
            {
                "subject_id": "actual-contribution",
                "fact_id": actual_version.id,
                "kind": "payroll_contribution_actual",
            },
        ),
    )
    saved = save_completion(company, completion)
    company.publish("completion")
    review = review_from_completion(
        completion, saved["fact_id"], basis["candidate_calculations"], "matched"
    )
    company.save(review, "review")
    company.publish("review")
    assert external_item(company)["basis_review_status"] == "reviewed"

    company.save(contribution_actual(employee=101_000), "actual-contribution", revision=1)
    company.confirm_payroll("january")
    company.publish("january")
    assert external_item(company)["actual_completion_status"] == "completed"
    assert external_item(company)["basis_review_status"] == "outdated"
    changed_basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    corrected_review = review_from_completion(
        completion,
        saved["fact_id"],
        changed_basis["candidate_calculations"],
        "difference_identified",
    )
    company.save(corrected_review, "difference-review")
    company.publish("difference-review")
    assert external_item(company)["basis_review_status"] == "difference_identified"


def test_financial_report_accepts_later_close_covering_empty_end_month(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation("quarterly_financial_report"), "obligation")
    company.close("2026-02")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    assert basis["candidate_calculations"] == []
    completion = completion_from_basis(
        basis,
        period="2026-03",
        completion_date="2026-03-02",
        accepted_calculations=(),
        no_reportable_activity_confirmed=True,
    )
    save_completion(company, completion)
    company.publish("completion")
    assert external_item(company, day="2026-03-10")["actual_completion_status"] == "completed"


def test_explicit_no_activity_submission_reconciles_to_later_accounting_difference(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "obligation")
    basis = workflow.Workflow(company.engine).obligation_basis("obligation")
    completion = completion_from_basis(
        basis, accepted_calculations=(), no_reportable_activity_confirmed=True
    )
    saved = save_completion(company, completion)
    company.publish("completion")
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january")
    company.publish("january")
    current = workflow.Workflow(company.engine).obligation_basis("obligation")
    review = review_from_completion(
        completion,
        saved["fact_id"],
        current["candidate_calculations"],
        "difference_identified",
    )
    company.save(review, "review")
    company.publish("review")
    item = external_item(company)
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "difference_identified"
