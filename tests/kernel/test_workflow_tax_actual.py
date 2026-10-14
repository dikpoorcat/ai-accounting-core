"""Actual tax filings bind covered periods and exact sources independently of posting."""

import json

import pytest

from ai_accounting.kernel import workflow
from ai_accounting.kernel.contracts import (
    Calculation,
    Context,
    FactVersion,
    KernelError,
    NeedsInformation,
)
from ai_accounting.kernel.dependencies import calculation_matches, fact_matches
from ai_accounting.kernel.domains import taxes

FILING = "a" * 64
RECEIPT = "b" * 64


def fact_version(model, subject, fields, *, evidence=(FILING,)):
    return FactVersion(
        subject + "-fact", subject, 1,
        model.model_validate_json(json.dumps(fields)), evidence,
    )


def history(kind="vat", *, actual_adopted=True):
    annual = kind == "annual"
    observation = "2027-05" if annual else "2026-10"
    if kind == "vat":
        assessment = fact_version(taxes.TaxAssessment, "assessment", {
            "period": observation, "period_start": "2026-07-01", "period_end": "2026-09-30",
            "vat_policy_id": "vat", "surtax_policy_id": "surtax",
        })
        source = fact_version(taxes.VatDeclarationActual, "actual", {
            "period": observation, "assessment_id": "assessment",
            "period_start": "2026-07-01", "period_end": "2026-09-30",
            "filing_evidence": FILING, "basis_type": "filed_return",
            "assessed_vat_fen": 0, "urban_tax_fen": 0,
            "education_tax_fen": 0, "local_education_tax_fen": 0,
        })
        values = {"period_start": "2026-07-01", "period_end": "2026-09-30",
                  "payable_vat_fen": 0, "urban_tax_fen": 0,
                  "education_tax_fen": 0, "local_education_tax_fen": 0}
    else:
        tax_end = "2026-12" if annual else "2026-09"
        assessment = fact_version(taxes.IncomeTaxAssessment, "assessment", {
            "period": observation if annual else tax_end, "year": 2026,
            "cumulative_assessed_fen": 10_000,
            "assessment_basis": "annual_settlement" if annual else "confirmed_provision",
        })
        source = fact_version(taxes.IncomeTaxDeclarationActual, "actual", {
            "period": observation, "assessment_id": "assessment", "year": 2026,
            "tax_period_end": tax_end, "filing_evidence": FILING,
            "basis_type": "filed_return", "cumulative_assessed_fen": 10_000,
        })
        values = {"year": 2026, "tax_period_end": tax_end,
                  "cumulative_assessed_fen": 10_000}
    if actual_adopted:
        values["actual_declaration"] = {
            "subject_id": source.subject_id, "fact_id": source.id,
            "filing_evidence": FILING, "basis_type": "filed_return",
        }
    else:
        values["actual_declaration"] = None
    calculation = Calculation(
        "assessment-calculation", assessment.subject_id, assessment.fact.kind,
        assessment.fact.period, values, assessment.id, "c" * 64,
    )
    obligation = fact_version(workflow.ExternalObligation, "obligation", {
        "period": "2026-12" if annual else "2026-09",
        "obligation_kind": "annual_income_tax" if annual else "quarterly_tax",
        "start_period": "2026-01" if annual else "2026-07",
        "end_period": "2026-12" if annual else "2026-09",
        "applicability_confirmed": True,
    })
    completion = fact_version(workflow.ExternalCompletion, "completion", {
        "period": observation, "obligation_id": obligation.subject_id,
        "obligation_fact_id": obligation.id,
        "obligation_kind": obligation.fact.obligation_kind,
        "start_period": str(obligation.fact.start_period),
        "end_period": str(obligation.fact.end_period),
        "source_facts": [{"subject_id": source.subject_id, "fact_id": source.id,
                          "kind": source.fact.kind}],
        "accepted_calculations": [{"subject_id": calculation.subject_id,
                                   "calculation_id": calculation.id}],
        "completion_status": "confirmed_complete", "date_status": "not_established",
    }, evidence=(RECEIPT,))
    return assessment, source, calculation, obligation, completion


