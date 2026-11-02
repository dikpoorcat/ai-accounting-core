"""The exemption subline follows ordinary booked income, not whole-return rounding."""

import pytest

from ai_accounting_modular.domains.taxes.public import create_taxes
from tests.modular.domains.financing._support import (
    COUNTERPARTY,
    SOURCE,
    assemble,
    fact,
    get,
    request,
)

from ._fixtures import Context, actual_vat, assessment, evidence, policy, surtax, tax_sale
from .test_investment_tax import InvestmentContext, basis, view
from .test_investment_tax_application import inputs, quarter_return


def exemption_component(result):
    (contribution,) = [
        row
        for row in result["basis"]["adopted_quantities"]
        if row["type"] == "ordinary_vat_accounting"
    ]
    assert result["calculation_revision"] == 2
    assert result["rule_refs"][0]["revision"] == 2
    return contribution


@pytest.mark.parametrize("actual", [False, True])
def test_financial_rounding_never_reduces_the_ordinary_exemption_subline(actual):
    current = assessment(period="2026-07")
    declared = (
        [
            actual_vat(
                current,
                "1",
                exemption_relief_fen="0",
                financial_transfer_vat_fen="0",
                investment_interest_vat_fen="0",
            )
        ]
        if actual
        else []
    )
    context = InvestmentContext(
        current,
        *declared,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=[
            view("tiny-buy", "acquisition", gross="10000", day="2026-07-01"),
            view(
                "tiny-sale",
                "disposal",
                gross="10051",
                day="2026-07-02",
                allocations=[("tiny-buy", "10")],
            ),
        ],
        sales=[tax_sale("tiny-service", "51", "1", period="2026-07", rate="0.01")],
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["rounding_adjustment_fen"] == "-1"
    assert not any(row["account_code"] == "6301" for row in result["lines"])
    assert any(
        row["account_code"] == "5111" and row["credit_fen"] == "1" for row in result["lines"]
    )
    assert exemption_component(result) == {
        "type": "ordinary_vat_accounting",
        "exemption_income_fen": "0",
        "rounding_adjustment_fen": "0",
    }


def test_ordinary_rounding_gain_stays_income_but_is_not_exemption_income():
    current = assessment(period="2026-07")
    context = Context(
        current,
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        sales=[
            tax_sale("tiny-first", "51", "1", period="2026-07", rate="0.01"),
            tax_sale("tiny-second", "51", "1", period="2026-07", rate="0.01"),
        ],
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["rounding_adjustment_fen"] == "-1"
    assert any(
        row["account_code"] == "6301" and row["credit_fen"] == "1" for row in result["lines"]
    )
    assert exemption_component(result) == {
        "type": "ordinary_vat_accounting",
        "exemption_income_fen": "0",
        "rounding_adjustment_fen": "-1",
    }


def test_ordinary_proven_exemption_is_the_report_subline_amount():
    current = assessment(period="2026-07")
    context = Context(
        current,
        policy(rate="0.01", threshold="1000000"),
        surtax(zero=True),
        sales=[tax_sale("eligible", "10000", "100", period="2026-07", rate="0.01", eligible=True)],
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert exemption_component(result) == {
        "type": "ordinary_vat_accounting",
        "exemption_income_fen": "100",
        "rounding_adjustment_fen": "0",
    }


def test_real_adopted_exemption_credit_reversal_publishes_negative_report_income(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    _, _, _, vat_policy, surtax_policy, _ = inputs()
    vat_policy["data"]["threshold_fen"] = "1000000"
    sale = fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": "10100",
            "vat": {
                "policy_id": vat_policy["business_id"],
                "exemption_eligible": True,
                "invoice_kind": "ordinary",
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-06",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-06",
        actual="2026-06-03",
    )
    original = quarter_return(vat_policy, surtax_policy, "2026-06", "2026-06")
    declared = fact(
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": original["business_id"],
            "start_month": "2026-06",
            "end_month": "2026-06",
            "basis_type": "filed_return",
            "source": {"source_id": SOURCE},
            "assessed_vat_fen": "0",
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            "exemption_relief_fen": "100",
        },
        period="2026-06",
    )
    returned = fact(
        "operations.sale_return",
        {
            "sale_id": sale["business_id"],
            "customer_id": COUNTERPARTY,
            "gross_fen": "10100",
            "credit_note_vat_fen": "100",
            "original_tax_period": "2026-06",
        },
        period="2026-07",
    )
    credit = fact(
        "taxes.tax_credit",
        {
            "original_assessment_id": original["business_id"],
            "start_month": "2026-06",
            "end_month": "2026-06",
            "returns": [
                {
                    "return_id": returned["business_id"],
                    "original_business_id": sale["business_id"],
                    "gross_fen": "10100",
                    "vat_fen": "100",
                }
            ],
            "original_filing_source": {"source_id": SOURCE},
            "authority_source": {"source_id": SOURCE},
            "disposition": "refund",
            "vat_credit_fen": "0",
            "surtax_credit_fen": "0",
            "exemption_reversal_fen": "100",
        },
        period="2026-07",
    )
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-07")
    current["data"]["credit_ids"] = [credit["business_id"]]
    body = request(
        company, sale, original, declared, returned, credit, current, vat_policy, surtax_policy
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    result = next(
        row
        for row in get(app, company, current["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "main"
    )
    assert result["target"]["values"]["relief_fen"] == "0"
    (contribution,) = [
        row
        for row in result["basis"]["adopted_quantities"]
        if row["type"] == "ordinary_vat_accounting"
    ]
    assert contribution == {
        "type": "ordinary_vat_accounting",
        "exemption_income_fen": "-100",
        "rounding_adjustment_fen": "0",
    }
