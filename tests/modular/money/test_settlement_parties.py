"""Unknown obligation attribution is distinct from a known, conflicting payee."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.money.public import MovementQuery, create_money

from .conftest import Context, objects, payable, payment
from .test_application import assemble, get, request
from .test_payroll import inputs as payroll_inputs


def unknown(obligation):
    result = deepcopy(obligation)
    result.pop("party_id")
    return result


def test_evidenced_actual_party_does_not_rewrite_unknown_source_attribution(ids):
    fact, obligation = payment(ids), unknown(payable(ids))
    context = Context([fact], objects=objects(ids), obligations=[obligation])
    plan = create_money().evaluate_slot(fact["business_id"], "main", context)
    assert plan["fund_movements"][0]["party_id"] == ids["party"]
    assert "party_id" not in plan["lines"][0]
    assert "party_id" not in plan["basis"]["adopted_quantities"][0]
    assert context.obligations[ids["expense"], "primary"] == obligation


def test_one_individual_payment_can_cover_matching_known_and_unknown_obligations(ids):
    fact = payment(ids, "200000")
    anonymous = unknown(payable({**ids, "expense": str(uuid4())}))
    fact["data"]["allocations"][0]["amount_fen"] = "100000"
    fact["data"]["allocations"].append(
        {
            "source": {"business_id": anonymous["business_id"], "slot": "primary"},
            "amount_fen": "100000",
        }
    )
    context = Context([fact], objects=objects(ids), obligations=[payable(ids), anonymous])
    plan = create_money().evaluate_slot(fact["business_id"], "main", context)
    assert plan["fund_movements"][0]["party_id"] == ids["party"]
    assert plan["lines"][0]["party_id"] == ids["party"]
    assert "party_id" not in plan["lines"][1]


def test_explicit_allocation_recipient_must_match_actual_party_even_for_unknown_source(ids):
    fact = payment(ids)
    other = str(uuid4())
    fact["data"]["allocations"][0]["recipient_id"] = other
    context = Context(
        [fact],
        objects=[*objects(ids), {"object_id": other, "kind": "person"}],
        obligations=[unknown(payable(ids))],
    )
    with pytest.raises(ContractError) as error:
        create_money().evaluate_slot(fact["business_id"], "main", context)
    assert error.value.code == "dependency_conflict"


def test_known_wrong_party_is_still_rejected_when_another_source_is_unknown(ids):
    fact = payment(ids, "200000")
    other = str(uuid4())
    fact["data"]["counterparty_id"] = other
    anonymous = unknown(payable({**ids, "expense": str(uuid4())}))
    fact["data"]["allocations"][0]["amount_fen"] = "100000"
    fact["data"]["allocations"].append(
        {
            "source": {"business_id": anonymous["business_id"], "slot": "primary"},
            "amount_fen": "100000",
        }
    )
    context = Context(
        [fact],
        objects=[*objects(ids), {"object_id": other, "kind": "organization"}],
        obligations=[payable(ids), anonymous],
    )
    with pytest.raises(ContractError):
        create_money().evaluate_slot(fact["business_id"], "main", context)


def test_individual_payment_cannot_merge_two_different_known_parties(ids):
    fact = payment(ids, "200000")
    other = str(uuid4())
    second = payable({**ids, "expense": str(uuid4()), "party": other})
    fact["data"]["allocations"][0]["amount_fen"] = "100000"
    fact["data"]["allocations"].append(
        {
            "source": {"business_id": second["business_id"], "slot": "primary"},
            "amount_fen": "100000",
        }
    )
    context = Context(
        [fact],
        objects=[*objects(ids), {"object_id": other, "kind": "organization"}],
        obligations=[payable(ids), second],
    )
    with pytest.raises(ContractError) as error:
        create_money().evaluate_slot(fact["business_id"], "main", context)
    assert error.value.code == "dependency_conflict"


def test_batch_recipient_rules_are_unchanged(ids):
    fact = payment(ids)
    fact["data"]["payment_method"] = "bank_batch"
    fact["data"].pop("counterparty_id")
    context = Context([fact], objects=objects(ids), obligations=[unknown(payable(ids))])
    with pytest.raises(ContractError) as error:
        create_money().evaluate_slot(fact["business_id"], "main", context)
    assert error.value.code == "needs_information"
    assert error.value.fact_issues[0]["code"] == "missing_recipient"


def payroll(company, *, estimated=False):
    call, prototype, wage = payroll_inputs(company, pay=False)
    organization, person = str(uuid4()), str(uuid4())
    call["objects"].extend(
        [
            {
                "object_id": organization,
                "kind": "organization",
                "name": "合成征收机构",
                "expected_seq": 0,
                "data": {},
            },
            {
                "object_id": person,
                "kind": "person",
                "name": "合成社保结算收款人",
                "expected_seq": 0,
                "data": {},
            },
        ]
    )
    source = call["sources"][0]
    source["text"] += (
        "合成资料确认社保分别向机构和个人实际支付，两笔合计结清该月社保；个税与公积金另付机构。"
    )
    source["sha256"] = sha256(source["text"].encode()).hexdigest()
    if estimated:
        wage["data"].pop("contribution_actual_id")
        wage["data"]["contribution_basis"] = "policy_until_actual"
        call["mutations"] = [
            item
            for item in call["mutations"]
            if item["fact"]["kind"] != "people.contribution_actual"
        ]
    payments = []
    for recipient, allocations in (
        (organization, (("employee_social", "100000"), ("employer_social", "150000"))),
        (person, (("employer_social", "50000"),)),
        (organization, (("employee_housing", "50000"), ("employer_housing", "50000"))),
        (organization, (("tax", "20000"),)),
    ):
        payments.append(
            {
                "business_id": str(uuid4()),
                "kind": "money.movement",
                "period": "2026-01",
                "effective_on": "2026-01-20",
                "evidence_ids": wage["evidence_ids"],
                "data": {
                    "type": "settlement",
                    "funds_account_id": prototype["data"]["funds_account_id"],
                    "direction": "outflow",
                    "amount_fen": str(sum(int(amount) for _, amount in allocations)),
                    "payment_method": "individual",
                    "counterparty_id": recipient,
                    "allocations": [
                        {
                            "source": {"business_id": wage["business_id"], "slot": slot},
                            "amount_fen": amount,
                        }
                        for slot, amount in allocations
                    ],
                },
            }
        )
    return call, wage, organization, person, payments


@pytest.mark.parametrize("estimated", [False, True])
def test_payroll_unknown_liabilities_clear_by_real_institution_and_person_payments(
    tmp_path, estimated
):
    app, storage, company, money, ledger = assemble(tmp_path, people=True)
    call, wage, organization, person, payments = payroll(company, estimated=estimated)
    call["mutations"].extend({"op": "create", "expected_seq": 0, "fact": fact} for fact in payments)
    response = app.accounting_apply(call)
    assert response["completion"] == "complete", response
    observed = get(app, company, wage["business_id"])
    assert observed["businesses"][0]["fact"] == wage
    with storage.read_unit(company) as read:
        states = ledger.resolve(
            [
                {"business_id": wage["business_id"], "slot": slot}
                for slot in (
                    "employee_social",
                    "employer_social",
                    "employee_housing",
                    "employer_housing",
                    "tax",
                )
            ],
            "obligation",
            read,
        )
        assert [state["balance"]["remaining_fen"] for state in states] == ["0"] * 5
        assert all("party_id" not in state["obligation"] for state in states)
        movements = money.query_movements(
            {
                "company_id": company,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(payments[0]["data"]["funds_account_id"]),
            PageQuery(),
            read,
        )
        assert movements.filtered_count == 4
        assert {entry["movement"]["party_id"] for entry in movements.items} == {
            organization,
            person,
        }
        assert sum(int(entry["movement"]["amount_fen"]) for entry in movements.items) == 420000
        for adopted in ledger.get_results([fact["business_id"] for fact in payments], read):
            assert all("party_id" not in line for line in adopted["vouchers"][0]["lines"])
            assert {source["business_id"] for source in adopted["basis"]["source_results"]} == {
                wage["business_id"]
            }
            assert {
                location["source_id"] for location in adopted["basis"]["source_locations"]
            } == set(wage["evidence_ids"])
            assert all(
                "party_id" not in quantity for quantity in adopted["basis"]["adopted_quantities"]
            )


@pytest.mark.parametrize("wrong_party", [False, True])
def test_mixed_known_expense_and_unknown_social_payment_is_atomic_and_preserves_creditor(
    tmp_path, wrong_party
):
    app, storage, company, money, ledger = assemble(tmp_path, people=True)
    call, wage, organization, person, payments = payroll(company)
    expense = {
        "business_id": str(uuid4()),
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": wage["evidence_ids"],
        "data": {
            "amount_fen": "50000",
            "creditor_kind": "supplier",
            "creditor_id": organization,
            "expense_class": "administration",
        },
    }
    call["mutations"].append({"op": "create", "expected_seq": 0, "fact": expense})
    seeded = app.accounting_apply(call)
    assert seeded["completion"] == "complete", seeded
    before = get(app, company, wage["business_id"], expense["business_id"])
    fact = payments[0]
    fact["data"]["amount_fen"] = "300000"
    fact["data"]["allocations"].append(
        {
            "source": {"business_id": expense["business_id"], "slot": "primary"},
            "amount_fen": "50000",
        }
    )
    if wrong_party:
        fact["data"]["counterparty_id"] = person
    response = app.accounting_apply(request(company, fact))
    if wrong_party:
        assert response["completion"] == "not_saved", response
        assert response["error"] == "dependency_conflict"
        assert get(app, company, wage["business_id"], expense["business_id"]) == before
        assert fact["business_id"] in get(app, company, fact["business_id"])["missing_business_ids"]
    else:
        assert response["completion"] == "complete", response
        with storage.read_unit(company) as read:
            state = ledger.resolve(
                [{"business_id": expense["business_id"], "slot": "primary"}], "obligation", read
            )[0]
            assert state["obligation"]["party_id"] == organization
            assert state["balance"]["remaining_fen"] == "0"
            movements = money.query_movements(
                {
                    "company_id": company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(fact["data"]["funds_account_id"]),
                PageQuery(),
                read,
            )
            assert movements.filtered_count == 1
            assert movements.items[0]["movement"]["party_id"] == organization
