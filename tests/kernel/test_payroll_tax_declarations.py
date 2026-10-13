"""Declared tax changes only approved bank instructions, never invents withholding."""

import json

import pytest
from test_exports import evidence, inventory, queue, template_bytes
from test_payroll import payroll
from test_tax_import import company as tax_company
from test_workflow import obligation

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.exports import Exports, run_export_jobs
from ai_accounting.kernel.payroll_tax_declarations import (
    PayrollDisbursementBasis,
    PayrollTaxDeclarationActual,
)
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.remuneration_tax_basis import (
    PooledTaxAllocation,
    PooledTaxProof,
    RemunerationTaxBasis,
)
from ai_accounting.kernel.workflow import Workflow


@pytest.fixture
def setup(tmp_path):
    company = tax_company(tmp_path)
    export = Exports(company.engine)
    export.save_payee(
        "employee",
        name="张三",
        account="001234567890",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    template = evidence(company, template_bytes())
    inventory(company)
    return company, export, template


def declare(company, *, extra=60000, subject="declared", **changes):
    tax = company.current("january").values["tax_fen"]
    fact = PayrollTaxDeclarationActual(
        **{
            "period": "2026-01",
            "employee_id": "employee",
            "tax_period": "2026-01",
            "income_category": "wages",
            "declared_tax_fen": tax + extra,
            "declaration_confirmed": True,
            "declaration_date": None,
            **changes,
        }
    )
    saved = company.save(fact, subject)
    return fact, saved


def adopt(company, declaration, *, subject="basis", publish=True, **changes):
    fact = PayrollDisbursementBasis(
        **{
            "period": "2026-01",
            "payroll_kind": "payroll",
            "payroll_id": "january",
            "employee_id": "employee",
            "tax_period": "2026-01",
            "declaration_id": declaration["subject_id"],
            "declaration_fact_id": declaration["fact_id"],
            "use_declared_tax_for_disbursement": True,
            **changes,
        }
    )
    saved = company.save(fact, subject)
    if publish:
        company.publish(subject)
    return fact, saved


def pay(company, amount, subject="payment"):
    company.save(
        Payment(
            period="2026-02",
            actual_date="2026-02-10",
            direction="outflow",
            bank_account_id="bank",
            counterparty_id="employee",
            amount_fen=amount,
            allocations=(
                Allocation(
                    source_kind="payroll",
                    source_id="january",
                    obligation="net",
                    amount_fen=amount,
                ),
            ),
        ),
        subject,
    )
    company.publish(subject)


def preview(export, template, source_ids=None):
    return export.preview("2026-01", template_evidence_digest=template, source_ids=source_ids)


def test_declared_difference_preserves_wages_and_leaves_600_payable(setup, tmp_path):
    company, export, template = setup
    original = company.current("january")
    ledger = company.engine.ledger("2026-01")
    _, declaration = declare(company)
    _, basis = adopt(company, declaration)
    plan, job, _ = queue(company, export, template, tmp_path / "out")
    assert plan["total_fen"] == original.values["net_fen"] - 60000
    record = plan["payroll_disbursements"][0]
    assert record["declaration_fact_id"] == declaration["fact_id"]
    assert record["basis_fact_id"] == basis["fact_id"]
    assert (
        record["basis_calculation_id"] == company.current("basis", "payroll_disbursement_basis").id
    )
    assert record["held_fen"] == 60000 and record["withholding_recorded"] is False
    assert record["adopted_tax_fen"] == original.values["tax_fen"]
    assert "calculated_tax_fen" not in record
    assert company.engine.ledger("2026-01") == ledger
    assert company.current("january") == original
    assert run_export_jobs(company.engine)[0]["job_id"] == job["job_id"]
    assert company.count("voucher_version") == 1
    pay(company, plan["total_fen"])
    with company.engine.store.connection(read_only=True) as connection:
        remaining = connection.execute(
            "SELECT amount FROM balance WHERE balance_key='payroll:january:net'"
        ).fetchone()[0]
        tax_remaining = connection.execute(
            "SELECT amount FROM balance WHERE balance_key='payroll:january:tax'"
        ).fetchone()[0]
    assert remaining == 60000 and tax_remaining == original.values["tax_fen"]
    assert company.current("january") == original
    with pytest.raises(KernelError, match="没有可代发"):
        preview(export, template)


@pytest.mark.parametrize("selected", [False, True])
def test_conflicting_declaration_requires_own_adoption_for_complete_and_selected(setup, selected):
    company, export, template = setup
    _, declaration = declare(company)
    sources = ["january"] if selected else None
    with pytest.raises(NeedsInformation) as error:
        preview(export, template, sources)
    assert error.value.issues[0]["field"] == "disbursement_basis"
    adopt(company, declaration)
    assert preview(export, template, sources)["total_fen"] == 847400


def test_equal_declared_tax_keeps_existing_net_and_freezes_fact(setup):
    company, export, template = setup
    _, declaration = declare(company, extra=0)
    plan = preview(export, template)
    assert plan["total_fen"] == 907400
    assert plan["payroll_disbursements"][0]["declaration_fact_id"] == declaration["fact_id"]
    assert plan["payroll_disbursements"][0]["withholding_recorded"] is False


def test_unrelated_stale_tax_source_does_not_block_selected_person(setup):
    company, export, template = setup
    from test_payroll import profile

    company.save(profile(employee_id="other"), "other-profile")
    other = payroll(employee_id="other", profile_id="other-profile")
    company.save(other, "b-wage")
    company.tax_basis("b-wage", 12600)
    company.save(other.model_copy(update={"accounting_gross_salary_fen": 2000000}), "b-wage", 1)
    inventory(company)
    _, declaration = declare(company, extra=0)
    from ai_accounting.kernel.payroll_tax_declarations import export_disbursement

    adopted = company.current("january")
    selected = {
        "kind": adopted.kind,
        "subject_id": adopted.subject_id,
        "period": adopted.period.ordinal,
        "id": adopted.id,
        "fact_id": adopted.fact_id,
    }
    with company.engine.store.connection(read_only=True) as connection:
        amount, details = export_disbursement(
            company.engine.store,
            connection,
            selected,
            adopted.values,
            907400,
        )
    assert amount == 907400 and details["declared_tax_fen"] == 12600
    adopt(company, declaration)
    with company.engine.store.connection(read_only=True) as connection:
        amount, details = export_disbursement(
            company.engine.store,
            connection,
            selected,
            adopted.values,
            907400,
        )
    assert amount == 907400 and details["declared_tax_fen"] == 12600


@pytest.mark.parametrize(("expired_sources", "reassigned"), [(0, False), (65, False), (1, True)])
def test_large_tax_period_cohort_keeps_bounded_reads_and_explicit_history_limit(
    expired_sources,
    reassigned,
):
    from ai_accounting.kernel.contracts import Context, FactVersion, Read
    from ai_accounting.kernel.dependencies import select_fact_dependencies
    from ai_accounting.kernel.payroll_tax_declarations import _declared_for_source
    from ai_accounting.kernel.remuneration_tax_basis import remuneration_tax_period_scope
    from ai_accounting.kernel.types import YearMonth

    month, proof = YearMonth("2026-01"), "0" * 64
    current, taxes = [], []
    for index in range(101):
        source = FactVersion(
            f"source-{index}",
            f"wage-{index}",
            1,
            payroll(employee_id="employee-0" if reassigned and index == 1 else f"employee-{index}"),
            (proof,),
        )
        current.append(
            FactVersion(
                f"current-{index}",
                source.subject_id,
                2,
                source.fact.model_copy(update={"employee_id": "reassigned-person"})
                if reassigned
                else source.fact,
                (proof,),
            )
            if (index == 1 if reassigned else index < expired_sources)
            else source
        )
        if reassigned and index == 1:
            reassigned_original = source
        taxes.append(
            FactVersion(
                f"tax-{index}",
                f"basis-{index}",
                1,
                RemunerationTaxBasis(
                    period=month,
                    source_kind="payroll",
                    source_id=source.subject_id,
                    source_fact_id=source.id,
                    tax_period=month,
                    tax_amount_fen=12600,
                    basis_type="final_statement",
                    confirmed=True,
                ),
                (proof,),
            )
        )
    plan = PayrollDisbursementBasis(
        period=month,
        payroll_kind="payroll",
        payroll_id="wage-0",
        employee_id="employee-0",
        tax_period=month,
        declaration_id="declared",
        declaration_fact_id="declaration-version",
        use_declared_tax_for_disbursement=True,
    )
    selected = {read: () for read in plan.reads()}
    selected[Read("fact", RemunerationTaxBasis.kind, remuneration_tax_period_scope(month))] = tuple(
        taxes
    )
    selected[Read("fact", "payroll", str(month))] = tuple(current)
    selected[Read("calculation", "payroll", str(month))] = ()
    if reassigned:
        selected[Read("fact", "payroll", "#" + reassigned_original.id)] = (reassigned_original,)
    widths = []

    def select_many(reads):
        widths.append(len(reads))
        return {read: selected[read] for read in reads}

    def declared_reads(version, selections=None):
        return (
            version.fact.reads()
            if selections is None
            else (
                *version.fact.reads(),
                *version.fact.reads_after(version.subject_id, Context(selections)),
            )
        )

    version = FactVersion("plan-version", "plan", 1, plan, (proof,))
    if expired_sources > 64:
        with pytest.raises(NeedsInformation) as error:
            select_fact_dependencies((version,), select_many, declared_reads)
        assert error.value.issues[0]["field"] == "disbursement_tax_allocation"
    else:
        context = Context(select_fact_dependencies((version,), select_many, declared_reads))
        declaration = FactVersion(
            "declaration-version",
            "declared",
            1,
            PayrollTaxDeclarationActual(
                period=month,
                employee_id="employee-0",
                tax_period=month,
                income_category="wages",
                declared_tax_fen=12600,
                declaration_confirmed=True,
            ),
            (proof,),
        )
        if reassigned:
            with pytest.raises(NeedsInformation) as error:
                _declared_for_source(
                    context,
                    declaration,
                    "employee-0",
                    month,
                    "payroll",
                    "wage-0",
                    {"tax_basis": {"fact_id": taxes[0].id}},
                )
            assert error.value.issues[0]["field"] == "disbursement_tax_allocation"
            assert "wage-1" in error.value.issues[0]["reusable_sources"]
        else:
            assert (
                _declared_for_source(
                    context,
                    declaration,
                    "employee-0",
                    month,
                    "payroll",
                    "wage-0",
                    {"tax_basis": {"fact_id": taxes[0].id}},
                )
                == 12600
            )
    assert len(widths) == (3 if reassigned else 2) and max(widths) < 64


def test_equal_actual_withholding_is_recorded_without_theoretical_fallback(setup):
    company, export, template = setup
    company.save(
        RemunerationTaxBasis(
            period="2026-01",
            source_kind="payroll",
            source_id="january",
            source_fact_id=company.current("january").fact_id,
            tax_period="2026-01",
            tax_amount_fen=12_600,
            basis_type="actual_withholding",
            confirmed=True,
        ),
        "tax-basis",
        revision=1,
    )
    company.publish("january")
    _, declaration = declare(company, extra=0)
    plan = preview(export, template)
    assert plan["total_fen"] == 907400
    assert plan["payroll_disbursements"][0]["withholding_recorded"] is True


def test_declared_tax_uses_adopted_tax_period_instead_of_wage_month(setup):
    company, export, template = setup
    company.save(
        RemunerationTaxBasis(
            period="2026-01",
            source_kind="payroll",
            source_id="january",
            source_fact_id=company.current("january").fact_id,
            tax_period="2026-02",
            tax_amount_fen=12_600,
            basis_type="final_statement",
            confirmed=True,
        ),
        "tax-basis",
        revision=1,
    )
    company.publish("january")
    _, declaration = declare(company, period="2026-02", tax_period="2026-02")
    adopt(company, declaration, period="2026-02", tax_period="2026-02")
    plan = preview(export, template)
    assert plan["total_fen"] == 847_400
    assert plan["payroll_disbursements"][0]["tax_period"] == "2026-02"
    assert company.current("january").period == "2026-01"


@pytest.mark.parametrize("allocated", [False, True])
def test_tax_period_summary_cannot_be_duplicated_across_wage_sources(setup, allocated):
    company, export, template = setup
    february = company.save(payroll(period="2026-02"), "february")
    company.confirm_payroll("february")
    proof = (
        PooledTaxProof(
            evidence_digest=company.owner_confirmation,
            source_location="合成申报第1行",
            total_tax_fen=25_200,
            allocations=(
                PooledTaxAllocation(
                    source_kind="payroll",
                    source_id="january",
                    period="2026-01",
                    tax_amount_fen=12_600,
                ),
                PooledTaxAllocation(
                    source_kind="payroll",
                    source_id="february",
                    period="2026-02",
                    tax_amount_fen=12_600,
                ),
            ),
        )
        if allocated
        else None
    )
    for subject, source_fact, source_period, basis_id, revision in (
        ("january", company.current("january").fact_id, "2026-01", "tax-basis", 1),
        ("february", february["fact_id"], "2026-02", "february-tax", 0),
    ):
        basis = RemunerationTaxBasis(
            period=source_period,
            source_kind="payroll",
            source_id=subject,
            source_fact_id=source_fact,
            tax_period="2026-02",
            tax_amount_fen=12_600,
            basis_type="final_statement",
            confirmed=True,
            pooled_tax_proof=proof,
        )
        company.engine.save_fact(
            basis.kind,
            basis_id,
            basis.model_dump(mode="json"),
            evidence=(company.owner_confirmation,),
            expected_revision=revision,
            request_id=company.request(),
        )
    company.publish("january", "february")
    declared = PayrollTaxDeclarationActual(
        period="2026-02",
        employee_id="employee",
        tax_period="2026-02",
        income_category="wages",
        declared_tax_fen=25_200,
        declaration_confirmed=True,
    )
    declaration = company.engine.save_fact(
        declared.kind,
        "declared",
        declared.model_dump(mode="json"),
        evidence=(company.owner_confirmation,),
        expected_revision=0,
        request_id=company.request(),
    )
    inventory(company)
    if not allocated:
        with pytest.raises(NeedsInformation) as exc:
            preview(export, template, ["january"])
        assert exc.value.issues[0]["field"] == "disbursement_tax_allocation"
        adopt(company, declaration, publish=False, period="2026-02", tax_period="2026-02")
        with pytest.raises(NeedsInformation) as exc:
            company.engine.preview(["basis"])
        assert exc.value.issues[0]["field"] == "disbursement_tax_allocation"
    else:
        plan = preview(export, template, ["january"])
        assert plan["total_fen"] == 907_400
        assert plan["payroll_disbursements"][0]["declared_tax_fen"] == 12_600
        assert plan["payroll_disbursements"][0]["declared_period_total_fen"] == 25_200


@pytest.mark.parametrize("change", ["unknown", "wrong_person", "wrong_month", "wrong_version"])
def test_adoption_requires_exact_unique_matching_declaration(setup, change):
    company, export, template = setup
    _, declaration = declare(company)
    options = {
        "unknown": {"declaration_id": "unknown"},
        "wrong_person": {"employee_id": "other"},
        "wrong_month": {"tax_period": "2026-02"},
        "wrong_version": {"declaration_fact_id": "old-version"},
    }[change]
    adopt(company, declaration, publish=False, **options)
    with pytest.raises(KernelError):
        company.engine.preview(["basis"])
    with pytest.raises(NeedsInformation):
        preview(export, template)


@pytest.mark.parametrize("extra", [-1, 1000000])
def test_target_above_payable_or_negative_needs_explicit_disposition(setup, extra):
    company, _, _ = setup
    _, declaration = declare(company, extra=extra)
    adopt(company, declaration, publish=False)
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["basis"])
    assert error.value.issues[0]["field"] == "disbursement_difference"


