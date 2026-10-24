"""Existing owner pages consume later payroll tax without inventing wages or payment."""

import pytest

from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as working_case
from tests.modular.domains.people.test_people import PERSON, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import add, bank_object, payment
from tests.modular.domains.people.test_read_remuneration_tax import tax_target, unpaid_wage

from . import test_brief_activity as brief_activity
from . import test_brief_open as brief_open
from .test_employees import page, publish


@pytest.fixture(params=MODES)
def case(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = working_case.__wrapped__(tmp_path)
        try:
            yield next(fixture)
        finally:
            fixture.close()


def test_employee_actual_payments_exclude_tax_transfers_and_keep_original_payroll_count(case):
    case.seed(expense=False)
    wage, bank = unpaid_wage(), bank_object()
    tax = tax_target(wage, "10000")
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "90000"))
    add(case, no_contributions(), wage, tax, paid, objects=[bank])
    data = page(case, publish(case, "2026-02"), employee_id=PERSON)
    employee = data["collections"]["employees"]["items"][0]
    assert employee["batch_count"] == 1
    assert employee["payroll_periods"] == ["2026-01"]
    assert employee["company_cost_fen"] == "0"
    assert employee["individual_income_tax_fen"] == "10000"
    assert employee["net_salary_fen"] == "-10000"
    assert employee["recorded_net_payments_fen"] == employee["direct_net_payments_fen"] == "90000"
    assert employee["other_net_settlements_fen"] == employee["outstanding_net_fen"] == "0"
    assert data["employees"]["direct_net_payments_fen"] == "90000"
    assert data["employees"]["other_net_settlements_fen"] == "0"
    assert data["workforce_cost"]["total_fen"] == "0"


@pytest.mark.parametrize("name, capitalized", [("B17L", False), ("B17P", True)])
def test_labor_source_shows_cutoff_composition_and_keeps_tax_out_of_employee_payment(
    case, name, capitalized
):
    case.seed(expense=False)
    labor = business_case(name)
    labor["data"].pop("tax_basis", None)
    labor["data"]["withholding_state"] = "not_withheld_not_filed"
    tax = tax_target(labor, "10000", basis_type="actual_withholding")
    add(case, labor, tax)
    snapshot = publish(case, "2026-02")
    data = page(case, snapshot, employee_id=labor["data"]["person_id"], section="labor_sources")
    source = data["collections"]["labor_sources"]["items"][0]
    assert source["capitalized"] is capitalized
    assert (source["gross_fen"], source["booked_tax_fen"], source["net_fen"]) == (
        "100000",
        "10000",
        "90000",
    )
    assert source["withholding_method"] == "actual_withholding"
    obligations = {item["name"]: item for item in source["obligations"]}
    assert obligations["net"]["amount_fen"] == obligations["net"]["remaining_fen"] == "90000"
    assert obligations["net"]["paid_fen"] == obligations["net"]["other_settled_fen"] == "0"
    assert obligations["net"]["period_other_settled_fen"] == "0"
    assert obligations["tax"]["amount_fen"] == obligations["tax"]["remaining_fen"] == "10000"
    assert data["workforce_cost"]["total_fen"] == "0"


def test_later_tax_voucher_has_employee_and_business_amount_and_keeps_original_payroll_period(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    tax = tax_target(wage, "10000")
    add(case, no_contributions(), wage, tax)
    brief_activity.capture(case, "2026-02")
    groups = brief_activity.activity(case, "2026-02")
    assert len(groups["items"]) == 1
    group = groups["items"][0]
    assert group["group"] == "people"
    assert group["amount_fen"] == "10000"
    assert group["title"] == "薪酬个人所得税"
    members = brief_activity.group(case, group["key"], "2026-02")
    row = members["members"]["items"][0]
    assert row["subject_id"] == tax["business_id"]
    assert row["party"] not in {"—", "对象名称未提供"}
    assert row["amount_label"] == "转入应交个税金额"
    voucher = members["vouchers"]["items"][0]
    assert voucher["business_amount_fen"] == "10000"

    opened = brief_open.publish(case, "2026-02")
    groups = brief_open.page(case, opened)["collections"]["open_items"]["items"]
    tax_group = next(item for item in groups if item["description"] == "代扣个人所得税")
    assert tax_group["category_key"] == "payroll_payables"
    detail = brief_open.members(case, opened, tax_group["group_key"])
    member = detail["collections"]["members"]["items"][0]
    assert member["payroll_period"] == "2026-01"
    assert member["description"] == "代扣个人所得税"
