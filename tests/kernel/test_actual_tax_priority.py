"""Actual tax assessments replace reference arithmetic without rewriting business facts."""

import json

import pytest
from test_banking import inventories
from test_quarter_vat_rounding import (
    ACCRUED_VAT,
    ASSESSMENT,
    NET_SALES,
    PAYABLE_VAT,
    PERIOD,
    vat_account_balance,
)
from test_quarter_vat_rounding import (
    quarter_company as quarter_company,
)
from test_tax_credits import Company

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.domains import taxes, transactions
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.tax_filing_data import TaxFilingData


def vat_actual(company, **changes):
    fields = (
        dict(
            period="2026-10",
            assessment_id=ASSESSMENT,
            period_start="2026-07-01",
            period_end="2026-09-30",
            filing_evidence=company.filing,
            basis_type="filed_return",
            assessed_vat_fen=0,
            urban_tax_fen=0,
            education_tax_fen=0,
            local_education_tax_fen=0,
            exemption_relief_fen=0,
            prepaid_vat_fen=0,
            vat_due_fen=0,
            urban_due_fen=0,
            education_due_fen=0,
            local_education_due_fen=0,
            row_15_fen=0,
            row_16_fen=0,
            row_17_fen=0,
            row_18_fen=0,
        )
        | changes
    )
    return company.save(taxes.VatDeclarationActual, "actual-vat", **fields)


def snapshot(company):
    with company.engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


def preview_result(company, subject):
    try:
        return company.engine.preview([subject])
    except NeedsInformation as error:
        return error.response()


def publish(company, subject, request, **options):
    preview = company.engine.preview([subject], **options)
    assert preview["status"] == "preview", preview
    result = company.engine.confirm(
        [subject],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=request,
        **options,
    )
    assert result["status"] == "published"
    return preview, result


@pytest.mark.parametrize("vat", [0, PAYABLE_VAT - 19, PAYABLE_VAT + 37])
def test_actual_vat_and_each_surtax_component_drive_journal_and_filing(quarter_company, vat):
    company = quarter_company
    vat_actual(
        company,
        assessed_vat_fen=vat,
        vat_due_fen=vat,
        row_15_fen=vat,
        urban_tax_fen=123,
        education_tax_fen=45,
        local_education_tax_fen=6,
        urban_due_fen=123,
        education_due_fen=45,
        local_education_due_fen=6,
    )
    publish(company, ASSESSMENT, "publish-actual")
    values = company.current("tax_assessment", ASSESSMENT).values
    assert values["amount_source"] == "actual"
    assert values["payable_vat_fen"] == vat and values["surtax_fen"] == 174
    assert values["calculated_assessment"]["payable_vat_fen"] == PAYABLE_VAT
    assert values["declaration_adjustment_fen"] == vat - PAYABLE_VAT
    assert values["net_sales_fen"] == NET_SALES
    assert vat_account_balance(company) == vat
    assert values["relief_fen"] == 0 and not values["vat_exemption_profit_lines"]
    assert company.balance(f"tax_assessment:{ASSESSMENT}:vat") == vat
    assert company.balance(f"tax_assessment:{ASSESSMENT}:surtax") == 174
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT sum(credit-debit) FROM monthly_account WHERE account='5001'"
            ).fetchone()[0]
            == NET_SALES
        )
        assert verify_integrity(company.engine, connection)["status"] == "verified"
    result = TaxFilingData(company.engine).query(2026, 3)
    validate_response("tax_filing_data", result)
    fields = {row["field_id"]: row for row in result["fields"] if row["period_basis"] == "current"}
    for name, amount in (
        ("row_20_fen", vat),
        ("row_22_fen", vat),
        ("urban_tax_fen", 123),
        ("education_tax_fen", 45),
        ("local_education_tax_fen", 6),
    ):
        field = fields["vat_and_surtax." + name]
        assert field["value_fen"] == amount and field["value_source"] == "actual"
        assert any(ref["type"] == "vat_declaration_actual_fact_id" for ref in field["source_basis"])
    assert fields["vat_and_surtax.payable_vat_fen"]["calculated_value_fen"] == PAYABLE_VAT


