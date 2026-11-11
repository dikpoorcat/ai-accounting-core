"""Sourced cutover holdings and cooperation resources continue through the real app."""

from uuid import uuid4

import pytest

from ai_accounting_modular.domains.financing.public import create_investment

from ._support import PARTY, PERSON, SOURCE, assemble, fact, get, member, opening, request, result


def investment_terms(product, category, method, *, current=False, maturity="2028-12-31"):
    right = {
        "equity": "company_equity",
        "bond": "direct_bond",
        "bank_wealth": "financial_contract",
    }.get(category, "fund_units")
    data = {
        "product_id": product,
        "product_type": category,
        "right_type": right,
        "measurement": method,
        "quantity_unit": "nominal_fen" if category == "bond" else "units",
        "cost_method": "fifo",
        "holding_arrangement": "current" if current else "noncurrent",
        "terms_source": {"source_id": SOURCE, "location": "合成期初投资条款"},
    }
    if method == "long_bond_cost":
        data["coupon_schedule"] = [{"payment_on": maturity, "coupon_rate": "0.1"}]
    if right in {"direct_bond", "company_equity", "partnership_interest"}:
        data["initial_holding_intent"] = (
            "short_term" if method == "short_term_cost" else "long_term"
        )
    return fact("investment.terms", data, period="2026-06")


def holding(terms, amount="1000", *, tax_basis=True):
    data = {
        "type": "investment",
        "terms_id": terms["business_id"],
        "original_lot_reference": "合成既有批次",
        "acquired_period": "2026-06",
        "quantity": "1000" if terms["data"]["product_type"] == "bond" else "10",
        "historical_cost_fen": amount,
        "carrying_fen": amount,
    }
    if tax_basis:
        data.update(tax_buy_basis_fen="800", eit_tax_basis_fen="900")
    if terms["data"]["measurement"] == "long_bond_cost":
        data["debt"] = {
            "face_fen": "1000",
            "unamortized_premium_fen": "0",
            "accrued_interest_fen": "0",
            "interest_start_on": "2026-07-01",
        }
    return member(data)


def equity(amount):
    return member(
        {
            "type": "equity",
            "equity_kind": "retained_earnings",
            "holder_or_basis_id": PARTY,
            "balance_fen": str(amount),
        }
    )


def submit(app, company, *facts, products=()):
    payload = request(company, *facts)
    payload["objects"].extend(
        {
            "object_id": product,
            "kind": "investment_product",
            "name": "合成期初投资",
            "expected_seq": 0,
            "data": {"category": category},
        }
        for product, category in products
    )
    return app.accounting_apply(payload)


def report(app, company, month="2026-07"):
    with app.storage.read_unit(company) as read:
        return app.ledger.financial_snapshot(
            {"company_id": company, "period_from": month, "period_to": month, "view": "current"},
            read,
        )


def value(report_, statement, row, column="ending"):
    return report_[statement][str(row)][column]["amount_fen"]


def test_all_financial_categories_cut_over_without_historical_cash_or_income(tmp_path):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    configurations = [
        ("money_fund", "short_term_cost", True),
        ("bank_wealth", "other_financial_cost", True),
        ("other_fund", "other_financial_cost", False),
        ("bond", "long_bond_cost", False),
        ("equity", "long_equity_cost", False),
    ]
    products = [(str(uuid4()), category) for category, _, _ in configurations]
    terms = [
        investment_terms(product, category, method, current=current)
        for (product, _), (category, method, current) in zip(products, configurations, strict=True)
    ]
    holdings = [holding(item) for item in terms]
    holdings[0]["data"]["acquisition_sequence"] = 17
    item = opening(*holdings, equity(5000))
    response = submit(app, company, *terms, item, products=products)
    assert response.get("completion") == "complete", response.get("message", response)
    formal = result(app, company, item)
    assert formal["vouchers"] == []
    lots = [q for q in formal["basis"]["adopted_quantities"] if q["type"] == "investment_lot"]
    assert len(lots) == 5
    assert {(q["tax_buy_basis_fen"], q["eit_tax_basis_fen"], q["carrying_fen"]) for q in lots} == {
        ("800", "900", "1000")
    }
    assert all("tax_facts" not in q and "right_type" in q for q in lots)
    assert [q["acquisition_sequence"] for q in lots if "acquisition_sequence" in q] == [17]
    snapshot = report(app, company)
    assert {row: value(snapshot, "balance_sheet", row) for row in (2, 14, 16, 17, 28, 30)} == {
        2: "1000",
        14: "1000",
        16: "1000",
        17: "1000",
        28: "1000",
        30: "5000",
    }
    assert value(snapshot, "profit_statement", 30, "current") == "0"
    assert all(value(snapshot, "cash_flow_statement", row, "current") == "0" for row in (8, 9, 11))


