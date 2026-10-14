"""Real publications keep computed tax, statements and later settlement aligned."""

import pytest
from entity_fixture import seed_entities
from material_fixture import supporting_text
from tax_point_fixture import sourced_tax_data
from test_business_domains import vat_policy
from test_computed_income_tax_reports import provision
from test_reports import book as book, scenario

from ai_accounting.kernel.contracts import Read
from ai_accounting.kernel.tax_filing_data import TaxFilingData


def sale(book, subject, *, period="2026-03", gross_fen=101_000):
    engine, save, publish, _ = book
    seed_entities(engine, (("customer", "organization", None),))
    with engine.store.connection(read_only=True) as connection:
        existing = connection.execute("SELECT 1 FROM subject WHERE id='vat-policy'").fetchone()
    if not existing:
        save("vat_policy", "vat-policy", {
            "period": "2026-01", "policy": vat_policy().model_dump(mode="json"),
        })
    data = sourced_tax_data(engine, "service_sale", subject, {
        "period": period, "customer_id": "customer", "gross_fen": gross_fen,
        "fulfillment_date": period + "-10", "vat_policy_id": "vat-policy",
        "exemption_eligible": False, "tax_obligation_period": period,
    })
    save("service_sale", subject, data)
    publish(subject)


def adopted(book, subject="computed-tax"):
    with book[0].store.connection(read_only=True) as connection:
        return book[0].store.select(
            connection, Read("calculation", "income_tax_assessment", "@" + subject),
        )[0]


def voucher(book, subject="computed-tax"):
    with book[0].store.connection(read_only=True) as connection:
        return tuple(connection.execute(
            "SELECT v.voucher_id,v.id,n.number FROM voucher_current c "
            "JOIN voucher_version v ON v.id=c.version_id JOIN voucher n ON n.id=v.voucher_id "
            "JOIN calculation a ON a.id=v.calculation_id WHERE a.subject_id=?",
            (subject,),
        ).fetchone())


def tax_field(book, *, quarter=1, subject="computed-tax"):
    return next(item for item in TaxFilingData(book[0]).query(2026, quarter)["income_tax_assessments"]
                if item["assessment_id"] == subject)


@pytest.mark.parametrize("treatment,expected", [("standard", 22_500), ("small_low_profit", 4_500)])
def test_positive_book_profit_produces_real_tax_voucher_and_matching_three_statements(
    book, treatment, expected,
):
    report = scenario(book, tax=False)
    sale(book, "quarter-sale")
    provision(book, treatment=treatment)
    result = report.report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    profit = result["statements"]["profit_statement"]
    assert profit["1"]["current_fen"] == 100_000
    assert profit["14"]["current_fen"] == 10_000
    assert profit["30"]["current_fen"] == 90_000
    assert profit["31"]["current_fen"] == expected
    assert profit["32"]["current_fen"] == 90_000 - expected
    current = adopted(book)
    assert current.values["cumulative_assessed_fen"] == expected
    assert current.values["calculated_assessment"]["profit_before_tax_fen"] == 90_000
    assert tax_field(book)["cumulative_assessed_fen"] == expected
    with book[0].store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT sum(credit-debit) FROM monthly_account WHERE account='222106'",
        ).fetchone()[0] == expected
        assert connection.execute(
            "SELECT sum(debit-credit) FROM monthly_account WHERE account='5801'",
        ).fetchone()[0] == expected
        assert connection.execute(
            "SELECT count(*) FROM fact_report_income_tax_confirmation",
        ).fetchone()[0] == 0
    assert all(item["passed"] for item in result["checks"])


def test_second_quarter_uses_cumulative_profit_and_subtracts_prior_adopted_provision(book):
    report = scenario(book, tax=False)
    sale(book, "first-sale")
    provision(book)
    first = adopted(book)
    sale(book, "second-sale", period="2026-06", gross_fen=202_000)
    provision(book, period="2026-06", subject="second-quarter-tax")
    second = adopted(book, "second-quarter-tax")
    assert first.values["cumulative_assessed_fen"] == 22_500
    assert second.values["cumulative_assessed_fen"] == 72_500
    assert second.values["change_fen"] == 50_000
    result = report.report(2026, 2)
    assert result["status"] == "ready", result["fact_issues"]
    profit = result["statements"]["profit_statement"]
    assert profit["30"]["current_fen"] == 200_000
    assert profit["30"]["year_to_date_fen"] == 290_000
    assert profit["31"]["current_fen"] == 50_000
    assert profit["31"]["year_to_date_fen"] == 72_500
    assert tax_field(book, quarter=2, subject="second-quarter-tax")["current_change_fen"] == 50_000
    assert report.report(2026, 1)["status"] == "ready"


