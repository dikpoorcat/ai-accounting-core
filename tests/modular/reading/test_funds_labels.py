"""Shared formal meanings survive payment aggregation and later changes to current facts."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.reading._business_labels import (
    BusinessLabel,
    business_label,
    settlement_label,
)
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, fact, payment, six
from tests.modular.money.test_payroll import inputs as payroll_inputs

from .test_funds import apply, capture, funding, initialize, page
from .test_funds_integrity import original_row, pair


def formal(kind, data):
    return SimpleNamespace(target={"kind": kind}, basis={"input": data})


@pytest.mark.parametrize(
    "kind,data,category,title",
    [
        ("financing.owner_funding", {"funding_kind": "loan"}, None, "股东借入款"),
        ("financing.owner_funding", {"funding_kind": "capital"}, None, "股东实缴出资"),
        ("people.remuneration", {"type": "wage"}, "net_pay", "员工工资"),
        ("people.remuneration", {"type": "labor"}, "net_pay", "劳务报酬"),
        ("people.remuneration", {"type": "bonus"}, "net_pay", "员工奖金"),
        ("people.remuneration", {"type": "wage"}, "social", "社保费"),
        ("people.remuneration", {"type": "wage"}, "housing", "住房公积金"),
        ("people.remuneration", {"type": "labor"}, "withholding", "代扣个人所得税"),
        ("operations.expense", {"expense_class": "bank_fee"}, "trade", "银行手续费"),
        (
            "operations.other_income",
            {"income_kind": "retained_verification"},
            None,
            "账户验证收入",
        ),
        ("taxes.assessment", {"type": "income_tax"}, "tax", "企业所得税"),
        ("unrecognized.future_business", {}, None, "业务用途未明确"),
    ],
)
def test_business_label_uses_formal_type_and_the_specific_resource(kind, data, category, title):
    resource = (
        None
        if category is None
        else SimpleNamespace(value={"category": category}, reference={"slot": "primary"})
    )
    assert business_label(formal(kind, data), resource).title == title


def test_unknown_and_mixed_labels_are_text_not_inferred_accounting_or_composite_codes():
    content = formal("money.movement", {"type": "settlement"})
    assert settlement_label(content, (), "outflow") == BusinessLabel("unknown", "付款用途未明确")
    actual = settlement_label(
        content,
        (BusinessLabel("wage", "员工工资"), BusinessLabel("unknown", "业务用途未明确")),
        "outflow",
    )
    assert actual == BusinessLabel("mixed_settlement", "支付员工工资、其他已登记款项")
    assert settlement_label(
        content, (BusinessLabel("wage", "员工工资"),) * 2, "outflow"
    ) == BusinessLabel("wage", "支付员工工资")


def test_excess_receipts_and_payments_use_the_formal_obligation_direction():
    content = formal("operations.overpayment", {})
    assert business_label(content).title == "超额收付款处理"
    for direction, expected in (("receivable", "多付款追收"), ("payable", "多收款退回")):
        resource = SimpleNamespace(value={"category": "other", "direction": direction})
        assert business_label(content, resource).title == expected


def labels(case, month="2026-01"):
    capture(case, month)
    return {
        row["subject_id"]: row["list_summary"]
        for row in page(case, month)["data"]["collections"]["movements"]["items"]
    }


def test_other_receipts_keep_their_distinct_purposes_in_actual_funds_pages(case):
    initialize(case)
    kinds = (
        ("bank_interest", "银行利息收入"),
        ("company_reward", "公司奖励收入"),
        ("retained_verification", "账户验证收入"),
    )
    receipts = [
        fact(
            "operations.other_income",
            {"income_kind": kind, "amount_fen": "1", "funds_account_id": BANK},
            actual="2026-01-15",
        )
        for kind, _ in kinds
    ]
    apply(case, *receipts)
    actual = labels(case)
    assert [actual[item["business_id"]] for item in receipts] == [title for _, title in kinds]
    data = page(case)["data"]
    assert data["inflow_fen"] == "3" and data["outflow_fen"] == "0"
    assert data["movement_count"] == 3 and data["total_fen"] == "3"


def test_loan_capital_and_frozen_old_type_keep_their_exact_formal_meaning(case):
    initialize(case)
    loan, capital = funding("100"), funding("200", "2026-01-03")
    capital["data"]["funding_kind"] = "capital"
    rows = [
        original_row(case, str(uuid4()), "100", "2026-01-02", "inflow"),
        original_row(case, str(uuid4()), "200", "2026-01-03", "inflow"),
    ]
    loan, capital, *_ = apply(case, loan, capital, *pair(BANK, rows, [loan, capital]))
    actual = labels(case)
    assert actual[loan["business_id"]] == "股东借入款"
    assert actual[capital["business_id"]] == "股东实缴出资"
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    loan["data"]["funding_kind"] = "capital"
    result = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": loan}],
        )
    )
    assert result["completion"] == "complete", result
    assert labels(case) == actual
    assert (
        case.get(loan["business_id"])["businesses"][0]["fact"]["data"]["funding_kind"] == "capital"
    )


def test_mixed_payment_keeps_both_actual_purposes_in_its_display_summary(case):
    initialize(case)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "300"
    expense = case.expense("700")
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(purchase, 1000, party=COUNTERPARTY, slot="primary")
    paid["data"]["allocations"] = [
        {
            "source": {"business_id": purchase["business_id"], "slot": "primary"},
            "amount_fen": "300",
        },
        {"source": {"business_id": expense["business_id"], "slot": "primary"}, "amount_fen": "700"},
    ]
    apply(case, purchase, expense, paid)
    actual = labels(case)[paid["business_id"]]
    assert actual.startswith("支付")
    assert set(actual.removeprefix("支付").split("、")) == {"货币基金申购", "管理费用"}
    data = page(case)["data"]
    assert data["outflow_fen"] == "1000" and data["investments"]["actual_payments_fen"] == "300"


def test_normal_wage_labor_social_and_housing_payments_keep_their_individual_purpose(case):
    case.seed(expense=False)
    call, gross, wage = payroll_inputs(case.company, pay=False)
    labor_person = str(uuid4())
    call["objects"].append(
        {
            "object_id": labor_person,
            "kind": "person",
            "name": "合成劳务人员",
            "expected_seq": 0,
            "data": {},
        }
    )
    labor = fact(
        "people.remuneration",
        {
            "type": "labor",
            "person_id": labor_person,
            "gross_fen": "50000",
            "destination": {"type": "expense", "area": "administration"},
            "withholding_state": "deduction_established",
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "0",
                "basis_type": "final_statement",
                "source": {"source_id": case.source_id, "location": "合成最终扣税资料"},
            },
        },
    )
    labor["evidence_ids"] = [case.source_id]
    call["mutations"].append({"op": "create", "expected_seq": 0, "fact": labor})
    result = case.app.accounting_apply(call)
    assert result["completion"] == "complete", result
    account = gross["data"]["funds_account_id"]
    paid = []
    for source, slots, value in (
        (wage, ("net_pay",), "830000"),
        (labor, ("net_pay",), "50000"),
        (wage, ("employee_social",), "100000"),
        (wage, ("employee_housing",), "50000"),
    ):
        data = {
            "type": "settlement",
            "funds_account_id": account,
            "direction": "outflow",
            "amount_fen": value,
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": slots[0]},
                    "amount_fen": value,
                }
            ],
        }
        paid.append(fact("money.movement", data, actual="2026-01-20"))
    apply(case, *paid)
    actual = labels(case)
    assert [actual[item["business_id"]] for item in paid] == [
        "支付员工工资",
        "支付劳务报酬",
        "支付社保费",
        "支付住房公积金",
    ]
