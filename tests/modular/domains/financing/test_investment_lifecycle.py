"""Public lifecycle checks cover effects across lots, months and adopted versions."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.domains.financing.public import create_investment

from ._investment_support import acquire, dispose, source, terms
from ._support import (
    COUNTERPARTY,
    OWNER,
    PRODUCT,
    SOURCE,
    assemble,
    change,
    fact,
    payment,
    request,
    result,
    six,
)


def apply_facts(app, company, *facts, initial=False):
    payload = request(company, *facts)
    if initial:
        agreements = [item for item in facts if item["kind"] == "investment.terms"]
        category = agreements[0]["data"]["product_type"]
        for obj in payload["objects"]:
            if obj["object_id"] == PRODUCT:
                obj["data"]["category"] = category
    else:
        payload.pop("objects")
        payload.pop("sources")
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", actual
    return actual


def activity(app, company, month="2026-01"):
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    with app.storage.read_unit(company) as read:
        return owner.investment_activity(
            {
                "company_id": company,
                "period_from": month,
                "period_to": month,
                "view": "current",
            },
            read,
        )


def test_fifo_uses_real_batches_and_candidate_lot_use(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    first = acquire(agreement, "100", "3", "2026-01-02")
    second = acquire(agreement, "200", "3", "2026-01-03")
    sold = dispose(agreement, "4", "300", "2026-01-05")
    apply_facts(app, company, agreement, first, second, sold, initial=True)
    values = result(app, company, sold)["target"]["values"]
    assert values["cost_fen"] == "167"
    assert [(row["source"], row["quantity"]) for row in values["allocations"]] == [
        (source(first), "3"),
        (source(second), "1"),
    ]


def test_real_return_resets_interval_but_keeps_earlier_exit(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchased = acquire(agreement, "10000", "10")
    first = dispose(agreement, "4", "4200", "2026-01-04")
    returned = fact(
        "investment.capital_return",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "source": source(purchased),
            "amount_fen": "1200",
        },
        actual="2026-01-05",
    )
    last = dispose(agreement, "3", "2500", "2026-01-06")
    apply_facts(app, company, agreement, purchased, first, returned, last, initial=True)
    assert result(app, company, first)["target"]["values"]["cost_fen"] == "4000"
    assert result(app, company, last)["target"]["values"]["cost_fen"] == "2400"
    product = activity(app, company).products[0]
    assert product.quantity == "3"
    assert product.closing_cost == {"state": "known", "amount_fen": "2400"}


def test_market_estimate_never_revalues_cost_or_creates_income(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchased = acquire(agreement, "10000", "10")
    estimate = fact(
        "investment.market_estimate",
        {
            "terms_id": agreement["business_id"],
            "estimated_value_fen": "7500",
            "as_of_on": "2026-01-31",
            "estimate_source": {"source_id": SOURCE, "location": "合成对账单市场估值"},
        },
    )
    apply_facts(app, company, agreement, purchased, estimate, initial=True)
    body = result(app, company, estimate)
    assert body["vouchers"] == []
    assert "tax_event" not in body["target"]["values"]
    product = activity(app, company).products[0]
    assert product.closing_cost == {"state": "known", "amount_fen": "10000"}
    assert product.market_value_fen == 7500
    assert product.investment_income_fen == 0


def test_same_product_different_holdings_are_separate(tmp_path):
    app, company = assemble(tmp_path)
    first = terms()
    second = terms(measurement="other_financial_cost")
    apply_facts(
        app, company, first, second, acquire(first, "100"), acquire(second, "200"), initial=True
    )
    rows = activity(app, company).products
    assert len(rows) == 2
    assert {row.terms_id: row.closing_cost["amount_fen"] for row in rows} == {
        first["business_id"]: "100",
        second["business_id"]: "200",
    }
    assert {row.holding_arrangement for row in rows} == {"current", "noncurrent"}


def test_holding_change_preserves_earlier_classification(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms(measurement="other_financial_cost")
    agreement["data"]["holding_arrangement"] = "current"
    purchased = acquire(agreement, "1000")
    apply_facts(app, company, agreement, purchased, initial=True)
    moved = fact(
        "investment.holding_change",
        {
            "terms_id": agreement["business_id"],
            "holding_arrangement": "noncurrent",
            "effective_from": "2026-02-01",
            "change_source": {"source_id": SOURCE, "location": "合成持有安排变更"},
        },
        period="2026-02",
    )
    apply_facts(app, company, moved)
    body = result(app, company, moved)
    assert {
        (line["account_code"], line["debit_fen"], line["credit_fen"])
        for voucher in body["vouchers"]
        for line in voucher["lines"]
    } == {
        ("149901", "0", "1000"),
        ("189902", "1000", "0"),
    }
    assert activity(app, company).products[0].holding_arrangement == "current"
    assert activity(app, company, "2026-02").products[0].holding_arrangement == "noncurrent"


def test_two_coupon_public_results_preserve_principal(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms(
        product_type="bond",
        measurement="long_bond_cost",
        schedule=[
            {"payment_on": "2026-06-30", "coupon_rate": "0.1"},
            {"payment_on": "2026-12-31", "coupon_rate": "0.1"},
        ],
    )
    purchased = acquire(agreement, "102000", "100000")
    coupons = [
        fact(
            "investment.debt_interest",
            {
                "terms_id": agreement["business_id"],
                "source": source(purchased),
                "counterparty_id": COUNTERPARTY,
                "payment_on": day,
            },
            period=day[:7],
            actual=day,
        )
        for day in ("2026-06-30", "2026-12-31")
    ]
    apply_facts(app, company, agreement, purchased, *coupons, initial=True)
    assert [
        (
            result(app, company, item)["target"]["values"]["amount_fen"],
            result(app, company, item)["target"]["values"]["gain_loss_fen"],
        )
        for item in coupons
    ] == [("10000", "9000"), ("10000", "9000")]
    assert activity(app, company, "2026-12").products[0].closing_cost == {
        "state": "known",
        "amount_fen": "100000",
    }


def test_withdrawal_reallocates_later_cost_and_blocks_orphan_sources(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchased = acquire(agreement)
    first, last = dispose(agreement), dispose(agreement, day="2026-01-05")
    apply_facts(app, company, agreement, purchased, first, last, initial=True)
    withdrawn = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "business_id": first["business_id"], "expected_seq": 1}],
    }
    actual = app.accounting_apply(withdrawn)
    assert actual["completion"] == "complete", actual
    assert result(app, company, last)["target"]["values"]["cost_fen"] == "33"
    withdrawn["command_id"] = str(uuid4())
    withdrawn["mutations"] = [
        {"op": "withdraw", "business_id": purchased["business_id"], "expected_seq": 1}
    ]
    rejected = app.accounting_apply(withdrawn)
    assert rejected["error"] == "dependency_conflict", rejected


def test_closed_cost_correction_keeps_frozen_body(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchased, sold = acquire(agreement), dispose(agreement)
    apply_facts(app, company, agreement, purchased, sold, initial=True)
    original = deepcopy(result(app, company, sold))
    with app.storage.write_unit(company) as uow:
        uow.repository("ledger").freeze_heads("2026-01")
    altered = deepcopy(purchased)
    altered["data"]["purchase_price_fen"] = "120"
    actual = app.accounting_apply(change(company, altered, correction_period="2026-02"))
    assert actual["completion"] == "complete", actual
    current = result(app, company, sold)
    assert current["target"]["values"]["cost_fen"] == "40"
    assert current["reference"]["baseline_result_id"] == original["reference"]["result_id"]


def test_silent_month_omits_cleared_holding_but_keeps_income_only_right(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchased, sold = acquire(agreement, "1", "1"), dispose(agreement, "1", "3")
    paid = payment(purchased, 1, slot="primary", party=COUNTERPARTY, day="2026-01-03")
    received = payment(
        sold, 3, slot="primary", party=COUNTERPARTY, direction="inflow", day="2026-01-05"
    )
    apply_facts(app, company, agreement, purchased, sold, paid, received, six()[0], initial=True)
    later = terms()
    income = fact(
        "investment.income",
        {
            "terms_id": later["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "5",
        },
        period="2026-02",
        actual="2026-02-01",
    )
    apply_facts(app, company, later, income)
    rows = activity(app, company, "2026-02").products
    assert len(rows) == 1
    assert rows[0].terms_id == later["business_id"]
    assert rows[0].quantity is None and rows[0].activity_only
    assert rows[0].accrued_income_fen == 5


def test_rights_loss_is_separate_from_market_estimate(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchased = acquire(agreement, "100")
    lost = fact(
        "investment.loss",
        {
            "terms_id": agreement["business_id"],
            "source": source(purchased),
            "counterparty_id": COUNTERPARTY,
            "quantity": "3",
            "loss_basis": "legal_extinction",
            "recovered_fen": "20",
        },
        actual="2026-01-05",
    )
    apply_facts(app, company, agreement, purchased, lost, initial=True)
    body = result(app, company, lost)
    assert body["target"]["values"]["gain_loss_fen"] == "-80"
    assert any(
        line["account_code"] == "571108" and line["debit_fen"] == "80"
        for voucher in body["vouchers"]
        for line in voucher["lines"]
    )
    assert activity(app, company).products[0].quantity == "0"


def test_issuer_maturity_income_and_existing_right_are_not_duplicated(tmp_path):
    for recognized in (False, True):
        app, company = assemble(tmp_path / str(recognized))
        agreement = terms(product_type="bank_wealth", measurement="other_financial_cost")
        agreement["data"].update(
            legal_form="financial_contract",
            tax_facts={
                "instrument_type": "bank_wealth",
                "principal_protected": True,
                "source": {"source_id": SOURCE, "location": "合成保本条款"},
            },
        )
        purchased = acquire(agreement, "10000", "100")
        purchased["data"]["acquisition_fees_fen"] = "1000"
        income = fact(
            "investment.income",
            {
                "terms_id": agreement["business_id"],
                "counterparty_id": COUNTERPARTY,
                "income_character": "interest",
                "amount_fen": "1200",
            },
            actual="2026-01-04",
        )
        maturity = dispose(agreement, "100", "11200", "2026-01-05")
        maturity["data"].update(
            exit_mode="maturity",
            maturity_income={
                "character": "interest",
                "amount_fen": "1200",
                "source": {"source_id": SOURCE, "location": "合成发行人本金10000及收益1200结算单"},
            },
        )
        if recognized:
            maturity["data"]["maturity_income"]["recognized_income_source"] = {
                "business_id": income["business_id"],
                "slot": "income",
            }
        apply_facts(
            app,
            company,
            agreement,
            purchased,
            *([income] if recognized else []),
            maturity,
            initial=True,
        )
        body = result(app, company, maturity)
        values = body["target"]["values"]
        assert values["gain_loss_fen"] == ("-1000" if recognized else "200")
        assert values["tax_event"]["maturity_income"]["amount_fen"] == "1200"
        assert activity(app, company).products[0].investment_income_fen == 200
        for voucher in body["vouchers"]:
            assert (
                sum(int(line["debit_fen"]) - int(line["credit_fen"]) for line in voucher["lines"])
                == 0
            )
        paid = payment(purchased, 11000, slot="primary", party=COUNTERPARTY, day="2026-01-03")
        received = payment(
            maturity,
            11200,
            slot="primary",
            party=COUNTERPARTY,
            direction="inflow",
            day="2026-01-06",
        )
        received["data"]["allocations"] = [
            {
                "source": {"business_id": maturity["business_id"], "slot": "primary"},
                "amount_fen": "10000",
            },
            {
                "source": {
                    "business_id": (income if recognized else maturity)["business_id"],
                    "slot": "income",
                },
                "amount_fen": "1200",
            },
        ]
        apply_facts(app, company, paid, received, six()[0])
        view = activity(app, company)
        assert view.actual_receipts_fen == 11200
        assert {item.kind for item in view.events if item.settlement_fen is not None} == {
            "acquisition_payment",
            "disposal_receipt",
            "income_receipt",
        }
        assert view.products[0].pending_receipts_fen == 0
        with app.storage.read_unit(company) as read:
            snapshot = app.ledger.financial_snapshot(
                {
                    "company_id": company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "current",
                },
                read,
            )
        assert snapshot["cash_flow_statement"]["8"]["current"]["amount_fen"] == "10000"
        assert snapshot["cash_flow_statement"]["9"]["current"]["amount_fen"] == "1200"


@pytest.mark.parametrize(
    ("category", "measurement", "income_character"),
    [
        ("money_fund", "short_term_cost", "fund_distribution"),
        ("bank_wealth", "other_financial_cost", "interest"),
        ("other_fund", "other_financial_cost", "fund_distribution"),
        ("bond", "long_bond_cost", "interest"),
        ("equity", "long_equity_cost", "dividend"),
    ],
)
def test_five_financial_categories_keep_sourced_lifecycle_and_frozen_corrections(
    tmp_path,
    category,
    measurement,
    income_character,
):
    app, company = assemble(tmp_path)
    schedule = [{"payment_on": "2026-06-30", "coupon_rate": "0.1"}] if category == "bond" else None
    agreement = terms(product_type=category, measurement=measurement, schedule=schedule)
    if category == "bank_wealth":
        agreement["data"].update(
            legal_form="financial_contract",
            tax_facts={
                "instrument_type": "bank_wealth",
                "principal_protected": True,
                "source": {"source_id": SOURCE, "location": "合成理财产品法律权利"},
            },
        )
    if category == "equity":
        agreement["data"]["quantity_unit"] = "shares"
    purchased = acquire(
        agreement,
        "102000" if category == "bond" else "100",
        "100000" if category == "bond" else "3",
    )
    if category == "bond":
        income = fact(
            "investment.debt_interest",
            {
                "terms_id": agreement["business_id"],
                "source": source(purchased),
                "counterparty_id": COUNTERPARTY,
                "payment_on": "2026-06-30",
                "transaction_sequence": 1,
            },
            period="2026-06",
            actual="2026-06-30",
        )
        sold = dispose(agreement, "100000", "100000", "2026-06-30")
        sold["data"].update(exit_mode="maturity", transaction_sequence=2)
        subject, frozen_month, correction_month = income, "2026-06", "2026-07"
    else:
        income = fact(
            "investment.income",
            {
                "terms_id": agreement["business_id"],
                "counterparty_id": COUNTERPARTY,
                "income_character": income_character,
                "amount_fen": "10",
            },
            actual="2026-01-04",
        )
        sold = dispose(agreement, "3", "120", "2026-01-05")
        subject, frozen_month, correction_month = sold, "2026-01", "2026-02"
    apply_facts(app, company, agreement, purchased, income, sold, initial=True)
    assert result(app, company, sold)["target"]["values"]["remaining_quantity"] == "0"
    altered = deepcopy(purchased)
    altered["data"]["purchase_price_fen"] = "104000" if category == "bond" else "120"
    updated = app.accounting_apply(change(company, altered))
    assert updated["completion"] == "complete", updated
    before = result(app, company, subject)
    assert before["target"]["values"]["gain_loss_fen"] == ("6000" if category == "bond" else "0")
    with app.storage.write_unit(company) as uow:
        uow.repository("ledger").freeze_heads(frozen_month)
    altered["data"]["purchase_price_fen"] = "106000" if category == "bond" else "150"
    corrected = app.accounting_apply(
        change(company, altered, seq=2, correction_period=correction_month)
    )
    assert corrected["completion"] == "complete", corrected
    after = result(app, company, subject)
    assert after["target"]["values"]["gain_loss_fen"] == ("4000" if category == "bond" else "-30")
    assert {voucher["role"] for voucher in after["vouchers"]} == {"reversal", "replacement"}
    assert after["reference"]["baseline_result_id"] == before["reference"]["result_id"]
    with app.storage.read_unit(company, view="as_posted", as_of=frozen_month) as read:
        (frozen,) = app.ledger.get_results([subject["business_id"]], read)
    assert frozen["target"]["values"] == before["target"]["values"]


@pytest.mark.parametrize(
    ("valid_from", "valid_through", "accepted"),
    [
        ("2026-01-15", "2026-01", True),
        ("2026-01", "2026-01-15", True),
        ("2026-02", "2026-01-31", False),
    ],
)
def test_authorization_mixed_precision_uses_complete_month_boundaries(
    tmp_path,
    valid_from,
    valid_through,
    accepted,
):
    app, company = assemble(tmp_path)
    agreement = terms()
    agreement["data"]["settlement_authorizations"] = [
        {
            "entry_key": "manager",
            "principal_party_id": COUNTERPARTY,
            "agent_id": OWNER,
            "purposes": ["acquisition_payment"],
            "valid_from": valid_from,
            "valid_through": valid_through,
            "source": {"source_id": SOURCE, "location": "合成代理结算授权有效期"},
        }
    ]
    actual = app.accounting_apply(request(company, agreement))
    assert (actual.get("completion") == "complete") is accepted, actual
    if not accepted:
        assert actual["error"] == "invalid_input"


def test_bond_maturity_requires_coupon_but_not_actual_interest_receipt(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms(
        product_type="bond",
        measurement="long_bond_cost",
        schedule=[
            {"payment_on": "2026-06-30", "coupon_rate": "0.1"},
        ],
    )
    purchased = acquire(agreement, "102000", "100000")
    matured = dispose(agreement, "100000", "100000", "2026-06-30")
    matured["data"]["exit_mode"] = "maturity"
    payload = request(company, agreement, purchased, matured)
    for obj in payload["objects"]:
        if obj["object_id"] == PRODUCT:
            obj["data"]["category"] = "bond"
    missing_coupon = app.accounting_apply(payload)
    assert missing_coupon["error"] == "needs_information", missing_coupon
    coupon = fact(
        "investment.debt_interest",
        {
            "terms_id": agreement["business_id"],
            "source": source(purchased),
            "counterparty_id": COUNTERPARTY,
            "payment_on": "2026-06-30",
        },
        period="2026-06",
    )
    payload["command_id"] = str(uuid4())
    payload["mutations"].append({"op": "create", "expected_seq": 0, "fact": coupon})
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", actual
    assert result(app, company, coupon)["target"]["values"]["gain_loss_fen"] == "8000"
    assert result(app, company, matured)["target"]["values"]["gain_loss_fen"] == "0"
    assert activity(app, company, "2026-06").products[0].accrued_income_fen == 10000
    bypass = fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "interest",
            "amount_fen": "10000",
        },
        period="2026-06",
    )
    rejected = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "create", "expected_seq": 0, "fact": bypass}],
        }
    )
    assert rejected["error"] == "invalid_input", rejected