def test_zero_book_units_and_unknown_tax_bases_are_not_guessed_or_lost(tmp_path):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product = str(uuid4())
    terms = investment_terms(product, "other_fund", "other_financial_cost")
    item = opening(holding(terms, "0", tax_basis=False))
    response = submit(app, company, terms, item, products=[(product, "other_fund")])
    assert response.get("completion") == "complete", response.get("message", response)
    lot = next(
        q
        for q in result(app, company, item)["basis"]["adopted_quantities"]
        if q["type"] == "investment_lot"
    )
    assert lot["quantity"] == "10" and lot["carrying_fen"] == "0"
    assert "tax_buy_basis_fen" not in lot and "eit_tax_basis_fen" not in lot


@pytest.mark.parametrize(
    "maturity,short,long", [("2027-06-30", "1000", "0"), ("2028-12-31", "0", "1000")]
)
def test_opening_bond_maturity_classifies_the_same_gl_balance(tmp_path, maturity, short, long):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product = str(uuid4())
    terms = investment_terms(product, "bond", "long_bond_cost", maturity=maturity)
    item = opening(holding(terms), equity(1000))
    response = submit(app, company, terms, item, products=[(product, "bond")])
    assert response.get("completion") == "complete", response.get("message", response)
    snapshot = report(app, company)
    assert value(snapshot, "balance_sheet", 14) == short
    assert value(snapshot, "balance_sheet", 16) == long
    assert value(snapshot, "balance_sheet", 30) == "1000"


def cooperation():
    product = str(uuid4())
    agreement = fact(
        "investment.cooperation_contract",
        {
            "profile": "direct_joint_operation_cash_labor_v1",
            "project_id": product,
            "operator_id": PERSON,
            "company_initial_commitment_fen": "100",
            "company_revenue_expense_share": "0.6",
            "contract_source": {"source_id": SOURCE, "location": "合成60/40合作"},
        },
        period="2026-06",
    )
    return product, agreement


def open_trade(key, side, amount, *, cleared="0", company_cleared="0"):
    return {
        "entry_key": key,
        "side": side,
        "counterparty_id": PARTY,
        "original_business_reference": key,
        "original_project_amount_fen": amount,
        "project_cleared_before_fen": cleared,
        "company_cleared_before_fen": company_cleared,
        "project_range_from_fen": "0",
    }


def coop_member(agreement, trades, *, company_cash="60", operator_cash="40"):
    return member(
        {
            "type": "cooperation",
            "contract_id": agreement["business_id"],
            "status": "active",
            "company_cash_right_fen": company_cash,
            "operator_cash_right_fen": operator_cash,
            "open_trades": trades,
        }
    )


def position(app, company, agreement):
    return next(
        r["target"]["values"]
        for r in get(app, company, agreement["business_id"])["businesses"][0]["results"]
        if r["reference"]["result_slot"] == "position:2026-07"
    )


def agency(agreement, package, member_, key, side, amount, day):
    return fact(
        "investment.cooperation_" + ("collection" if side == "revenue" else "payment"),
        {
            "contract_id": agreement["business_id"],
            "source": {
                "business_id": package["business_id"],
                "slot": "member:" + member_["member_id"] + ":trade:" + key,
            },
            "project_amount_fen": amount,
        },
        period="2026-07",
        actual=day,
    )


