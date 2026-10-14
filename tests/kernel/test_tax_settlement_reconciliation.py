"""Tax settlement comparisons use published accounting, exact dates and source identities."""

import pytest
from test_actual_tax_priority import (
    ASSESSMENT,
    income_actual,
    publish,
    vat_actual,
)
from test_actual_tax_priority import (
    income_company as income_company,
)
from test_actual_tax_priority import (
    quarter_company as quarter_company,
)
from test_tax_credits import Company

from ai_accounting.kernel.domains import adjustments, cash, taxes, transactions
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.tax_settlement_reconciliation import reconcile


def result(company):
    current = company.current("income_tax_assessment", "current-tax")
    with QueryReads.snapshot(company.engine) as reads:
        return reconcile(reads, reads.calculation(current.id))


def save_payment(company, *, day="2026-10-01", amount=1000):
    return company.save(
        transactions.Payment, "current-payment", period=day[:7], actual_date=day,
        bank_account_id="bank", counterparty_id="authority", amount_fen=amount,
        direction="outflow", allocations=[{
            "source_kind": "income_tax_assessment", "source_id": "current-tax",
            "obligation": "tax", "amount_fen": amount,
        }],
    )


def test_formal_prior_payment_and_current_assessment_reconcile_to_total_due(income_company):
    company = income_company
    income_actual(company)
    publish(company, "current-tax", "adopt-actual")
    reconciliation = result(company)
    assert reconciliation["status"] == "matched"
    assert reconciliation["book_due_fen"] == 133_358
    assert reconciliation["book_settled_fen"] == 9_629
    assert reconciliation["settlement_fact_ids"] == [
        company.current("payment", "prior-payment").fact_id,
    ]


def test_observed_but_unpublished_payment_cannot_prove_ledger_match(income_company):
    company = income_company
    income_actual(company)
    publish(company, "current-tax", "adopt-actual")
    saved = save_payment(company)
    reconciliation = result(company)
    assert reconciliation["status"] == "unestablished"
    assert reconciliation["book_due_fen"] is None
    assert saved["fact_id"] not in reconciliation["settlement_fact_ids"]


@pytest.mark.parametrize("channel", ["bank", "cash", "employee"])
def test_formal_payment_or_owner_advance_at_cutoff_changes_ledger_due(income_company, channel):
    company = income_company
    income_actual(company)
    publish(company, "current-tax", "adopt-actual")
    allocation = {
        "source_kind": "income_tax_assessment", "source_id": "current-tax",
        "obligation": "tax", "amount_fen": 1000,
    }
    if channel == "bank":
        saved = save_payment(company)
    elif channel == "cash":
        saved = company.save(
            cash.CashPayment, "current-payment", period="2026-10", actual_date="2026-10-01",
            cash_account_id="cash", counterparty_id="authority", amount_fen=1000,
            direction="outflow", allocations=[allocation],
        )
    else:
        saved = company.save(
            adjustments.EmployeeAdvance, "current-payment", period="2026-10", payer_id="owner",
            payer_kind="owner", payment_on_behalf_confirmed=True,
            actual_creditor_payment_date="2026-10-01", sources=[allocation],
        )
    publish(company, "current-payment", "publish-current-payment")
    reconciliation = result(company)
    assert reconciliation["status"] == "different"
    assert reconciliation["book_due_fen"] == 132_358
    assert reconciliation["book_settled_fen"] == 10_629
    assert saved["fact_id"] in reconciliation["settlement_fact_ids"]


def test_formal_later_payment_is_excluded_from_original_cutoff(income_company):
    company = income_company
    income_actual(company)
    publish(company, "current-tax", "adopt-actual")
    saved = save_payment(company, day="2026-10-08")
    publish(company, "current-payment", "publish-later-payment")
    reconciliation = result(company)
    assert reconciliation["status"] == "matched"
    assert reconciliation["book_due_fen"] == 133_358
    assert saved["fact_id"] not in reconciliation["settlement_fact_ids"]


def test_negative_actual_due_matches_tax_receivable_after_zero_cumulative_assessment(
    income_company,
):
    company = income_company
    income_actual(company, cumulative_assessed_fen=0, tax_due_fen=-9_629)
    publish(company, "current-tax", "adopt-zero")
    reconciliation = result(company)
    assert reconciliation["status"] == "matched"
    assert reconciliation["book_due_fen"] == -9_629


@pytest.mark.parametrize("cutoff", [None, "2026-08-31", "2026-09-15"])
def test_unknown_cutoff_or_unestablished_assessment_at_day_cannot_prove_match(
    income_company, cutoff,
):
    company = income_company
    income_actual(company, settlement_as_of=cutoff)
    publish(company, "current-tax", "adopt-actual")
    reconciliation = result(company)
    assert reconciliation["status"] == "unestablished"
    assert reconciliation["book_due_fen"] is None


