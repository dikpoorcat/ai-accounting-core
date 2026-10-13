"""Selected bank payments validate their exact sources independently of month close."""

from __future__ import annotations

import pytest
from test_exports import queue
from test_exports import setup as setup  # noqa: F401
from test_exports_payroll_scope import assert_incomplete, receive, resolve, selected

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.adjustments import EmployeeAdvance, PassThroughReturn
from ai_accounting.kernel.domains.transactions import (
    Advance,
    Allocation,
    Expense,
    Overpayment,
    PassThrough,
    Payment,
    Settlement,
)
from ai_accounting.kernel.exports import run_export_jobs
from ai_accounting.kernel.periods import Periods


def reimbursement(company, *, subject="reimbursement", period="2026-01", amount=12000):
    company.save(Expense(
        period=period, counterparty_id="employee", amount_fen=amount,
        expense_class="administration", creditor_kind="employee",
    ), subject)
    company.publish(subject)
    return Allocation(source_kind="expense", source_id=subject, obligation="primary",
                      amount_fen=amount)


def test_selected_reimbursement_delivers_without_unrelated_month_or_tax_work(setup, tmp_path):
    company, export, template = setup
    reimbursement(company)
    company.save(Expense(
        period="2026-01", counterparty_id="supplier", amount_fen=500,
        expense_class="administration", creditor_kind="supplier",
    ), "unrelated-unpublished")
    Periods(company.engine).inventory(
        "2026-01", "tax", evidence=[], expected=1, no_business=False,
        confirmation_evidence=company.owner_confirmation, request_id=company.request(),
    )
    preview, queued, _ = queue(
        company, export, template, tmp_path / "selected-reimbursement",
        source_ids=["reimbursement"],
    )
    assert preview["total_fen"] == 12000
    assert preview["rows"][0]["category"] == "报销"
    assert all(value == {} for value in preview["inventories"].values())
    result = run_export_jobs(company.engine)[0]
    assert result["status"] == "succeeded" and result["job_id"] == queued["job_id"]
    with pytest.raises(KernelError) as complete:
        export.preview("2026-01", template_evidence_digest=template)
    assert complete.value.code == "materials_incomplete"


def test_pure_reimbursement_does_not_pull_in_unrelated_salary_original_or_inventory(setup):
    company, export, template = setup
    reimbursement(company)
    receive(company, "unrelated-salary", b"name,amount,period\nwage,10.00,2026-01\n")
    originals = sorted({proof for month, proof in company.materials["payroll"]
                        if month == "2026-01"})
    Periods(company.engine).inventory(
        "2026-01", "payroll", evidence=originals, expected=len(originals) + 1, no_business=False,
        confirmation_evidence=company.owner_confirmation, request_id=company.request(),
    )
    assert selected(export, template, ["reimbursement"])["total_fen"] == 12000
    assert_incomplete(export, template, sources=["january"])


def test_mixed_payment_scope_does_not_expand_salary_originals_to_reimbursement_month(setup):
    company, export, template = setup
    reimbursement(company, period="2026-02")
    Periods(company.engine).management(
        "reimbursement", payment_period="2026-01", payment_category=None, expected_revision=0,
        request_id=company.request(),
    )
    receive(company, "unrelated-february-salary",
            b"name,amount,period\nwage,10.00,2026-02\n", period="2026-02")
    preview = selected(export, template, ["january", "reimbursement"])
    assert preview["total_fen"] == 919400
    assert {item["subject_id"] for row in preview["rows"] for item in row["sources"]} == {
        "january", "reimbursement",
    }


def test_related_reimbursement_original_keeps_full_shared_file_validation(setup):
    company, export, template = setup
    reimbursement(company)
    source, proof = receive(
        company, "reimbursement-original",
        b"name,amount,period\nreimbursement,120.00,2026-01\nunresolved,1.00,2026-01\n",
        category="transactions",
    )
    resolve(company, source, proof, subject="reimbursement", amount=12000,
            field="fact.amount_fen")
    issues = assert_incomplete(export, template, sources=["reimbursement"],
                               code="material_item_unresolved")
    assert any(issue.get("source_id") == source["subject_id"]
               and issue.get("location") == "CSV!B3" for issue in issues)