@pytest.mark.parametrize("duplicate", ["declaration", "basis"])
def test_duplicate_active_sources_never_choose_one_by_amount_or_order(setup, duplicate):
    company, export, template = setup
    _, declaration = declare(company)
    adopt(company, declaration)
    if duplicate == "declaration":
        declare(company, subject="duplicate")
    else:
        adopt(company, declaration, subject="duplicate", publish=False)
    with pytest.raises(KernelError) as error:
        preview(export, template)
    assert error.value.code.startswith("ambiguous_")


def test_partial_actual_payment_reduces_target_without_clearing_holdback(setup):
    company, export, template = setup
    _, declaration = declare(company)
    adopt(company, declaration)
    pay(company, 400000)
    plan = preview(export, template)
    assert plan["total_fen"] == 447400
    detail = plan["payroll_disbursements"][0]
    assert detail["remaining_payable_fen"] == 507400
    assert detail["settled_fen"] == 400000 and detail["held_fen"] == 60000


@pytest.mark.parametrize("paid", [880000, 907400])
def test_actual_already_paid_above_target_is_not_rewritten(setup, paid):
    company, export, template = setup
    pay(company, paid)
    payment = company.current("payment", "payment")
    _, declaration = declare(company)
    adopt(company, declaration)
    with pytest.raises(NeedsInformation) as error:
        preview(export, template)
    assert error.value.issues[0]["field"] == "disbursement_difference"
    assert company.current("payment", "payment") == payment


