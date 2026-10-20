"""Tax and opening attribution survives independently of resource production."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    RECOVERY,
    SOURCE,
    SUPPLIER,
    _candidate,
    _payment_at,
    adopt_declared,
    expense,
    seed_payment,
)
from test_adoption import (
    store as store,
)

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.ledger.public import create_ledger


def _object(uow, object_id, kind):
    uow.repository("identity").session.execute(
        "INSERT INTO identity_object VALUES(?,?,?,?,?,?,?,?,?)",
        (object_id, kind, "合成对象", "a", 1, "{}", "0" * 64, 0, str(uuid4())),
    )


def test_real_bonus_january_result_can_have_february_tax_and_january_payment(store):
    person = str(uuid4())
    fact = {
        "business_id": BUSINESS,
        "kind": "people.remuneration",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "bonus",
            "person_id": person,
            "gross_fen": "100000",
            "expense_area": "sales",
            "tax_basis": {
                "tax_period": "2026-02",
                "tax_fen": "10000",
                "basis_type": "final_statement",
                "source": {"source_id": SOURCE, "location": "实际奖金的已明确二月税期"},
            },
        },
    }

    class Inputs:
        def get_fact(self, business_id):
            return deepcopy(fact) if business_id == BUSINESS else None

        def get_object(self, object_id):
            return {"object_id": person, "kind": "person"} if object_id == person else None

        def resolve_sources(self, ids):
            assert set(ids) <= {SOURCE}
            return [{"source_id": source_id} for source_id in ids]

    people, ledger, inputs = create_people(), create_ledger(), Inputs()
    description = people.describe_change(
        [{"business_id": BUSINESS, "op": "create", "expected_seq": 0, "fact": fact}], inputs
    )
    plan = people.evaluate_slot(BUSINESS, "main", inputs)
    change = {**description, "plans": [plan]}
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        _object(uow, person, "person")
        uow.repository("application").session.execute(
            "UPDATE business_head SET family='people',kind='people.remuneration' "
            "WHERE business_id=?",
            (BUSINESS,),
        )
        adopt_declared(ledger, [change], uow)
        candidate = ledger.begin_candidate([], uow).complete()
        ref = {"business_id": BUSINESS, "slot": "tax"}
        state = candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01"))[0]
        assert state.definition.value["business_period"] == "2026-02"
        assert state.remaining_fen == 10000
        payment = _payment_at("10000")
        paid = payment["plans"][0]
        payment["businesses"][0]["fact"]["data"].pop("counterparty_id")
        paid["basis"]["input"]["allocations"][0]["source"]["slot"] = "tax"
        paid["lines"][0].update(account_code="222103")
        paid["lines"][0].pop("party_id")
        paid["settlements"][0]["source"]["slot"] = "tax"
        paid["fund_movements"][0].pop("party_id")
        paid["fund_movements"][0]["cashflow_parts"][0]["category"] = "tax_payments"
        paid["basis"]["source_results"] = [state.definition.source]
        adopt_declared(ledger, [payment], uow)
    with store.read_unit(COMPANY) as read:
        definition = ledger.resource_definitions([ref], "obligation", read)[0]
        assert definition.value["business_period"] == "2026-02"
        result = ledger.get_results([BUSINESS], read)[0]
        assert result["reference"]["business_period"] == "2026-01"
        assert result["target"]["values"]["net_fen"] == "90000"
        assert ledger.resolve([ref], "obligation", read)[0]["balance"]["remaining_fen"] == "0"


def _opening(person, fund):
    change = expense()
    data = {
        "basis": "existing_ledger",
        "members": [
            {
                "member_id": str(uuid4()),
                "evidence_ids": [SOURCE],
                "data": {
                    "type": "tax",
                    "authority_id": SUPPLIER,
                    "tax_kind": "individual_income_tax",
                    "balance_kind": "payable",
                    "amount_fen": "10000",
                    "tax_period": "2025-12",
                },
            },
            {
                "member_id": str(uuid4()),
                "evidence_ids": [SOURCE],
                "data": {
                    "type": "payroll_payable",
                    "person_id": person,
                    "recipient_id": person,
                    "payroll_period": "2025-12",
                    "component": "net",
                    "amount_fen": "90000",
                },
            },
            {
                "member_id": str(uuid4()),
                "evidence_ids": [SOURCE],
                "data": {
                    "type": "money_fund",
                    "fund_id": fund,
                    "counterparty_id": SUPPLIER,
                    "original_lot_reference": "有据的2025年12月原申购批次",
                    "cost_fen": "100000",
                    "classification": "readily_redeemable_held_not_over_one_year",
                    "cost_basis": "documented_cost",
                },
            },
        ],
    }
    change["domain"] = "opening"
    change["businesses"][0]["fact"].update(kind="opening.package", data=data)
    plan = change["plans"][0]
    plan.update(
        calculation_key="opening.package",
        rule_refs=[
            {
                "key": "opening.package",
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [],
            }
        ],
        target={
            "kind": "opening.package",
            "result_slot": "main",
            "values": {
                "bookkeeping_start": "2026-01",
                "debit_fen": "100000",
                "credit_fen": "100000",
                "member_count": 3,
            },
        },
        basis={
            "kind": "opening.package",
            "result_slot": "main",
            "period": "2026-01",
            "input": data,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        lines=[
            {
                "line_no": 1,
                "account_code": "1101",
                "debit_fen": "100000",
                "credit_fen": "0",
                "object_id": fund,
                "summary": "原批次基金成本",
            },
            {
                "line_no": 2,
                "account_code": "222103",
                "debit_fen": "0",
                "credit_fen": "10000",
                "party_id": SUPPLIER,
                "summary": "开账前十二月税款",
            },
            {
                "line_no": 3,
                "account_code": "221101",
                "debit_fen": "0",
                "credit_fen": "90000",
                "party_id": person,
                "summary": "开账前十二月净薪",
            },
        ],
        cost_lots=[
            {
                "business_id": BUSINESS,
                "slot": "fund_cost",
                "object_id": fund,
                "category": "money_fund",
                "amount_fen": "100000",
                "business_period": "2025-12",
            }
        ],
    )
    base = plan["obligations"][0]
    tax = {
        **base,
        "slot": "tax",
        "category": "withholding",
        "account_code": "222103",
        "amount_fen": "10000",
        "business_period": "2025-12",
        "cashflow_weights": [{"category": "tax_payments", "weight": "1"}],
    }
    wage = {
        **base,
        "slot": "net_pay",
        "party_id": person,
        "category": "net_pay",
        "account_code": "221101",
        "amount_fen": "90000",
        "business_period": "2025-12",
        "cashflow_weights": [{"category": "employee_payments", "weight": "1"}],
    }
    plan["obligations"] = [tax, wage]
    return change


def test_opening_keeps_old_tax_payroll_and_cost_periods_but_is_available_from_boundary(store):
    ledger, person, fund = create_ledger(), str(uuid4()), str(uuid4())
    opening = _opening(person, fund)
    with store.write_unit(COMPANY) as uow:
        _object(uow, person, "person")
        _object(uow, fund, "investment_product")
        adopt_declared(ledger, [opening], uow)
        candidate = ledger.begin_candidate([], uow).complete()
        for kind, slot, amount in (
            ("obligation", "tax", 10000),
            ("obligation", "net_pay", 90000),
            ("cost", "fund_cost", 100000),
        ):
            ref = {"business_id": BUSINESS, "slot": slot}
            state = candidate.resource_states([ref], kind, ResourceCutoff("2026-01"))[0]
            assert state.definition.value["business_period"] == "2025-12"
            assert state.remaining_fen == amount
            with pytest.raises(ContractError, match="尚未成立"):
                candidate.resource_states([ref], kind, ResourceCutoff("2025-12"))
    with store.read_unit(COMPANY) as read:
        for kind, slot in (("obligation", "tax"), ("obligation", "net_pay"), ("cost", "fund_cost")):
            definition = ledger.resource_definitions(
                [{"business_id": BUSINESS, "slot": slot}], kind, read
            )[0]
            assert definition.value["business_period"] == "2025-12"
        assert (
            ledger.get_results([BUSINESS], read)[0]["basis"]["input"]
            == opening["businesses"][0]["fact"]["data"]
        )


def test_opening_cost_consumption_uses_boundary_and_its_own_known_day(store):
    ledger, person, fund = create_ledger(), str(uuid4()), str(uuid4())
    opening = _opening(person, fund)
    release = expense("20000", business_id=RECOVERY)
    data = {
        "fund_id": fund,
        "counterparty_id": SUPPLIER,
        "classification": "readily_redeemable_held_not_over_one_year",
        "cost_basis": "documented_cost",
        "costs": [
            {"source": {"business_id": BUSINESS, "slot": "fund_cost"}, "amount_fen": "20000"}
        ],
        "net_proceeds_fen": "25000",
    }
    release["domain"] = "financing"
    release["businesses"][0]["fact"].update(
        kind="investment.money_fund_redemption", data=data, effective_on="2026-01-15"
    )
    plan = release["plans"][0]
    plan.update(
        calculation_key="investment.money_fund_redemption",
        target={
            "kind": "investment.money_fund_redemption",
            "result_slot": "main",
            "values": {
                "fund_id": fund,
                "cost_fen": "20000",
                "net_proceeds_fen": "25000",
                "gain_loss_fen": "5000",
            },
        },
        basis={
            "kind": "investment.money_fund_redemption",
            "result_slot": "main",
            "period": "2026-01",
            "effective_on": "2026-01-15",
            "input": data,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        rule_refs=[
            {
                "key": "investment.money_fund_redemption",
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [],
            }
        ],
        lines=[
            {
                "line_no": 1,
                "account_code": "1221",
                "debit_fen": "25000",
                "credit_fen": "0",
                "party_id": SUPPLIER,
                "summary": "基金赎回应收",
            },
            {
                "line_no": 2,
                "account_code": "1101",
                "debit_fen": "0",
                "credit_fen": "20000",
                "object_id": fund,
                "summary": "消耗原批次成本",
            },
            {
                "line_no": 3,
                "account_code": "5111",
                "debit_fen": "0",
                "credit_fen": "5000",
                "summary": "本次基金收益",
            },
        ],
        cost_consumptions=[
            {
                "source": {"business_id": BUSINESS, "slot": "fund_cost"},
                "consumer": {"business_id": RECOVERY, "slot": "cost"},
                "amount_fen": "20000",
                "period": "2026-01",
            }
        ],
    )
    plan["obligations"][0].update(
        direction="receivable", account_code="1221", category="investment", amount_fen="25000"
    )
    plan["obligations"][0].pop("cashflow_weights")
    with store.write_unit(COMPANY) as uow:
        _object(uow, person, "person")
        _object(uow, fund, "investment_product")
        uow.repository("application").business(
            RECOVERY, family="financing", kind="investment.money_fund_redemption"
        )
        plan["basis"]["source_results"] = [
            ledger.get_result_source(BUSINESS, "main", uow, plan=opening["plans"][0])
        ]
        candidate = _candidate(ledger, uow, [opening, release], reverse=True)
        ref = {"business_id": BUSINESS, "slot": "fund_cost"}
        before = candidate.resource_states([ref], "cost", ResourceCutoff("2026-01", "2026-01-10"))[
            0
        ]
        after = candidate.resource_states([ref], "cost", ResourceCutoff("2026-01", "2026-01-20"))[0]
        assert before.consumed_fen == 0 and before.remaining_fen == 100000
        assert after.consumed_fen == 20000 and after.remaining_fen == 80000
        adopt_declared(ledger, [opening, release], uow)
    with store.write_unit(COMPANY) as uow:
        saved = ledger.begin_candidate([], uow).complete()
        assert (
            saved.resource_states([ref], "cost", ResourceCutoff("2026-01", "2026-01-10"))[0]
            == before
        )
        assert (
            saved.resource_states([ref], "cost", ResourceCutoff("2026-01", "2026-01-20"))[0]
            == after
        )
