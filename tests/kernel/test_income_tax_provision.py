"""Computed provisions retain accounting provenance and actual-tax precedence."""

import json
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.contracts import (
    Calculation, Context, FactVersion, NeedsInformation, Read, Registry,
)
from ai_accounting.kernel.dependencies import calculation_matches, fact_matches
from ai_accounting.kernel.domains import income_tax, taxes
from ai_accounting.kernel.types import YearMonth

EVIDENCE = "a" * 64
ELIGIBILITY = "b" * 64
LOSSES = "c" * 64


def fact(model, subject, *, evidence=(), **fields):
    return FactVersion(subject + "-v1", subject, 1,
                       model.model_validate_json(json.dumps(fields)), tuple(evidence))


def policy(**changes):
    fields = {
        "version": "synthetic-2026", "source_url": "https://www.chinatax.gov.cn/test-policy",
        "effective_from": "2026-01-01", "effective_to": "2026-12-31",
        "standard_rate_percent": "25", "small_profit_rate_percent": "20",
        "small_profit_taxable_fraction": "0.25", "small_profit_limit_fen": 300_000_000,
    }
    return fact(income_tax.IncomeTaxPolicyFact, "policy", period="2026-01",
                policy=fields | changes)


def basis(*, evidence=(EVIDENCE, ELIGIBILITY, LOSSES), **changes):
    return fact(income_tax.IncomeTaxPrepaymentBasis, "basis", evidence=evidence, **({
        "period": "2026-09", "year": 2026, "tax_period_end": "2026-09",
        "collection_method": "actual_profit", "treatment": "standard",
        "basis_evidence": EVIDENCE, "eligibility_evidence": None,
        "loss_deduction_fen": 0, "loss_evidence": None, "special_treatments": [],
    } | changes))


def assessment(**changes):
    return fact(income_tax.IncomeTaxAssessment, "assessment", **({
        "period": "2026-09", "year": 2026, "assessment_basis": "computed_provision",
        "policy_id": "policy", "basis_id": "basis",
    } | changes))


def snapshot(profit=100_000, **changes):
    return {
        "year": 2026, "quarter": 3, "tax_period_end": "2026-09",
        "profit_before_tax_fen": profit, "basis_digest": "d" * 64,
        "source_basis": [{"type": "calculation_id", "value": "sale-calculation"}],
        "calculation_ids": ["sale-calculation"], "fact_ids": [], "fact_issues": [],
    } | changes


def context(source, *, facts=(), calculations=(), report=None):
    selections = {}
    for read in source.fact.reads_for(source.subject_id):
        if read.source == "fact":
            selections[read] = tuple(item for item in facts if fact_matches(read, item))
        else:
            selections[read] = tuple(item for original, item in calculations
                                     if calculation_matches(read, item, original.fact))
    report_reads = (Read("calculation", "*", "#sale-calculation"),)
    selections[report_reads[0]] = (
        Calculation("sale-calculation", "sale", "service_sale", YearMonth("2026-09"), {},
                    "sale-fact"),
    )
    return Context(selections, income_tax_snapshot=report, income_tax_reads=report_reads)


def calculate(*, profit=100_000, policy_version=None, basis_version=None,
              source=None, actual=None, earlier=(), report=None):
    source = source or assessment()
    facts = (policy_version or policy(), basis_version or basis(), *((actual,) if actual else ()))
    ctx = context(source, facts=facts, calculations=earlier,
                  report=snapshot(profit) if report is None else report)
    return taxes.calculate_income_tax_assessment(source, ctx), ctx


@pytest.mark.parametrize("profit,amount", [(100_001, 25_000), (2, 1), (-500, 0), (0, 0)])
def test_standard_rate_uses_shared_profit_and_rounds_once_to_integer_fen(profit, amount):
    outcome, ctx = calculate(profit=profit)
    assert outcome.values["cumulative_assessed_fen"] == amount
    assert outcome.values["amount_source"] == "provisional"
    reference = outcome.values["calculated_assessment"]
    assert reference["profit_before_tax_fen"] == profit
    assert reference["basis_digest"] == "d" * 64
    assert reference["source_basis"] == snapshot()["source_basis"]
    assert {"policy-v1", "basis-v1"} <= ctx.versions
    assert sum(row.debit for row in outcome.lines) == sum(row.credit for row in outcome.lines)
    assert all(row.cashflow is None for row in outcome.lines)


