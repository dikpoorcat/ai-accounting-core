"""Formal weights survive zero corrections without changing actual money."""

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.money.public import MoneyQuery

from .conftest import Context
from .test_application import assemble, get, payment, request, seed


def recovery(ids, source, value):
    return {
        "business_id": str(uuid4()),
        "kind": "operations.overpayment",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {"source": source, "counterparty_id": ids["party"], "amount_fen": value},
    }


def collected(ids, excess, value):
    fact = payment(
        {**ids, "business": str(uuid4()), "expense": excess["business_id"]}, value, day="2026-01-25"
    )
    fact["data"]["direction"] = "inflow"
    fact["data"]["allocations"][0]["source"]["slot"] = "recovery"
    return fact


def actual_cashflow(money, account_id, read):
    movements = money.query_movements(MoneyQuery(account_id, "2026-01", "2026-01"), read)
    totals = defaultdict(int)
    for value in movements.items:
        actual = value["movement"]
        sign = 1 if actual["direction"] == "outflow" else -1
        assert sum(int(part["amount_fen"]) for part in actual["cashflow_parts"]) == int(
            actual["amount_fen"]
        )
        for part in actual["cashflow_parts"]:
            totals[part["category"]] += sign * int(part["amount_fen"])
    return movements, {category: value for category, value in totals.items() if value}


