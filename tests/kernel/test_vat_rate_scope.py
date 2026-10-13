"""Adopted source rates govern each filing bucket across real business paths."""

from decimal import Decimal

import pytest
from tax_point_fixture import contract_tax_basis
from test_business_domains import surtax_policy, vat_policy
from test_tax_credits import Company

from ai_accounting.kernel.domains import taxes, transactions


@pytest.fixture
def rate_book(tmp_path):
    company = Company(tmp_path / "mixed-source-rates.sqlite")
    ordinary = vat_policy()
    for subject, rate in (("vat", "1"), ("vat-three", "3")):
        policy = ordinary.model_copy(update={
            "rate_percent": Decimal(rate), "version": "synthetic-confirmed-rate-" + rate,
        })
        company.save(taxes.VatPolicyFact, subject, period="2026-01",
                     policy=policy.model_dump(mode="json"))
    company.save(taxes.SurtaxPolicyFact, "surtax", period="2026-01",
                 policy=surtax_policy().model_dump(mode="json"))
    return company


def source_sale(company, subject, *, rate, contract_period="2026-01"):
    contract_tax_basis(
        company.engine, "service_sale", subject,
        business_period="2026-01", contract_period=contract_period,
        contract_date=contract_period + "-10",
    )
    company.save(
        transactions.ServiceSale, subject,
        period="2026-01", customer_id="customer", gross_fen=101_000 if rate == "1" else 103_000,
        fulfillment_date="2026-01-08", vat_policy_id="vat" if rate == "1" else "vat-three",
        exemption_eligible=False,
    )
    company.publish(subject)


def adopted_fields(company, *, assessment="january", period="2026-01"):
    company.assess(assessment, period)
    company.publish(assessment)
    return company.current("tax_assessment", assessment)


def test_mixed_one_and_three_percent_sources_are_not_reclassified_as_rounding_income(rate_book):
    source_sale(rate_book, "reduced-rate-sale", rate="1")
    source_sale(rate_book, "full-rate-sale", rate="3")
    adopted = adopted_fields(rate_book)
    values = adopted.values
    assert values["accrued_vat_fen"] == values["payable_vat_fen"] == 4000
    assert values["rounding_adjustment_fen"] == 0
    assert values["filing_fields"]["row_15_fen"] == 6000
    assert values["filing_fields"]["row_16_fen"] == 2000
    assert values["filing_fields"]["row_20_fen"] == 4000
    assert all(issue["field"] != "vat.form" for issue in values["filing_issues"])
    with rate_book.engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM voucher_version v JOIN voucher_line l ON l.version_id=v.id "
            "WHERE v.calculation_id=? AND l.account='6301'", (adopted.id,),
        ).fetchone()[0] == 0


def test_three_percent_source_has_no_reduction_under_one_percent_period_policy(rate_book):
    source_sale(rate_book, "full-rate-sale", rate="3")
    adopted = adopted_fields(rate_book)
    assert adopted.values["accrued_vat_fen"] == adopted.values["payable_vat_fen"] == 3000
    assert adopted.values["filing_fields"]["row_16_fen"] == 0
    assert adopted.values["rounding_adjustment_fen"] == 0


def test_same_period_return_retains_the_original_three_percent_bucket(rate_book):
    source_sale(rate_book, "reduced-rate-sale", rate="1")
    source_sale(rate_book, "full-rate-sale", rate="3")
    rate_book.save(
        transactions.SaleReturn, "full-rate-return", period="2026-01",
        sale_id="full-rate-sale", customer_id="customer", returned_gross_fen=103_000,
        credit_note_vat_fen=3000,
    )
    rate_book.publish("full-rate-return")
    values = adopted_fields(rate_book).values
    assert values["net_sales_fen"] == 100_000
    assert values["accrued_vat_fen"] == values["payable_vat_fen"] == 1000
    assert values["filing_fields"]["row_15_fen"] == 3000
    assert values["filing_fields"]["row_16_fen"] == 2000
    assert values["rounding_adjustment_fen"] == 0


def test_deferred_three_percent_service_uses_source_rate_at_its_contractual_tax_point(rate_book):
    source_sale(rate_book, "deferred-full-rate-sale", rate="3", contract_period="2026-02")
    sale = rate_book.current("service_sale", "deferred-full-rate-sale")
    assert sale.values["accrued_vat_fen"] == 0
    assert sale.values["vat_fen"] - sale.values["accrued_vat_fen"] == 3000
    rate_book.save(
        transactions.ServiceTaxPoint, "actual-contract-tax-point", period="2026-02",
        sale_id="deferred-full-rate-sale", trigger="declared", declaration_confirmed=True,
    )
    rate_book.publish("actual-contract-tax-point")
    values = adopted_fields(rate_book, assessment="february", period="2026-02").values
    assert values["net_sales_fen"] == 100_000
    assert values["accrued_vat_fen"] == values["payable_vat_fen"] == 3000
    assert values["filing_fields"]["row_16_fen"] == 0
    assert values["rounding_adjustment_fen"] == 0


def test_native_surtax_provision_does_not_claim_unknown_filing_payments(rate_book):
    from ai_accounting.kernel.tax_filing_data import TaxFilingData

    source_sale(rate_book, "reduced-rate-sale", rate="1")
    source_sale(rate_book, "full-rate-sale", rate="3")
    rate_book.save(
        taxes.TaxAssessment, "first-quarter", period="2026-03",
        period_start="2026-01-01", period_end="2026-03-31",
        vat_policy_id="vat", surtax_policy_id="surtax",
    )
    rate_book.publish("first-quarter")
    result = TaxFilingData(rate_book.engine).query(2026, 1)
    fields = {item["field_id"].split(".", 1)[1]: item for item in result["fields"]
              if item["section"] == "vat_and_surtax" and item["period_basis"] == "current"}
    assert fields["row_20_fen"]["status"] == "ready"
    assert fields["row_20_fen"]["value_fen"] == 4000
    for native in ("urban_tax_fen", "education_tax_fen", "local_education_tax_fen"):
        assert fields[native]["status"] == "ready"
        assert fields[native]["definition"]["form_field"] is None
    for name in ("row_21_fen", "row_22_fen", "row_23_fen", "row_24_fen", "row_25_fen"):
        assert fields[name]["status"] == "needs_information"
        assert fields[name]["value_fen"] is None
        assert any(issue.get("amount_field") == name for issue in fields[name]["fact_issues"])
