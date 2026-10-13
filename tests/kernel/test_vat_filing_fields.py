"""The journal and fixed filing fields consume the same period assessment."""

from datetime import date
from decimal import Decimal, localcontext

import pytest
from test_business_domains import surtax_policy, vat_policy

from ai_accounting.kernel.contracts import Calculation, Context, FactVersion, NeedsInformation, Read
from ai_accounting.kernel.domains import taxes
from ai_accounting.kernel.domains.taxes import VatSales, calculate_vat_period
from ai_accounting.kernel.types import YearMonth


def assess(sales, *, rate="1", threshold=30_000_000):
    policy = vat_policy().model_copy(update={
        "rate_percent": Decimal(rate), "threshold_fen": threshold,
        "threshold_operator": "strictly_below",
    })
    return calculate_vat_period(
        sales=tuple(sales), start=date(2026, 7, 1), end=date(2026, 9, 30),
        vat_policy=policy, surtax_policy=surtax_policy(),
    )


def test_one_percent_uses_actual_three_percent_and_reduction_columns():
    result = assess([VatSales(net_sales_fen=41_373_925, accrued_vat_fen=413_739,
                              exemption_eligible=True)])
    fields = result.filing_fields
    assert fields["row_1_fen"] == result.taxable_sales_fen == 41_373_925
    assert fields["row_15_fen"] == 1_241_218
    assert fields["row_16_fen"] == 827_479
    assert fields["row_20_fen"] == result.payable_vat_fen == 413_739
    assert result.urban_tax_fen == 14_481
    assert result.education_tax_fen == 6_206
    assert result.local_education_tax_fen == 4_137
    assert not {"row_23_fen", "row_24_fen", "row_25_fen"} & fields.keys()
    assert {issue["amount_field"] for issue in result.filing_issues} == {
        "row_21_fen", "row_22_fen", "row_23_fen", "row_24_fen", "row_25_fen",
    }


@pytest.mark.parametrize("precision", [2, 28])
@pytest.mark.parametrize("parts", [
    [(33_433_651, 334_336)],
    [(10_000_017, 100_000), (10_000_017, 100_000), (13_433_617, 134_336)],
], ids=["one-quarter-total", "three-month-sources"])
def test_quarterly_sales_rounding_unifies_vat_and_surtax(parts, precision):
    sales = [
        VatSales(net_sales_fen=net, accrued_vat_fen=accrued,
                 exemption_eligible=True, rate_percent="1")
        for net, accrued in parts
    ]
    with localcontext() as ctx:
        ctx.prec = precision
        result = assess(sales)
    assert result.net_sales_fen == result.taxable_sales_fen == 33_433_651
    assert result.accrued_vat_fen == 334_336
    assert result.exempt_sales_fen == result.relief_fen == 0
    assert result.filing_fields["row_15_fen"] == 1_003_010
    assert result.filing_fields["row_16_fen"] == 668_673
    assert result.filing_fields["row_20_fen"] == result.payable_vat_fen == 334_337
    assert result.rounding_adjustment_fen == 1
    assert (result.urban_tax_fen, result.education_tax_fen,
            result.local_education_tax_fen) == (11_702, 5_015, 3_343)
    assert result.surtax_fen == 20_060
    assert result.payable_vat_fen + result.surtax_fen == 354_397


@pytest.mark.parametrize("parts,before,reduction,payable,adjustment", [
    ([(17, 0)], 1, 0, 1, 1),
    ([(25, 0)], 1, 1, 0, 0),
    ([(17, 0), (17, 0)], 1, 1, 0, 0),
    ([(50, 1), (50, 1)], 3, 2, 1, -1),
], ids=["positive-difference", "half-fen-reduction", "aggregate-before-rounding",
        "negative-difference"])
def test_separately_rounded_columns_reconcile_source_accruals(
    parts, before, reduction, payable, adjustment,
):
    result = assess([
        VatSales(net_sales_fen=net, accrued_vat_fen=accrued,
                 exemption_eligible=False, rate_percent="1")
        for net, accrued in parts
    ])
    assert result.filing_fields["row_15_fen"] == before
    assert result.filing_fields["row_16_fen"] == reduction
    assert result.filing_fields["row_20_fen"] == result.payable_vat_fen == payable
    assert result.rounding_adjustment_fen == adjustment
    assert result.accrued_vat_fen + adjustment - result.relief_fen == payable


def test_exemption_is_displayed_without_subtracting_it_twice():
    result = assess([
        VatSales(net_sales_fen=100_000, accrued_vat_fen=1000, exemption_eligible=True),
        VatSales(net_sales_fen=100_000, accrued_vat_fen=1000, exemption_eligible=False),
    ])
    fields = result.filing_fields
    assert fields["row_9_fen"] == fields["row_10_fen"] == 100_000
    assert fields["row_17_fen"] == fields["row_18_fen"] == 3000
    assert fields["row_20_fen"] == fields["row_15_fen"] - fields["row_16_fen"] == 1000
    assert result.relief_fen == 1000


def test_statutory_period_rounding_is_explicit_and_decimal_context_independent():
    with localcontext() as ctx:
        ctx.prec = 2
        result = assess([VatSales(net_sales_fen=17, accrued_vat_fen=0,
                                  exemption_eligible=False)])
    assert result.filing_fields["row_15_fen"] == 1
    assert result.filing_fields["row_16_fen"] == 0
    assert result.payable_vat_fen == result.rounding_adjustment_fen == 1


