"""Earned labor costs, actual cash and tax completion remain distinct facts."""

import pytest
from pydantic import ValidationError
from test_exports import evidence, inventory, template_bytes
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.workflow import ExternalObligation, Workflow


@pytest.fixture
def company(tmp_path):
    return Company(tmp_path / "labor.sqlite")


def accrual(**changes):
    return LaborAccrual(
        **{
            "period": "2026-01",
            "person_id": "contractor",
            "expense_class": "service",
            "gross_fee_fen": 208_330,
            "tax_treatment": "not_withheld_not_filed",
            **changes,
        }
    )


def actual_payment(amount=208_330, **changes):
    return Payment(
        **{
            "period": "2026-02",
            "actual_date": "2026-02-04",
            "direction": "outflow",
            "bank_account_id": "company-bank",
            "counterparty_id": "contractor",
            "amount_fen": amount,
            "allocations": (
                Allocation(
                    source_kind="labor_accrual",
                    source_id="labor",
                    obligation="net",
                    amount_fen=amount,
                ),
            ),
            **changes,
        }
    )


def payable(company):
    with company.engine.store.connection(read_only=True) as connection:
        return connection.execute(
            "SELECT coalesce(sum(amount),0) FROM balance "
            "WHERE category='payable' AND balance_key='labor_accrual:labor:net'"
        ).fetchone()[0]


def test_unpaid_labor_month_needs_no_invented_payment_date_or_tax_residency(company):
    saved = company.save(accrual(), "labor")
    company.publish("labor")
    result = company.current("labor", "labor_accrual")
    assert result.fact_id == saved["fact_id"]
    assert result.values["gross_fen"] == 208_330
    assert result.values["net_fen"] == 208_330
    assert result.values["tax_fen"] == 0  # Withholding recorded by this accrual only.
    assert result.values["theoretical_tax_fen"] is None
    assert result.values["tax_assessed"] is False
    assert result.values["tax_review_required"] is True
    assert result.values["rule_versions"] == ()
    assert result.values["obligations"] == (
        {
            "name": "net",
            "key": "labor_accrual:labor:net",
            "account": "224104",
            "normal": "credit",
            "amount_fen": 208_330,
            "category": "payable",
            "counterparty_id": "contractor",
            "cashflow": "labor",
        },
    )
    assert payable(company) == 208_330
    with company.engine.store.connection(read_only=True) as connection:
        rows = connection.execute("SELECT account,debit,credit FROM voucher_line").fetchall()
        assert [tuple(row) for row in rows] == [
            ("540104", 208_330, 0),
            ("224104", 0, 208_330),
        ]
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind IN "
                "('payment','cash_payment','external_completion','labor_income_tax_policy')"
            ).fetchone()[0]
            == 0
        )
    fields = LaborAccrual.model_json_schema()["properties"]
    assert "income_date" not in fields
    assert "actual_date" not in fields
    assert "recipient_tax_status" not in fields


@pytest.mark.parametrize("field", ["gross_fee_fen", "tax_treatment"])
def test_missing_amount_or_tax_status_cannot_silently_become_zero_or_exempt(company, field):
    company.save(accrual(**{field: None}), "labor")
    with pytest.raises(NeedsInformation) as error:
        company.publish("labor")
    assert error.value.issues[0]["field"] == field
    assert company.count("voucher") == 0
    assert payable(company) == 0


@pytest.mark.parametrize("amount", [0, -1, 2083.30, "208330"])
def test_accrual_amount_is_positive_integer_fen(amount):
    with pytest.raises(ValidationError):
        accrual(gross_fee_fen=amount)


def test_next_month_payment_settles_accrual_without_repeating_expense_or_tax(company):
    company.save(accrual(), "labor")
    _, first = company.publish("labor")
    original = company.current("labor", "labor_accrual")
    january = company.engine.ledger("2026-01")
    company.save(actual_payment(), "actual")
    company.publish("actual")
    assert company.current("labor", "labor_accrual") == original
    assert company.engine.ledger("2026-01") == january
    assert january[0]["number"] == first["labor"]["voucher_number"]
    assert payable(company) == 0
    assert company.pending() == set()
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT sum(debit) FROM voucher_line WHERE account='540104'"
            ).fetchone()[0]
            == 208_330
        )
        assert (
            connection.execute(
                "SELECT sum(credit) FROM voucher_line WHERE account='1002'"
            ).fetchone()[0]
            == 208_330
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM voucher_line WHERE account='222103'"
            ).fetchone()[0]
            == 0
        )
        assert str(company.engine.store.current_fact(connection, "actual").fact.actual_date) == (
            "2026-02-04"
        )


