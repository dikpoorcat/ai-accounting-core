"""Payroll and opening dues retain the actual person, component and old payroll month."""

from uuid import uuid4

from ai_accounting_modular.reading import _brief_open
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import PERSON, SOURCE
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import (
    add,
    bank_object,
    payment,
    wage_with_actuals,
)

from .test_brief_open import members, page, publish


def test_wage_contribution_pairs_are_per_person_month_and_share_exact_complete_voucher(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    labor = business_case("B17P")
    labor["data"].update(person_id=PERSON, gross_fen="300000")
    labor["data"]["tax_basis"]["tax_fen"] = "30000"
    bank = bank_object()
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "100000"))
    paid["data"]["allocations"][0]["source"]["slot"] = "employee_social"
    paid["data"]["amount_fen"] = paid["data"]["allocations"][0]["amount_fen"] = "30000"
    add(case, wage, employment, actual, labor, paid, objects=[bank])
    snapshot = publish(case)
    data = page(case, snapshot)
    groups = data["collections"]["open_items"]["items"]
    categories = {row["key"]: row for row in data["open_items"]["categories"]}
    assert categories["payroll_payables"]["count"] == 6
    assert categories["labor_payables"]["count"] == 2
    social = next(row for row in groups if row["description"] == "社保费")
    assert social["party"] == "合成人员" and social["member_count"] == 2
    detail = members(case, snapshot, social["group_key"])["collections"]
    parts = {row["contribution_component"]: row for row in detail["members"]["items"]}
    assert set(parts) == {"employee_social", "employer_social"}
    assert {row["payroll_period"] for row in parts.values()} == {"2026-01"}
    assert len({row["contribution_group_key"] for row in parts.values()}) == 1
    assert len(detail["vouchers"]["items"]) == 1
    assert {row["voucher_version_id"] for row in parts.values()} == {
        detail["vouchers"]["items"][0]["voucher_version_id"]
    }
    assert parts["employee_social"]["outstanding_fen"] == "50000"
    assert parts["employee_social"]["current_outstanding_fen"] == "20000"
    assert parts["employer_social"]["current_outstanding_fen"] == "60000"
    with case.kernel.storage.read_unit(case.company) as read:
        scope = _brief_open.member_scope(snapshot, read, parts["employee_social"]["id"])
    assert scope["obligations"][0]["person_id"] == PERSON
    assert scope["obligations"][0]["component"] == "employee_social"
    assert scope["allocations"] == []


def test_opening_social_person_is_not_the_receiving_institution_and_net_keeps_original_month(case):
    bank, recipient = bank_object(), str(uuid4())
    amounts = {"net": "10000", "employee_social": "3000", "employer_social": "5000"}
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [SOURCE],
                    "data": {
                        "type": "bank",
                        "funds_account_id": bank["object_id"],
                        "balance_fen": "18000",
                    },
                },
                *[
                    {
                        "member_id": str(uuid4()),
                        "evidence_ids": [SOURCE],
                        "data": {
                            "type": "payroll_payable",
                            "person_id": PERSON,
                            "recipient_id": recipient,
                            "payroll_period": "2025-12",
                            "component": component,
                            "amount_fen": amount,
                        },
                    }
                    for component, amount in amounts.items()
                ],
            ],
        },
    }
    add(
        case,
        package,
        objects=[
            bank,
            {
                "object_id": PERSON,
                "kind": "person",
                "name": "期初员工",
                "expected_seq": 0,
                "data": {},
            },
            {
                "object_id": recipient,
                "kind": "organization",
                "name": "实际收款机构",
                "expected_seq": 0,
                "data": {},
            },
        ],
    )
    snapshot = publish(case)
    data = page(case, snapshot)
    assert data["open_items"]["complete"] and data["open_items"]["payable_fen"] == "18000"
    groups = data["collections"]["open_items"]["items"]
    assert {row["party"] for row in groups} == {"期初员工"}
    for group in groups:
        result = members(case, snapshot, group["group_key"])
        assert result["collections"]["vouchers"]["items"] == []
        for item in result["collections"]["members"]["items"]:
            assert item["payroll_period"] == "2025-12" and item["voucher_version_id"] is None
            with case.kernel.storage.read_unit(case.company) as read:
                scope = _brief_open.member_scope(snapshot, read, item["id"])
            assert scope["obligations"][0]["person_id"] == PERSON
            assert scope["obligations"][0]["recipient_id"] == recipient
