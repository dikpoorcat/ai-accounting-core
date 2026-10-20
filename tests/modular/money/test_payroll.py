import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.ledger.public import LedgerQuery
from ai_accounting_modular.money.public import MovementQuery

from .test_application import assemble, get, request

CASES = {
    item["case_id"]: item["when"]
    for item in json.loads(
        (Path(__file__).parents[3] / "docs/redesign/examples/business-facts-cases.json").read_text(
            encoding="utf-8"
        )
    )["valid_cases"]
}


def inputs(company, *, pay=True):
    wage, actual, policy, employment = (
        deepcopy(CASES[key]) for key in ("B17", "B16", "B15", "B14")
    )
    payment = deepcopy(CASES["B29P"])
    text = "合成完整工资与有据代管返入：毛工资10000元、净薪8300元、明确返入备用金1700元。"
    evidence = wage["evidence_ids"][0]
    source = {
        "source_id": evidence,
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "archive_month": "2026-01",
    }
    objects = [
        {
            "object_id": wage["data"]["person_id"],
            "kind": "person",
            "name": "合成职工",
            "expected_seq": 0,
            "data": {},
        },
        {
            "object_id": payment["data"]["funds_account_id"],
            "kind": "bank_account",
            "name": "合成公司银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic-only",
                "currency": "CNY",
                "owner": "company",
            },
        },
    ]
    facts = (
        [payment, wage, actual, policy, employment] if pay else [wage, actual, policy, employment]
    )
    return request(company, *facts, objects=objects, sources=[source]), payment, wage


def test_one_real_gross_bank_payment_settles_complete_net_group_and_known_reserve_cost(tmp_path):
    app, storage, company, money, ledger = assemble(tmp_path, people=True)
    call, payment, wage = inputs(company)
    result = app.accounting_apply(call)
    assert result["completion"] == "complete", result
    with storage.read_unit(company) as read:
        movements = money.query_movements(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(payment["data"]["funds_account_id"]),
            PageQuery(),
            read,
        )
        assert movements.filtered_count == 1
        actual = movements.items[0]["movement"]
        assert actual["amount_fen"] == "1000000"
        assert actual["cashflow_parts"] == [
            {"category": "employee_payments", "amount_fen": "830000"},
            {"category": "other_operating_payments", "amount_fen": "170000"},
        ]
        balance = ledger.resolve(
            [{"business_id": wage["business_id"], "slot": "net_pay"}], "obligation", read
        )[0]["balance"]
        assert balance["money_settled_fen"] == "830000"
        assert balance["remaining_fen"] == "0"
        result = ledger.get_results([payment["business_id"]], read)[0]
        assert result["target"]["values"]["reserve_expense_fen"] == "170000"
        voucher = result["vouchers"][0]
        assert [
            (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in voucher["lines"]
        ] == [("221101", "830000", "0"), ("5602", "170000", "0"), ("1002", "0", "1000000")]
        assert ledger.query_vouchers(LedgerQuery(), read).total == 2


@pytest.mark.parametrize("fault", ["partial", "wrong_gross", "return_month", "return_date"])
def test_gross_group_rejects_incomplete_or_untrue_model_without_any_formal_payment(tmp_path, fault):
    app, storage, company, money, _ = assemble(tmp_path, people=True)
    call, payment, _ = inputs(company)
    if fault == "partial":
        payment["data"]["allocations"][0]["amount_fen"] = "829999"
        payment["data"]["reserve_expense_fen"] = "170001"
    elif fault == "wrong_gross":
        payment["data"]["amount_fen"] = "1000001"
        payment["data"]["reserve_expense_fen"] = "170001"
    elif fault == "return_month":
        payment["data"]["return_period"] = "2026-02"
    elif fault == "return_date":
        payment["data"]["returned_on"] = "2026-01-19"
    result = app.accounting_apply(call)
    assert result["completion"] == "not_saved", result
    with storage.read_unit(company) as read:
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(payment["data"]["funds_account_id"]),
                PageQuery(),
                read,
            ).filtered_count
            == 0
        )


def test_same_month_new_wage_member_cannot_hide_outside_an_existing_gross_group(tmp_path):
    app, _, company, _, _ = assemble(tmp_path, people=True)
    call, payment, wage = inputs(company)
    assert app.accounting_apply(call)["completion"] == "complete"
    before = get(app, company, payment["business_id"], wage["business_id"])
    new_person = str(uuid4())
    employment = deepcopy(CASES["B14"])
    employment["business_id"] = str(uuid4())
    employment["data"]["employee_id"] = new_person
    employment["data"].update(
        social_insurance_participating=False, housing_fund_participating=False
    )
    employment["data"].pop("social_insurance_base_fen", None)
    employment["data"].pop("housing_fund_base_fen", None)
    extra = deepcopy(CASES["B17Z"])
    extra["business_id"] = str(uuid4())
    extra["data"]["person_id"] = new_person
    extra["data"]["employment_id"] = employment["business_id"]
    response = app.accounting_apply(
        request(
            company,
            extra,
            employment,
            objects=[
                {
                    "object_id": new_person,
                    "kind": "person",
                    "name": "新增合成员工",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
    )
    assert response["completion"] == "not_saved", response
    assert get(app, company, payment["business_id"], wage["business_id"]) == before
