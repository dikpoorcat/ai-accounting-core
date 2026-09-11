"""Declared tax changes only approved bank instructions, never invents withholding."""

import json

import pytest
from test_exports import inventory, queue
from test_exports import setup as export_fixture
from test_payroll import payroll
from test_workflow import obligation

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.exports import run_export_jobs
from ai_accounting.kernel.payroll_tax_declarations import (
    PayrollDisbursementBasis,
    PayrollTaxDeclarationActual,
)
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.workflow import Workflow

setup = export_fixture


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
    from test_payroll import opening, profile

    company.save(profile(employee_id="other"), "other-profile")
    company.save(opening(employee_id="other"), "other-opening")
    company.save(payroll(employee_id="other", profile_id="other-profile"), "other-wage")
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
    company.save(obligation("quarterly_tax_and_reports"), "quarter")
    company.close("2026-01")
    accepted = Workflow(company.engine).obligation_basis("quarter")["accepted_calculations"]
    assert [item["subject_id"] for item in accepted] == ["january"]
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


def test_adopted_disbursement_does_not_bypass_material_completeness(setup):
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
    with pytest.raises(KernelError) as error:
        preview(export, template, ["january"])
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