def context_for(version, facts, calculations):
    source_facts = {item.id: item for item in facts}

    def select(read):
        if read.source == "fact":
            return tuple(item for item in facts if fact_matches(read, item))
        return tuple(
            item for item in calculations
            if calculation_matches(read, item, source_facts[item.fact_id].fact)
        )

    selections = {read: select(read) for read in version.fact.reads_for(version.subject_id)}
    initial = Context(selections)
    selections.update({
        read: select(read) for read in version.fact.reads_after(version.subject_id, initial)
    })
    return Context(selections, accounting=lambda ident: {"contract": "test", "digest": ident})


@pytest.mark.parametrize("kind", ["vat", "cit", "annual"])
def test_actual_tax_completion_uses_covered_tax_period_with_later_observation(kind):
    assessment, source, calculation, obligation, completion = history(kind)
    context = context_for(completion, (assessment, source, obligation, completion), (calculation,))
    outcome = workflow.calculate_completion(completion, context)
    assert outcome.values["source_facts"][0]["fact_id"] == source.id
    assert outcome.values["accepted_calculations"][0]["calculation_id"] == calculation.id
    assert outcome.lines == ()


@pytest.mark.parametrize("kind", ["vat", "cit", "annual"])
def test_tax_actual_observed_submission_can_precede_accounting_adoption(kind):
    assessment, source, calculation, obligation, completion = history(kind, actual_adopted=False)
    completion = FactVersion(
        completion.id, completion.subject_id, completion.revision,
        completion.fact.model_copy(update={"accepted_calculations": ()}), completion.evidence,
    )
    context = context_for(completion, (assessment, source, obligation, completion), ())
    outcome = workflow.calculate_completion(completion, context)
    assert outcome.values["accepted_calculations"] == []
    assert outcome.values["source_facts"][0]["fact_id"] == source.id


@pytest.mark.parametrize("kind", ["vat", "cit", "annual"])
@pytest.mark.parametrize("failure", ["orphan", "wrong_kind", "wrong_coverage"])
def test_source_only_tax_completion_requires_a_matching_registered_assessment(kind, failure):
    assessment, source, calculation, obligation, completion = history(kind)
    completion = FactVersion(
        completion.id, completion.subject_id, 1,
        completion.fact.model_copy(update={"accepted_calculations": ()}), completion.evidence,
    )
    if failure == "orphan":
        facts = (source, obligation, completion)
    else:
        data = assessment.fact.model_dump(mode="json")
        if failure == "wrong_kind":
            model = taxes.IncomeTaxAssessment if kind == "vat" else taxes.TaxAssessment
            data = (
                {"period": "2026-09", "year": 2026, "cumulative_assessed_fen": 0,
                 "assessment_basis": "confirmed_provision"}
                if kind == "vat" else
                {"period": "2026-09", "period_start": "2026-07-01",
                 "period_end": "2026-09-30", "vat_policy_id": "vat", "surtax_policy_id": "surtax"}
            )
        else:
            model = type(assessment.fact)
            data.update(
                {"period_start": "2026-04-01"} if kind == "vat" else
                {"period": "2026-08"} if kind == "cit" else {"year": 2025}
            )
        assessment = fact_version(model, assessment.subject_id, data)
        facts = (assessment, source, obligation, completion)
    with pytest.raises(KernelError) as error:
        workflow.calculate_completion(completion, context_for(completion, facts, ()))
    assert error.value.code == (
        "invalid_adopted_source" if failure == "wrong_coverage" else "needs_information"
    )


@pytest.mark.parametrize("kind", ["vat", "cit", "annual"])
def test_tax_workflow_basis_finds_assessments_by_tax_coverage_not_posting_month(kind):
    assessment, source, calculation, obligation, completion = history(kind)
    context = context_for(
        fact_version(workflow.ExternalBasisReview, "review", {
            "period": str(completion.fact.period), "completion_id": completion.subject_id,
            "completion_fact_id": completion.id, "obligation_id": obligation.subject_id,
            "obligation_fact_id": obligation.id,
            "obligation_kind": obligation.fact.obligation_kind,
            "start_period": str(obligation.fact.start_period),
            "end_period": str(obligation.fact.end_period), "reviewed_calculations": [],
            "review_result": "unestablished",
        }),
        (assessment, source, obligation, completion), (calculation,),
    )
    selected, issues = workflow._basis_state(obligation.fact, context.select)
    assert selected == (calculation,)
    assert issues == []


