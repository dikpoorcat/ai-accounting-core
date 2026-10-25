"""Tax targets use known source amounts and existing debts, never another wage cost."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.people.public import create_people

from .test_application import assembled_with_money as assembled_with_money
from .test_application import get, replacement, request
from .test_people import PERSON, SOURCE, Context, case, evaluate, no_contributions


def wage(*, initial=None):
    fact = case("B17Z")
    if initial is None:
        fact["data"].pop("tax_basis")
    else:
        fact["data"]["tax_basis"]["tax_fen"] = initial
    return fact


def goal(original, amount="20000", *, period="2026-02", tax_period=None, components=None):
    return {
        "business_id": str(uuid4()),
        "kind": "people.remuneration_tax",
        "period": period,
        "evidence_ids": [SOURCE],
        "data": {
            "remuneration_id": original["business_id"],
            "tax_period": tax_period or period,
            "basis_type": "actual_withholding",
            "components": components
            if components is not None
            else [{"tax_fen": amount, "source": {"source_id": SOURCE, "location": "完整扣税表"}}],
        },
    }


def plan(target, original, *extra):
    return evaluate(target, original, no_contributions(), *extra)


def declaration(original, amount="20000", *, tax_period="2026-02"):
    identifier, row = str(uuid4()), str(uuid4())
    return {
        "business_id": identifier,
        "kind": "taxes.actual_declaration",
        "period": tax_period,
        "evidence_ids": [SOURCE],
        "data": {
            "type": "payroll",
            "tax_period": tax_period,
            "source": {"source_id": SOURCE, "location": "完整个税申报"},
            "rows": [
                {
                    "row_id": row,
                    "person_id": PERSON,
                    "income_category": "wage",
                    "tax_period": tax_period,
                    "declared_tax_fen": amount,
                    "allocations": [
                        {"remuneration_id": original["business_id"], "tax_fen": amount}
                    ],
                    "source": {"source_id": SOURCE, "location": "个税申报人员行"},
                }
            ],
        },
    }, {"declaration_id": identifier, "row_id": row}


@pytest.mark.parametrize(
    "initial,target,expected",
    [(None, "20000", "20000"), ("10000", "10000", "0"), ("10000", "12000", "2000")],
)
def test_target_uses_initial_adopted_component_and_creates_only_the_difference(
    initial, target, expected
):
    original = wage(initial=initial)
    target = goal(original, target, tax_period="2026-01")
    result = plan(target, original)
    values = result["target"]["values"]
    assert values["initial_tax_fen"] == (initial or "0")
    assert values["additional_tax_fen"] == expected
    assert result["fund_movements"] == result["settlements"] == result["cost_lots"] == []
    assert all(
        line["account_code"] not in {"560201", "560101", "540101"} for line in result["lines"]
    )
    if expected == "0":
        assert (
            result["lines"] == result["obligations"] == result["obligation_reclassifications"] == []
        )
    else:
        assert [
            (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in result["lines"]
        ] == [("221101", expected, "0"), ("222103", "0", expected)]
        assert result["obligations"][0]["slot"] == "tax"
        assert result["obligation_reclassifications"][0]["source"] == {
            "business_id": original["business_id"],
            "slot": "net_pay",
        }
    adopted = result["basis"]["adopted_quantities"][0]
    assert adopted["type"] == "remuneration"
    assert adopted["values"]["gross_fen"] == "100000"
    assert adopted["period"] == "2026-01"


def test_initial_tax_only_seeds_its_own_tax_period():
    original = wage(initial="10000")
    result = plan(goal(original, "2000"), original)
    assert result["target"]["values"]["initial_tax_fen"] == "0"
    assert result["target"]["values"]["additional_tax_fen"] == "2000"


@pytest.mark.parametrize("name,account", [("B17B", "221101"), ("B17L", "224104")])
def test_bonus_and_labor_split_their_existing_payable_without_another_cost(name, account):
    original = case(name)
    result = plan(goal(original), original)
    assert result["target"]["values"]["remuneration_type"] == original["data"]["type"]
    assert result["lines"][0]["account_code"] == account
    assert result["lines"][0]["debit_fen"] == "20000"
    assert result["cost_lots"] == result["report_contributions"] == []


def test_parts_are_summed_and_source_reordering_does_not_change_accounting():
    original = wage(initial="10000")
    parts = [
        {"tax_fen": "10000", "source": original["data"]["tax_basis"]["source"]},
        {"tax_fen": "2000", "source": {"source_id": SOURCE, "location": "追加税款行"}},
    ]
    target = goal(original, tax_period="2026-01", components=parts)
    context = Context(original, no_contributions(), target)
    people = create_people()
    before = people.evaluate_slot(target["business_id"], "main", context)
    context.facts[target["business_id"]]["data"]["components"].reverse()
    after = people.evaluate_slot(target["business_id"], "main", context)
    assert before == after
    assert before["target"]["values"]["target_tax_fen"] == "12000"
    assert before["target"]["values"]["additional_tax_fen"] == "2000"


def test_actual_declaration_allocation_supplies_amount_without_caller_arithmetic():
    original = wage()
    actual, reference = declaration(original)
    target = goal(original, components=[reference])
    result = plan(target, original, actual)
    assert result["target"]["values"]["target_tax_fen"] == "20000"
    assert result["basis"]["input"]["components"] == [{**reference, "tax_fen": "20000"}]


@pytest.mark.parametrize("duplicate", ["direct", "declared", "mixed"])
def test_duplicate_component_or_actual_row_is_rejected(duplicate):
    original = wage()
    actual, reference = declaration(original)
    direct = {"tax_fen": "20000", "source": actual["data"]["rows"][0]["source"]}
    parts = [direct, direct] if duplicate == "direct" else [reference, reference]
    if duplicate == "mixed":
        parts = [reference, direct]
    target = goal(original, components=parts)
    with pytest.raises(ContractError, match="重复"):
        plan(target, original, actual)


def test_target_below_initial_tax_never_invents_an_employee_refund():
    original = wage(initial="10000")
    with pytest.raises(ContractError, match="低于"):
        plan(goal(original, "9000", tax_period="2026-01"), original)


@pytest.mark.parametrize("missing", ["remuneration", "declaration", "row", "allocation"])
def test_references_and_missing_actual_allocation_keep_distinct_feedback(missing):
    original = wage()
    actual, reference = declaration(original)
    target = goal(original, components=[reference])
    context = Context(target, no_contributions())
    if missing != "remuneration":
        context.facts[original["business_id"]] = original
    if missing in {"row", "allocation"}:
        context.facts[actual["business_id"]] = actual
    if missing == "row":
        target["data"]["components"][0]["row_id"] = str(uuid4())
        context.facts[target["business_id"]] = target
    elif missing == "allocation":
        actual["data"]["rows"][0]["allocations"] = [
            {"remuneration_id": str(uuid4()), "tax_fen": "0"}
        ]
        actual["data"]["rows"][0]["declared_tax_fen"] = "0"
        context.facts[actual["business_id"]] = actual
    with pytest.raises(ContractError) as caught:
        create_people().evaluate_slot(target["business_id"], "main", context)
    expected = {
        "remuneration": ("dependency_conflict", "result_not_ready", "/data/remuneration_id"),
        "declaration": (
            "invalid_input",
            "reference_not_found",
            "/data/components/0/declaration_id",
        ),
        "row": ("invalid_input", "reference_not_found", "/data/components/0/row_id"),
        "allocation": ("needs_information", "missing_fact", "/data/rows/0/allocations"),
    }
    code, issue_code, field = expected[missing]
    assert caught.value.code == code
    problem = caught.value.fact_issues[0]
    assert problem["code"] == issue_code and problem["field"] == field
    assert problem["business_id"] == (
        actual["business_id"] if missing == "allocation" else target["business_id"]
    )


def tax_payment(business_id, amount="10000"):
    bank = str(uuid4())
    account = {
        "object_id": bank,
        "kind": "bank_account",
        "name": "合成银行",
        "expected_seq": 0,
        "data": {
            "bank_name": "合成银行",
            "account_number": "synthetic",
            "currency": "CNY",
            "owner": "company",
        },
    }
    paid = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-02",
        "effective_on": "2026-02-18",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": amount,
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": business_id, "slot": "tax"},
                    "amount_fen": amount,
                }
            ],
        },
    }
    return account, paid


def test_real_seed_paid_already_still_accepts_same_total_without_deducting_again(
    assembled_with_money,
):
    app, company = assembled_with_money
    original = wage(initial="10000")
    initial = request(company, no_contributions(), original)
    account, paid = tax_payment(original["business_id"])
    initial["objects"].append(account)
    initial["mutations"].append({"op": "create", "expected_seq": 0, "fact": paid})
    result = app.accounting_apply(initial)
    assert result["completion"] == "complete", result
    target = goal(original, "10000", tax_period="2026-01")
    result = app.accounting_apply(request(company, target))
    assert result["completion"] == "complete", result
    created = get(app, company, target["business_id"])["businesses"][0]["results"][0]
    assert created["vouchers"] == []
    assert created["target"]["values"]["additional_tax_fen"] == "0"
    assert get(app, company, paid["business_id"])["businesses"][0]["fact"] == paid


@pytest.mark.parametrize("target_total", ["10000", "11000"])
def test_decreasing_already_paid_additional_tax_does_not_invent_a_refund(
    assembled_with_money, target_total
):
    app, company = assembled_with_money
    original = wage(initial="10000")
    target = goal(original, "12000", tax_period="2026-01")
    account, paid = tax_payment(target["business_id"], "2000")
    initial = request(company, no_contributions(), original, target, paid)
    initial["objects"].append(account)
    saved = app.accounting_apply(initial)
    assert saved["completion"] == "complete", saved
    ids = [original["business_id"], target["business_id"], paid["business_id"]]
    before = get(app, company, *ids)
    target["data"]["components"][0]["tax_fen"] = target_total
    saved = app.accounting_apply(replacement(company, target))
    assert saved["completion"] == "not_saved", saved
    assert saved["error"] == "dependency_conflict", saved
    assert get(app, company, *ids) == before


def test_multiple_tax_periods_share_one_wage_capacity_and_natural_key(assembled_with_money):
    app, company = assembled_with_money
    original = wage()
    february, march = goal(original), goal(original, "30000", period="2026-03")
    initial = request(company, no_contributions(), original, february, march)
    saved = app.accounting_apply(initial)
    assert saved["completion"] == "complete", saved
    assert app.accounting_apply(initial)["changes"] == "none"
    with app.storage.read_unit(company) as read:
        remaining = app.ledger.resolve(
            [{"business_id": original["business_id"], "slot": "net_pay"}], "obligation", read
        )[0]["balance"]["remaining_fen"]
    assert remaining == "50000"
    duplicate = goal(original, "20000")
    result = app.accounting_apply(request(company, duplicate))
    assert result["completion"] == "not_saved", result
    assert get(app, company, duplicate["business_id"])["businesses"] == []
    before = get(
        app, company, original["business_id"], february["business_id"], march["business_id"]
    )
    too_large = deepcopy(march)
    too_large["data"]["components"][0]["tax_fen"] = "90000"
    result = app.accounting_apply(replacement(company, too_large))
    assert result["completion"] == "not_saved", result
    assert result["error"] == "dependency_conflict", result
    assert (
        get(app, company, original["business_id"], february["business_id"], march["business_id"])
        == before
    )


def test_corrected_initial_component_recalculates_target_from_its_new_adopted_seed(
    assembled_with_money,
):
    app, company = assembled_with_money
    original = wage(initial="10000")
    target = goal(original, "12000", tax_period="2026-01")
    saved = app.accounting_apply(request(company, no_contributions(), original, target))
    assert saved["completion"] == "complete", saved
    original["data"]["tax_basis"]["tax_fen"] = "8000"
    saved = app.accounting_apply(replacement(company, original))
    assert saved["completion"] == "complete", saved
    current = get(app, company, target["business_id"])["businesses"][0]
    assert current["state"]["seq"] == 1
    assert current["results"][0]["target"]["values"]["target_tax_fen"] == "12000"
    assert current["results"][0]["target"]["values"]["initial_tax_fen"] == "8000"
    assert current["results"][0]["target"]["values"]["additional_tax_fen"] == "4000"
    with app.storage.read_unit(company) as read:
        unpaid = app.ledger.resolve(
            [{"business_id": original["business_id"], "slot": "net_pay"}], "obligation", read
        )[0]["balance"]["remaining_fen"]
    assert unpaid == "88000"


def test_target_can_reduce_its_own_unpaid_difference_without_changing_initial_wage(
    assembled_with_money,
):
    app, company = assembled_with_money
    original = wage(initial="10000")
    target = goal(original, "14000", tax_period="2026-01")
    saved = app.accounting_apply(request(company, no_contributions(), original, target))
    assert saved["completion"] == "complete", saved
    before = get(app, company, original["business_id"])["businesses"]
    target["data"]["components"][0]["tax_fen"] = "12000"
    saved = app.accounting_apply(replacement(company, target))
    assert saved["completion"] == "complete", saved
    assert get(app, company, original["business_id"])["businesses"] == before
    with app.storage.read_unit(company) as read:
        tax = app.ledger.resolve(
            [{"business_id": target["business_id"], "slot": "tax"}], "obligation", read
        )[0]["obligation"]["amount_fen"]
    assert tax == "2000"