def test_evidenced_prior_loss_revises_tax_with_same_voucher_number(book):
    report = scenario(book, tax=False)
    sale(book, "quarter-sale")
    proof = provision(book)
    original = voucher(book)
    engine, _, publish, _ = book
    loss_proof = engine.register_evidence(
        b"Synthetic prior-year loss schedule: confirmed cumulative deductible loss 200 yuan",
        "text/plain", "confirmed prior loss", request_id="loss-proof",
    )["digest"]
    supporting_text(engine, loss_proof)
    with engine.store.connection(read_only=True) as connection:
        previous = engine.store.current_fact(connection, "income-basis-2026-03")
    engine.save_fact("income_tax_prepayment_basis", previous.subject_id, {
        **previous.fact.model_dump(mode="json"), "loss_deduction_fen": 20_000,
        "loss_evidence": loss_proof,
    }, evidence=(proof, loss_proof), expected_revision=previous.revision,
        request_id="adopt-loss-basis")
    publish("computed-tax")
    current = adopted(book)
    assert current.values["cumulative_assessed_fen"] == 17_500
    assert current.values["calculated_assessment"]["loss_deduction_fen"] == 20_000
    replacement = voucher(book)
    assert (replacement[0], replacement[2]) == (original[0], original[2])
    assert replacement[1] != original[1]
    result = report.report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    assert result["statements"]["profit_statement"]["31"]["current_fen"] == 17_500


def test_tax_payment_changes_cash_and_payable_without_invalidating_profit_basis_or_forming_cycle(book):
    report = scenario(book, tax=False)
    sale(book, "quarter-sale")
    provision(book)
    current = adopted(book)
    engine, save, publish, _ = book
    seed_entities(engine, (("tax-authority", "organization", None),))
    save("cash_payment", "tax-payment", {
        "period": "2026-03", "actual_date": "2026-03-25", "direction": "outflow",
        "cash_account_id": "cash", "counterparty_id": "tax-authority", "amount_fen": 22_500,
        "allocations": [{"source_kind": "income_tax_assessment", "source_id": "computed-tax",
                         "obligation": "tax", "amount_fen": 22_500}],
    })
    publish("tax-payment")
    after_payment = adopted(book)
    assert after_payment.id == current.id
    assert after_payment.values["calculated_assessment"]["basis_digest"] == (
        current.values["calculated_assessment"]["basis_digest"]
    )
    result = report.report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    assert result["statements"]["cash_flow_statement"]["5"]["current_fen"] == 22_500
    assert tax_field(book)["cumulative_assessed_fen"] == 22_500
    # A fresh explicit review also stays outside the payment-to-tax dependency cycle.
    reviewed = publish("computed-tax")
    assert all(item["mode"] == "review_no_impact" for item in reviewed["results"])
    assert report.report(2026, 1)["status"] == "ready"


def test_actual_feedback_keeps_formal_tax_when_later_profit_only_changes_reference(book):
    report = scenario(book, tax=False)
    sale(book, "quarter-sale")
    proof = provision(book)
    engine, save, publish, _ = book
    engine.save_fact("income_tax_declaration_actual", "actual-tax", {
        "period": "2026-04", "assessment_id": "computed-tax", "year": 2026,
        "tax_period_end": "2026-03", "filing_evidence": proof, "basis_type": "filed_return",
        "cumulative_assessed_fen": 20_000, "prepaid_tax_fen": 0, "tax_due_fen": 20_000,
    }, evidence=(proof,), expected_revision=0, request_id="actual-tax-feedback")
    publish("computed-tax")
    original = voucher(book)
    save("expense", "later-cost", {
        "period": "2026-03", "counterparty_id": "supplier", "amount_fen": 500,
        "expense_class": "bank_fee", "creditor_kind": "supplier",
    })
    publish("later-cost")
    assert adopted(book).values["cumulative_assessed_fen"] == 20_000
    assert tax_field(book)["amount_source"] == "actual"
    reviewed = publish("computed-tax")
    assert next(item for item in reviewed["results"] if item["subject_id"] == "computed-tax")[
        "mode"
    ] == "review_no_impact"
    current = adopted(book)
    assert current.values["amount_source"] == "actual"
    assert current.values["cumulative_assessed_fen"] == 20_000
    assert current.values["calculated_assessment"]["cumulative_assessed_fen"] == 22_375
    assert voucher(book) == original
    result = report.report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    assert result["income_tax_source"] == "actual"
    assert result["statements"]["profit_statement"]["31"]["current_fen"] == 20_000