def test_quarterly_tax_basis_does_not_adopt_following_year_annual_settlement():
    annual, source, annual_calculation, _, completion = history("annual")
    quarter = fact_version(taxes.IncomeTaxAssessment, "quarter", {
        "period": "2026-12", "year": 2026, "cumulative_assessed_fen": 10_000,
        "assessment_basis": "confirmed_provision",
    })
    quarter_calculation = Calculation(
        "quarter-calculation", quarter.subject_id, quarter.fact.kind, quarter.fact.period,
        {"year": 2026, "tax_period_end": "2026-12", "cumulative_assessed_fen": 10_000},
        quarter.id, "d" * 64,
    )
    obligation = fact_version(workflow.ExternalObligation, "obligation", {
        "period": "2026-12", "obligation_kind": "quarterly_tax",
        "start_period": "2026-10", "end_period": "2026-12",
        "applicability_confirmed": True,
    })
    review = fact_version(workflow.ExternalBasisReview, "review", {
        "period": "2027-05", "completion_id": completion.subject_id,
        "completion_fact_id": completion.id, "obligation_id": obligation.subject_id,
        "obligation_fact_id": obligation.id, "obligation_kind": "quarterly_tax",
        "start_period": "2026-10", "end_period": "2026-12",
        "reviewed_calculations": [], "review_result": "unestablished",
    })
    context = context_for(
        review, (annual, quarter, source, obligation, completion),
        (annual_calculation, quarter_calculation),
    )
    selected, issues = workflow._basis_state(obligation.fact, context.select)
    assert selected == (quarter_calculation,)
    assert issues == []


@pytest.mark.parametrize("failure", ["missing_evidence", "future_observation", "wrong_coverage"])
def test_tax_completion_rejects_missing_original_evidence_or_foreign_coverage(failure):
    assessment, source, calculation, obligation, completion = history()
    if failure == "missing_evidence":
        source = FactVersion(source.id, source.subject_id, 1, source.fact, ())
    elif failure == "future_observation":
        source = FactVersion(
            source.id, source.subject_id, 1,
            source.fact.model_copy(update={"period": type(source.fact.period)("2026-11")}),
            source.evidence,
        )
    else:
        source = FactVersion(
            source.id, source.subject_id, 1,
            taxes.VatDeclarationActual.model_validate_json(json.dumps({
                **source.fact.model_dump(mode="json"), "period_start": "2026-04-01",
            })), source.evidence,
        )
    context = context_for(completion, (assessment, source, obligation, completion), (calculation,))
    with pytest.raises(KernelError) as error:
        workflow.calculate_completion(completion, context)
    assert error.value.code == (
        "needs_information" if failure == "missing_evidence" else "invalid_adopted_source"
    )
    if failure == "missing_evidence":
        assert isinstance(error.value, NeedsInformation)
        assert error.value.issues[0]["field"] == "source_facts.filing_evidence"


@pytest.mark.parametrize("kind", ["vat", "cit", "annual"])
@pytest.mark.parametrize("actual_adopted", [False, True])
def test_tax_review_requires_exact_actual_source_even_when_amount_is_equal(kind, actual_adopted):
    assessment, source, calculation, obligation, completion = history(
        kind, actual_adopted=actual_adopted,
    )
    reference = workflow.AcceptedCalculation(
        subject_id=calculation.subject_id, calculation_id=calculation.id,
    )
    review = fact_version(workflow.ExternalBasisReview, "review", {
        "period": str(completion.fact.period), "completion_id": completion.subject_id,
        "completion_fact_id": completion.id, "obligation_id": obligation.subject_id,
        "obligation_fact_id": obligation.id,
        "obligation_kind": obligation.fact.obligation_kind,
        "start_period": str(obligation.fact.start_period),
        "end_period": str(obligation.fact.end_period),
        "source_facts": [item.model_dump() for item in completion.fact.source_facts],
        "adopted_calculations": [reference.model_dump()],
        "reviewed_calculations": [reference.model_dump()],
        "review_result": "matched" if actual_adopted else "difference_identified",
    })
    context = context_for(review, (assessment, source, obligation, completion), (calculation,))
    outcome = workflow.calculate_basis_review(review, context)
    assert outcome.values["review_result"] == review.fact.review_result
    if not actual_adopted:
        forged = FactVersion(
            review.id, review.subject_id, 1,
            review.fact.model_copy(update={"review_result": "matched"}), review.evidence,
        )
        with pytest.raises(KernelError) as error:
            workflow.calculate_basis_review(
                forged, context_for(
                    forged, (assessment, source, obligation, completion), (calculation,),
                ),
            )
        assert error.value.code == "review_not_proven"