def test_unrelated_pending_plan_does_not_block_selected_wage_or_close(setup):
    company, export, template = setup
    _, declaration = declare(company, employee_id="other")
    adopt(company, declaration, publish=False, payroll_id="other-wage", employee_id="other")
    assert "basis" in company.pending()
    assert preview(export, template, ["january"])["total_fen"] == 907400
    company.close("2026-01")


def test_unfiled_labor_does_not_block_approved_wage_disbursement(setup):
    company, export, template = setup
    _, declaration = declare(company)
    adopt(company, declaration)
    company.save(
        LaborAccrual(
            period="2026-01",
            person_id="unrelated-contractor",
            expense_class="service",
            gross_fee_fen=100000,
            tax_treatment="not_withheld_not_filed",
        ),
        "unfiled-labor",
    )
    company.publish("unfiled-labor")
    inventory(company)
    plan = preview(export, template, ["january"])
    assert plan["total_fen"] == 847400
    assert company.current("unfiled-labor", "labor_accrual").values["tax_review_required"]


def test_unchosen_conflicting_wage_does_not_block_another_person(setup):
    company, export, template = setup
    from test_payroll import profile

    company.save(profile(employee_id="other"), "other-profile")
    company.save(payroll(employee_id="other", profile_id="other-profile"), "other-wage")
    company.confirm_payroll("other-wage")
    company.tax_basis("other-wage", 12_600)
    company.publish("other-wage")
    inventory(company)
    _, declaration = declare(company, employee_id="other")
    adopt(company, declaration, payroll_id="other-wage", employee_id="other", publish=False)
    assert preview(export, template, ["january"])["total_fen"] == 907400
    with pytest.raises(NeedsInformation):
        preview(export, template)


