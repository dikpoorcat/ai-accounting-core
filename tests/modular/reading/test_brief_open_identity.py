"""Trade direction and display grouping do not determine the economic claim identity."""

from uuid import uuid4

import pytest

from tests.modular.application._working_case import case as case
from tests.modular.domains.operations.test_tax_lifecycle import policy, sale
from tests.modular.domains.people.test_people import PERSON, SOURCE
from tests.modular.domains.people.test_read_remuneration import add, bank_object
from tests.modular.reading._support import source

from .test_brief_open import apply, members, page, publish


def test_supplier_expense_refund_is_not_a_customer_receivable(case):
    expense = case.seed()
    recovery = {
        "business_id": str(uuid4()),
        "kind": "operations.expense_recovery",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "expense_id": expense["business_id"],
            "counterparty_id": case.party,
            "amount_fen": "20000",
        },
    }
    apply(case, recovery)
    snapshot = publish(case)
    groups = page(case, snapshot)["collections"]["open_items"]["items"]
    recovered = next(row for row in groups if row["description"] == "费用退回款")
    assert recovered["category_key"] == "other_receivables"
    assert recovered["outstanding_fen"] == "20000"
    assert (
        next(row for row in groups if row["description"] == "管理费用")["category_key"]
        == "supplier_payables"
    )
    detail = members(case, snapshot, recovered["group_key"])["collections"]["members"]["items"][0]
    assert detail["subject_id"] == recovery["business_id"] and detail["party"] == "合成供应商"


def test_customer_sale_refund_is_not_a_supplier_payable(case):
    case.seed(expense=False)
    ids = {"source": case.source_id, "party": case.party}
    rule = policy(ids)
    sold = sale(ids, rule, "2026-01")
    returned = {
        "business_id": str(uuid4()),
        "kind": "operations.sale_return",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "sale_id": sold["business_id"],
            "customer_id": case.party,
            "gross_fen": "5050",
            "credit_note_vat_fen": "50",
        },
    }
    apply(case, rule, sold, returned)
    snapshot = publish(case)
    groups = page(case, snapshot)["collections"]["open_items"]["items"]
    refund = next(row for row in groups if row["description"] == "销售退款")
    assert refund["category_key"] == "other_payables" and refund["outstanding_fen"] == "5050"
    assert (
        next(row for row in groups if row["description"] == "服务业务款")["category_key"]
        == "customer_receivables"
    )


def test_opening_categories_follow_declared_nature_instead_of_creditor_direction(case):
    categories = {
        "customer_receivable": "customer_receivables",
        "supplier_service_payable": "supplier_payables",
        "supplier_administration_payable": "supplier_payables",
        "supplier_sales_payable": "supplier_payables",
        "employee_reimbursement": "employee_payables",
        "owner_reimbursement": "other_payables",
        "deposit_receivable": "refundable_deposit_receivables",
        "deposit_payable": "other_payables",
        "other_receivable": "other_receivables",
        "other_payable": "other_payables",
    }
    bank = bank_object()
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {"member_id": str(uuid4()), "evidence_ids": [case.source_id], "data": data}
                for data in (
                    {
                        "type": "bank",
                        "funds_account_id": bank["object_id"],
                        "balance_fen": "400",
                    },
                    *[
                        {
                            "type": "obligation",
                            "counterparty_id": case.party,
                            "nature": nature,
                            "amount_fen": "100",
                            "original_business_reference": nature,
                        }
                        for nature in categories
                    ],
                )
            ],
        },
    }
    apply(
        case,
        package,
        objects=[
            bank,
            {
                "object_id": case.party,
                "kind": "person",
                "name": "合成往来对象",
                "expected_seq": 0,
                "data": {},
            },
        ],
        sources=[source("opening-roles") | {"source_id": case.source_id}],
    )
    snapshot = publish(case)
    data = page(case, snapshot)
    assert (data["open_items"]["receivable_fen"], data["open_items"]["payable_fen"]) == (
        "300",
        "700",
    )
    for group in data["collections"]["open_items"]["items"]:
        item = members(case, snapshot, group["group_key"])["collections"]["members"]["items"][0]
        assert item["category_key"] == categories[item["purpose"]]


@pytest.mark.parametrize("change", ("original_reference", "nature", "amount"))
def test_opening_ordinary_current_progress_follows_the_actual_declared_claim(case, change):
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [case.source_id],
                    "data": {
                        "type": "obligation",
                        "counterparty_id": case.party,
                        "nature": nature,
                        "amount_fen": "100",
                        "original_business_reference": reference,
                    },
                }
                for nature, reference in (
                    ("customer_receivable", "原合同款"),
                    ("other_payable", "另一应付款"),
                )
            ],
        },
    }
    apply(
        case,
        package,
        objects=[
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "合成往来对象",
                "expected_seq": 0,
                "data": {},
            }
        ],
        sources=[source("opening-identity") | {"source_id": case.source_id}],
    )
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    if change == "original_reference":
        package["data"]["members"][0]["data"]["original_business_reference"] = "另一真实合同款"
    elif change == "nature":
        package["data"]["members"][0]["data"]["nature"] = "other_receivable"
    else:
        for member in package["data"]["members"]:
            member["data"]["amount_fen"] = "80"
    changed = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": package}],
            correction_period="2026-02",
        )
    )
    assert changed["completion"] == "complete", changed
    snapshot = publish(case)
    group = next(
        row
        for row in page(case, snapshot)["collections"]["open_items"]["items"]
        if row["category_key"] == "customer_receivables"
    )
    item = members(case, snapshot, group["group_key"])["collections"]["members"]["items"][0]
    assert item["purpose"] == "原合同款" and item["outstanding_fen"] == "100"
    expected = ("open", "80") if change == "amount" else ("reversed", "0")
    assert (item["current_status"], item["current_outstanding_fen"]) == expected


