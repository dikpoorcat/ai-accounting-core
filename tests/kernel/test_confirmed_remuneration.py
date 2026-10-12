"""Remuneration amounts follow explicit individual deductions, without PIT policy inputs."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.contracts import (
    Calculation,
    Context,
    FactVersion,
    KernelError,
    NeedsInformation,
)
from ai_accounting.kernel.dependencies import read_matches, scope_keys
from ai_accounting.kernel.domains.payroll import (
    AnnualBonus,
    ContributionRuleFact,
    LaborRemuneration,
    Payroll,
    PayrollBounded,
    PayrollContributionPolicy,
    PayrollProfile,
    calculate_annual_bonus,
    calculate_labor,
    calculate_payroll,
    prepare_payroll_contributions,
)
from ai_accounting.kernel.payroll_confirmation import FactRevisionReference
from ai_accounting.kernel.payroll_preparation import (
    PayrollNoChange,
    PayrollPlan,
    PayrollReuseEmployee,
)
from ai_accounting.kernel.remuneration_tax_basis import RemunerationTaxBasis


def version(fact, subject, *, revision=1, evidence=("confirmed-source",)):
    return FactVersion(f"{subject}:v{revision}", subject, revision, fact, evidence)


def context(current, facts=(), calculations=()):
    all_facts = (*facts, current)
    heads = {(item.fact.kind, item.subject_id): item.id for item in all_facts}
    selections = {}

    def select(read):
        candidates = all_facts if read.source == "fact" else calculations
        return tuple(
            item
            for item in candidates
            if read_matches(
                read,
                source=read.source,
                kind=item.fact.kind if read.source == "fact" else item.kind,
                ident=item.id,
                current=(
                    read.source != "fact" or heads[(item.fact.kind, item.subject_id)] == item.id
                ),
                period=item.fact.period if read.source == "fact" else item.period,
                scopes=scope_keys("fact", item.fact, item.subject_id)
                if read.source == "fact"
                else (
                    "@" + item.subject_id,
                    str(item.period),
                ),
            )
        )

    for read in current.fact.reads_for(current.subject_id):
        selections[read] = select(read)
    first = Context(selections)
    for read in current.fact.reads_after(current.subject_id, first):
        selections[read] = select(read)
    return Context(selections)


def tax_basis(
    current, amount, *, subject="tax-basis", revision=1, evidence=("tax-record",), **fields
):
    return version(
        RemunerationTaxBasis(
            period=current.fact.period,
            source_kind=current.fact.kind,
            source_id=current.subject_id,
            source_fact_id=current.id,
            tax_period=current.fact.period,
            tax_amount_fen=amount,
            basis_type="final_statement",
            confirmed=True,
            **fields,
        ),
        subject,
        revision=revision,
        evidence=evidence,
    )


def wage_case(model=Payroll, *, period="2026-08"):
    wage = version(
        model(
            period=period,
            employee_id="employee",
            profile_revision=FactRevisionReference(subject_id="profile", revision=1),
            contribution_policy_revision=FactRevisionReference(subject_id="policy", revision=1),
            accounting_gross_salary_fen=4_000_000,
            expense_class="management",
            contribution_basis="policy_until_actual",
        ),
        "wage:" + period,
    )
    profile = version(
        PayrollProfile(
            period="2026-01",
            employee_id="employee",
            effective_from="2026-01",
            effective_to=None,
            social_insurance_base_fen=None,
            housing_fund_base_fen=None,
            social_insurance_participating=False,
            housing_fund_participating=False,
            contribution_shortfall="reject",
        ),
        "profile",
    )
    policy = version(
        PayrollContributionPolicy(
            period="2026-01",
            version="contributions",
            jurisdiction="test",
            effective_from="2026-01-01",
            effective_to=None,
            primary_source_url="https://www.mof.gov.cn/",
            rules=(
                ContributionRuleFact(
                    code="pension",
                    base_kind="social_insurance",
                    employee_rate="0.08",
                    employer_rate="0.16",
                    minimum_base_fen=0,
                    maximum_base_fen=10_000_000,
                    rounding="half_up",
                    enabled=True,
                ),
            ),
        ),
        "policy",
    )
    plan = version(
        PayrollPlan(
            period=period,
            payroll_revision=FactRevisionReference(subject_id=wage.subject_id, revision=1),
        ),
        "plan:" + period,
    )
    return wage, (profile, policy, plan)


@pytest.mark.parametrize("model", [Payroll, PayrollBounded])
def test_wages_require_actual_source_even_without_contributions_or_tax_calculation(model):
    wage, sources = wage_case(model)
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(wage, context(wage, sources))
    assert error.value.issues[0]["field"] == "remuneration_tax_basis"
    observed = tax_basis(wage, 90_000)
    outcome = calculate_payroll(wage, context(wage, (*sources, observed)))
    assert outcome.values["tax_fen"] == 90_000
    assert outcome.values["net_fen"] == 3_910_000
    assert outcome.values["tax_basis"]["fact_id"] == observed.id
    assert not {"tax_state", "tax_input", "calculated_tax_fen"} & outcome.values.keys()
    assert sum(line.debit for line in outcome.lines) == sum(line.credit for line in outcome.lines)


def test_zero_requires_evidence_and_tax_change_reuses_exact_wage_plan():
    wage, sources = wage_case()
    zero = tax_basis(wage, 0)
    first = calculate_payroll(wage, context(wage, (*sources, zero)))
    changed = tax_basis(wage, 90_000, revision=2)
    second = calculate_payroll(wage, context(wage, (*sources, changed)))
    assert first.values["tax_fen"] == 0
    assert second.values["payroll_confirmation"] == first.values["payroll_confirmation"]
    assert second.values["net_fen"] == first.values["net_fen"] - 90_000
    with pytest.raises(NeedsInformation):
        calculate_payroll(wage, context(wage, (*sources, replace(zero, evidence=()))))


def test_pre_tax_contribution_preparation_does_not_require_or_adopt_tax_basis():
    wage, sources = wage_case()
    prepared = prepare_payroll_contributions(wage, context(wage, sources))
    assert prepared["burden"].employee_total_fen == 0
    assert prepared["confirmation"]["mode"] == "monthly_plan"


def test_tax_basis_conflicts_and_impossible_net_are_rejected():
    wage, sources = wage_case()
    basis = tax_basis(wage, 90_000)
    duplicate = replace(basis, id="other:v1", subject_id="other")
    with pytest.raises(KernelError, match="多个当前税额"):
        calculate_payroll(wage, context(wage, (*sources, basis, duplicate)))
    excessive = tax_basis(wage, 4_000_001)
    with pytest.raises(KernelError) as error:
        calculate_payroll(wage, context(wage, (*sources, excessive)))
    assert error.value.code == "negative_net_pay"


@pytest.mark.parametrize(
    "model, calculator, fields",
    [
        (AnnualBonus, calculate_annual_bonus, {"employee_id": "employee", "bonus_fen": 100_000}),
        (
            LaborRemuneration,
            calculate_labor,
            {"person_id": "person", "remuneration_method": "fixed", "fixed_fee_fen": 100_000},
        ),
    ],
)
def test_bonus_and_labor_use_same_source_amount_without_policy_or_method_choice(
    model, calculator, fields
):
    current = version(
        model(period="2026-08", income_date="2026-08-20", expense_class="management", **fields),
        "remuneration",
    )
    with pytest.raises(NeedsInformation):
        calculator(current, context(current))
    outcome = calculator(current, context(current, (tax_basis(current, 12_345),)))
    assert outcome.values["tax_fen"] == 12_345
    assert outcome.values["net_fen"] == 87_655
    assert outcome.values["rule_versions"] == []


def test_unchanged_month_requires_its_own_tax_basis():
    prior, sources = wage_case(period="2026-08")
    outcome = calculate_payroll(prior, context(prior, (*sources, tax_basis(prior, 0))))
    previous = Calculation(
        "prior-result",
        prior.subject_id,
        prior.fact.kind,
        prior.fact.period,
        outcome.values,
        prior.id,
    )
    current = version(
        prior.fact.model_copy(update={"period": type(prior.fact.period)("2026-09")}), "wage:2026-09"
    )
    unchanged = version(
        PayrollNoChange(
            period="2026-09",
            employees=(
                PayrollReuseEmployee(
                    prior_payroll_revision=FactRevisionReference(
                        subject_id=prior.subject_id, revision=1
                    )
                ),
            ),
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "unchanged",
    )
    facts = (*sources[:2], prior, unchanged, tax_basis(prior, 0))
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context(current, facts, (previous,)))
    assert error.value.issues[0]["field"] == "remuneration_tax_basis"
    current_tax = tax_basis(current, 90_000, subject="september-tax")
    result = calculate_payroll(current, context(current, (*facts, current_tax), (previous,)))
    assert result.values["tax_fen"] == 90_000
    assert result.values["payroll_confirmation"]["mode"] == "explicit_no_change"


def test_final_statement_cannot_claim_a_real_withholding_date():
    wage, _ = wage_case()
    with pytest.raises(ValidationError):
        tax_basis(wage, 0, withholding_date="2026-08-20")


def test_actual_withholding_date_is_preserved_independently_of_tax_period():
    wage, sources = wage_case()
    original = tax_basis(wage, 90_000)
    actual = replace(
        original,
        fact=RemunerationTaxBasis(
            **{
                **original.fact.model_dump(),
                "basis_type": "actual_withholding",
                "withholding_date": "2026-09-05",
            }
        ),
    )
    result = calculate_payroll(wage, context(wage, (*sources, actual)))
    assert result.values["tax_basis"]["tax_period"] == "2026-08"
    assert result.values["tax_basis"]["withholding_date"] == "2026-09-05"


def pooled_proof(current, other, *, current_amount=90_000, other_amount=10_000):
    return {
        "evidence_digest": "a" * 64,
        "source_location": "第1页申报汇总第2行",
        "total_tax_fen": 100_000,
        "allocations": (
            {
                "source_kind": current.fact.kind,
                "source_id": current.subject_id,
                "period": str(current.fact.period),
                "tax_amount_fen": current_amount,
            },
            {
                "source_kind": other.fact.kind,
                "source_id": other.subject_id,
                "period": str(other.fact.period),
                "tax_amount_fen": other_amount,
            },
        ),
    }


def test_complete_pooled_tax_proof_adopts_individual_amount_and_reuses_source_evidence():
    wage, sources = wage_case()
    other = version(
        AnnualBonus(
            period="2026-08",
            income_date="2026-08-20",
            employee_id="other",
            bonus_fen=100_000,
            expense_class="management",
        ),
        "other-bonus",
    )
    proof = pooled_proof(wage, other)
    source = tax_basis(wage, 90_000, evidence=("a" * 64,), pooled_tax_proof=proof)
    result = calculate_payroll(wage, context(wage, (*sources, other, source)))
    assert result.values["tax_fen"] == 90_000
    assert result.values["tax_basis"]["pooled_tax_proof"]["total_tax_fen"] == 100_000
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(
            wage, context(wage, (*sources, other, replace(source, evidence=("other-evidence",))))
        )
    assert error.value.issues[0]["field"] == "pooled_tax_proof.evidence_digest"


def test_pooled_total_cannot_be_adopted_as_individual_tax_or_incomplete_split():
    wage, _ = wage_case()
    other = version(
        AnnualBonus(
            period="2026-08",
            income_date="2026-08-20",
            employee_id="other",
            bonus_fen=100_000,
            expense_class="management",
        ),
        "other-bonus",
    )
    with pytest.raises(ValidationError, match="individual pooled allocation"):
        tax_basis(wage, 100_000, pooled_tax_proof=pooled_proof(wage, other))
    with pytest.raises(ValidationError, match="complete individual split"):
        tax_basis(wage, 90_000, pooled_tax_proof=pooled_proof(wage, other, other_amount=0))


def test_same_pooled_source_cannot_have_conflicting_individual_splits():
    wage, sources = wage_case()
    other = version(
        AnnualBonus(
            period="2026-08",
            income_date="2026-08-20",
            employee_id="other",
            bonus_fen=100_000,
            expense_class="management",
        ),
        "other-bonus",
    )
    source = tax_basis(
        wage, 90_000, evidence=("a" * 64,), pooled_tax_proof=pooled_proof(wage, other)
    )
    conflicting = tax_basis(
        other,
        20_000,
        subject="other-tax",
        evidence=("a" * 64,),
        pooled_tax_proof=pooled_proof(wage, other, current_amount=80_000, other_amount=20_000),
    )
    with pytest.raises(KernelError) as error:
        calculate_payroll(wage, context(wage, (*sources, other, source, conflicting)))
    assert error.value.code == "conflicting_pooled_tax_allocation"


def test_same_pool_conflict_is_detected_even_when_target_sets_do_not_overlap():
    wage, sources = wage_case()
    others = [
        version(
            AnnualBonus(
                period="2026-08",
                income_date="2026-08-20",
                employee_id=f"other-{index}",
                bonus_fen=100_000,
                expense_class="management",
            ),
            f"other-bonus-{index}",
        )
        for index in range(3)
    ]
    proof = pooled_proof(wage, others[0])
    source = tax_basis(wage, 90_000, evidence=("a" * 64,), pooled_tax_proof=proof)
    different = tax_basis(
        others[1],
        90_000,
        subject="different-target-tax",
        evidence=("a" * 64,),
        pooled_tax_proof=pooled_proof(others[1], others[2]),
    )
    with pytest.raises(KernelError) as error:
        calculate_payroll(wage, context(wage, (*sources, *others, source, different)))
    assert error.value.code == "conflicting_pooled_tax_allocation"


def test_large_complete_pool_uses_bounded_group_reads_and_preserves_each_individual_amount():
    wage, sources = wage_case()
    others = [
        version(
            AnnualBonus(
                period="2026-08",
                income_date="2026-08-20",
                employee_id=f"other-{index}",
                bonus_fen=100_000,
                expense_class="management",
            ),
            f"other-bonus-{index}",
        )
        for index in range(100)
    ]
    allocations = (
        {
            "source_kind": wage.fact.kind,
            "source_id": wage.subject_id,
            "period": str(wage.fact.period),
            "tax_amount_fen": 90_000,
        },
        *(
            {
                "source_kind": item.fact.kind,
                "source_id": item.subject_id,
                "period": str(item.fact.period),
                "tax_amount_fen": 100,
            }
            for item in others
        ),
    )
    source = tax_basis(
        wage,
        90_000,
        evidence=("a" * 64,),
        pooled_tax_proof={
            "evidence_digest": "a" * 64,
            "source_location": "汇总表第1行",
            "total_tax_fen": 100_000,
            "allocations": allocations,
        },
    )
    prepared_context = context(wage, (*sources, *others, source))
    assert len(wage.fact.reads_after(wage.subject_id, prepared_context)) <= 4
    assert calculate_payroll(wage, prepared_context).values["tax_fen"] == 90_000


def test_old_actual_withholding_can_only_be_adopted_by_explicit_matching_reference():
    from ai_accounting.kernel.payroll_history import PayrollWithholdingActual

    wage, sources = wage_case()
    old = version(
        PayrollWithholdingActual(
            period="2026-08",
            employee_id="employee",
            withheld_tax_fen=90_000,
            withholding_confirmed=True,
        ),
        "old-withholding",
    )
    with pytest.raises(NeedsInformation):
        calculate_payroll(wage, context(wage, (*sources, old)))
    source = tax_basis(wage, 90_000, subject="new-basis")
    adopted = replace(
        source,
        fact=source.fact.model_copy(
            update={
                "basis_type": "actual_withholding",
                "origin_references": (),
            }
        ),
    )
    # Parse through the public grammar so nested exact provenance is validated.
    adopted = replace(
        adopted,
        fact=RemunerationTaxBasis.model_validate_json(
            RemunerationTaxBasis(
                **{
                    **adopted.fact.model_dump(),
                    "origin_references": (
                        {"kind": old.fact.kind, "subject_id": old.subject_id, "fact_id": old.id},
                    ),
                }
            ).model_dump_json()
        ),
    )
    assert (
        calculate_payroll(wage, context(wage, (*sources, old, adopted))).values["tax_fen"] == 90_000
    )
    changed = replace(old, fact=old.fact.model_copy(update={"withheld_tax_fen": 80_000}))
    with pytest.raises(KernelError) as error:
        calculate_payroll(wage, context(wage, (*sources, changed, adopted)))
    assert error.value.code == "remuneration_tax_origin_conflict"


def test_tax_stage_and_evidence_are_no_impact_but_amount_and_tax_period_remain_significant():
    from dataclasses import asdict

    from ai_accounting.kernel.accounting import AccountingBook
    from ai_accounting.kernel.contracts import Registry
    from ai_accounting.kernel.domains import accounting

    wage, sources = wage_case()
    original = tax_basis(wage, 90_000)
    occurred = version(
        RemunerationTaxBasis(
            **{
                **original.fact.model_dump(),
                "basis_type": "actual_withholding",
                "withholding_date": "2026-08-20",
            }
        ),
        original.subject_id,
        revision=2,
        evidence=("actual-withholding-evidence",),
    )
    changed_amount = tax_basis(wage, 90_001, revision=3)
    changed_period = version(
        RemunerationTaxBasis(
            **{
                **original.fact.model_dump(),
                "tax_period": "2026-09",
            }
        ),
        original.subject_id,
        revision=4,
    )
    registry = Registry()
    accounting.register(registry)
    book = AccountingBook(registry)
    signatures, outcomes = [], []
    for index, basis in enumerate((original, occurred, changed_amount, changed_period)):
        result = calculate_payroll(wage, context(wage, (*sources, basis)))
        calculation = Calculation(
            f"result-{index}",
            wage.subject_id,
            wage.fact.kind,
            wage.fact.period,
            result.values,
            wage.id,
        )
        book.facts[basis.id] = basis
        book.add(calculation, wage, asdict(result), fact_ids=(basis.id,))
        signatures.append(book.signature(calculation.id))
        outcomes.append(result)
    assert signatures[0] == signatures[1]
    assert signatures[0] != signatures[2]
    assert signatures[0] != signatures[3]
    # Signature projection never replaces the complete retained business outcome.
    assert outcomes[1].values["tax_basis"]["evidence"] == ["actual-withholding-evidence"]
    assert outcomes[1].values["tax_basis"]["withholding_date"] == "2026-08-20"


@pytest.mark.parametrize(
    "change", [{"accounting_gross_salary_fen": 4_000_001}, {"employee_id": "another"}]
)
def test_person_or_gross_revision_requires_explicit_current_tax_source(change):
    prior, sources = wage_case()
    old_tax = tax_basis(prior, 90_000)
    current = version(prior.fact.model_copy(update=change), prior.subject_id, revision=2)
    profile = sources[0]
    if "employee_id" in change:
        profile = replace(
            profile, fact=profile.fact.model_copy(update={"employee_id": change["employee_id"]})
        )
    plan = version(
        PayrollPlan(
            period=current.fact.period,
            payroll_revision=FactRevisionReference(subject_id=current.subject_id, revision=2),
        ),
        "revised-plan",
    )
    facts = (profile, sources[1], plan, prior, old_tax)
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context(current, facts))
    assert error.value.issues[0]["field"] == "remuneration_tax_basis.source_fact_id"
    confirmed = tax_basis(current, 90_000, revision=2)
    result = calculate_payroll(
        current, context(current, (profile, sources[1], plan, prior, confirmed))
    )
    assert result.values["tax_fen"] == 90_000


def test_new_policy_revision_can_reuse_individual_tax_when_person_and_gross_are_unchanged():
    prior, sources = wage_case()
    old_tax = tax_basis(prior, 90_000)
    updated_profile = version(sources[0].fact, "profile", revision=2)
    current = version(
        prior.fact.model_copy(
            update={
                "profile_revision": FactRevisionReference(subject_id="profile", revision=2),
            }
        ),
        prior.subject_id,
        revision=2,
    )
    plan = version(
        PayrollPlan(
            period=current.fact.period,
            payroll_revision=FactRevisionReference(subject_id=current.subject_id, revision=2),
        ),
        "revised-plan",
    )
    result = calculate_payroll(
        current, context(current, (updated_profile, sources[1], plan, prior, old_tax))
    )
    assert result.values["tax_fen"] == 90_000
    assert result.values["tax_basis"]["source_fact_id"] == prior.id


def test_archived_wage_grammar_cannot_be_recalculated_by_current_evaluator():
    from ai_accounting.kernel.payroll_history import HistoricalPayroll

    current, _ = wage_case()
    old = version(
        HistoricalPayroll(
            **{
                **current.fact.model_dump(),
                "income_tax_policy_revision": None,
                "tax_reported_salary_fen": 4_000_000,
                "tax_exempt_income_fen": 0,
                "special_additional_deduction_fen": 0,
                "other_legal_deduction_fen": 0,
                "tax_relief_fen": 0,
            }
        ),
        current.subject_id,
    )
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(old, Context({}))
    assert error.value.issues[0]["field"] == "remuneration_revision"