def test_pending_changed_plan_blocks_only_its_own_source_and_not_quarter_basis(setup):
    company, export, template = setup
    _, declaration = declare(company)
    adopt(company, declaration)
    company.save(obligation("quarterly_financial_report"), "quarter")
    company.close("2026-01")
    accepted = Workflow(company.engine).obligation_basis("quarter")["candidate_calculations"]
    accepted_subjects = {item["subject_id"] for item in accepted}
    assert "january" in accepted_subjects and "basis" not in accepted_subjects
    actual, _ = declare(company, subject="other-declared", employee_id="other")
    # A metadata-only declaration revision must not rewrite closed accounting.
    closed = company.engine.ledger("2026-01")
    company.engine.amend_fact(
        actual.kind,
        "declared",
        actual.model_copy(update={"employee_id": "employee"}).model_dump(mode="json"),
        evidence=(company.owner_confirmation,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    assert "basis" in company.pending()
    with pytest.raises(NeedsInformation):
        preview(export, template)
    assert company.engine.ledger("2026-01") == closed


def test_real_payroll_pending_still_blocks_selected_export_and_close(setup):
    company, export, template = setup
    company.save(payroll(accounting_gross_salary_fen=1100000), "january", revision=1)
    company.confirm_payroll("january")
    inventory(company)
    with pytest.raises(KernelError):
        preview(export, template, ["january"])
    with pytest.raises(KernelError):
        company.close("2026-01")


@pytest.mark.parametrize("mutation", ["declaration", "basis"])
def test_any_adopted_fact_revision_expires_export_and_keeps_frozen_job(setup, tmp_path, mutation):
    company, export, template = setup
    actual, declaration = declare(company)
    basis, _ = adopt(company, declaration)
    plan, job, options = queue(company, export, template, tmp_path / "out")
    options["request_id"] = company.request()
    if mutation == "declaration":
        company.engine.amend_fact(
            actual.kind,
            "declared",
            actual.model_dump(mode="json"),
            evidence=(company.owner_confirmation,),
            expected_revision=1,
            recording_error_confirmed=True,
            request_id=company.request(),
        )
    else:
        company.save(basis, "basis", revision=1)
    with pytest.raises(KernelError):
        export.confirm("2026-01", **options)
    with company.engine.store.connection(read_only=True) as connection:
        frozen = json.loads(
            connection.execute("SELECT payload FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[
                0
            ]
        )
    assert frozen["plan"] == plan
    assert run_export_jobs(company.engine)[0]["status"] == "succeeded"


def test_unknown_declared_amount_is_not_saved_as_zero_or_current_calculation(setup):
    company, _, _ = setup
    data = {
        "period": "2026-01",
        "employee_id": "employee",
        "tax_period": "2026-01",
        "income_category": "wages",
        "declaration_confirmed": True,
    }
    before = company.count("fact_revision")
    with pytest.raises(NeedsInformation) as error:
        company.engine.save_fact(
            PayrollTaxDeclarationActual.kind,
            "unknown",
            data,
            evidence=(company.owner_confirmation,),
            expected_revision=0,
            request_id=company.request(),
        )
    assert error.value.issues[0]["field"] == "declared_tax_fen"
    assert company.count("fact_revision") == before


def test_adopted_disbursement_selected_scope_keeps_complete_month_gate(setup):
    company, export, template = setup
    _, declaration = declare(company)
    adopt(company, declaration)
    Periods(company.engine).inventory(
        "2026-01",
        "bank",
        evidence=[],
        expected=1,
        no_business=False,
        confirmation_evidence=company.owner_confirmation,
        request_id=company.request(),
    )
    assert preview(export, template, ["january"])["scope"] == "selected"
    with pytest.raises(KernelError) as error:
        preview(export, template)
    assert error.value.code == "materials_incomplete"


def test_actual_declaration_cannot_be_replaced_as_ordinary_edit(setup):
    company, _, _ = setup
    fact, saved = declare(company)
    with pytest.raises(KernelError) as error:
        company.save(fact.model_copy(update={"declared_tax_fen": 1}), "declared", revision=1)
    assert error.value.code == "immutable_fact"
    with company.engine.store.connection(read_only=True) as connection:
        current = company.engine.store.current_fact(connection, "declared")
    assert current.id == saved["fact_id"] and current.fact.declaration_date is None