@pytest.mark.parametrize(
    "missing", ["assessed_vat_fen", "urban_tax_fen", "education_tax_fen", "local_education_tax_fen"]
)
def test_partial_actual_source_blocks_without_calculated_fallback(quarter_company, missing):
    company = quarter_company
    vat_actual(company, **{missing: None})
    before = snapshot(company)
    preview = preview_result(company, ASSESSMENT)
    assert preview["status"] == "needs_information"
    assert any(
        issue["field"] == "vat_declaration_actual." + missing for issue in preview["fact_issues"]
    )
    assert snapshot(company) == before


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"period_start": "2026-08-01"}, "period_start"),
        ({"filing_evidence": "0" * 64}, "filing_evidence"),
        ({"assessed_vat_fen": 100, "vat_due_fen": 0}, "vat_due_fen"),
        ({"row_15_fen": 100}, "row_15_fen"),
        ({"exemption_relief_fen": ACCRUED_VAT + 1}, "exemption_relief_fen"),
    ],
)
def test_actual_conflicts_return_structured_issues(quarter_company, changes, field):
    company = quarter_company
    vat_actual(company, **changes)
    preview = preview_result(company, ASSESSMENT)
    assert preview["status"] == "needs_information"
    assert any(
        issue["field"] == "vat_declaration_actual." + field for issue in preview["fact_issues"]
    )


def test_two_current_actual_sources_do_not_select_latest_timestamp(quarter_company):
    company = quarter_company
    vat_actual(company)
    data = taxes.VatDeclarationActual.model_validate_json(
        json.dumps(
            dict(
                period="2026-10",
                assessment_id=ASSESSMENT,
                period_start="2026-07-01",
                period_end="2026-09-30",
                filing_evidence=company.filing,
                basis_type="filed_return",
            )
        )
    )
    company.save(taxes.VatDeclarationActual, "other-actual", **data.model_dump(mode="json"))
    preview = preview_result(company, ASSESSMENT)
    assert preview["status"] == "needs_information"
    assert any(issue["field"] == "vat_declaration_actual" for issue in preview["fact_issues"])


def test_unavailable_calculation_cannot_block_complete_actual_amounts(quarter_company, monkeypatch):
    company = quarter_company
    vat_actual(company)

    def unavailable(**kwargs):
        raise NeedsInformation("reference_only", "synthetic unsupported reference calculation")

    monkeypatch.setattr(taxes, "calculate_vat_period", unavailable)
    publish(company, ASSESSMENT, "actual-without-reference")
    values = company.current("tax_assessment", ASSESSMENT).values
    assert values["payable_vat_fen"] == 0 and values["calculated_assessment"] is None
    assert values["calculation_reference_issue"]["status"] == "needs_information"
    result = TaxFilingData(company.engine).query(2026, 3)
    field = next(
        row
        for row in result["fields"]
        if row["field_id"] == "vat_and_surtax.row_20_fen" and row["period_basis"] == "current"
    )
    assert field["status"] == "ready" and field["value_fen"] == 0
    assert field["calculated_value_fen"] is None


def test_missing_reference_policies_do_not_block_actual_tax(quarter_company):
    company = quarter_company
    company.save(
        taxes.TaxAssessment,
        ASSESSMENT,
        period=PERIOD,
        period_start="2026-07-01",
        period_end="2026-09-30",
        vat_policy_id="unavailable-vat-policy",
        surtax_policy_id="unavailable-surtax-policy",
    )
    vat_actual(company)
    publish(company, ASSESSMENT, "actual-without-policies")
    values = company.current("tax_assessment", ASSESSMENT).values
    assert values["payable_vat_fen"] == 0 and values["calculated_assessment"] is None
    assert values["vat_policy_version"] is None and values["surtax_policy_version"] is None


