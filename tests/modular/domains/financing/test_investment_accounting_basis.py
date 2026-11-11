"""Investment accounting uses known rights without requiring future tax qualification."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, content_digest
from ai_accounting_modular.domains.financing import _investment_plans
from ai_accounting_modular.domains.financing._investment_events import replay
from ai_accounting_modular.domains.financing._investment_rules import (
    account_code,
    validate_term_compatibility,
)
from ai_accounting_modular.domains.financing.public import (
    investment_accounting_terms,
    investment_economic_events,
)

from ._investment_support import acquire, terms
from ._support import COUNTERPARTY, SOURCE, assemble, fact, request, result, six
from .test_investment_lifecycle import apply_facts


@pytest.mark.parametrize("legacy_right", ["securities_fund", "contractual_fund"])
def test_frozen_terms_have_a_detached_accounting_view(legacy_right):
    current = terms(measurement="other_financial_cost")["data"]
    original = deepcopy(current)
    original.pop("right_type")
    original.update(
        legal_form=legacy_right,
        tax_facts={
            "instrument_type": "securities_fund",
            "source": {"source_id": SOURCE},
        },
        financial_instrument_reference={"reference": "fund-A", "source": {"source_id": SOURCE}},
    )
    before, digest = deepcopy(original), content_digest(original)
    viewed = investment_accounting_terms(original)
    assert viewed == current
    viewed["terms_source"]["location"] = "changed only in the in-memory view"
    assert original == before
    assert content_digest(original) == digest


def test_fund_acquisition_uses_known_accounting_arrangement_without_tax_qualification():
    holding = terms(measurement="other_financial_cost")
    validate_term_compatibility(holding)
    assert "initial_holding_intent" not in holding["data"]
    purchased = acquire(holding, "20000000", "53868.87")
    events, lots, _ = replay(holding["business_id"], holding["data"], [purchased])
    assert events[purchased["business_id"]].cost == 20000000
    assert lots[0].remaining_cost == 20000000
    assert account_code(holding["data"]) == "189902"


def test_known_fund_units_cannot_use_a_direct_equity_accounting_model():
    holding = terms(measurement="long_equity_cost")
    with pytest.raises(ContractError, match="成本核算方法"):
        validate_term_compatibility(holding)


@pytest.mark.parametrize("name", ["economic_event", "economic_events", "tax_event", "tax_events"])
def test_economic_reading_keeps_original_values_and_order(name):
    original = {
        "terms_id": "terms",
        "event_kind": "acquisition",
        "quantity": "10",
        "gross_price_fen": "12345",
        "transaction_fees_fen": "5",
    }
    if name.startswith("tax"):
        original.update(
            legal_form="contractual_fund",
            tax_facts={
                "instrument_type": "contractual_asset_management",
                "source": {"source_id": SOURCE},
            },
        )
    else:
        original["right_type"] = "fund_units"
    values = {name: [original] if name.endswith("events") else original}
    before, digest = deepcopy(values), content_digest(values)
    (view,) = investment_economic_events(values)
    assert view == {
        "terms_id": "terms",
        "event_kind": "acquisition",
        "quantity": "10",
        "gross_price_fen": "12345",
        "transaction_fees_fen": "5",
        "right_type": "fund_units",
    }
    view["quantity"] = "1"
    assert values == before
    assert content_digest(values) == digest


def test_old_event_without_accounting_right_does_not_infer_it_from_tax_label():
    (event,) = investment_economic_events(
        {"tax_event": {"tax_facts": {"instrument_type": "securities_fund"}}}
    )
    assert "right_type" not in event


def test_historical_reading_support_does_not_reopen_the_old_submission_contract(tmp_path):
    app, company = assemble(tmp_path)
    holding = terms()
    holding["data"].pop("right_type")
    holding["data"].update(
        legal_form="securities_fund",
        tax_facts={"instrument_type": "securities_fund", "source": {"source_id": SOURCE}},
    )
    rejected = app.accounting_apply(request(company, holding))
    assert rejected["code"] == "invalid_input", rejected


def test_event_reader_rejects_ambiguous_original_event_positions():
    with pytest.raises(ContractError, match="多组交易正文"):
        investment_economic_events({"economic_event": {}, "tax_event": {}})


def test_simple_fund_purchase_exposes_actual_price_and_fees_without_inventing_tax_identity():
    values = {"fund_id": "fund", "cost_fen": "10100"}
    original_input = {
        "fund_id": "fund",
        "purchase_price_fen": "10000",
        "acquisition_fees_fen": "100",
    }
    before, before_input = deepcopy(values), deepcopy(original_input)
    (event,) = investment_economic_events(
        values, kind="investment.money_fund_purchase", input=original_input
    )
    assert event == {
        "event_kind": "simple_fund_acquisition",
        "product_id": "fund",
        "purchase_price_fen": "10000",
        "acquisition_fees_fen": "100",
        "accounting_cost_fen": "10100",
    }
    event["accounting_cost_fen"] = "0"
    assert values == before
    assert original_input == before_input


def test_simple_fund_redemption_preserves_original_cost_references_without_gross_proceeds():
    values = {"fund_id": "fund", "cost_fen": "6000", "net_proceeds_fen": "7200"}
    original_input = {
        "fund_id": "fund",
        "costs": [{"source": {"business_id": "purchase", "slot": "cost"}, "amount_fen": "6000"}],
    }
    before, before_input = deepcopy(values), deepcopy(original_input)
    (event,) = investment_economic_events(
        values, kind="investment.money_fund_redemption", input=original_input
    )
    assert event == {
        "event_kind": "simple_fund_redemption",
        "product_id": "fund",
        "net_proceeds_fen": "7200",
        "accounting_cost_fen": "6000",
        "cost_allocations": original_input["costs"],
    }
    event["cost_allocations"][0]["source"]["business_id"] = "another"
    assert values == before
    assert original_input == before_input


def test_simple_fund_reading_preserves_actual_formal_sources(tmp_path):
    app, company = assemble(tmp_path)
    *_, purchased, redeemed = six()
    outcome = app.accounting_apply(request(company, purchased, redeemed))
    assert outcome.get("completion") == "complete", outcome
    for subject in (purchased, redeemed):
        before = deepcopy(result(app, company, subject))
        with app.storage.read_unit(company) as read:
            source = app.ledger.get_result_source(subject["business_id"], "main", read)
        (event,) = investment_economic_events(
            before["target"]["values"], kind=subject["kind"], input=before["basis"]["input"]
        )
        assert event["product_id"] == subject["data"]["fund_id"]
        assert "terms_id" not in event
        assert "quantity" not in event
        assert "tax_facts" not in event
        assert result(app, company, subject) == before
        with app.storage.read_unit(company) as read:
            assert app.ledger.get_result_source(subject["business_id"], "main", read) == source


def test_purchase_records_only_economic_quantities(tmp_path):
    app, company = assemble(tmp_path)
    holding = terms(measurement="other_financial_cost")
    purchased = acquire(holding, "20000000", "53868.87")
    apply_facts(app, company, holding, purchased, initial=True)
    body = result(app, company, purchased)
    event = body["target"]["values"]["economic_event"]
    assert event["right_type"] == "fund_units"
    assert event["gross_price_fen"] == "20000000"
    assert event["quantity"] == "53868.87"
    assert event["transaction_fees_fen"] == "0"
    assert "tax_facts" not in event
    assert "financial_instrument_reference" not in event
    assert "tax_event" not in body["target"]["values"]
    lots = [row for row in body["basis"]["adopted_quantities"] if row["type"] == "investment_lot"]
    assert len(lots) == 1
    assert lots[0]["right_type"] == "fund_units"
    assert "tax_facts" not in lots[0]
    assert "financial_instrument_reference" not in lots[0]


def test_new_income_adopts_exact_retained_terms_without_rewriting_them(tmp_path, monkeypatch):
    app, company = assemble(tmp_path)
    holding = terms(measurement="other_financial_cost")
    purchased = acquire(holding)
    current_plan = _investment_plans.terms

    def predecessor_terms(*args, **kwargs):
        plan = current_plan(*args, **kwargs)
        values = plan["target"]["values"]
        values.pop("right_type")
        values.update(
            legal_form="contractual_fund",
            tax_facts={
                "instrument_type": "contractual_asset_management",
                "source": {"source_id": SOURCE},
            },
        )
        plan["basis"]["input"] = deepcopy(values)
        return plan

    with monkeypatch.context() as predecessor:
        predecessor.setattr(_investment_plans, "terms", predecessor_terms)
        apply_facts(app, company, holding, purchased, initial=True)
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    retained = deepcopy(result(app, company, holding))
    with app.storage.read_unit(company) as read:
        retained_source = app.ledger.get_result_source(holding["business_id"], "main", read)
    income = fact(
        "investment.income",
        {
            "terms_id": holding["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "15",
        },
        period="2026-02",
        actual="2026-02-02",
    )
    apply_facts(app, company, income)
    assert result(app, company, holding) == retained
    adopted = result(app, company, income)
    assert adopted["target"]["values"]["economic_event"]["right_type"] == "fund_units"
    source = next(
        row
        for row in adopted["basis"]["source_results"]
        if row["business_id"] == holding["business_id"]
    )
    assert source == retained_source
