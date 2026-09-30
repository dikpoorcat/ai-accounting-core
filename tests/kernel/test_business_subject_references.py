"""Generated business identities remain usable through typed settlement sources."""

import pytest
from pydantic import TypeAdapter, ValidationError
from test_payroll import contribution_policy, income_tax_policy, profile
from test_payroll import opening as wage_opening
from test_payroll import payroll as wage_fact
from test_payroll_corrections import Company
from test_payroll_preparation import company, confirm_no_change

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains import assets, investments, opening, payroll, taxes, transactions
from ai_accounting.kernel.domains.banking import (
    BankEntry,
    BankOpening,
    BankReconciliation,
    BankStatement,
    Match,
)
from ai_accounting.kernel.domains.cash import CashPayment
from ai_accounting.kernel.domains.payroll_reserve_payment import (
    PayrollNetAllocation,
    PayrollReservePayment,
)
from ai_accounting.kernel.domains.transactions import (
    Allocation,
    Expense,
    ExpenseRecovery,
    Identifier,
    Overpayment,
    Payment,
    Settlement,
)
from ai_accounting.kernel.types import SubjectId


@pytest.fixture
def generated_wage(tmp_path):
    instance = company(tmp_path)
    service = confirm_no_change(instance)
    proposal = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    subject = saved["publish_subjects"][0]
    assert subject == "payroll:employee:2026-02"
    instance.publish(subject)
    return instance, subject, instance.current(subject)


def allocations(subject, wage):
    return tuple(
        Allocation(
            source_kind="payroll",
            source_id=subject,
            obligation=name,
            amount_fen=wage.values[key],
        )
        for name, key in (
            ("employee_social", "employee_contributions_fen"),
            ("employer_social", "employer_contributions_fen"),
        )
    )


def reconcile(instance, subject, kind, amount):
    instance.save(
        BankOpening(
            period="2026-02",
            bank_account_id="bank",
            opening_fen=0,
            basis="new_account",
        ),
        "bank:opening:2026-02",
    )
    instance.publish("bank:opening:2026-02")
    statement = "bank:statement:2026-02"
    instance.save(
        BankStatement(
            period="2026-02",
            bank_account_id="bank",
            opening_fen=0,
            closing_fen=-amount,
            entries=(BankEntry(reference="row-1", actual_date="2026-02-10", signed_fen=-amount),),
        ),
        statement,
    )
    instance.publish(statement)
    instance.save(
        BankReconciliation(
            period="2026-02",
            bank_account_id="bank",
            statement_id=statement,
            matches=(Match(reference="row-1", source_kind=kind, source_id=subject),),
        ),
        "bank:reconciliation:2026-02",
    )
    instance.publish("bank:reconciliation:2026-02")
    result = instance.current("bank:reconciliation:2026-02", "bank_reconciliation")
    assert result.values["matched_count"] == 1


@pytest.mark.parametrize("method", ["bank", "cash", "gross_batch"])
def test_prepared_published_payroll_settles_via_actual_funds(generated_wage, method):
    instance, wage_subject, wage = generated_wage
    if method == "gross_batch":
        amount = wage.values["gross_fen"]
        fact = PayrollReservePayment(
            period="2026-02",
            actual_date="2026-02-10",
            bank_account_id="bank",
            amount_fen=amount,
            reserve_expense_fen=amount - wage.values["net_fen"],
            return_period="2026-02",
            actual_return_date=None,
            return_confirmed=True,
            complete_group_confirmed=True,
            allocations=(
                PayrollNetAllocation(
                    source_kind="payroll",
                    source_id=wage_subject,
                    recipient_id="employee",
                    amount_fen=wage.values["net_fen"],
                ),
            ),
        )
    else:
        refs = allocations(wage_subject, wage)
        amount = sum(ref.amount_fen for ref in refs)
        data = dict(
            period="2026-02",
            actual_date="2026-02-10",
            direction="outflow",
            counterparty_id="authority",
            amount_fen=amount,
            allocations=refs,
        )
        fact = (
            Payment(**data, bank_account_id="bank")
            if method == "bank"
            else CashPayment(**data, cash_account_id="cash")
        )
    # Exercise the full 200-character engine identity limit, including ':';
    # bank matches must reuse it verbatim rather than introducing an alias.
    subject = "payment:" + "x" * 192
    assert len(subject) == 200
    instance.save(fact, subject)
    instance.publish(subject)
    result = instance.current(subject, fact.kind)
    assert result.values["amount_fen"] == amount
    assert all(item["source_calculation"] == wage.id for item in result.values["settlements"])
    if method != "cash":
        reconcile(instance, subject, fact.kind, amount)