def test_cooperation_snapshot_and_open_trades_clear_without_new_profit(tmp_path):
    app, company = assemble(tmp_path, investment_accounting=create_investment(), operations=True)
    product, agreement = cooperation()
    current = coop_member(
        agreement,
        [open_trade("receivable", "revenue", "50"), open_trade("payable", "expense", "20")],
    )
    package = opening(current, equity(78))
    response = submit(app, company, agreement, package, products=[(product, "cooperation")])
    assert response.get("completion") == "complete", response.get("message", response)
    before = position(app, company, agreement)
    assert (before["held_fen"], before["company_capital_fen"], before["operator_capital_fen"]) == (
        "60",
        "78",
        "52",
    )
    assert before["lifetime_totals"] is None and before["totals_from_period"] == "2026-07"
    collected = agency(agreement, package, current, "receivable", "revenue", "50", "2026-07-10")
    paid = agency(agreement, package, current, "payable", "expense", "20", "2026-07-11")
    response = submit(app, company, collected, paid)
    assert response.get("completion") == "complete", response.get("message", response)
    after = position(app, company, agreement)
    assert (after["held_fen"], after["company_capital_fen"], after["company_profit_fen"]) == (
        "78",
        "78",
        "0",
    )
    assert after["company_trade_receivable_net_fen"] == after["company_trade_payable_fen"] == "0"
    snapshot = report(app, company)
    assert value(snapshot, "profit_statement", 30, "current") == "0"
    assert value(snapshot, "cash_flow_statement", 20, "current") == "0"


def test_open_cooperation_resource_keeps_original_rounding_offset(tmp_path):
    app, company = assemble(tmp_path, investment_accounting=create_investment(), operations=True)
    product, agreement = cooperation()
    current = coop_member(
        agreement,
        [open_trade("rounded", "revenue", "3", cleared="1", company_cleared="1")],
        company_cash="0",
        operator_cash="0",
    )
    package = opening(current, equity(1))
    response = submit(app, company, agreement, package, products=[(product, "cooperation")])
    assert response.get("completion") == "complete", response.get("message", response)
    first = agency(agreement, package, current, "rounded", "revenue", "1", "2026-07-10")
    second = agency(agreement, package, current, "rounded", "revenue", "1", "2026-07-11")
    response = submit(app, company, first, second)
    assert response.get("completion") == "complete", response.get("message", response)
    outcomes = [
        result(app, company, item)["target"]["values"]["company_amount_fen"]
        for item in (first, second)
    ]
    assert outcomes == ["0", "1"]
    assert position(app, company, agreement)["held_fen"] == "1"


def test_cooperation_open_trade_cannot_also_be_an_ordinary_member(tmp_path):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product, agreement = cooperation()
    current = coop_member(agreement, [open_trade("duplicate", "revenue", "50")])
    duplicate = member(
        {
            "type": "obligation",
            "nature": "customer_receivable",
            "counterparty_id": PARTY,
            "amount_fen": "30",
            "original_business_reference": "duplicate",
        }
    )
    package = opening(current, duplicate, equity(120))
    response = submit(app, company, agreement, package, products=[(product, "cooperation")])
    assert response.get("completion") == "not_saved" and response["error"] == "invalid_input", (
        response
    )
    assert get(app, company, package["business_id"])["businesses"] == []


@pytest.mark.parametrize("invalid", ["face_mismatch", "fraction_over_one"])
def test_opening_unit_facts_cannot_contradict_their_legal_quantity(tmp_path, invalid):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product = str(uuid4())
    category = "bond" if invalid == "face_mismatch" else "equity"
    terms = investment_terms(
        product, category, "long_bond_cost" if category == "bond" else "long_equity_cost"
    )
    current = holding(terms)
    if invalid == "face_mismatch":
        current["data"]["debt"].update(face_fen="0", unamortized_premium_fen="1000")
    else:
        terms["data"]["quantity_unit"] = "ownership_fraction"
        current["data"]["quantity"] = "1.1"
    package = opening(current, equity(1000))
    response = submit(app, company, terms, package, products=[(product, category)])
    assert response.get("completion") != "complete" and response["error"] == "invalid_input", (
        response
    )
    assert get(app, company, package["business_id"])["businesses"] == []