def test_actual_observation_month_does_not_replace_accounting_month(quarter_company):
    company = quarter_company
    company.save(
        taxes.TaxAssessment,
        ASSESSMENT,
        period="2026-10",
        period_start="2026-07-01",
        period_end="2026-09-30",
        vat_policy_id="vat",
        surtax_policy_id="surtax",
    )
    vat_actual(company, period="2026-09")
    publish(company, ASSESSMENT, "actual-before-accounting-posting")
    assert company.current("tax_assessment", ASSESSMENT).period == "2026-10"


def test_missing_optional_actual_fields_are_not_filled_from_reference(quarter_company):
    company = quarter_company
    vat_actual(
        company,
        row_15_fen=None,
        row_16_fen=None,
        prepaid_vat_fen=None,
        vat_due_fen=None,
        exemption_relief_fen=None,
    )
    publish(company, ASSESSMENT, "actual-with-unknown-form-fields")
    values = company.current("tax_assessment", ASSESSMENT).values
    assert values["payable_vat_fen"] == 0 and values["exemption_relief_confirmed"] is False
    assert not values["vat_exemption_profit_lines"]
    fields = {
        row["field_id"]: row
        for row in TaxFilingData(company.engine).query(2026, 3)["fields"]
        if row["period_basis"] == "current"
    }
    for name in ("row_15_fen", "row_16_fen", "row_21_fen", "row_22_fen", "relief_fen"):
        assert fields["vat_and_surtax." + name]["value_fen"] is None
        assert fields["vat_and_surtax." + name]["status"] == "needs_information"


def test_actual_source_revision_invalidates_then_replaces_original_voucher(quarter_company):
    company = quarter_company
    _, first = publish(company, ASSESSMENT, "provisional")
    old = company.current("tax_assessment", ASSESSMENT)
    vat_actual(company)
    result = TaxFilingData(company.engine).query(2026, 3)
    assert any(issue["field"] == "tax_assessment" for issue in result["fact_issues"])
    _, actual = publish(company, ASSESSMENT, "replace-with-actual")
    first_result = next(row for row in first["results"] if row["subject_id"] == ASSESSMENT)
    actual_result = next(row for row in actual["results"] if row["subject_id"] == ASSESSMENT)
    assert first_result["voucher_number"] == actual_result["voucher_number"]
    assert (
        company.engine.trace(old.id)["calculation"]["outcome"]["values"]["payable_vat_fen"]
        == PAYABLE_VAT
    )
    source = vat_actual(company, assessed_vat_fen=700, vat_due_fen=700, row_15_fen=700)
    publish(company, ASSESSMENT, "revise-actual")
    current = company.current("tax_assessment", ASSESSMENT)
    assert current.values["actual_declaration"]["fact_id"] == source["fact_id"]
    assert vat_account_balance(company) == 700


@pytest.mark.parametrize("stage", ["begin", "lines", "calculation", "published", "commit"])
def test_actual_replacement_failure_is_atomic_and_retry_idempotent(quarter_company, stage):
    company = quarter_company
    publish(company, ASSESSMENT, "provisional")
    vat_actual(company)
    preview = company.engine.preview([ASSESSMENT])
    before = snapshot(company)

    def fail(actual, connection):
        if actual == stage:
            raise RuntimeError("synthetic actual adoption failure")

    company.engine.fault = fail
    with pytest.raises(RuntimeError, match="actual adoption"):
        company.engine.confirm(
            [ASSESSMENT],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="actual-retry",
        )
    assert snapshot(company) == before
    company.engine.fault = lambda stage, connection: None
    result = company.engine.confirm(
        [ASSESSMENT],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="actual-retry",
    )
    after = snapshot(company)
    assert (
        company.engine.confirm(
            [ASSESSMENT],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="actual-retry",
        )
        == result
    )
    assert snapshot(company) == after


