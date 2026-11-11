"""Actual investment publishers, monthly tax positions and one VAT return in SQLite."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.ledger.public import LedgerQuery
from tests.modular.domains.financing._support import (
    BANK,
    COUNTERPARTY,
    OWNER,
    PRODUCT,
    SOURCE,
    assemble,
    fact,
    get,
    payment,
    request,
)

from ._fixtures import evidence, income_inputs, policy, surtax


def inputs():
    location = {"source_id": SOURCE}
    terms = fact(
        "investment.terms",
        {
            "product_id": PRODUCT,
            "product_type": "money_fund",
            "right_type": "fund_units",
            "measurement": "short_term_cost",
            "quantity_unit": "units",
            "holding_arrangement": "current",
            "terms_source": location,
        },
        period="2026-07",
    )
    acquired = fact(
        "investment.acquire",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": "10",
            "purchase_price_fen": "1000000",
            "acquisition_fees_fen": "100000",
            "separate_declared_income_fen": "0",
        },
        period="2026-07",
        actual="2026-07-01",
    )
    disposed = fact(
        "investment.dispose",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": "10",
            "gross_proceeds_fen": "1202000",
            "disposal_fees_fen": "0",
            "exit_mode": "transfer",
        },
        period="2026-07",
        actual="2026-07-02",
    )
    vat_policy = policy(rate="0.01", threshold="0")
    tax_basis = fact(
        "taxes.financial_transfer_basis",
        {
            "method": "weighted_average",
            "elected_on": "2026-07-01",
            "source": location,
            "vat_policy_ids": [vat_policy["business_id"]],
        },
        period="2026-07",
    )
    return terms, acquired, disposed, vat_policy, surtax(zero=True), tax_basis


def qualification(terms, *, instrument="securities_fund", **fields):
    return fact(
        "taxes.investment_basis",
        {
            "terms_id": terms["business_id"],
            "applicable_from": terms["period"] + "-01",
            "tax_facts": {
                "instrument_type": instrument,
                "source": {"source_id": SOURCE},
                **fields,
            },
        },
        period=terms["period"],
    )


def tax_values(app, company, basis, period):
    records = get(app, company, basis["business_id"])["businesses"][0]["results"]
    return next(
        row["target"]["values"]
        for row in records
        if row["reference"]["result_slot"] == "financial_transfer:" + period
    )


def income_adjustments(app, company, producer, slot):
    result = next(
        row
        for row in get(app, company, producer["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == slot
    )
    return [
        row
        for row in result["basis"]["adopted_quantities"]
        if row["type"] == "investment_income_adjustment"
    ]


def test_atomic_publish_month_without_return_and_open_replacement(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, disposed, vat_policy, surtax_policy, basis = inputs()
    body = request(
        company, terms, qualification(terms), acquired, disposed, vat_policy, surtax_policy, basis
    )
    body["sources"].append(evidence())
    result = app.accounting_apply(body)
    assert result.get("completion") == "complete", result
    assert tax_values(app, company, basis, "2026-07")["payable_tax_fen"] == "2000"
    assert journal_credit(app, company, "5111", "2026-07") == 100000
    (adjustment,) = income_adjustments(app, company, basis, "financial_transfer:2026-07")
    assert adjustment == {
        "type": "investment_income_adjustment",
        "component": "transfer_vat",
        "attribution": "company_pool",
        "amount_fen": "-2000",
    }

    amended = deepcopy(disposed)
    amended["data"]["gross_proceeds_fen"] = "1303000"
    change = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "replace", "expected_seq": 1, "fact": amended}],
    }
    result = app.accounting_apply(change)
    assert result.get("completion") == "complete", result
    current = tax_values(app, company, basis, "2026-07")
    assert current["current_difference_fen"] == "303000"
    assert current["payable_tax_fen"] == "3000"

    assessment = fact(
        "taxes.assessment",
        {
            "type": "vat",
            "start_month": "2026-07",
            "end_month": "2026-07",
            "vat_policy_id": vat_policy["business_id"],
            "surtax_policy_id": surtax_policy["business_id"],
            "filing_profile": {
                "taxpayer_type": "small_scale",
                "form_scope": "nonpilot",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-07",
    )
    result = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "create", "expected_seq": 0, "fact": assessment}],
        }
    )
    assert result.get("completion") == "complete", result
    records = get(app, company, assessment["business_id"])["businesses"][0]["results"]
    assert records[0]["target"]["values"]["payable_vat_fen"] == "3000"


def submit(app, company, *facts):
    result = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "create", "expected_seq": 0, "fact": row} for row in facts],
        }
    )
    assert result.get("completion") == "complete", result


def journal_credit(app, company, account, through):
    with app.storage.read_unit(company) as read:
        vouchers = app.ledger.query_vouchers(
            LedgerQuery(period_from="2026-01", period_to=through), read
        )
    return sum(
        int(line["credit_fen"]) - int(line["debit_fen"])
        for voucher in vouchers.items
        for line in voucher["lines"]
        if line["account_code"] == account
    )


def advance(month):
    return fact(
        "financing.owner_funding",
        {
            "owner_id": OWNER,
            "funding_kind": "loan",
            "amount_fen": "10000",
            "funds_account_id": BANK,
        },
        period=month,
        actual=month + "-15",
    )


def quarter_return(vat_policy, surtax_policy, start, end):
    return fact(
        "taxes.assessment",
        {
            "type": "vat",
            "start_month": start,
            "end_month": end,
            "vat_policy_id": vat_policy["business_id"],
            "surtax_policy_id": surtax_policy["business_id"],
            "filing_profile": {
                "taxpayer_type": "small_scale",
                "form_scope": "nonpilot",
                "source": {"source_id": SOURCE},
            },
        },
        period=end,
    )


def new_trade(terms, month, buy, sell):
    acquired = fact(
        "investment.acquire",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": "10",
            "purchase_price_fen": str(buy),
            "acquisition_fees_fen": "0",
            "separate_declared_income_fen": "0",
        },
        period=month,
        actual=month + "-01",
    )
    disposed = fact(
        "investment.dispose",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": "10",
            "gross_proceeds_fen": str(sell),
            "disposal_fees_fen": "0",
            "exit_mode": "transfer",
        },
        period=month,
        actual=month + "-02",
    )
    return acquired, disposed


def start_quarter(app, company):
    terms, acquired, disposed, vat_policy, surtax_policy, basis = inputs()
    acquired["data"]["acquisition_fees_fen"] = "0"
    disposed["data"]["gross_proceeds_fen"] = "2010000"
    vat_policy["data"]["threshold_period"] = "quarter"
    body = request(
        company, terms, qualification(terms), acquired, disposed, vat_policy, surtax_policy, basis
    )
    body["sources"].append(evidence())
    result = app.accounting_apply(body)
    assert result.get("completion") == "complete", result
    return terms, vat_policy, surtax_policy, basis


def test_real_quarter_loss_offsets_earlier_month_gain_once(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, vat_policy, surtax_policy, basis = start_quarter(app, company)
    submit(app, company, *new_trade(terms, "2026-08", 1010000, 0))
    submit(app, company, advance("2026-09"))
    september = tax_values(app, company, basis, "2026-09")
    assert september["payable_tax_fen"] == september["closing_loss_fen"] == "0"
    assert journal_credit(app, company, "222107", "2026-09") == 0
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-09")
    submit(app, company, current)
    assert (
        get(app, company, current["business_id"])["businesses"][0]["results"][0]["target"][
            "values"
        ]["payable_vat_fen"]
        == "0"
    )
    submit(app, company, *new_trade(terms, "2026-10", 1000000, 2010000))
    assert tax_values(app, company, basis, "2026-10")["payable_tax_fen"] == "10000"


@pytest.mark.parametrize("paid", [False, True])
def test_real_december_expires_credit_without_erasing_prior_unpaid_or_paid_tax(tmp_path, paid):
    app, company = assemble(tmp_path, taxes=True)
    terms, vat_policy, surtax_policy, basis = start_quarter(app, company)
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-09")
    submit(app, company, advance("2026-09"), current)
    assert tax_values(app, company, basis, "2026-09")["payable_tax_fen"] == "10000"
    submit(app, company, *new_trade(terms, "2026-10", 1010000, 0))
    if paid:
        submit(
            app,
            company,
            advance("2026-10"),
            payment(current, 10000, slot="financial_transfer_vat", day="2026-10-16"),
        )
    assert journal_credit(app, company, "222107", "2026-10") == (-10000 if paid else 0)
    submit(app, company, advance("2026-12"))
    december = tax_values(app, company, basis, "2026-12")
    assert december["expired_loss_fen"] == "1010000"
    assert december["closing_loss_fen"] == december["closing_tax_carry_fen"] == "0"
    assert journal_credit(app, company, "222107", "2026-12") == (0 if paid else 10000)
    (adjustment,) = income_adjustments(app, company, basis, "financial_transfer:2026-12")
    assert adjustment["attribution"] == "company_pool"
    assert adjustment["amount_fen"] == "-10000"


@pytest.mark.parametrize("recognized", [False, True])
def test_real_bank_wealth_maturity_uses_issuer_interest_composition_once(tmp_path, recognized):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, disposed, vat_policy, surtax_policy, basis = inputs()
    terms["data"].update(
        product_type="bank_wealth",
        right_type="financial_contract",
        measurement="other_financial_cost",
    )
    income = fact(
        "investment.income",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "interest",
            "amount_fen": "120000",
        },
        period="2026-07",
        actual="2026-07-02",
    )
    disposed["effective_on"] = "2026-07-03"
    disposed["data"].update(
        exit_mode="maturity",
        gross_proceeds_fen="1120000",
        maturity_income={
            "character": "interest",
            "amount_fen": "120000",
            "source": {"source_id": SOURCE, "location": "合成发行人本金10000、收益1200结算单"},
        },
    )
    if recognized:
        disposed["data"]["maturity_income"]["recognized_income_source"] = {
            "business_id": income["business_id"],
            "slot": "income",
        }
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-07")
    body = request(
        company,
        terms,
        qualification(terms, instrument="bank_wealth", principal_protected=True),
        acquired,
        *([income] if recognized else []),
        disposed,
        vat_policy,
        surtax_policy,
        basis,
        current,
    )
    body["sources"].append(evidence())
    next(obj for obj in body["objects"] if obj["object_id"] == PRODUCT)["data"]["category"] = (
        "bank_wealth"
    )
    result = app.accounting_apply(body)
    assert result.get("completion") == "complete", result
    july = tax_values(app, company, basis, "2026-07")
    assert july["current_difference_fen"] == "0"
    assert july["taxable_interest_gross_fen"] == "120000"
    assert july["interest_vat_fen"] == "1188"
    assert journal_credit(app, company, "222101", "2026-07") == 1188
    assert journal_credit(app, company, "222107", "2026-07") == 0
    assert journal_credit(app, company, "5111", "2026-07") == 18812
    (adjustment,) = income_adjustments(app, company, basis, "financial_transfer:2026-07")
    assert adjustment["attribution"] == "event"
    assert adjustment["component"] == "interest_vat"
    assert adjustment["amount_fen"] == "-1188"
    assert adjustment["terms_id"] == terms["business_id"]
    assert (
        adjustment["source"]["business_id"] == (income if recognized else disposed)["business_id"]
    )
    target = get(app, company, current["business_id"])["businesses"][0]["results"][0]["target"][
        "values"
    ]
    assert target["payable_vat_fen"] == "1188"


def test_real_partnership_annual_actual_preserves_taxed_retained_payout_capacity(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, _, _, _, _ = inputs()
    terms["data"].update(
        product_type="other_fund",
        right_type="partnership_interest",
        measurement="long_equity_cost",
        holding_arrangement="noncurrent",
        initial_holding_intent="long_term",
    )
    location = {"source_id": SOURCE}

    def income(year):
        return fact(
            "investment.income",
            {
                "terms_id": terms["business_id"],
                "counterparty_id": COUNTERPARTY,
                "income_character": "market_distribution",
                "amount_fen": "2000",
            },
            period=f"{year}-08",
            actual=f"{year}-08-01",
        )

    def allocation(year, income_fact, taxable="0", retained="0", claims=None):
        data = {
            "investment_id": terms["business_id"],
            "tax_year": year,
            "allocated_taxable_income_fen": taxable,
            "distributed_income_fen": "2000",
            "retained_income_fen": retained,
            "source": location,
            "distributions": [
                {
                    "source": {"business_id": income_fact["business_id"], "slot": "main"},
                    "amount_fen": "2000",
                }
            ],
        }
        if claims:
            data["previously_taxed_distributions"] = claims
        return fact("taxes.partnership_allocation", data, period=f"{year}-12")

    first_income = income(2026)
    first = allocation(2026, first_income, "5000", "3000")
    annual = fact(
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "annual_settlement",
            "tax_year": 2026,
            "tax_period_end": "2026-12",
            "cumulative_tax_fen": "1250",
            "source": location,
            "partnership_inclusions": [
                {
                    "allocation_id": first["business_id"],
                    "included_income_fen": "5000",
                    "taxed_retained_income_fen": "3000",
                }
            ],
        },
        period="2027-05",
    )
    actual = fact(
        "taxes.actual_declaration",
        {
            "type": "income_tax",
            "assessment_id": annual["business_id"],
            "tax_year": 2026,
            "tax_period_end": "2026-12",
            "basis_type": "filed_return",
            "source": location,
            "cumulative_tax_fen": "1250",
            "prepaid_tax_fen": "0",
            "tax_due_fen": "1250",
        },
        period="2027-05",
    )
    body = request(company, terms, acquired, first_income, first, annual, actual)
    next(obj for obj in body["objects"] if obj["object_id"] == PRODUCT)["data"]["category"] = (
        "other_fund"
    )
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response.get("message", response)
    result = next(
        row
        for row in get(app, company, annual["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "main"
    )
    assert result["target"]["values"]["adopted_basis"] == "actual"
    assert (
        next(
            q
            for q in result["basis"]["adopted_quantities"]
            if q["type"] == "partnership_tax_inclusion"
        )["taxed_retained_income_fen"]
        == "3000"
    )
    later_income = income(2027)
    claim = {
        "allocation_id": first["business_id"],
        "tax_assessment_id": annual["business_id"],
        "income_source": {"business_id": later_income["business_id"], "slot": "main"},
        "amount_fen": "2000",
    }
    later = allocation(2027, later_income, claims=[claim])
    submit(app, company, later_income, later)
    result = get(app, company, later["business_id"])["businesses"][0]["results"][0]
    assert result["target"]["values"]["annual_adjustment_fen"] == "-2000"
    assert result["target"]["values"]["previously_taxed_distribution_fen"] == "2000"
    final_income = income(2028)
    final = allocation(
        2028,
        final_income,
        claims=[
            {**claim, "income_source": {"business_id": final_income["business_id"], "slot": "main"}}
        ],
    )
    response = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "create", "expected_seq": 0, "fact": row} for row in (final_income, final)
            ],
        }
    )
    assert "累计超过" in str(response)
    assert response.get("completion") != "complete"


def test_real_monthly_weighted_sale_before_split_and_later_purchase_needs_no_new_facts(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, sold, vat_policy, surtax_policy, basis = inputs()
    terms["period"] = acquired["period"] = "2026-06"
    acquired["effective_on"] = "2026-06-01"
    acquired["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    sold["effective_on"] = "2026-07-05"
    sold["data"].update(quantity="5", gross_proceeds_fen="20000")
    split = fact(
        "investment.unit_change",
        {
            "terms_id": terms["business_id"],
            "source": {"business_id": acquired["business_id"], "slot": "cost"},
            "quantity_after": "10",
        },
        period="2026-07",
        actual="2026-07-10",
    )
    later = fact(
        "investment.acquire",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": "10",
            "purchase_price_fen": "20000",
            "acquisition_fees_fen": "0",
            "separate_declared_income_fen": "0",
        },
        period="2026-07",
        actual="2026-07-20",
    )
    body = request(
        company,
        terms,
        qualification(terms),
        acquired,
        sold,
        split,
        later,
        vat_policy,
        surtax_policy,
        basis,
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    values = tax_values(app, company, basis, "2026-07")
    assert values["disposals"][0]["vat_buy_price_fen"] == "10000"
    assert values["positions"][0]["vat_basis_fen"] == "20000"
    assert values["payable_tax_fen"] == "99"


@pytest.mark.parametrize(
    "second_class,cost,tax,pool_count", [("A", "10000", "0", 1), ("C", "5000", "50", 2)]
)
def test_real_same_product_accounts_share_only_the_same_financial_instrument_pool(
    tmp_path, second_class, cost, tax, pool_count
):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, sold, vat_policy, surtax_policy, basis = inputs()
    tax_basis = qualification(terms)
    tax_basis["data"]["financial_instrument_reference"] = {
        "reference": "share-class:A",
        "source": {"source_id": SOURCE},
    }
    acquired["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    other_terms = deepcopy(terms)
    other_terms["business_id"] = str(uuid4())
    other_basis = qualification(other_terms)
    other_basis["data"]["financial_instrument_reference"] = deepcopy(
        tax_basis["data"]["financial_instrument_reference"]
    )
    other_basis["data"]["financial_instrument_reference"]["reference"] = (
        "share-class:" + second_class
    )
    other = deepcopy(acquired)
    other["business_id"] = str(uuid4())
    other["effective_on"] = "2026-07-02"
    other["data"].update(terms_id=other_terms["business_id"], purchase_price_fen="30000")
    sold["effective_on"] = "2026-07-03"
    sold["data"].update(quantity="5", gross_proceeds_fen="10000")
    body = request(
        company,
        terms,
        other_terms,
        tax_basis,
        other_basis,
        acquired,
        other,
        sold,
        vat_policy,
        surtax_policy,
        basis,
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    values = tax_values(app, company, basis, "2026-07")
    assert values["disposals"][0]["vat_buy_price_fen"] == cost
    assert values["disposals"][0]["eit_basis_fen"] == "5000"
    assert values["payable_tax_fen"] == tax
    assert len(values["positions"]) == pool_count


@pytest.mark.parametrize("reference_available", [False, True])
def test_real_actual_vat_components_accept_true_ordinary_residual(tmp_path, reference_available):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, disposed, vat_policy, surtax_policy, basis = inputs()
    acquired["data"].update(purchase_price_fen="10000", acquisition_fees_fen="1000")
    disposed["data"]["gross_proceeds_fen"] = "12020"
    sale = fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": "10100",
            "vat": {
                "policy_id": vat_policy["business_id"],
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
                "waive_exemption": False,
                "waive_rate_reduction": False,
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-07",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-07",
        actual="2026-07-03",
    )
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-07")
    if not reference_available:
        current["data"].pop("vat_policy_id")
        current["data"].pop("surtax_policy_id")
    actual = fact(
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": current["business_id"],
            "start_month": "2026-07",
            "end_month": "2026-07",
            "basis_type": "filed_return",
            "source": {"source_id": SOURCE},
            "assessed_vat_fen": "110",
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            "exemption_relief_fen": "10",
            "financial_transfer_vat_fen": "20",
            "investment_interest_vat_fen": "0",
        },
        period="2026-07",
    )
    body = request(
        company,
        terms,
        qualification(terms),
        acquired,
        disposed,
        sale,
        vat_policy,
        surtax_policy,
        basis,
        current,
        actual,
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    values = get(app, company, current["business_id"])["businesses"][0]["results"][0]["target"][
        "values"
    ]
    assert values["payable_vat_fen"] == "110"
    assert journal_credit(app, company, "222101", "2026-07") == 90
    assert journal_credit(app, company, "222107", "2026-07") == 20
    assert journal_credit(app, company, "6301", "2026-07") == 10


def government_terms(terms, acquired, issued):
    terms["data"].update(
        product_type="bond",
        right_type="direct_bond",
        measurement="long_bond_cost",
        quantity_unit="nominal_fen",
        holding_arrangement="noncurrent",
        initial_holding_intent="long_term",
        coupon_schedule=[{"payment_on": "2026-12-31", "coupon_rate": "0.04"}],
    )
    adopted = qualification(
        terms,
        instrument="government_bond",
        **{
            "original_issue_on": issued,
            "maturity_on": "2026-12-31",
            "source": {"source_id": SOURCE},
            "government_bond_eit": {
                "amount_basis": "face_value",
                "rate_periods": [
                    {"start_on": "2026-01-01", "end_before": "2027-01-01", "annual_rate": "0.04"}
                ],
                "source": {"source_id": SOURCE},
            },
        },
    )
    acquired["data"].update(
        quantity="100000",
        purchase_price_fen="102000",
        acquisition_fees_fen="0",
        acquisition_origin="secondary_market",
    )
    return adopted


@pytest.mark.parametrize(
    "issued,vat,exempt,taxable",
    [("2024-01-01", 0, "2005", "9995"), ("2026-01-01", 40, "1985", "9975")],
)
def test_real_secondary_government_coupon_uses_183_days_and_its_own_vat_share(
    tmp_path, issued, vat, exempt, taxable
):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, _, vat_policy, surtax_policy, basis = inputs()
    adopted = government_terms(terms, acquired, issued)
    coupon = fact(
        "investment.debt_interest",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "source": {"business_id": acquired["business_id"], "slot": "cost"},
            "payment_on": "2026-12-31",
        },
        period="2026-12",
        actual="2026-12-31",
    )
    sale = fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": "10100",
            "vat": {
                "policy_id": vat_policy["business_id"],
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-12",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-12",
        actual="2026-12-30",
    )
    eit, prepayment, eit_policy = income_inputs()
    for value in (eit, prepayment):
        value["period"] = value["data"]["quarter_end"] = "2026-12"
    prepayment["data"]["treatment"] = "standard"
    body = request(
        company,
        terms,
        adopted,
        acquired,
        coupon,
        sale,
        vat_policy,
        surtax_policy,
        basis,
        eit,
        prepayment,
        eit_policy,
    )
    body["sources"].append(evidence())
    next(row for row in body["objects"] if row["object_id"] == PRODUCT)["data"]["category"] = "bond"
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    result = next(
        row
        for row in get(app, company, eit["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "main"
    )
    assert result["target"]["values"]["investment_exempt_income_fen"] == exempt
    assert result["target"]["values"]["taxable_profit_fen"] == taxable
    assert tax_values(app, company, basis, "2026-12")["interest_vat_fen"] == str(vat)
    (realization,) = [
        row
        for row in result["basis"]["adopted_quantities"]
        if row["type"] == "government_bond_interest_realization"
    ]
    assert realization["gross_exempt_interest_fen"] == "2005"
    if vat:
        (allocation,) = [
            row
            for row in result["basis"]["adopted_quantities"]
            if row["type"] == "investment_relief_vat_allocation"
        ]
        assert allocation["allocated_vat_fen"] == "20"


def test_real_government_early_transfer_realizes_91_day_interest_without_extra_book_income(
    tmp_path,
):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, sold, vat_policy, surtax_policy, basis = inputs()
    adopted = government_terms(terms, acquired, "2024-01-01")
    sold["period"], sold["effective_on"] = "2026-09", "2026-09-30"
    sold["data"].update(quantity="100000", gross_proceeds_fen="103000")
    eit, prepayment, eit_policy = income_inputs()
    for value in (eit, prepayment):
        value["period"] = value["data"]["quarter_end"] = "2026-09"
    prepayment["data"]["treatment"] = "standard"
    body = request(
        company,
        terms,
        adopted,
        acquired,
        sold,
        vat_policy,
        surtax_policy,
        basis,
        eit,
        prepayment,
        eit_policy,
    )
    body["sources"].append(evidence())
    next(row for row in body["objects"] if row["object_id"] == PRODUCT)["data"]["category"] = "bond"
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    result = next(
        row
        for row in get(app, company, eit["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "main"
    )
    assert result["target"]["values"]["investment_exempt_income_fen"] == "997"
    assert result["target"]["values"]["taxable_profit_fen"] == "0"
    assert journal_credit(app, company, "5111", "2026-09") == 990
    assert tax_values(app, company, basis, "2026-09")["payable_tax_fen"] == "10"
    (realization,) = [
        row
        for row in result["basis"]["adopted_quantities"]
        if row["type"] == "government_bond_interest_realization"
    ]
    assert realization["gross_exempt_interest_fen"] == "997"


@pytest.mark.parametrize(
    "reference,total,transfer,interest,relief,ordinary,other_income,investment_income,net_return",
    [
        (True, "0", "0", None, "120", 0, 100, 2020, False),
        (False, "0", "0", None, "120", 0, 100, 2020, False),
        (True, "110", "20", None, "10", 90, 10, 2000, False),
        (False, "110", "10", None, "0", 100, 0, 2010, False),
        (True, "130", "0", "30", "20", 100, 0, 4010, False),
        (True, "0", "0", None, "20", 0, 0, 2020, True),
    ],
)
def test_real_actual_exemption_keeps_whole_period_amount_and_books_only_proven_ordinary_part(
    tmp_path,
    reference,
    total,
    transfer,
    interest,
    relief,
    ordinary,
    other_income,
    investment_income,
    net_return,
):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, disposed, vat_policy, surtax_policy, basis = inputs()
    acquired["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    disposed["data"]["gross_proceeds_fen"] = "12020"
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
                "tax_period": "2026-07",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-07",
        actual="2026-07-03",
    )
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-07")
    if not reference:
        current["data"].pop("vat_policy_id")
        current["data"].pop("surtax_policy_id")
    actual = fact(
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": current["business_id"],
            "start_month": "2026-07",
            "end_month": "2026-07",
            "basis_type": "filed_return",
            "source": {"source_id": SOURCE},
            "assessed_vat_fen": total,
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            "exemption_relief_fen": relief,
            "financial_transfer_vat_fen": transfer,
            "investment_interest_vat_fen": interest or "0",
        },
        period="2026-07",
    )
    extra = []
    if net_return:
        sale["period"], sale["effective_on"] = "2026-06", "2026-06-03"
        sale["data"]["vat"]["exemption_eligible"] = False
        sale["data"]["tax_point"]["tax_period"] = "2026-06"
        extra.append(
            fact(
                "operations.sale_return",
                {
                    "sale_id": sale["business_id"],
                    "customer_id": COUNTERPARTY,
                    "gross_fen": "10100",
                    "credit_note_vat_fen": "100",
                    "original_tax_period": "2026-06",
                },
                period="2026-07",
                actual="2026-07-03",
            )
        )
    if interest is not None:
        interest_terms = deepcopy(terms)
        interest_terms["business_id"] = str(uuid4())
        interest_terms["data"].update(
            product_id=str(uuid4()),
            product_type="bank_wealth",
            right_type="financial_contract",
            measurement="other_financial_cost",
        )
        income = fact(
            "investment.income",
            {
                "terms_id": interest_terms["business_id"],
                "counterparty_id": COUNTERPARTY,
                "income_character": "interest",
                "amount_fen": "2020",
            },
            period="2026-07",
            actual="2026-07-04",
        )
        extra = [
            interest_terms,
            income,
            qualification(interest_terms, instrument="bank_wealth", principal_protected=True),
        ]
    body = request(
        company,
        terms,
        qualification(terms),
        acquired,
        disposed,
        sale,
        vat_policy,
        surtax_policy,
        basis,
        current,
        actual,
        *extra,
    )
    body["sources"].append(evidence())
    if interest is not None:
        product = deepcopy(next(row for row in body["objects"] if row["object_id"] == PRODUCT))
        product["object_id"] = extra[0]["data"]["product_id"]
        product["data"]["category"] = "bank_wealth"
        body["objects"].append(product)
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    result = next(
        row
        for row in get(app, company, current["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "main"
    )
    values = result["target"]["values"]
    assert values["payable_vat_fen"] == total
    assert values["relief_fen"] == relief
    if net_return:
        assert values["accrued_vat_fen"] == "-80"
    assert journal_credit(app, company, "222101", "2026-07") == ordinary + int(interest or "0")
    assert journal_credit(app, company, "222107", "2026-07") == int(transfer)
    assert journal_credit(app, company, "6301", "2026-07") == other_income
    assert journal_credit(app, company, "5111", "2026-07") == investment_income
    assert journal_credit(app, company, "571105", "2026-07") == 0
    if int(total):
        assert "rounding_adjustment_fen" not in values


@pytest.mark.parametrize(
    "transfer,interest,include_trade,issue_field",
    [
        ("20", "0", False, "/data/financial_transfer_vat_fen"),
        ("0", "20", False, "/data/investment_interest_vat_fen"),
        ("0", "0", False, None),
        ("20", "0", True, "/data"),
    ],
)
def test_real_actual_investment_parts_cannot_be_ignored_without_monthly_positions(
    tmp_path, transfer, interest, include_trade, issue_field
):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, disposed, vat_policy, surtax_policy, _ = inputs()
    sale = fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": "10100",
            "vat": {
                "policy_id": vat_policy["business_id"],
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-07",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-07",
        actual="2026-07-03",
    )
    current = quarter_return(vat_policy, surtax_policy, "2026-07", "2026-07")
    actual = fact(
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": current["business_id"],
            "start_month": "2026-07",
            "end_month": "2026-07",
            "basis_type": "filed_return",
            "source": {"source_id": SOURCE},
            "assessed_vat_fen": "120" if include_trade else "100",
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            "financial_transfer_vat_fen": transfer,
            "investment_interest_vat_fen": interest,
        },
        period="2026-07",
    )
    body = request(
        company,
        sale,
        vat_policy,
        surtax_policy,
        current,
        actual,
        *([terms, qualification(terms), acquired, disposed] if include_trade else []),
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    if issue_field is None:
        assert response.get("completion") == "complete", response
        assert journal_credit(app, company, "222101", "2026-07") == 100
        assert journal_credit(app, company, "222107", "2026-07") == 0
        return
    assert response.get("completion") == "not_saved", response
    assert response["error"] == "needs_information"
    (issue,) = response["fact_issues"]
    assert issue["field"] == issue_field
    assert issue["business_id"] == (current if include_trade else actual)["business_id"]
    if include_trade:
        assert "买入价方法" in issue["message"]