@pytest.mark.parametrize(
    "category,method,loss_code,detail",
    [
        ("bond", "long_bond_cost", "571106", 26),
        ("equity", "long_equity_cost", "571107", 27),
        ("other_fund", "other_financial_cost", "571108", None),
    ],
)
@pytest.mark.parametrize("actual_loss", [False, True])
def test_actual_loss_and_ordinary_exit_keep_distinct_profit_meanings(
    tmp_path, category, method, loss_code, detail, actual_loss
):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product = str(uuid4())
    terms = investment_terms(product, category, method)
    current = holding(terms)
    package = opening(current, equity(1000))
    data = {
        "terms_id": terms["business_id"],
        "counterparty_id": PARTY,
        "quantity": current["data"]["quantity"],
    }
    if actual_loss:
        data.update(
            source={
                "business_id": package["business_id"],
                "slot": "member:" + current["member_id"] + ":cost",
            },
            loss_basis="legal_extinction",
            recovered_fen="0",
        )
    else:
        data.update(gross_proceeds_fen="0", disposal_fees_fen="0", exit_mode="transfer")
    exited = fact(
        "investment.loss" if actual_loss else "investment.dispose", data, actual="2026-07-15"
    )
    response = submit(app, company, terms, package, exited, products=[(product, category)])
    assert response.get("completion") == "complete", response.get("message", response)
    formal = result(app, company, exited)
    expense = loss_code if actual_loss else "5111"
    assert any(
        line["account_code"] == expense and line["debit_fen"] == "1000"
        for voucher in formal["vouchers"]
        for line in voucher["lines"]
    )
    snapshot = report(app, company)
    assert value(snapshot, "profit_statement", 20, "current") == ("0" if actual_loss else "-1000")
    assert value(snapshot, "profit_statement", 24, "current") == ("1000" if actual_loss else "0")
    assert value(snapshot, "profit_statement", 30, "current") == "-1000"
    if detail is not None:
        assert value(snapshot, "profit_statement", detail, "current") == (
            "1000" if actual_loss else "0"
        )


@pytest.mark.parametrize(
    "category,method,nature,allowed",
    [
        ("equity", "long_equity_cost", "interest", False),
        ("money_fund", "short_term_cost", "interest", False),
        ("bond", "long_bond_cost", "dividend", False),
        ("bond", "long_bond_cost", "interest", True),
        ("bank_wealth", "other_financial_cost", "dividend", True),
        ("other_fund", "other_financial_cost", "dividend", True),
    ],
)
def test_opening_income_respects_existing_rights_without_rejecting_sourced_contract_distributions(
    tmp_path, category, method, nature, allowed
):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product = str(uuid4())
    terms = investment_terms(product, category, method, current=method == "short_term_cost")
    receivable = member(
        {
            "type": "investment_receivable",
            "terms_id": terms["business_id"],
            "counterparty_id": PARTY,
            "income_kind": nature,
            "amount_fen": "7",
            "original_business_reference": "建账前已确认收益权利",
        }
    )
    package = opening(receivable, equity(7))
    response = submit(app, company, terms, package, products=[(product, category)])
    if not allowed:
        assert response.get("completion") == "not_saved" and response["error"] == "invalid_input", (
            response
        )
        return
    assert response.get("completion") == "complete", response.get("message", response)
    snapshot = report(app, company)
    assert value(snapshot, "balance_sheet", 7 if nature == "interest" else 6) == "7"
    assert value(snapshot, "profit_statement", 30, "current") == "0"