def test_closed_provisional_tax_keeps_frozen_history_and_posts_linked_correction(quarter_company):
    company = quarter_company
    publish(company, ASSESSMENT, "provisional")
    old = company.current("tax_assessment", ASSESSMENT)
    periods = None
    for month, active in (
        ("2026-07", {"transactions"}),
        ("2026-08", {"transactions"}),
        ("2026-09", {"tax"}),
    ):
        periods = inventories(company.engine, company.confirmation, month, active)
        preview = periods.preview_close(month, owner_confirmation=company.confirmation)
        periods.close(
            month,
            owner_confirmation=company.confirmation,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close-" + month,
        )
    frozen = periods.closed_report(PERIOD)
    vat_actual(company)
    _, correction = publish(
        company, ASSESSMENT, "closed-actual-correction", posting_period="2026-10"
    )
    assert (
        next(row for row in correction["results"] if row["subject_id"] == ASSESSMENT)["mode"]
        == "closed_correction"
    )
    assert periods.closed_report(PERIOD) == frozen
    assert (
        company.engine.trace(old.id)["calculation"]["outcome"]["values"]["payable_vat_fen"]
        == PAYABLE_VAT
    )
    closed = TaxFilingData(company.engine).query(2026, 3, source="closed")
    field = next(
        row
        for row in closed["fields"]
        if row["field_id"] == "vat_and_surtax.row_20_fen" and row["period_basis"] == "current"
    )
    assert field["value_fen"] == PAYABLE_VAT and field["value_source"] == "calculated"


@pytest.fixture
def income_company(tmp_path):
    company = Company(tmp_path / "income-tax.sqlite")
    company.save(
        taxes.IncomeTaxAssessment,
        "prior-tax",
        period="2026-06",
        year=2026,
        cumulative_assessed_fen=10_000,
        assessment_basis="confirmed_provision",
    )
    publish(company, "prior-tax", "prior-tax")
    company.save(
        transactions.Payment,
        "prior-payment",
        period="2026-07",
        actual_date="2026-07-15",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=9_629,
        direction="outflow",
        allocations=[
            dict(
                source_kind="income_tax_assessment",
                source_id="prior-tax",
                obligation="tax",
                amount_fen=9_629,
            )
        ],
    )
    publish(company, "prior-payment", "prior-payment")
    company.save(
        taxes.IncomeTaxAssessment,
        "current-tax",
        period="2026-09",
        year=2026,
        cumulative_assessed_fen=142_987,
        assessment_basis="confirmed_provision",
    )
    return company


def income_actual(company, **changes):
    return company.save(
        taxes.IncomeTaxDeclarationActual,
        "actual-income",
        **(
            dict(
                period="2026-10",
                year=2026,
                tax_period_end="2026-09",
                assessment_id="current-tax",
                filing_evidence=company.filing,
                basis_type="filed_return",
                cumulative_assessed_fen=142_987,
                prepaid_tax_fen=9_629,
                tax_due_fen=133_358,
                settlement_as_of="2026-10-01",
            )
            | changes
        ),
    )


def test_income_actual_cumulative_accrual_and_due_keep_prior_unpaid_amount(income_company):
    company = income_company
    income_actual(company)
    publish(company, "current-tax", "actual-income")
    values = company.current("income_tax_assessment", "current-tax").values
    assert values["change_fen"] == 132_987
    assert values["tax_due_fen"] == 133_358
    assert company.balance("income_tax_assessment:prior-tax:tax") == 371
    assert company.balance("income_tax_assessment:current-tax:tax") == 132_987
    result = TaxFilingData(company.engine).query(2026, 3)
    validate_response("tax_filing_data", result)
    adopted = next(
        row for row in result["income_tax_assessments"] if row["assessment_id"] == "current-tax"
    )
    assert adopted["current_change_fen"] == 132_987 and adopted["tax_due_fen"] == 133_358
    assert adopted["amount_source"] == "actual"
    assert adopted["settlement_reconciliation"]["status"] == "matched"
    assert adopted["settlement_reconciliation"]["book_due_fen"] == 133_358