def test_non_cash_offset_uses_generated_payroll_and_colon_source(generated_wage):
    instance, subject, _ = generated_wage
    instance.save(
        Expense(
            period="2026-02",
            counterparty_id="employee",
            amount_fen=100,
            expense_class="administration",
            creditor_kind="employee",
        ),
        "expense:original",
    )
    instance.publish("expense:original")
    instance.save(
        ExpenseRecovery(
            period="2026-02",
            source_expense_id="expense:original",
            counterparty_id="employee",
            amount_fen=100,
            recovery_right_confirmed=True,
        ),
        "expense:recovery",
    )
    instance.publish("expense:recovery")
    instance.save(
        Settlement(
            period="2026-02",
            settlement_kind="debt_offset",
            offset_right_confirmed=True,
            first=Allocation(
                source_kind="payroll", source_id=subject, obligation="net", amount_fen=100
            ),
            second=Allocation(
                source_kind="expense_recovery",
                source_id="expense:recovery",
                obligation="primary",
                amount_fen=100,
            ),
        ),
        "settlement:offset",
    )
    instance.publish("settlement:offset")
    assert instance.current("settlement:offset", "settlement").values["amount_fen"] == 100
    recovery = Overpayment(
        period="2026-02",
        source_kind="payroll",
        source_id=subject,
        obligation_name="net",
        counterparty_id="employee",
        amount_fen=1,
        recovery_right_confirmed=True,
    )
    instance.save(recovery, "overpayment:unsupported")
    # The reference is accepted, while absent actual overpayment still blocks.
    with pytest.raises(KernelError) as caught:
        instance.engine.preview(["overpayment:unsupported"])
    assert caught.value.code == "overpayment_difference_changed"


@pytest.mark.parametrize("bad", ["", "x" * 201, 7])
def test_malformed_source_registration_is_atomic(generated_wage, bad):
    instance, subject, wage = generated_wage
    refs = allocations(subject, wage)
    data = Payment(
        period="2026-02",
        actual_date="2026-02-10",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=sum(ref.amount_fen for ref in refs),
        allocations=refs,
    ).model_dump(mode="json")
    data["allocations"][0]["source_id"] = bad
    before = tuple(
        instance.count(table) for table in ("subject", "fact_revision", "calculation", "voucher")
    )
    epochs = instance.engine.preview([subject])["epochs"]
    with pytest.raises(KernelError):
        instance.engine.save_fact(
            "payment",
            "invalid-payment",
            data,
            evidence=(instance.owner_confirmation,),
            expected_revision=0,
            request_id=instance.request(),
        )
    assert (
        tuple(
            instance.count(table)
            for table in ("subject", "fact_revision", "calculation", "voucher")
        )
        == before
    )
    assert instance.engine.preview([subject])["epochs"] == epochs


def test_subject_wire_limit_and_business_identifier_are_distinct():
    source = "payroll:" + "x" * 184 + ":2026-02"
    assert len(source) == 200
    assert TypeAdapter(SubjectId).validate_python(source) == source
    with pytest.raises(ValidationError):
        TypeAdapter(Identifier).validate_python("entity:alias")


def test_maximum_generated_payroll_identity_can_be_paid_and_reconciled(tmp_path):
    employee = "entity_" + "x" * 177
    instance = Company(tmp_path / "maximum-generated-subject.sqlite")
    for fact, subject in (
        (profile(employee_id=employee), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (wage_opening(employee_id=employee), "opening"),
        (wage_fact(employee_id=employee), "january"),
    ):
        instance.save(fact, subject)
    instance.confirm_payroll("january")
    instance.publish("january")
    service = confirm_no_change(instance)
    proposal = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=proposal["digest"], request_id=instance.request()
    )
    subject = saved["publish_subjects"][0]
    assert subject == f"payroll:{employee}:2026-02" and len(subject) == 200
    instance.publish(subject)
    refs = allocations(subject, instance.current(subject))
    amount = sum(ref.amount_fen for ref in refs)
    fact = Payment(
        period="2026-02",
        actual_date="2026-02-10",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=amount,
        allocations=refs,
    )
    instance.save(fact, "payment:maximum-payroll")
    instance.publish("payment:maximum-payroll")
    reconcile(instance, "payment:maximum-payroll", "payment", amount)


@pytest.mark.parametrize(
    "model,field",
    [
        (Allocation, "source_id"),
        (Match, "source_id"),
        (BankReconciliation, "statement_id"),
        (Overpayment, "source_id"),
        (transactions.ProjectCostSource, "source_id"),
        (investments.RedemptionCost, "source_id"),
        (opening.Member, "subject_id"),
        (assets.ReimbursedAsset, "acceptance_id"),
        (assets.LoanInterest, "drawdown_id"),
        (payroll.AnnualBonus, "regular_payroll_id"),
        (taxes.TaxCreditReturn, "return_id"),
        (taxes.TaxCreditConfirmation, "original_assessment_id"),
    ],
)
def test_reference_fields_share_subject_length_without_entity_pattern(model, field):
    adapter = TypeAdapter(model.model_fields[field].rebuild_annotation())
    maximum = "source:" + "x" * 193
    assert adapter.validate_python(maximum) == maximum
    for malformed in ("", maximum + "x", 42):
        with pytest.raises(ValidationError):
            adapter.validate_python(malformed)