def test_formal_vat_and_surtax_settlements_match_each_actual_due_component(quarter_company):
    company = quarter_company
    vat_actual(
        company, assessed_vat_fen=1000, row_15_fen=1000, prepaid_vat_fen=1000,
        vat_due_fen=0, urban_tax_fen=10, education_tax_fen=5, local_education_tax_fen=5,
        settlement_as_of="2026-10-01",
    )
    publish(company, ASSESSMENT, "adopt-actual")
    saved = company.save(
        transactions.Payment, "tax-paid", period="2026-10", actual_date="2026-10-01",
        bank_account_id="bank", counterparty_id="authority", amount_fen=1020,
        direction="outflow", allocations=[{
            "source_kind": "tax_assessment", "source_id": ASSESSMENT,
            "obligation": name, "amount_fen": amount,
        } for name, amount in (("vat", 1000), ("surtax", 20))],
    )
    publish(company, "tax-paid", "publish-paid")
    current = company.current("tax_assessment", ASSESSMENT)
    with QueryReads.snapshot(company.engine) as reads:
        calculation = reads.calculation(current.id)
        for name, paid in (("vat", 1000), ("surtax", 20)):
            reconciliation = reconcile(reads, calculation, tax_name=name)
            assert reconciliation["status"] == "matched"
            assert reconciliation["book_due_fen"] == 0
            assert reconciliation["book_settled_fen"] == paid
            assert reconciliation["settlement_fact_ids"] == [saved["fact_id"]]


def test_zero_actual_vat_obligations_still_have_formal_adoption(quarter_company):
    company = quarter_company
    vat_actual(company, settlement_as_of="2026-10-01")
    publish(company, ASSESSMENT, "adopt-zero")
    current = company.current("tax_assessment", ASSESSMENT)
    with QueryReads.snapshot(company.engine) as reads:
        calculation = reads.calculation(current.id)
        for name in ("vat", "surtax"):
            reconciliation = reconcile(reads, calculation, tax_name=name)
            assert reconciliation["status"] == "matched"
            assert reconciliation["book_due_fen"] == 0
            assert reconciliation["book_settled_fen"] == 0


def test_no_voucher_income_tax_result_still_has_formal_adoption(tmp_path):
    company = Company(tmp_path / "zero-income-tax.sqlite")
    company.save(
        taxes.IncomeTaxAssessment, "current-tax", period="2026-09", year=2026,
        cumulative_assessed_fen=0, assessment_basis="confirmed_provision",
    )
    income_actual(company, cumulative_assessed_fen=0, prepaid_tax_fen=0, tax_due_fen=0)
    publish(company, "current-tax", "adopt-zero")
    current = company.current("income_tax_assessment", "current-tax")
    assert company.engine.trace(current.id)["calculation"]["outcome"]["lines"] == []
    reconciliation = result(company)
    assert reconciliation["status"] == "matched"
    assert reconciliation["book_due_fen"] == reconciliation["book_settled_fen"] == 0


def test_same_amount_actual_source_reviews_preserve_voucher_and_prove_exact_adoption(
    income_company,
):
    company = income_company
    publish(company, "current-tax", "adopt-provisional")
    original = company.engine.ledger("2026-09")
    previous_calculation = company.current("income_tax_assessment", "current-tax").id
    for index, basis_type in enumerate(("filed_return", "tax_authority_assessment")):
        income_actual(company, basis_type=basis_type)
        _, published = publish(company, "current-tax", f"adopt-actual-{index}")
        current = company.current("income_tax_assessment", "current-tax")
        assert current.id != previous_calculation
        assert next(item for item in published["results"]
                    if item["subject_id"] == "current-tax")["mode"] == "review_no_impact"
        assert company.engine.ledger("2026-09") == original
        reconciliation = result(company)
        assert reconciliation["status"] == "matched"
        assert reconciliation["book_due_fen"] == 133_358
        previous_calculation = current.id


@pytest.mark.parametrize("cutoff,expected", [("2026-10-01", "unestablished"),
                                             ("2026-10-31", "matched")])
def test_month_precision_non_cash_offset_does_not_invent_a_day(income_company, cutoff, expected):
    company = income_company
    company.save(
        taxes.IncomeTaxAssessment, "refund-tax", period="2026-08", year=2026,
        cumulative_assessed_fen=5000, assessment_basis="confirmed_provision",
    )
    publish(company, "refund-tax", "adopt-refund")
    income_actual(company, settlement_as_of=cutoff)
    publish(company, "current-tax", "adopt-actual")
    company.save(
        transactions.Settlement, "tax-offset", period="2026-10", settlement_kind="debt_offset",
        offset_right_confirmed=True,
        first={"source_kind": "income_tax_assessment", "source_id": "refund-tax",
               "obligation": "tax", "amount_fen": 1000},
        second={"source_kind": "income_tax_assessment", "source_id": "current-tax",
                "obligation": "tax", "amount_fen": 1000},
    )
    publish(company, "tax-offset", "adopt-offset")
    reconciliation = result(company)
    assert reconciliation["status"] == expected
    if expected == "matched":
        assert reconciliation["book_due_fen"] == 133_358
