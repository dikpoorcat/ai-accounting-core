"""Professional investment income stays complete while funds expose only owner positions."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.domains.taxes import _financial_transfer
from ai_accounting_modular.reading import _monthly
from tests.modular.domains.financing._support import COUNTERPARTY, fact, payment
from tests.modular.domains.taxes.test_investment_tax_application import (
    advance,
    inputs,
    new_trade,
    qualification,
    quarter_return,
)

from .test_funds import apply, capture, check_data, initialize, investment_activity, page, selection
from .test_funds import case as case
from .test_investments import _sources


def tax_setup(case, *, quarterly=False, defer_tax=False):
    initialize(case)
    values = inputs()
    if quarterly:
        values[1]["data"]["acquisition_fees_fen"] = "0"
        values[2]["data"]["gross_proceeds_fen"] = "2010000"
        values[3]["data"]["threshold_period"] = "quarter"
    tax_basis = qualification(values[0])
    for value in (*values, tax_basis):
        _sources(value, case.source_id)
    apply(case, *(values[:-1] if defer_tax else values), tax_basis)
    return values


def data(case, month):
    capture(case, month)
    response = page(case, month)["data"]
    check_data(response)
    return response


def test_professional_tax_changes_do_not_invalidate_unchanged_funds(case):
    values = tax_setup(case, defer_tax=True)
    before = data(case, "2026-07")
    assert investment_activity(case, "2026-07").investment_income_fen == 102000
    assert set(before["investments"]) == {"closing_cost_fen"}
    apply(case, values[-1])
    with case.kernel.storage.read_unit(case.company) as read:
        saved = read.repository("reading").state(_monthly.key(selection(case, "2026-07")), "funds")
    assert saved["state"] == "ready"
    response = page(case, "2026-07")["data"]
    assert response == before
    formal = investment_activity(case, "2026-07")
    assert formal.recognized_income_fen == 102000
    assert formal.vat_adjustment_fen == -2000
    assert formal.investment_income_fen == 100000
    (product,) = response["collections"]["investment_products"]["items"]
    assert product["recognized_income_fen"] == "102000"
    assert formal.products[0].investment_income_fen == 102000
    assert "investment_events" not in response["collections"]
    tax = [row for row in formal.income_adjustments if row.attribution == "company_pool"]
    assert len(tax) == 1
    assert tax[0].amount_fen == -2000
    assert tax[0].component == "transfer_vat"
    assert tax[0].product_id is tax[0].terms_id is None
    with case.kernel.storage.read_unit(case.company) as read:
        scope = {
            "company_id": case.company,
            "period_from": "2026-07",
            "period_to": "2026-07",
            "view": "as_posted",
        }
        contributions = case.kernel.ledger.profit_contributions(scope, PageQuery(500), read)
    assert (
        sum(item.investment_income_fen - item.baseline_income_fen for item in contributions.items)
        == 100000
    )


def test_company_tax_expiry_preserves_income_without_dashboard_positions(case):
    agreement, _, sold, policy, surtax, _ = tax_setup(case, quarterly=True)
    assessment = quarter_return(policy, surtax, "2026-07", "2026-09")
    received = payment(
        sold, 2010000, slot="primary", party=COUNTERPARTY, direction="inflow", day="2026-07-03"
    )
    values = [
        received,
        advance("2026-09"),
        assessment,
        *new_trade(agreement, "2026-10", 1010000, 0),
    ]
    for value in values:
        _sources(value, case.source_id)
    apply(case, *values)
    later = advance("2026-12")
    _sources(later, case.source_id)
    apply(case, later)
    response = data(case, "2026-12")
    assert response["collections"]["investment_products"]["items"] == []
    assert "investment_events" not in response["collections"]
    assert response["investments"] == {"closing_cost_fen": "0"}
    formal = investment_activity(case, "2026-12")
    (tax,) = formal.income_adjustments
    assert tax.attribution == "company_pool"
    assert tax.product_id is tax.terms_id is None
    assert tax.amount_fen == -10000
    assert formal.investment_income_fen == -10000
    assert formal.recognized_income_fen == 0


def test_cross_period_transfer_credit_stays_in_company_pool(case):
    agreement, _, _, _, _, _ = tax_setup(case, quarterly=True)
    values = [*new_trade(agreement, "2026-08", 1010000, 0)]
    for value in values:
        _sources(value, case.source_id)
    apply(case, *values)
    data(case, "2026-08")
    formal = investment_activity(case, "2026-08")
    assert formal.recognized_income_fen == -1010000
    assert formal.vat_adjustment_fen == 10000
    assert formal.investment_income_fen == -1000000
    (tax,) = [row for row in formal.income_adjustments if row.attribution == "company_pool"]
    assert tax.product_id is None


def test_tax_only_interest_adjustment_keeps_professional_origin_without_funds_record(case):
    initialize(case)
    agreement, _, _, policy, surtax, basis = inputs()
    product = str(uuid4())
    agreement["data"].update(
        product_id=product,
        product_type="bank_wealth",
        right_type="financial_contract",
        measurement="other_financial_cost",
    )
    policy["data"].update(threshold_period="quarter", threshold_fen="30000000")
    interest = fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "interest",
            "amount_fen": "10100",
        },
        period="2026-07",
        actual="2026-07-02",
    )
    received = payment(
        interest, 10100, slot="income", party=COUNTERPARTY, direction="inflow", day="2026-07-03"
    )
    values = [
        agreement,
        qualification(agreement, instrument="bank_wealth", principal_protected=True),
        interest,
        received,
        policy,
        surtax,
        basis,
    ]
    for value in values:
        _sources(value, case.source_id)
    apply(
        case,
        *values,
        objects=[
            {
                "object_id": product,
                "kind": "investment_product",
                "name": "合成保本理财",
                "expected_seq": 0,
                "data": {"category": "bank_wealth"},
            }
        ],
    )
    values = [advance("2026-09"), quarter_return(policy, surtax, "2026-07", "2026-09")]
    for value in values:
        _sources(value, case.source_id)
    apply(case, *values)
    response = data(case, "2026-09")
    assert response["collections"]["investment_products"]["items"] == []
    assert response["investments"] == {"closing_cost_fen": "0"}
    formal = investment_activity(case, "2026-09")
    (product_row,) = [row for row in formal.products if row.terms_id == agreement["business_id"]]
    assert product_row.fund_id == product
    assert product_row.investment_income_fen == 0
    assert product_row.actual_payments_fen == product_row.actual_receipts_fen == 0
    assert product_row.closing_cost is None
    assert product_row.activity_only is True
    assert product_row.pending_receipts_fen == 0
    (tax,) = formal.income_adjustments
    assert tax.attribution == "event"
    assert tax.product_id == product
    assert tax.terms_id == agreement["business_id"]
    assert formal.investment_income_fen == 100


@pytest.mark.parametrize("frozen", [False, True])
def test_missing_tax_explanation_is_only_compatible_with_old_frozen_result(
    case, monkeypatch, frozen
):
    with monkeypatch.context() as old:
        old.setattr(_financial_transfer, "pool_effect", lambda *args: None)
        old.setattr(_financial_transfer, "interest_effects", lambda *args: None)
        old.setattr(_financial_transfer, "check_effects", lambda *args: None)
        values = tax_setup(case)
    if not frozen:
        response = data(case, "2026-07")
        (product,) = response["collections"]["investment_products"]["items"]
        assert product["recognized_income_fen"] == "102000"
        with pytest.raises(ContractError, match="归属量") as error:
            investment_activity(case, "2026-07")
        assert error.value.code == "storage_corrupt"
        return
    with case.kernel.storage.write_unit(case.company) as unit:
        unit.repository("ledger").freeze_heads("2026-07")
    original = data(case, "2026-07")
    formal = investment_activity(case, "2026-07")
    assert formal.investment_income_fen == 100000
    (tax,) = [row for row in formal.income_adjustments if row.attribution == "company_pool"]
    assert tax.component == "investment_vat"
    changed = deepcopy(values[2])
    changed["evidence_ids"] = [case.source_id]
    changed["data"]["gross_proceeds_fen"] = "1303000"
    outcome = case.app.accounting_apply(
        case.request(
            correction_period="2026-08",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
        )
    )
    assert outcome["completion"] == "complete", outcome
    assert data(case, "2026-07") == original
    assert investment_activity(case, "2026-07").investment_income_fen == 100000
    data(case, "2026-08")
    correction = investment_activity(case, "2026-08")
    assert correction.recognized_income_fen == 101000
    assert correction.vat_adjustment_fen == -1000
    assert correction.investment_income_fen == 100000
