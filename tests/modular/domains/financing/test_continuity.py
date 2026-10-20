"""Corrections, real money and failure recovery through public assembly."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing.public import create_investment
from ai_accounting_modular.money.public import MoneyQuery

from ._support import (
    BANK,
    COUNTERPARTY,
    LENDER,
    OWNER,
    PLATFORM,
    SOURCE,
    assemble,
    change,
    fact,
    get,
    payment,
    request,
    result,
    six,
)


@pytest.fixture
def assembled(tmp_path):
    return assemble(tmp_path)


def movements(app, company):
    with app.storage.read_unit(company) as read:
        return tuple(
            x["movement"]
            for x in app.money.query_movements(MoneyQuery(BANK, "2026-01", "2026-03"), read).items
        )


def test_interest_and_investment_publish_once_to_shared_pretax(assembled):
    app, company = assembled
    facts = six()
    actual = app.accounting_apply(request(company, *facts))
    assert actual["completion"] == "complete", actual
    with app.storage.read_unit(company) as read:
        profit = app.ledger.pre_tax("2026-01", "2026-01", read)
    assert profit.fields["profit_before_tax"] == {"state": "known", "amount_fen": "-100"}
    assert {x["business_id"] for x in profit.sources} == {
        facts[3]["business_id"],
        facts[5]["business_id"],
    }


def test_contract_rate_change_recalculates_drawdown_basis_and_interest_without_new_money(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    assert (
        app.accounting_apply(request(company, contract, loan, interest))["completion"] == "complete"
    )
    before_money = movements(app, company)
    before_interest = result(app, company, interest)
    contract["data"]["annual_rate"] = "0.072"
    actual = app.accounting_apply(change(company, contract))
    assert actual["completion"] == "complete", actual
    after = result(app, company, interest)
    assert after["target"]["values"]["interest_fen"] == "6200"
    assert after["reference"]["voucher_number"] == before_interest["reference"]["voucher_number"]
    assert movements(app, company) == before_money


def test_numerically_equal_rate_spellings_do_not_change_interest_adoption(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    assert (
        app.accounting_apply(request(company, contract, loan, interest))["completion"] == "complete"
    )
    before = result(app, company, interest)
    contract["data"]["annual_rate"] = "0.03600000"
    actual = app.accounting_apply(change(company, contract))
    assert actual["completion"] == "complete", actual
    assert result(app, company, interest) == before


def test_frozen_interest_correction_preserves_old_adoption_and_real_drawdown(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    assert (
        app.accounting_apply(request(company, contract, loan, interest))["completion"] == "complete"
    )
    before_money = movements(app, company)
    before = result(app, company, interest)
    # Freeze an isolated adopted baseline; this test concerns correction adoption.
    with app.storage.write_unit(company) as uow:
        uow.repository("ledger").freeze_heads("2026-01")
    contract["data"]["annual_rate"] = "0.072"
    actual = app.accounting_apply(change(company, contract, correction_period="2026-03"))
    assert actual["completion"] == "complete", actual
    current = result(app, company, interest)
    assert current["target"]["values"]["interest_fen"] == "6200"
    assert current["reference"]["result_id"] != before["reference"]["result_id"]
    assert {x["role"] for x in current["vouchers"]} == {"reversal", "replacement"}
    with app.storage.read_unit(company, view="as_posted", as_of="2026-01") as read:
        (old,) = app.ledger.get_results([interest["business_id"]], read)
    assert old["target"]["values"]["interest_fen"] == "3100"
    assert movements(app, company) == before_money


def test_known_personal_principal_repayment_uses_its_real_day(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    advanced = fact(
        "operations.reimbursement",
        {
            "type": "personal_payment",
            "payer_id": OWNER,
            "payer_kind": "owner",
            "amount_fen": "400000",
            "allocations": [
                {
                    "source": {"business_id": loan["business_id"], "slot": "principal"},
                    "amount_fen": "400000",
                    "recipient_id": LENDER,
                }
            ],
        },
        actual="2026-01-16",
    )
    actual = app.accounting_apply(request(company, contract, loan, interest, advanced))
    assert actual["completion"] == "complete", actual
    assert result(app, company, interest)["target"]["values"]["interest_fen"] == "2460"
    assert len(movements(app, company)) == 1  # Actual drawdown only; no company repayment invented.


def test_unknown_personal_payment_day_is_not_replaced_by_acceptance_date(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    accepted = fact(
        "operations.reimbursement",
        {
            "type": "acceptance",
            "payer_id": OWNER,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": loan["business_id"], "slot": "principal"},
                    "amount_fen": "400000",
                    "recipient_id": LENDER,
                }
            ],
        },
        actual="2026-01-20",
    )
    actual = app.accounting_apply(request(company, contract, loan, interest, accepted))
    assert actual["error"] == "needs_information", actual
    assert any(
        x["code"] == "actual_payment_basis_required" and x["field"] == "/data/type"
        for x in actual["fact_issues"]
    )
    assert get(app, company, loan["business_id"])["businesses"] == []


def test_late_owner_failure_rolls_back_all_six_facts_and_fund_identities(assembled, monkeypatch):
    app, company = assembled
    facts = six()
    real = app.records.replace_source_uses

    def fail_business(kind, key, ids, unit):
        if kind == "business":
            raise ContractError("temporary_failure", "合成失败：正式采用后未能保存来源关系。")
        return real(kind, key, ids, unit)

    monkeypatch.setattr(app.records, "replace_source_uses", fail_business)
    actual = app.accounting_apply(request(company, *facts))
    assert actual["completion"] == "not_saved" and actual["error"] == "temporary_failure", actual
    assert get(app, company, *[x["business_id"] for x in facts])["businesses"] == []
    assert movements(app, company) == ()


def test_document_failure_retry_does_not_recount_loans_or_investment(assembled, monkeypatch):
    app, company = assembled
    facts = six()
    submitted = request(company, *facts)
    real = app.records.prepare_documents

    def interrupted(snapshot):
        raise OSError("合成失败：标准文档暂未写完。")

    monkeypatch.setattr(app.records, "prepare_documents", interrupted)
    actual = app.accounting_apply(submitted)
    assert actual["completion"] == "saved_documents_pending", actual
    before = get(app, company, *[x["business_id"] for x in facts])
    before_money = movements(app, company)
    monkeypatch.setattr(app.records, "prepare_documents", real)
    assert app.accounting_apply(submitted)["completion"] == "complete"
    assert get(app, company, *[x["business_id"] for x in facts]) == before
    assert movements(app, company) == before_money


def test_partial_redemptions_conserve_original_cost_and_profit(assembled):
    app, company = assembled
    *_, purchase, first = six()
    purchase["data"]["purchase_price_fen"] = "100001"
    first["data"]["costs"][0]["amount_fen"] = "40000"
    first["data"]["net_proceeds_fen"] = "41000"
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    second["period"] = "2026-02"
    second["data"]["costs"][0]["amount_fen"] = "60001"
    second["data"]["net_proceeds_fen"] = "61001"
    actual = app.accounting_apply(request(company, second, first, purchase))
    assert actual["completion"] == "complete", actual
    assert result(app, company, first)["target"]["values"]["gain_loss_fen"] == "1000"
    assert result(app, company, second)["target"]["values"]["gain_loss_fen"] == "1000"


def test_cost_array_order_changes_no_formal_values_or_journal(assembled):
    app, company = assembled
    *_, first, redeemed = six()
    first["data"]["purchase_price_fen"] = "50000"
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    redeemed["data"]["costs"] = [
        {"source": {"business_id": x["business_id"], "slot": "cost"}, "amount_fen": "50000"}
        for x in (first, second)
    ]
    actual = app.accounting_apply(request(company, first, second, redeemed))
    assert actual["completion"] == "complete", actual
    before = result(app, company, redeemed)
    redeemed["data"]["costs"].reverse()
    actual = app.accounting_apply(change(company, redeemed))
    assert actual["completion"] == "complete", actual
    assert result(app, company, redeemed) == before


def test_platform_funding_binds_original_once_without_second_cash(assembled):
    app, company = assembled
    owner = six()[0]
    owner["data"]["funds_account_id"] = PLATFORM
    row = str(uuid4())
    owner["data"]["platform_row_ids"] = [row]
    activity = fact(
        "money.platform_activity",
        {
            "platform_account_id": PLATFORM,
            "rows": [
                {
                    "row_id": row,
                    "actual_on": "2026-01-02",
                    "direction": "inflow",
                    "amount_fen": "100000",
                    "source": {"source_id": SOURCE, "location": "合成原行"},
                }
            ],
        },
    )
    actual = app.accounting_apply(request(company, owner, activity))
    assert actual["completion"] == "complete", actual
    with app.storage.read_unit(company) as read:
        funds = app.money.query_movements(MoneyQuery(PLATFORM, "2026-01", "2026-01"), read)
    assert len(funds.items) == 1
    assert result(app, company, activity)["vouchers"] == []


def test_zero_proceeds_correction_cannot_erase_preexisting_real_receipt(assembled):
    app, company = assembled
    *_, purchase, redeemed = six()
    received = payment(redeemed, 103000, slot="primary", party=COUNTERPARTY, direction="inflow")
    assert (
        app.accounting_apply(request(company, purchase, redeemed, received))["completion"]
        == "complete"
    )
    before = get(app, company, redeemed["business_id"], received["business_id"])
    money_before = movements(app, company)
    redeemed["data"]["net_proceeds_fen"] = "0"
    actual = app.accounting_apply(change(company, redeemed))
    # This correction omits the true refund responsibility. It must retain the
    # complete previously valid state and cannot discard the actual receipt.
    assert actual["completion"] == "not_saved" and actual["error"] == "dependency_conflict", actual
    assert get(app, company, redeemed["business_id"], received["business_id"]) == before
    assert movements(app, company) == money_before


def test_one_redemption_can_consume_more_than_one_public_source_batch(assembled):
    app, company = assembled
    *_, prototype, redeemed = six()
    prototype["data"]["purchase_price_fen"] = "1"
    purchases = []
    for _ in range(501):
        item = deepcopy(prototype)
        item["business_id"] = str(uuid4())
        purchases.append(item)
    first = app.accounting_apply(request(company, *purchases[:500]))
    assert first["completion"] == "complete", first
    redeemed["data"]["costs"] = [
        {"source": {"business_id": item["business_id"], "slot": "cost"}, "amount_fen": "1"}
        for item in purchases
    ]
    redeemed["data"]["net_proceeds_fen"] = "502"
    actual = app.accounting_apply(request(company, purchases[-1], redeemed))
    assert actual["completion"] == "complete", actual
    adopted = result(app, company, redeemed)
    assert adopted["target"]["values"] == {
        "fund_id": prototype["data"]["fund_id"],
        "cost_fen": "501",
        "net_proceeds_fen": "502",
        "gain_loss_fen": "1",
    }
    assert len(adopted["basis"]["source_results"]) == 501
    assert len(adopted["basis"]["adopted_quantities"]) == 501


def test_resource_provider_order_cannot_change_adopted_cost_identity(tmp_path, monkeypatch):
    investment = create_investment()
    evaluate = investment.evaluate_slot

    class ReorderedResources:
        def __init__(self, context):
            self.context = context

        def __getattr__(self, name):
            return getattr(self.context, name)

        def resource_definitions(self, refs, kind):
            return tuple(reversed(self.context.resource_definitions(refs, kind)))

    def reordered(business_id, slot, context):
        return evaluate(business_id, slot, ReorderedResources(context))

    monkeypatch.setattr(investment, "evaluate_slot", reordered)
    app, company = assemble(tmp_path, investment=investment)
    *_, first, redeemed = six()
    first["data"]["purchase_price_fen"] = "50000"
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    second["data"]["purchase_price_fen"] = "60000"
    redeemed["data"]["net_proceeds_fen"] = "113000"
    redeemed["data"]["costs"] = [
        {"source": {"business_id": item["business_id"], "slot": "cost"}, "amount_fen": value}
        for item, value in ((first, "50000"), (second, "60000"))
    ]
    actual = app.accounting_apply(request(company, first, second, redeemed))
    assert actual["completion"] == "complete", actual
    adopted = result(app, company, redeemed)
    assert adopted["target"]["values"]["gain_loss_fen"] == "3000"
    assert {
        x["source"]["business_id"]: x["amount_fen"] for x in adopted["basis"]["adopted_quantities"]
    } == {first["business_id"]: "50000", second["business_id"]: "60000"}