def test_reaching_the_2026_threshold_taxates_the_whole_period():
    result = assess([VatSales(net_sales_fen=30_000_000, accrued_vat_fen=300_000,
                              exemption_eligible=True)])
    assert result.exempt_sales_fen == result.relief_fen == 0
    assert result.taxable_sales_fen == 30_000_000
    assert result.payable_vat_fen == 300_000


def test_used_asset_amount_is_preserved_without_claiming_standard_form_support():
    result = assess([VatSales(net_sales_fen=100_000, accrued_vat_fen=2000,
                              exemption_eligible=False, filing_bucket="used_asset")])
    assert result.payable_vat_fen == 2000
    assert result.rounding_adjustment_fen == 0
    assert result.filing_issues[0]["field"] == "vat.form"


def test_each_business_rate_controls_reduction_not_the_assessment_policy():
    result = assess([
        VatSales(net_sales_fen=100_000, accrued_vat_fen=1000,
                 exemption_eligible=False, rate_percent="1"),
        VatSales(net_sales_fen=100_000, accrued_vat_fen=3000,
                 exemption_eligible=False, rate_percent="3"),
    ])
    assert result.filing_fields["row_15_fen"] == 6000
    assert result.filing_fields["row_16_fen"] == 2000
    assert result.payable_vat_fen == 4000
    assert result.rounding_adjustment_fen == 0


@pytest.mark.parametrize("period_rate", ["1", "3"])
def test_mixed_rates_share_three_percent_column_but_only_one_percent_is_reduced(period_rate):
    result = assess([
        VatSales(net_sales_fen=17, accrued_vat_fen=0,
                 exemption_eligible=False, rate_percent="1"),
        VatSales(net_sales_fen=17, accrued_vat_fen=1,
                 exemption_eligible=False, rate_percent="3"),
    ], rate=period_rate)
    assert result.filing_fields["row_1_fen"] == 34
    assert result.filing_fields["row_15_fen"] == 1
    assert result.filing_fields["row_16_fen"] == 0
    assert result.filing_fields["row_20_fen"] == result.payable_vat_fen == 1
    assert result.rounding_adjustment_fen == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_same_period_return_is_netted_before_both_columns_are_rounded(reverse):
    sales = [
        VatSales(net_sales_fen=100, accrued_vat_fen=1,
                 exemption_eligible=False, rate_percent="1"),
        VatSales(net_sales_fen=-17, accrued_vat_fen=0,
                 exemption_eligible=False, rate_percent="1"),
    ]
    result = assess(reversed(sales) if reverse else sales)
    assert result.net_sales_fen == result.taxable_sales_fen == 83
    assert result.filing_fields["row_15_fen"] == 2
    assert result.filing_fields["row_16_fen"] == 2
    assert result.filing_fields["row_20_fen"] == result.payable_vat_fen == 0
    assert result.accrued_vat_fen == 1
    assert result.rounding_adjustment_fen == -1
    assert result.surtax_fen == 0


def test_return_cannot_use_another_rate_bucket_to_hide_its_negative_sales():
    with pytest.raises(NeedsInformation) as failure:
        assess([
            VatSales(net_sales_fen=100, accrued_vat_fen=1,
                     exemption_eligible=False, rate_percent="1"),
            VatSales(net_sales_fen=17, accrued_vat_fen=1,
                     exemption_eligible=False, rate_percent="3"),
            VatSales(net_sales_fen=-34, accrued_vat_fen=-1,
                     exemption_eligible=False, rate_percent="3"),
        ])
    assert failure.value.issues[0]["field"] == "tax_credit_disposition"


@pytest.mark.parametrize("kind", ["service_sale", "payment"])
def test_adopted_source_missing_rate_cannot_be_reinterpreted_by_period_policy(kind):
    fact = taxes.TaxAssessment(
        period="2026-01", period_start="2026-01-01", period_end="2026-01-31",
        vat_policy_id="vat", surtax_policy_id="surtax",
    )
    selections = dict.fromkeys(fact.reads(), ())
    for model, subject, policy in (
        (taxes.VatPolicyFact, "vat", vat_policy()),
        (taxes.SurtaxPolicyFact, "surtax", surtax_policy()),
    ):
        source = model(period="2026-01", policy=policy)
        selections[Read("fact", source.kind, "@" + subject)] = (
            FactVersion(subject + "-fact", subject, 1, source),
        )
    legacy_values = {
        "net_sales_fen": 100_000, "accrued_vat_fen": 3000, "exemption_eligible": False,
    }
    values = {"tax_sales": [legacy_values]} if kind == "payment" else legacy_values
    selections[Read("calculation", kind, "tax:2026-01")] = (
        Calculation("old-result", "old-source", kind, YearMonth("2026-01"), values, "old-fact"),
    )
    with pytest.raises(NeedsInformation) as failure:
        taxes.calculate_tax_assessment(
            FactVersion("assessment-fact", "assessment", 1, fact), Context(selections),
        )
    assert failure.value.issues[0]["field"] == "tax_source.rate_percent"
