"""Payroll business components and exact progress follow their actual formal journal."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, posting_lines
from ai_accounting_modular.domains.people import public as people
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_read_remuneration import (
    add,
    correct,
    declaration,
    wage_with_actuals,
)

from .test_brief_activity import activity, capture, group, vouchers
from .test_brief_open import members, page, publish
from .test_business_status import progress


def payroll(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    add(case, wage, employment, actual)
    return wage


def context(member):
    return {
        "detail_scope_key": member["detail_scope_key"],
        "voucher_version_id": member["voucher_version_id"],
    }


def test_payroll_components_share_only_their_phase_voucher_and_progress_amounts(case):
    wage = payroll(case)
    capture(case)
    whole = progress(case, wage["business_id"])["data"]["current_business_result"]
    assert (whole["amount_fen"], whole["amount_label"]) == ("600000", "应发金额")
    rows = activity(case)["items"]
    assert {(row["title"], row["amount_fen"]) for row in rows} == {
        ("员工工资", "600000"),
        ("单位社保", "60000"),
        ("单位公积金", "30000"),
        ("代扣个人社保", "50000"),
        ("代扣个人公积金", "20000"),
        ("代扣个人所得税", "30000"),
    }
    by_label = {item["business_amount_label"]: item for item in vouchers(case)["items"]}
    assert set(by_label) == {"计提金额", "代扣金额"}
    accrual, deduction = by_label["计提金额"], by_label["代扣金额"]
    assert accrual["business_amount_fen"] == accrual["amount_fen"] == "690000"
    assert deduction["business_amount_fen"] == deduction["amount_fen"] == "100000"
    assert accrual["voucher_version_id"] != deduction["voucher_version_id"]
    assert len(accrual["lines"]) == len(deduction["lines"]) == 6

    for row in rows:
        collections = group(case, row["key"])
        member = collections["members"]["items"][0]
        expected = deduction if row["title"].startswith("代扣") else accrual
        assert member["voucher_version_id"] == expected["voucher_version_id"]
        assert collections["vouchers"]["items"] == [expected]
        data = progress(case, wage["business_id"], **context(member))["data"]
        assert data["current_business_result"]["amount_fen"] == row["amount_fen"]
        assert data["detail_scope"]["amount_fen"] == row["amount_fen"]
        assert data["display_profiles"]["business"]["values"]["display_name"] == row["title"]

    for voucher, allowed in (
        (
            accrual,
            {
                "net",
                "employee_social",
                "employee_housing",
                "employer_social",
                "employer_housing",
                "tax",
            },
        ),
        (deduction, {"employee_social", "employee_housing", "tax"}),
    ):
        data = progress(
            case,
            wage["business_id"],
            detail_scope_key="voucher:" + voucher["voucher_version_id"],
            voucher_version_id=voucher["voucher_version_id"],
        )["data"]
        assert data["current_business_result"]["amount_fen"] == voucher["business_amount_fen"]
        assert data["current_business_result"]["amount_label"] == voucher["business_amount_label"]
        assert {item["name"] for item in data["settlements"]["obligations"]} == allowed


def test_open_social_components_select_different_real_vouchers_and_reject_cross_phase(case):
    wage = payroll(case)
    snapshot = publish(case)
    social = next(
        row
        for row in page(case, snapshot)["collections"]["open_items"]["items"]
        if row["description"] == "社保费"
    )
    collections = members(case, snapshot, social["group_key"])["collections"]
    linked = {item["business_amount_label"]: item for item in collections["vouchers"]["items"]}
    assert set(linked) == {"计提金额", "代扣金额"}
    for member in collections["members"]["items"]:
        own = "代扣金额" if member["contribution_component"] == "employee_social" else "计提金额"
        assert member["voucher_version_id"] == linked[own]["voucher_version_id"]
        data = progress(
            case,
            wage["business_id"],
            detail_scope_key="open:" + member["id"],
            voucher_version_id=member["voucher_version_id"],
        )["data"]
        assert data["current_business_result"]["amount_fen"] == member["source_amount_fen"]
        assert len(data["settlements"]["obligations"]) == 1
        wrong = linked["计提金额" if own == "代扣金额" else "代扣金额"]["voucher_version_id"]
        with pytest.raises(ContractError) as error:
            progress(
                case,
                wage["business_id"],
                detail_scope_key="open:" + member["id"],
                voucher_version_id=wrong,
            )
        assert error.value.code == "snapshot_changed"


def test_closed_group_reversal_and_replacement_count_each_component_once(case):
    wage = payroll(case)
    assert case.close(case.check())["state"] == "closed"
    changed = deepcopy(wage)
    changed["data"]["gross_fen"] = "650000"
    correct(case, changed, period="2026-02")
    capture(case, "2026-02")
    rows = activity(case, "2026-02")["items"]
    cost_rows = [row for row in rows if "代扣" not in row["title"]]
    deduction_rows = [row for row in rows if "代扣" in row["title"]]
    assert sum(int(row["amount_fen"]) for row in cost_rows) == 50000
    assert sum(int(row["amount_fen"]) for row in deduction_rows) == 0
    physical = vouchers(case, "2026-02")["items"]
    assert len(physical) == 4
    assert {(item["business_amount_label"], item["business_amount_fen"]) for item in physical} == {
        ("计提金额", "-690000"),
        ("计提金额", "740000"),
        ("代扣金额", "-100000"),
        ("代扣金额", "100000"),
    }
    original = {item["voucher_version_id"] for item in physical if item["state"] == "冲正"}
    assert len(original) == 2


def test_frozen_and_current_voucher_progress_keep_the_selected_phase_amount_and_title(case):
    wage = payroll(case)
    assert case.close(case.check())["state"] == "closed"
    changed = deepcopy(wage)
    changed["data"]["gross_fen"] = "650000"
    correct(case, changed, period="2026-02")
    capture(case, "2026-02")
    reversed_vouchers = [
        item for item in vouchers(case, "2026-02")["items"] if item["state"] == "冲正"
    ]
    assert len(reversed_vouchers) == 2
    for voucher in reversed_vouchers:
        data = progress(
            case,
            wage["business_id"],
            month="2026-02",
            detail_scope_key="voucher:" + voucher["voucher_version_id"],
            voucher_version_id=voucher["voucher_version_id"],
        )["data"]
        accrual = voucher["business_amount_label"] == "计提金额"
        assert data["current_business_result"]["amount_fen"] == ("740000" if accrual else "100000")
        assert data["frozen_adoption"]["amount_fen"] == ("690000" if accrual else "100000")
        assert data["frozen_adoption"]["amount_label"] == voucher["business_amount_label"]
        assert data["display_profiles"]["business"]["values"]["display_name"] == (
            "薪酬计提" if accrual else "个人扣款"
        )


def test_disbursement_hold_does_not_become_another_deduction_activity_or_voucher(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    add(case, wage, employment, actual, declared)
    capture(case)
    assert len(activity(case)["items"]) == 6
    deduction = next(
        item for item in vouchers(case)["items"] if item["business_amount_label"] == "代扣金额"
    )
    assert deduction["business_amount_fen"] == deduction["amount_fen"] == "100000"
    tax = next(row for row in activity(case)["items"] if row["title"] == "代扣个人所得税")
    assert tax["amount_fen"] == "30000"


@pytest.mark.parametrize("net_journal", [False, True])
def test_frozen_main_remuneration_keeps_complete_business_components(
    case, monkeypatch, net_journal
):
    calculator = people._CALCULATORS["people.remuneration"]

    def old_rule(fact, evaluation):
        plan = calculator(fact, evaluation)
        lines = [dict(line, line_no=index) for index, line in enumerate(posting_lines(plan), 1)]
        if net_journal:
            lines = [
                {
                    "line_no": index,
                    "account_code": account,
                    "debit_fen": debit,
                    "credit_fen": credit,
                    "summary": "合成旧规则薪酬",
                    **(
                        {"party_id": fact["data"]["person_id"]}
                        if account in {"560201", "221101"}
                        else {}
                    ),
                }
                for index, (account, debit, credit) in enumerate(
                    (
                        ("560201", "690000", "0"),
                        ("221101", "0", "500000"),
                        ("222103", "0", "30000"),
                        ("224102", "0", "50000"),
                        ("224103", "0", "20000"),
                        ("221102", "0", "60000"),
                        ("221103", "0", "30000"),
                    ),
                    1,
                )
            ]
        plan["journals"] = [{"journal_key": "main", "summary": "合成旧规则薪酬", "lines": lines}]
        plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = 1 if net_journal else 2
        return plan

    with monkeypatch.context() as installation:
        installation.setitem(people._CALCULATORS, "people.remuneration", old_rule)
        wage = payroll(case)
    assert case.close(case.check())["state"] == "closed"
    capture(case)
    assert len(activity(case)["items"]) == 6
    old = vouchers(case)["items"]
    assert len(old) == 1 and old[0]["business_amount_fen"] == "690000"
    correct(case, deepcopy(wage), period="2026-02")
    capture(case, "2026-02")
    assert len(vouchers(case, "2026-02")["items"]) == 3
    rows = activity(case, "2026-02")["items"]
    assert sum(int(row["amount_fen"]) for row in rows if "代扣" not in row["title"]) == 0
    assert sum(int(row["amount_fen"]) for row in rows if "代扣" in row["title"]) == 0