def test_paid_wage_corrected_to_zero_keeps_known_cashflow_until_complete_actual_recovery(tmp_path):
    system = assemble(tmp_path, people=True)
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    ids.update(party=str(uuid4()), expense=str(uuid4()), source=str(uuid4()))
    text = "合成工资原确认1000元且已银行支付，核实后更正为0，原收款人实际全额退回。"
    source = {
        "source_id": ids["source"],
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "archive_month": "2026-01",
    }
    employment = {
        "business_id": str(uuid4()),
        "kind": "people.employment",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "employee_id": ids["party"],
            "effective_from": "2026-01",
            "social_insurance_participating": False,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
    }
    wage = {
        "business_id": ids["expense"],
        "kind": "people.remuneration",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "wage",
            "person_id": ids["party"],
            "gross_fen": "100000",
            "expense_area": "administration",
            "employment_id": employment["business_id"],
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "0",
                "basis_type": "final_statement",
                "source": {"source_id": ids["source"]},
            },
        },
    }
    paid = payment(ids)
    paid["data"]["allocations"][0]["source"]["slot"] = "net_pay"
    created = app.accounting_apply(
        request(
            company,
            paid,
            wage,
            employment,
            objects=[
                {
                    "object_id": ids["party"],
                    "kind": "person",
                    "name": "合成工资收款人",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
            sources=[source],
        )
    )
    assert created["completion"] == "complete", created
    before = get(app, company, wage["business_id"], paid["business_id"])
    corrected = deepcopy(wage)
    corrected["data"]["gross_fen"] = "0"
    replace = {"op": "replace", "expected_seq": 1, "fact": corrected}
    missing = app.accounting_apply(request(company, mutations=[replace]))
    assert missing["completion"] == "not_saved" and missing["error"] == "dependency_conflict", (
        missing
    )
    assert get(app, company, wage["business_id"], paid["business_id"]) == before

    source_ref = {"business_id": wage["business_id"], "slot": "net_pay"}
    excess = recovery(ids, source_ref, "100000")
    changed = app.accounting_apply(
        request(
            company,
            mutations=[
                {"op": "create", "expected_seq": 0, "fact": excess},
                replace,
            ],
        )
    )
    assert changed["completion"] == "complete", changed
    with storage.read_unit(company) as read:
        original = ledger.resource_definitions([source_ref], "obligation", read)[0].value
        assert original["amount_fen"] == "0"
        assert original["cashflow_weights"] == [{"category": "employee_payments", "weight": "1"}]
        movements, net = actual_cashflow(money, ids["bank"], read)
        assert movements.total == 1 and net == {"employee_payments": 100000}
        assert movements.items[0]["movement"]["amount_fen"] == "100000"
    assert get(app, company, paid["business_id"])["businesses"][0]["fact"] == paid

    received = collected(ids, excess, "100000")
    result = app.accounting_apply(request(company, received))
    assert result["completion"] == "complete", result
    with storage.read_unit(company) as read:
        movements, net = actual_cashflow(money, ids["bank"], read)
        assert movements.total == 2 and net == {}
        assert {value["movement"]["direction"] for value in movements.items} == {
            "inflow",
            "outflow",
        }
        assert all(value["movement"]["amount_fen"] == "100000" for value in movements.items)
        states = ledger.resolve(
            [source_ref, {"business_id": excess["business_id"], "slot": "recovery"}],
            "obligation",
            read,
        )
        assert [state["balance"]["remaining_fen"] for state in states] == ["0", "0"]


@pytest.mark.parametrize("with_offset", [False, True])
def test_mixed_formal_weights_recover_only_the_cash_excess_interval(tmp_path, with_offset):
    system = assemble(tmp_path)
    app, storage, company, money, ledger = system
    ids, expense = seed(system)
    expense = deepcopy(expense)
    expense["data"]["amount_fen"] = "2" if with_offset else "1"
    changed = app.accounting_apply(
        request(
            company,
            mutations=[
                {
                    "op": "replace",
                    "expected_seq": 1,
                    "fact": expense,
                }
            ],
        )
    )
    assert changed["completion"] == "complete", changed
    # This controlled program plan tests a future mixed source through the real
    # ledger. No business Fact or AI API can submit arbitrary classifications.
    context = Context([expense], objects=[{"object_id": ids["party"], "kind": "organization"}])
    operations = create_operations()
    change = operations.describe_change(
        [
            {
                "op": "recalculate",
                "expected_seq": 2,
                "business_id": expense["business_id"],
                "fact": expense,
            }
        ],
        context,
    )
    plan = operations.evaluate_slot(expense["business_id"], "main", context)
    plan["obligations"][0]["cashflow_weights"] = [
        {"category": "employee_payments", "weight": "1"},
        {"category": "other_operating_payments", "weight": "1"},
    ]
    with storage.write_unit(company) as uow:
        ledger.adopt([{**change, "retained_results": [], "plans": [plan]}], uow)
    excess = recovery(ids, {"business_id": expense["business_id"], "slot": "primary"}, "1")
    paid = payment(ids, "2")
    received = collected(ids, excess, "1")
    noncash = []
    if with_offset:
        advance = {
            "business_id": str(uuid4()),
            "kind": "operations.advance",
            "period": "2026-01",
            "evidence_ids": [ids["source"]],
            "data": {
                "side": "supplier",
                "counterparty_id": ids["party"],
                "amount_fen": "1",
                "purpose": "operating",
            },
        }
        offset = {
            "business_id": str(uuid4()),
            "kind": "money.transfer",
            "period": "2026-01",
            "evidence_ids": [ids["source"]],
            "data": {
                "type": "offset",
                "offset_kind": "advance_application",
                "first": {
                    "source": {"business_id": advance["business_id"], "slot": "advance"},
                    "amount_fen": "1",
                },
                "second": {
                    "source": {"business_id": expense["business_id"], "slot": "primary"},
                    "amount_fen": "1",
                },
            },
        }
        noncash = [offset, advance]
    result = app.accounting_apply(request(company, received, excess, paid, *noncash))
    assert result["completion"] == "complete", result
    with storage.read_unit(company) as read:
        recovered = ledger.resource_definitions(
            [
                {"business_id": excess["business_id"], "slot": "recovery"},
            ],
            "obligation",
            read,
        )[0].value
        assert recovered["cashflow_weights"] == [
            {"category": "other_operating_payments", "weight": "1"}
        ]
        movements, net = actual_cashflow(money, ids["bank"], read)
        assert movements.total == 2 and net == {"employee_payments": 1}
        by_direction = {
            value["movement"]["direction"]: value["movement"] for value in movements.items
        }
        assert by_direction["outflow"]["amount_fen"] == "2"
        assert by_direction["outflow"]["cashflow_parts"] == [
            {"category": "employee_payments", "amount_fen": "1"},
            {"category": "other_operating_payments", "amount_fen": "1"},
        ]
        assert by_direction["inflow"]["cashflow_parts"] == [
            {"category": "other_operating_payments", "amount_fen": "1"},
        ]
        balance = ledger.resolve(
            [
                {"business_id": expense["business_id"], "slot": "primary"},
            ],
            "obligation",
            read,
        )[0]["balance"]
        assert balance["money_settled_fen"] == "2"
        assert balance["offset_settled_fen"] == ("1" if with_offset else "0")
        assert balance["remaining_fen"] == "0"