@pytest.mark.parametrize("actual", [0, 400_000, 145_000])
def test_income_actual_overrides_input_and_preserves_reference(income_company, actual):
    company = income_company
    income_actual(company, cumulative_assessed_fen=actual, tax_due_fen=actual - 9_629)
    publish(company, "current-tax", "override-income")
    values = company.current("income_tax_assessment", "current-tax").values
    assert values["cumulative_assessed_fen"] == actual
    assert values["change_fen"] == actual - 10_000
    assert values["calculated_assessment"]["cumulative_assessed_fen"] == 142_987


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"cumulative_assessed_fen": None}, "cumulative_assessed_fen"),
        ({"tax_period_end": "2026-06"}, "tax_period_end"),
        ({"year": 2025, "tax_period_end": "2025-12"}, "tax_period_end"),
        ({"tax_due_fen": 132_987}, "tax_due_fen"),
    ],
)
def test_income_actual_missing_or_wrong_meaning_never_falls_back(income_company, changes, field):
    company = income_company
    income_actual(company, **changes)
    result = preview_result(company, "current-tax")
    assert result["status"] == "needs_information"
    assert any(
        issue["field"] == "income_tax_declaration_actual." + field
        for issue in result["fact_issues"]
    )


def test_missing_settlement_date_does_not_invent_management_fact(income_company):
    company = income_company
    income_actual(company, settlement_as_of=None)
    publish(company, "current-tax", "no-cutoff")
    values = company.current("income_tax_assessment", "current-tax").values
    assert values["change_fen"] == 132_987
    row = TaxFilingData(company.engine).query(2026, 3)["income_tax_assessments"][0]
    assert row["settlement_reconciliation"]["status"] == "unestablished"
    assert row["settlement_reconciliation"]["book_due_fen"] is None


def test_actual_payment_after_filing_cutoff_is_not_prior_prepayment(income_company):
    company = income_company
    income_actual(company)
    publish(company, "current-tax", "actual-income")
    company.save(
        transactions.Payment,
        "after-filing-payment",
        period="2026-10",
        actual_date="2026-10-08",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=132_987,
        direction="outflow",
        allocations=[
            dict(
                source_kind="income_tax_assessment",
                source_id="current-tax",
                obligation="tax",
                amount_fen=132_987,
            )
        ],
    )
    publish(company, "after-filing-payment", "later-payment")
    publish(company, "current-tax", "review-after-payment")
    row = TaxFilingData(company.engine).query(2026, 3)["income_tax_assessments"][0]
    assert row["settlement_reconciliation"]["book_due_fen"] == 133_358
    assert company.balance("income_tax_assessment:current-tax:tax") == 0


def test_downward_actual_after_payment_preserves_cash_and_requires_overpayment(quarter_company):
    company = quarter_company
    publish(company, ASSESSMENT, "provisional")
    company.save(
        transactions.Payment,
        "vat-paid",
        period="2026-10",
        actual_date="2026-10-01",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=PAYABLE_VAT,
        direction="outflow",
        allocations=[
            dict(
                source_kind="tax_assessment",
                source_id=ASSESSMENT,
                obligation="vat",
                amount_fen=PAYABLE_VAT,
            )
        ],
    )
    publish(company, "vat-paid", "paid")
    vat_actual(company)
    before = snapshot(company)
    result = preview_result(company, ASSESSMENT)
    assert result["status"] == "needs_information"
    assert any(issue["field"] == "overpayment" for issue in result["fact_issues"])
    assert snapshot(company) == before


def test_income_actual_pending_source_is_not_presented_as_current_adopted_amount(income_company):
    company = income_company
    publish(company, "current-tax", "provisional")
    income_actual(company, cumulative_assessed_fen=145_000, tax_due_fen=135_371)
    result = TaxFilingData(company.engine).query(2026, 3)
    row = next(
        item for item in result["income_tax_assessments"] if item["assessment_id"] == "current-tax"
    )
    assert row["status"] == "needs_information" and row["cumulative_assessed_fen"] is None