def test_evidenced_small_profit_treatment_and_loss_are_applied_before_rounding():
    selected = basis(treatment="small_low_profit", eligibility_evidence=ELIGIBILITY,
                     loss_deduction_fen=20_000, loss_evidence=LOSSES)
    outcome, _ = calculate(profit=100_010, basis_version=selected)
    assert outcome.values["cumulative_assessed_fen"] == 4_001
    assert outcome.values["calculated_assessment"]["taxable_profit_fen"] == 80_010
    with localcontext() as ctx:
        ctx.prec = 2
        repeated, _ = calculate(profit=100_010, basis_version=selected)
    assert repeated == outcome


def test_taxable_fraction_is_not_rounded_before_calculating_tax():
    selected = basis(treatment="small_low_profit", eligibility_evidence=ELIGIBILITY)
    outcome, _ = calculate(
        profit=6, basis_version=selected,
        policy_version=policy(small_profit_taxable_fraction="0.3", small_profit_rate_percent="25"),
    )
    assert outcome.values["cumulative_assessed_fen"] == 0


def test_cumulative_provision_subtracts_prior_adopted_tax_and_can_reverse_expense():
    previous = fact(taxes.HistoricalIncomeTaxAssessment, "previous", period="2026-06", year=2026,
                    cumulative_assessed_fen=30_000, assessment_basis="confirmed_provision")
    posted = Calculation("previous-calc", previous.subject_id, previous.fact.kind,
                         previous.fact.period, {"cumulative_assessed_fen": 30_000}, previous.id)
    outcome, _ = calculate(earlier=((previous, posted),))
    assert outcome.values["change_fen"] == -5_000
    assert [(row.account, row.debit, row.credit) for row in outcome.lines] == [
        ("222106", 5_000, 0), ("5801", 0, 5_000),
    ]
    assert outcome.values["calculated_assessment"]["change_fen"] == -5_000


@pytest.mark.parametrize("changes,field", [
    ({"treatment": "small_low_profit"}, "eligibility_evidence"),
    ({"loss_deduction_fen": 1}, "loss_evidence"),
    ({"loss_deduction_fen": 100_001, "loss_evidence": LOSSES}, "loss_deduction_fen"),
    ({"special_treatments": ["unimplemented-reduction"]}, "special_treatments"),
    ({"tax_period_end": "2026-06"}, "tax_period_end"),
])
def test_missing_or_unsupported_treatment_never_defaults_to_a_tax_amount(changes, field):
    with pytest.raises(NeedsInformation) as failure:
        calculate(basis_version=basis(**changes))
    assert failure.value.issues[0]["field"] == "income_tax_prepayment_basis." + field


def test_basis_original_must_be_registered_as_its_own_evidence():
    with pytest.raises(NeedsInformation) as failure:
        calculate(basis_version=basis(evidence=()))
    assert failure.value.issues[0]["field"] == "income_tax_prepayment_basis.basis_evidence"


def test_small_profit_ceiling_needs_new_applicability_basis_instead_of_silent_rate_switch():
    selected = basis(treatment="small_low_profit", eligibility_evidence=ELIGIBILITY)
    outcome, _ = calculate(profit=300_000_000, basis_version=selected)
    assert outcome.values["cumulative_assessed_fen"] == 15_000_000
    with pytest.raises(NeedsInformation) as failure:
        calculate(profit=300_000_001, basis_version=selected)
    assert failure.value.issues[0]["field"] == "income_tax_prepayment_basis.treatment"


@pytest.mark.parametrize("changes", [
    {"year": 2025}, {"quarter": 2}, {"tax_period_end": "2026-06"},
    {"profit_before_tax_fen": 1.0}, {"basis_digest": "invalid"}, {"source_basis": None},
])
def test_incomplete_or_different_profit_snapshot_is_rejected(changes):
    with pytest.raises(NeedsInformation) as failure:
        calculate(report=snapshot(**changes))
    assert failure.value.issues[0]["field"] == "income_tax_snapshot"


def test_snapshot_fact_issues_are_preserved_in_structured_response():
    problem = {"field": "pending", "message": "损益来源尚未采用", "semantics": "accounting",
               "reusable_sources": ["sale"], "allowed_precision": []}
    with pytest.raises(NeedsInformation) as failure:
        calculate(report=snapshot(fact_issues=[problem]))
    assert json.loads(json.dumps(failure.value.issues)) == [problem]