@pytest.mark.parametrize("handler", ["payment", "advance", "offset", "overpayment"])
def test_unpublished_exact_obligation_handler_blocks_selected_reimbursement(setup, handler):
    company, export, template = setup
    allocation = reimbursement(company).model_copy(update={"amount_fen": 1000})
    if handler == "payment":
        fact = Payment(
            period="2026-02", actual_date="2026-02-10", direction="outflow",
            bank_account_id="bank", counterparty_id="employee", amount_fen=1000,
            allocations=(allocation,),
        )
    elif handler == "advance":
        fact = EmployeeAdvance(
            period="2026-02", payer_id="owner", payer_kind="owner",
            payment_on_behalf_confirmed=True, actual_creditor_payment_date="2026-02-10",
            sources=(allocation,),
        )
    elif handler == "offset":
        company.save(Advance(
            period="2026-01", counterparty_id="employee", amount_fen=1000,
            side="supplier", contractual_obligation_established=True,
        ), "reimbursement-advance")
        company.publish("reimbursement-advance")
        fact = Settlement(
            period="2026-02", settlement_kind="advance_application",
            first=Allocation(source_kind="advance", source_id="reimbursement-advance",
                             obligation="advance", amount_fen=1000),
            second=allocation, offset_right_confirmed=True,
        )
    else:
        fact = Overpayment(
            period="2026-02", source_kind="expense", source_id="reimbursement",
            obligation_name="primary", counterparty_id="employee", amount_fen=1000,
            recovery_right_confirmed=True,
        )
    company.save(fact, "unpublished-handler")
    issues = assert_incomplete(export, template, sources=["reimbursement"])
    assert any(issue["field"] == "unpublished-handler" for issue in issues)


def test_partial_reimbursement_payment_keeps_amount_and_related_original_validation(setup):
    company, export, template = setup
    allocation = reimbursement(company).model_copy(update={"amount_fen": 1000})
    company.save(Payment(
        period="2026-02", actual_date="2026-02-10", direction="outflow",
        bank_account_id="bank", counterparty_id="employee", amount_fen=1000,
        allocations=(allocation,),
    ), "partial-payment")
    company.publish("partial-payment")
    assert selected(export, template, ["reimbursement"])["total_fen"] == 11000
    source, proof = receive(company, "payment-original",
                            b"name,amount,period\npayment,11.00,2026-02\n",
                            category="bank", period="2026-02")
    resolve(company, source, proof, subject="partial-payment", amount=1000,
            field="fact.amount_fen")
    assert_incomplete(export, template, sources=["reimbursement"], code="material_split_mismatch")


def test_unpublished_return_is_found_by_pass_through_remittance_obligation(setup):
    company, export, template = setup
    company.save(PassThrough(
        period="2026-01", payer_id="supplier", beneficiary_id="employee", amount_fen=12000,
        rights_and_obligation_confirmed=True,
    ), "pass-through")
    company.publish("pass-through")
    assert selected(export, template, ["pass-through"])["rows"][0]["category"] == "代付款"
    company.save(PassThroughReturn(
        period="2026-02", source_id="pass-through", amount_fen=1000,
        refund_right_confirmed=True,
    ), "unpublished-return")
    issues = assert_incomplete(export, template, sources=["pass-through"])
    assert any(issue["field"] == "unpublished-return" for issue in issues)


def test_personal_advance_keeps_upstream_unpublished_handler_in_payment_scope(setup):
    company, export, template = setup
    company.save(Expense(
        period="2026-01", counterparty_id="supplier", amount_fen=12000,
        expense_class="administration", creditor_kind="supplier",
    ), "supplier-expense")
    company.publish("supplier-expense")
    company.save(EmployeeAdvance(
        period="2026-01", payer_id="employee", payer_kind="employee",
        payment_on_behalf_confirmed=True, actual_creditor_payment_date="2026-01-10",
        sources=(Allocation(source_kind="expense", source_id="supplier-expense",
                            obligation="primary", amount_fen=1000),),
    ), "employee-advance")
    company.publish("employee-advance")
    company.save(Payment(
        period="2026-02", actual_date="2026-02-10", direction="outflow",
        bank_account_id="bank", counterparty_id="supplier", amount_fen=1000,
        allocations=(Allocation(source_kind="expense", source_id="supplier-expense",
                                obligation="primary", amount_fen=1000),),
    ), "unpublished-upstream-payment")
    issues = assert_incomplete(export, template, sources=["employee-advance"])
    assert any(issue["field"] == "unpublished-upstream-payment" for issue in issues)
