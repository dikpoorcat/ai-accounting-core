"""Company acceptance transfers paid liabilities without inventing a personal payment day."""

import json
from dataclasses import replace

import pytest
from test_banking import entry, funding, match, reconciliation, statement
from test_banking import opening as bank_opening
from test_deletion_boundaries import book as book
from test_exports import template_bytes
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.adjustments import ReimbursementAcceptance
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods


def wage(save, publish, *, kind="payroll"):
    policy = contribution_policy().model_dump(mode="json")
    policy["rules"].append(
        {
            **policy["rules"][0],
            "code": "housing",
            "base_kind": "housing_fund",
            "employee_rate": "0.05",
            "employer_rate": "0.05",
        }
    )
    for fact, subject in (
        (
            profile(
                effective_to="2026-01",
                housing_fund_participating=True,
                housing_fund_base_fen=1_000_000,
            ),
            "profile",
        ),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
    ):
        save(fact.kind, subject, fact.model_dump(mode="json"))
    save("payroll_contribution_policy", "contributions", policy)
    data = payroll().model_dump(mode="json")
    if kind == "payroll_bounded":
        data.update(accounting_gross_salary_fen=300_000, tax_reported_salary_fen=300_000)
        data.update(
            dict.fromkeys(
                (
                    "tax_exempt_income_fen",
                    "special_additional_deduction_fen",
                    "other_legal_deduction_fen",
                    "tax_relief_fen",
                )
            )
        )
    save(kind, "wage", data)
    publish("wage")


def source(name="employee_social", amount=20000, *, kind="payroll", subject="wage"):
    return {
        "source_kind": kind,
        "source_id": subject,
        "obligation": name,
        "amount_fen": amount,
        "recipient_id": "contribution-authority",
    }


def acceptance(**changes):
    return {
        "period": "2026-02",
        "payer_id": "payer",
        "payer_kind": "owner",
        "company_acceptance_confirmed": True,
        "original_debt_paid_confirmed": True,
        "sources": [source()],
        **changes,
    }


def current(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.outcome FROM calculation_current a JOIN calculation c "
            "ON c.id=a.calculation_id WHERE a.subject_id=?",
            (subject,),
        ).fetchone()
        return json.loads(row[0]) if row else None


def balance(engine, key):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT amount FROM balance WHERE balance_key=?", (key,)
        ).fetchone()
        return row[0] if row else 0


def payment(
    save, publish, *, subject="reimbursement", kind="reimbursement_acceptance", amount=20000
):
    save(
        "payment",
        subject,
        {
            "period": "2026-02",
            "actual_date": "2026-02-07",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "payer"
            if kind == "reimbursement_acceptance"
            else "contribution-authority",
            "amount_fen": amount,
            "allocations": [
                {
                    "source_kind": kind,
                    "source_id": "accepted" if kind == "reimbursement_acceptance" else "wage",
                    "obligation": "primary"
                    if kind == "reimbursement_acceptance"
                    else "employee_social",
                    "amount_fen": amount,
                }
            ],
        },
    )
    publish(subject)