def test_exact_snapshot_source_set_must_match_traced_calculation_dependencies():
    for changes in ({"calculation_ids": ["other-calculation"]}, {"fact_ids": ["missing-fact"]}):
        with pytest.raises(NeedsInformation) as failure:
            calculate(report=snapshot(**changes))
        assert failure.value.issues[0]["field"] == "income_tax_snapshot.source_basis"


def actual(**changes):
    return fact(taxes.IncomeTaxDeclarationActual, "actual", evidence=(EVIDENCE,), **({
        "period": "2026-10", "assessment_id": "assessment", "year": 2026,
        "tax_period_end": "2026-09", "filing_evidence": EVIDENCE,
        "basis_type": "filed_return", "cumulative_assessed_fen": 20_000,
        "prepaid_tax_fen": 12_000, "tax_due_fen": 8_000,
    } | changes))


def test_actual_cumulative_tax_overrides_computed_reference_without_using_amount_due():
    outcome, _ = calculate(actual=actual())
    assert outcome.values["amount_source"] == "actual"
    assert outcome.values["cumulative_assessed_fen"] == 20_000
    assert outcome.values["change_fen"] == 20_000
    assert outcome.values["tax_due_fen"] == 8_000
    assert outcome.values["calculated_assessment"]["cumulative_assessed_fen"] == 25_000


def test_actual_tax_survives_unavailable_reference_and_explicit_zero_is_authoritative():
    outcome, _ = calculate(actual=actual(cumulative_assessed_fen=0, prepaid_tax_fen=0, tax_due_fen=0),
                           report={"year": 2025})
    assert outcome.values["cumulative_assessed_fen"] == 0
    assert not outcome.lines
    assert outcome.values["calculated_assessment"] is None
    assert outcome.values["calculation_reference_issue"]["status"] == "needs_information"


def test_actual_tax_is_adopted_even_when_reference_policy_is_absent():
    source = assessment()
    ctx = context(source, facts=(basis(), actual()), report=snapshot())
    outcome = taxes.calculate_income_tax_assessment(source, ctx)
    assert outcome.values["cumulative_assessed_fen"] == 20_000
    assert outcome.values["amount_source"] == "actual"
    assert outcome.values["calculated_assessment"] is None


def test_actual_missing_cumulative_tax_cannot_fall_back_to_computed_tax():
    with pytest.raises(NeedsInformation) as failure:
        calculate(actual=actual(cumulative_assessed_fen=None))
    assert failure.value.issues[0]["field"] == "income_tax_declaration_actual.cumulative_assessed_fen"


@pytest.mark.parametrize("field,value", [
    ("standard_rate_percent", 25.0), ("small_profit_taxable_fraction", Decimal("NaN")),
    ("source_url", "https://example.com/tax"), ("effective_to", "2025-12-31"),
])
def test_policy_requires_finite_decimal_rates_official_source_and_effective_interval(field, value):
    if isinstance(value, Decimal):
        with pytest.raises(ValidationError):
            income_tax.IncomeTaxPolicy.model_validate({
                **policy().fact.policy.model_dump(mode="python"), field: value,
            })
    else:
        with pytest.raises(ValidationError):
            policy(**{field: value})


def test_policy_must_cover_the_entire_cumulative_year_to_quarter():
    with pytest.raises(NeedsInformation) as failure:
        calculate(policy_version=policy(effective_from="2026-07-01"))
    assert failure.value.issues[0]["field"] == "income_tax_policy.effective_from"


def test_computed_registration_requires_sources_without_accepting_an_ai_tax_amount():
    with pytest.raises(ValidationError):
        assessment(cumulative_assessed_fen=0)
    with pytest.raises(ValidationError):
        assessment(period="2026-08")
    missing = assessment(policy_id=None)
    with pytest.raises(NeedsInformation) as failure:
        missing.fact.validate_registration()
    assert failure.value.issues[0]["field"] == "policy_id"
    assessment().fact.validate_registration()


def test_historical_body_is_retained_unchanged_in_a_separate_table():
    registry = Registry()
    taxes.register(registry)
    assert registry.current_table("income_tax_assessment") == "fact_income_tax_assessment_computed"
    assert registry.fact_bodies("income_tax_assessment")[1] == (
        taxes.HistoricalIncomeTaxAssessment, "fact_income_tax_assessment",
    )
    assert set(taxes.HistoricalIncomeTaxAssessment.model_fields) == {
        "period", "year", "assessment_basis", "cumulative_assessed_fen",
    }