def test_open_accrual_correction_keeps_number_and_actual_payment_requires_overpay_fact(company):
    company.save(accrual(), "labor")
    _, first = company.publish("labor")
    company.save(accrual(gross_fee_fen=300_000), "labor", revision=1)
    _, second = company.publish("labor")
    assert second["labor"]["voucher_number"] == first["labor"]["voucher_number"]
    company.save(actual_payment(300_000), "actual")
    company.publish("actual")
    paid = company.current("actual", "payment")
    with company.engine.store.connection(read_only=True) as connection:
        actual = company.engine.store.current_fact(connection, "actual")
    company.save(accrual(gross_fee_fen=250_000), "labor", revision=2)
    counts = company.count("calculation"), company.count("voucher_version")
    with pytest.raises(NeedsInformation) as error:
        company.publish("labor")
    assert error.value.issues[0]["field"] == "overpayment"
    assert (company.count("calculation"), company.count("voucher_version")) == counts
    assert company.current("actual", "payment") == paid
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.current_fact(connection, "actual") == actual


def test_closed_unpaid_accrual_can_be_paid_later_without_reopening_or_refiling(company):
    company.save(accrual(), "labor")
    company.publish("labor")
    frozen = company.close("2026-01")
    january = company.engine.ledger("2026-01")
    company.save(actual_payment(), "actual")
    company.publish("actual")
    assert company.engine.ledger("2026-01") == january
    assert Periods(company.engine).closed_report("2026-01") == frozen
    assert payable(company) == 0
    assert company.pending() == set()


def test_closed_accrual_correction_uses_open_compensation(company):
    company.save(accrual(), "labor")
    company.publish("labor")
    frozen = company.close("2026-01")
    january = company.engine.ledger("2026-01")
    company.save(accrual(gross_fee_fen=300_000), "labor", revision=1)
    with pytest.raises(KernelError) as error:
        company.publish("labor")
    assert error.value.code == "closed_correction_required"
    company.publish("labor", correction_period="2026-02")
    assert company.engine.ledger("2026-01") == january
    assert Periods(company.engine).closed_report("2026-01") == frozen
    february = company.engine.ledger("2026-02")
    assert len(february) == 2
    assert any(row["reverses_id"] == january[0]["id"] for row in february)
    assert payable(company) == 300_000


def test_unpaid_labor_enters_export_and_filing_basis_without_claiming_completion(company):
    company.save(accrual(), "labor")
    company.publish("labor")
    company.save(
        ExternalObligation(
            period="2026-01",
            obligation_kind="individual_income_tax",
            start_period="2026-01",
            end_period="2026-01",
            due_date=None,
            applicability_confirmed=True,
        ),
        "labor-filing",
    )
    workflow = Workflow(company.engine)
    assert workflow.obligation_basis("labor-filing")["accepted_calculations"] == [
        {
            "subject_id": "labor",
            "calculation_id": company.current("labor", "labor_accrual").id,
        }
    ]
    assert workflow.query("2026-01", as_of="2026-02-05")["obligations"][0]["status"] != (
        "completed"
    )
    template = evidence(company, template_bytes())
    inventory(company)
    export = Exports(company.engine)
    with pytest.raises(NeedsInformation) as missing:
        export.preview("2026-01", template_evidence_digest=template)
    assert missing.value.issues[0]["field"] == "payee"
    assert payable(company) == 208_330  # Missing account details only block this file.
    export.save_payee(
        "contractor",
        name="测试劳务人员",
        account="001234567890",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    preview = export.preview("2026-01", template_evidence_digest=template)
    assert preview["total_fen"] == 208_330
    assert preview["rows"][0]["category"] == "劳务"
    assert preview["rows"][0]["sources"][0]["kind"] == "labor_accrual"
    company.save(actual_payment(100_000), "actual")
    company.publish("actual")
    assert export.preview("2026-01", template_evidence_digest=template)["total_fen"] == 108_330