@pytest.mark.parametrize("kind", ("payroll", "payroll_bounded"))
def test_closed_wage_four_paid_contributions_transfer_without_date_expense_or_cash(book, kind):
    engine, save, publish, close, _, _ = book
    wage(save, publish, kind=kind)
    close("2026-01")
    frozen = Periods(engine).closed_report("2026-01")
    parts = [
        source(name, amount, kind=kind)
        for name, amount in (
            ("employee_social", 20000),
            ("employer_social", 50000),
            ("employee_housing", 5000),
            ("employer_housing", 5000),
        )
    ]
    save("reimbursement_acceptance", "accepted", acceptance(sources=parts))
    publish("accepted")
    result = current(engine, "accepted")
    assert {line["account"] for line in result["lines"]} == {
        "224102",
        "221102",
        "224103",
        "221103",
        "2241",
    }
    assert all(line["cashflow"] is None for line in result["lines"])
    assert balance(engine, "reimbursement_acceptance:accepted:primary") == 80000
    assert result["values"]["amount_fen"] == 80000
    assert len(result["values"]["accepted_sources"]) == 4
    assert all(
        item["timing_basis"] == "company_confirmation_month"
        for item in result["values"]["accepted_sources"]
    )
    assert not any("date" in key for key in result["values"])
    assert result["values"]["obligations"][0]["cashflow"] == "payroll"
    assert {item["cashflow"] for item in result["values"]["accepted_sources"]} == {"payroll"}
    assert engine.overview("2026-02")["cashflow"] == []
    payment(save, publish, amount=80000)
    paid = current(engine, "reimbursement")
    assert [line for line in paid["lines"] if line["cashflow"]] == [
        {"account": "1002", "debit": 0, "credit": 80000, "cashflow": "payroll"}
    ]
    assert balance(engine, "reimbursement_acceptance:accepted:primary") == 0
    assert balance(engine, "bank") == -80000
    engine.rebuild_projections(request_id="rebuild")
    assert balance(engine, "reimbursement_acceptance:accepted:primary") == 0
    assert balance(engine, f"{kind}:wage:employee_social") == 60000
    assert Periods(engine).closed_report("2026-01") == frozen


@pytest.mark.parametrize("changed_cashflow", (None, "operating"))
def test_acceptance_rejects_unknown_or_mixed_original_cashflow(book, changed_cashflow):
    engine, save, publish, *_ = book
    original = engine.store.registry.evaluators["payroll"]

    def changed_source(version, context):
        outcome = original(version, context)
        obligations = [
            item | {"cashflow": changed_cashflow} if item["name"] == "employee_housing" else item
            for item in outcome.values["obligations"]
        ]
        return replace(outcome, values=outcome.values | {"obligations": obligations})

    # An isolated future calculator output cannot silently change repayment classification.
    engine.store.registry.evaluators["payroll"] = changed_source
    wage(save, publish)
    save(
        "reimbursement_acceptance",
        "accepted",
        acceptance(sources=[source(), source("employee_housing", 5000)]),
    )
    with pytest.raises(KernelError) as error:
        publish("accepted")
    assert error.value.code == "reimbursement_cashflow_conflict"
    assert current(engine, "accepted") is None
    assert balance(engine, "payroll:wage:employee_social") == 80000


@pytest.mark.parametrize(
    "change,field",
    [
        ({"company_acceptance_confirmed": None}, "company_acceptance_confirmed"),
        ({"company_acceptance_confirmed": False}, "company_acceptance_confirmed"),
        ({"original_debt_paid_confirmed": None}, "original_debt_paid_confirmed"),
        ({"original_debt_paid_confirmed": False}, "original_debt_paid_confirmed"),
        ({"sources": [source() | {"recipient_id": None}]}, "sources.recipient_id"),
    ],
)
def test_missing_acceptance_or_paid_evidence_is_not_defaulted(book, change, field):
    engine, save, publish, *_ = book
    wage(save, publish)
    save("reimbursement_acceptance", "accepted", acceptance(**change))
    with pytest.raises(NeedsInformation) as error:
        publish("accepted")
    assert error.value.issues[0]["field"] == field
    assert current(engine, "accepted") is None


def test_public_acceptance_has_no_actual_payment_day_and_requires_retained_evidence(book):
    engine, save, publish, *_ = book
    wage(save, publish)
    schema = ReimbursementAcceptance.model_json_schema()
    assert "actual_creditor_payment_date" not in schema["properties"]
    assert "actual_date" not in schema["properties"]
    with pytest.raises(KernelError):
        save(
            "reimbursement_acceptance",
            "invented-day",
            acceptance(actual_creditor_payment_date="2026-02-07"),
        )
    with pytest.raises(NeedsInformation):
        engine.save_fact(
            "reimbursement_acceptance",
            "no-evidence",
            acceptance(),
            evidence=(),
            expected_revision=0,
            request_id="no-evidence",
        )