@pytest.mark.parametrize(
    "field,value", (("tax_kind", "enterprise_income_tax"), ("tax_period", "2025-11"))
)
def test_opening_tax_identity_change_is_not_the_old_tax_claim(case, field, value):
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {"member_id": str(uuid4()), "evidence_ids": [case.source_id], "data": data}
                for data in (
                    {
                        "type": "obligation",
                        "counterparty_id": case.party,
                        "nature": "other_receivable",
                        "amount_fen": "100",
                        "original_business_reference": "旧应收款",
                    },
                    {
                        "type": "tax",
                        "authority_id": case.party,
                        "tax_kind": "vat",
                        "balance_kind": "payable",
                        "amount_fen": "100",
                        "tax_period": "2025-12",
                    },
                )
            ],
        },
    }
    apply(
        case,
        package,
        objects=[
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "合成往来机构",
                "expected_seq": 0,
                "data": {},
            }
        ],
        sources=[source("opening-tax") | {"source_id": case.source_id}],
    )
    assert case.close(case.check())["state"] == "closed"
    package["data"]["members"][1]["data"][field] = value
    changed = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": package}],
            correction_period="2026-02",
        )
    )
    assert changed.get("completion") == "complete", changed
    old = next(
        group
        for group in page(case, publish(case))["collections"]["open_items"]["items"]
        if group["description"] == "增值税"
    )
    assert old["outstanding_fen"] == "100"
    assert (old["current_status"], old["current_outstanding_fen"]) == ("reversed", "0")


def test_expense_display_classification_change_does_not_replace_the_supplier_claim(case):
    expense = case.seed()
    assert case.close(case.check())["state"] == "closed"
    expense["data"].update(
        expense_class="sales", report_details=[{"code": "sales_other", "amount_fen": "100000"}]
    )
    changed = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": expense}],
            correction_period="2026-02",
        )
    )
    assert changed.get("completion") == "complete", changed
    old = page(case, publish(case))["collections"]["open_items"]["items"][0]
    assert old["description"] == "管理费用" and old["outstanding_fen"] == "100000"
    assert (old["current_status"], old["current_outstanding_fen"]) == ("open", "100000")
    new = page(case, publish(case, "2026-02"))["collections"]["open_items"]["items"][0]
    assert new["description"] == "销售费用" and new["outstanding_fen"] == "100000"


@pytest.mark.parametrize("change", ("recipient", "payroll_period", "component", "amount"))
def test_opening_payroll_current_claim_uses_person_recipient_period_and_component(case, change):
    bank, first_recipient, second_recipient = bank_object(), str(uuid4()), str(uuid4())
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
                        "balance_fen": "100",
                    },
                },
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [SOURCE],
                    "data": {
                        "type": "payroll_payable",
                        "person_id": PERSON,
                        "recipient_id": first_recipient,
                        "payroll_period": "2025-12",
                        "component": "employee_social",
                        "amount_fen": "100",
                    },
                },
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
                "name": "合成员工",
                "expected_seq": 0,
                "data": {},
            },
            *[
                {
                    "object_id": recipient,
                    "kind": "organization",
                    "name": name,
                    "expected_seq": 0,
                    "data": {},
                }
                for recipient, name in (
                    (first_recipient, "原收款机构"),
                    (second_recipient, "另一收款机构"),
                )
            ],
        ],
    )
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    payroll = package["data"]["members"][1]["data"]
    if change == "recipient":
        payroll["recipient_id"] = second_recipient
    elif change == "payroll_period":
        payroll["payroll_period"] = "2025-11"
    elif change == "component":
        payroll["component"] = "employer_social"
    else:
        payroll["amount_fen"] = package["data"]["members"][0]["data"]["balance_fen"] = "80"
    changed = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": package}],
            correction_period="2026-02",
        )
    )
    assert changed["completion"] == "complete", changed
    january = publish(case)
    old_group = page(case, january)["collections"]["open_items"]["items"][0]
    old = members(case, january, old_group["group_key"])["collections"]["members"]["items"][0]
    assert old["party"] == "合成员工" and old["payroll_period"] == "2025-12"
    assert old["outstanding_fen"] == "100"
    expected = ("open", "80") if change == "amount" else ("reversed", "0")
    assert (old["current_status"], old["current_outstanding_fen"]) == expected
    february = publish(case, "2026-02")
    new_group = page(case, february)["collections"]["open_items"]["items"][0]
    new = members(case, february, new_group["group_key"])["collections"]["members"]["items"][0]
    assert new["outstanding_fen"] == ("80" if change == "amount" else "100")
    assert new["payroll_period"] == payroll["payroll_period"]