@pytest.mark.parametrize("name", ("net", "tax"))
def test_payroll_obligation_without_explicit_timing_authority_cannot_use_acceptance(book, name):
    engine, save, publish, *_ = book
    wage(save, publish)
    save("reimbursement_acceptance", "accepted", acceptance(sources=[source(name, 1)]))
    with pytest.raises(KernelError) as error:
        publish("accepted")
    assert error.value.code == "reimbursement_timing_not_authorized"
    assert current(engine, "accepted") is None


def test_expense_has_no_implicit_date_waiver_and_mixed_batch_fails_atomically(book):
    engine, save, publish, *_ = book
    wage(save, publish)
    save(
        "expense",
        "cost",
        {
            "period": "2026-01",
            "counterparty_id": "supplier",
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("cost")
    save(
        "reimbursement_acceptance",
        "accepted",
        acceptance(sources=[source(), source("primary", 100, kind="expense", subject="cost")]),
    )
    with pytest.raises(KernelError) as error:
        publish("accepted")
    assert error.value.code == "reimbursement_timing_not_authorized"
    assert balance(engine, "payroll:wage:employee_social") == 80000
    assert current(engine, "accepted") is None


def test_date_sensitive_loan_principal_cannot_use_recognition_month_acceptance(book):
    engine, save, publish, *_ = book
    save(
        "loan_agreement",
        "agreement",
        {
            "period": "2026-01",
            "lender_id": "lender",
            "lender_is_licensed": True,
            "currency": "CNY",
            "annual_rate_percent": "3.65",
            "day_count_basis": "actual_365",
            "maturity_date": "2026-12-31",
            "loan_term": "short_term",
        },
    )
    save(
        "loan_drawdown",
        "loan",
        {
            "period": "2026-01",
            "agreement_id": "agreement",
            "principal_fen": 100000,
            "actual_date": "2026-01-01",
            "bank_account_id": "bank",
        },
    )
    publish("loan")
    save(
        "reimbursement_acceptance",
        "accepted",
        acceptance(
            sources=[
                source("principal", 20000, kind="loan_drawdown", subject="loan")
                | {"recipient_id": "lender"}
            ]
        ),
    )
    with pytest.raises(KernelError) as error:
        publish("accepted")
    assert error.value.code == "reimbursement_timing_not_authorized"
    assert balance(engine, "loan_drawdown:loan:principal") == 100000
    assert current(engine, "accepted") is None


@pytest.mark.parametrize("other_kind", ("payment", "employee_advance", "reimbursement_acceptance"))
def test_same_claim_capacity_is_shared_with_actual_payment_and_personal_advance(book, other_kind):
    engine, save, publish, *_ = book
    wage(save, publish)
    if other_kind == "payment":
        payment(save, publish, subject="prior", kind="payroll", amount=70000)
    elif other_kind == "employee_advance":
        save(
            "employee_advance",
            "prior",
            {
                "period": "2026-02",
                "payer_id": "other",
                "payer_kind": "employee",
                "payment_on_behalf_confirmed": True,
                "actual_creditor_payment_date": "2026-02-01",
                "sources": [source(amount=70000)],
            },
        )
        publish("prior")
    else:
        save(
            "reimbursement_acceptance",
            "prior",
            acceptance(payer_id="other", sources=[source(amount=70000)]),
        )
        publish("prior")
    save("reimbursement_acceptance", "accepted", acceptance())
    with pytest.raises(KernelError) as error:
        publish("accepted")
    assert error.value.code == "overallocated_obligation"
    assert current(engine, "accepted") is None
    assert balance(engine, "payroll:wage:employee_social") == 10000


@pytest.mark.parametrize("mode", ("duplicate", "before-source"))
def test_duplicate_source_and_acceptance_before_source_month_are_rejected(book, mode):
    engine, save, publish, *_ = book
    wage(save, publish)
    changes = {"sources": [source(), source()]} if mode == "duplicate" else {"period": "2025-12"}
    save("reimbursement_acceptance", "accepted", acceptance(**changes))
    with pytest.raises(KernelError) as error:
        publish("accepted")
    assert error.value.code == (
        "duplicate_allocation" if mode == "duplicate" else "invalid_advance_source"
    )
    assert current(engine, "accepted") is None


def test_later_actual_payment_cannot_spend_the_already_accepted_source_again(book):
    engine, save, publish, *_ = book
    wage(save, publish)
    save("reimbursement_acceptance", "accepted", acceptance())
    publish("accepted")
    with pytest.raises(KernelError) as error:
        payment(save, publish, subject="double-pay", kind="payroll", amount=70000)
    assert error.value.code == "overallocated_obligation"
    assert current(engine, "double-pay") is None
    assert balance(engine, "payroll:wage:employee_social") == 60000


def test_accepted_claim_exports_only_remaining_reimbursement_and_checks_payee(book):
    engine, save, publish, close, _, proof = book
    wage(save, publish)
    save("reimbursement_acceptance", "accepted", acceptance())
    publish("accepted")
    bank_opening(save, publish, bank="bank", month="2026-02")
    funding(save, publish, bank="bank", day="2026-02-01", amount=10000)
    payment(save, publish, amount=5000)
    statement(
        save,
        publish,
        [entry("funding", "2026-02-01", 10000), entry("repaid", "2026-02-07", -5000)],
        bank="bank",
        month="2026-02",
    )
    reconciliation(
        save,
        publish,
        [match("funding"), match("repaid", "payment", "reimbursement")],
        bank="bank",
        month="2026-02",
    )
    close("2026-01")
    close("2026-02")
    template = engine.register_evidence(
        template_bytes(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "template",
        request_id="template",
    )["digest"]
    exports = Exports(engine)
    with pytest.raises(NeedsInformation, match="收款姓名和账号"):
        exports.preview("2026-02", template_evidence_digest=template, source_ids=["accepted"])
    exports.save_payee(
        "payer",
        name="测试负责人",
        account="0012345",
        evidence_digest=proof,
        expected_revision=0,
        request_id="payee",
    )
    plan = exports.preview("2026-02", template_evidence_digest=template, source_ids=["accepted"])
    assert plan["rows"][0]["amount_fen"] == 15000
    assert plan["rows"][0]["category"] == "报销"
    assert (
        plan["rows"][0]["sources"][0]["obligation"] == "reimbursement_acceptance:accepted:primary"
    )


def test_nonemployee_individual_reimbursement_exports_without_becoming_payroll(book):
    engine, save, publish, close, _, proof = book
    save(
        "expense",
        "individual-cost",
        {
            "period": "2026-01",
            "counterparty_id": "individual",
            "amount_fen": 120000,
            "expense_class": "administration",
            "creditor_kind": "individual",
        },
    )
    publish("individual-cost")
    close("2026-01")
    template = engine.register_evidence(
        template_bytes(), "application/octet-stream", "template", request_id="template"
    )["digest"]
    exports = Exports(engine)
    with pytest.raises(NeedsInformation, match="收款姓名和账号"):
        exports.preview("2026-01", template_evidence_digest=template)
    exports.save_payee(
        "individual",
        name="合成非员工个人",
        account="0012345",
        evidence_digest=proof,
        expected_revision=0,
        request_id="payee",
    )
    plan = exports.preview("2026-01", template_evidence_digest=template)
    assert plan["rows"][0]["amount_fen"] == 120000
    assert plan["rows"][0]["category"] == "报销"
    assert plan["rows"][0]["sources"][0]["kind"] == "expense"
    assert plan["rows"][0]["sources"][0]["obligation"] == "expense:individual-cost:primary"
    assert {line["account"] for line in current(engine, "individual-cost")["lines"]} == {
        "5602",
        "2241",
    }
