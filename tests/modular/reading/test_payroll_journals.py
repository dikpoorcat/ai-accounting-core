"""Payroll business components and exact progress follow their actual formal journal."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, posting_lines
from ai_accounting_modular.domains.people import public as people
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import (
    add,
    correct,
    declaration,
    wage_with_actuals,
)
from tests.modular.domains.people.test_read_remuneration_tax import tax_target

from .test_brief_activity import activity, capture, group, vouchers
from .test_brief_activity_incremental import matches_complete
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
        ("薪酬计提", "690000"),
        ("个人代扣", "100000"),
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
        expected = deduction if row["title"] == "个人代扣" else accrual
        assert member["amount_components"] == (
            [
                {"label": "员工工资", "amount_fen": "600000"},
                {"label": "单位社保", "amount_fen": "60000"},
                {"label": "单位公积金", "amount_fen": "30000"},
            ]
            if expected is accrual
            else [
                {"label": "代扣个人社保", "amount_fen": "50000"},
                {"label": "代扣个人公积金", "amount_fen": "20000"},
                {"label": "代扣个人所得税", "amount_fen": "30000"},
            ]
        )
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
    for row in rows:
        member = group(case, row["key"], "2026-02")["members"]["items"][0]
        assert sum(int(item["amount_fen"]) for item in member["amount_components"]) == int(
            member["amount_fen"]
        )
        assert all(int(item["amount_fen"]) < 0 for item in member["amount_components"]) == (
            member["state"] == "更正前"
        )
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
            "薪酬计提" if accrual else "个人代扣"
        )


def test_disbursement_hold_does_not_become_another_deduction_activity_or_voucher(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    add(case, wage, employment, actual, declared)
    capture(case)
    assert len(activity(case)["items"]) == 2
    deduction = next(
        item for item in vouchers(case)["items"] if item["business_amount_label"] == "代扣金额"
    )
    assert deduction["business_amount_fen"] == deduction["amount_fen"] == "100000"
    row = next(row for row in activity(case)["items"] if row["title"] == "个人代扣")
    member = group(case, row["key"])["members"]["items"][0]
    assert member["amount_components"][-1] == {"label": "代扣个人所得税", "amount_fen": "30000"}


@pytest.mark.parametrize(
    "gross, personal, unit, expected",
    [
        ("100000", ("0", "0"), ("0", "0"), {"薪酬计提": "100000"}),
        (
            "70000",
            ("50000", "20000"),
            ("60000", "30000"),
            {"薪酬计提": "160000", "个人代扣": "70000"},
        ),
        ("0", ("0", "0"), ("60000", "0"), {"薪酬计提": "60000"}),
        ("0", ("0", "0"), ("0", "0"), {"薪酬计提": "0"}),
    ],
)
def test_zero_and_single_component_stages_keep_stage_progress(
    case, gross, personal, unit, expected
):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    wage["data"]["gross_fen"] = gross
    wage["data"]["tax_basis"]["tax_fen"] = "0"
    for index, item in enumerate(actual["data"]["items"]):
        item.update(employee_fen=personal[index], employer_fen=unit[index])
    add(case, wage, employment, actual)
    assert case.close(case.check())["state"] == "closed"
    capture(case)
    rows = activity(case)["items"]
    assert {row["title"]: row["amount_fen"] for row in rows} == expected
    assert len(vouchers(case)["items"]) == sum(int(value) > 0 for value in expected.values())
    for row in rows:
        member = group(case, row["key"])["members"]["items"][0]
        components = member["amount_components"]
        assert 1 <= len(components) <= 3
        assert sum(int(item["amount_fen"]) for item in components) == int(row["amount_fen"])
        if row["amount_fen"] == "0":
            assert components == [{"label": "员工工资", "amount_fen": "0"}]
        data = progress(case, wage["business_id"], **context(member))["data"]
        assert data["current_business_result"]["amount_fen"] == row["amount_fen"]
        assert data["frozen_adoption"]["amount_fen"] == row["amount_fen"]
        assert data["display_profiles"]["business"]["values"]["display_name"] == row["title"]


def test_same_person_three_types_and_later_taxes_keep_stage_identity_incrementally(case):
    wage = payroll(case)
    bonus, labor = (business_case(name) for name in ("B17B", "B17L"))
    for item, gross in ((bonus, "200000"), (labor, "300000")):
        item["data"].update(
            person_id=wage["data"]["person_id"],
            gross_fen=gross,
            tax_basis=deepcopy(wage["data"]["tax_basis"]) | {"tax_fen": "10000"},
        )
    labor["data"]["withholding_state"] = "deduction_established"
    add(case, bonus, labor)
    capture(case)
    rows = activity(case)["items"]
    assert len(rows) == 6 and len({row["group_key"] for row in rows}) == 6
    by_subject = {}
    for row in rows:
        member = group(case, row["key"])["members"]["items"][0]
        by_subject.setdefault(member["subject_id"], {})[row["title"]] = member
    assert set(by_subject) == {item["business_id"] for item in (wage, bonus, labor)}
    for item, label in ((wage, "员工工资"), (bonus, "员工奖金"), (labor, "劳务报酬")):
        stages = by_subject[item["business_id"]]
        assert set(stages) == {"薪酬计提", "个人代扣"}
        assert stages["薪酬计提"]["amount_components"][0]["label"] == label
    changed = deepcopy(wage)
    changed["data"]["gross_fen"] = "650000"
    correct(case, changed)
    assert case.kernel.reading.run_pending(case.company)
    matches_complete(case)
    changed_member = next(
        member
        for row in activity(case)["items"]
        for member in group(case, row["key"])["members"]["items"]
        if member["subject_id"] == wage["business_id"] and member["title"] == "薪酬计提"
    )
    assert changed_member["amount_fen"] == "740000"
    assert changed_member["amount_components"][0] == {"label": "员工工资", "amount_fen": "650000"}
    taxes = [
        tax_target(item, target, tax_period="2026-01")
        for item, target in ((wage, "35000"), (bonus, "18000"), (labor, "24000"))
    ]
    expected = {
        tax["business_id"]: amount
        for tax, amount in zip(taxes, ("5000", "8000", "14000"), strict=True)
    }
    capture(case, "2026-02")
    add(case, *taxes)
    assert case.kernel.reading.run_pending(case.company)
    matches_complete(case, "2026-02")
    rows = activity(case, "2026-02")["items"]
    assert len(rows) == len({row["group_key"] for row in rows}) == 3
    actual, tokens = {}, set()
    for row in rows:
        assert row["title"] == "个人代扣" and row["member_count"] == row["voucher_count"] == 1
        detail = group(case, row["key"], "2026-02")
        (member,), (voucher,) = detail["members"]["items"], detail["vouchers"]["items"]
        subject = member["subject_id"]
        assert subject == voucher["subject_id"]
        assert member["voucher_version_id"] == voucher["voucher_version_id"]
        assert member["amount_components"] == [
            {"label": "代扣个人所得税", "amount_fen": expected[subject]}
        ]
        assert (
            row["amount_fen"] == member["amount_fen"] == voucher["amount_fen"] == expected[subject]
        )
        assert voucher["business_amount_fen"] == expected[subject]
        actual[subject] = member["amount_fen"]
        tokens.add(member["voucher_version_id"])
    assert actual == expected and len(tokens) == 3


def test_same_person_two_project_stages_and_later_taxes_keep_adopted_project_identity(case):
    case.seed(expense=False)
    first = business_case("B17P")
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    second["data"]["destination"]["project_id"] = str(uuid4())
    for labor in (first, second):
        labor["data"].update(
            withholding_state="deduction_established",
            tax_basis={
                "tax_period": "2026-01",
                "tax_fen": "10000",
                "basis_type": "final_statement",
                "source": {"source_id": labor["evidence_ids"][0], "location": "合成项目劳务扣税"},
            },
        )
    add(case, first, second)
    capture(case)
    rows = activity(case)["items"]
    assert len(rows) == 4 and len({row["group_key"] for row in rows}) == 4
    assert sorted(row["title"] for row in rows) == ["个人代扣", "个人代扣", "薪酬计提", "薪酬计提"]
    taxes = [tax_target(labor, "15000", tax_period="2026-01") for labor in (first, second)]
    tax_projects = {
        tax["business_id"]: labor["data"]["destination"]["project_id"]
        for tax, labor in zip(taxes, (first, second), strict=True)
    }
    add(case, *taxes)
    capture(case, "2026-02")
    rows = activity(case, "2026-02")["items"]
    assert len(rows) == 2 and len({row["group_key"] for row in rows}) == 2
    for row in rows:
        (member,) = group(case, row["key"], "2026-02")["members"]["items"]
        data = progress(case, member["subject_id"], month="2026-02", **context(member))["data"]
        assert member["amount_components"] == [{"label": "代扣个人所得税", "amount_fen": "5000"}]
        assert len(data["display_profiles"]["assets"]) == 1
        assert (
            data["display_profiles"]["assets"][0]["entity_id"] == tax_projects[member["subject_id"]]
        )


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
    rows = activity(case)["items"]
    assert len(rows) == 2
    old = vouchers(case)["items"]
    assert len(old) == 1 and old[0]["business_amount_fen"] == "690000"
    for row in rows:
        member = group(case, row["key"])["members"]["items"][0]
        assert member["voucher_version_id"] == old[0]["voucher_version_id"]
        detail = progress(case, wage["business_id"], **context(member))["data"]
        assert detail["current_business_result"]["amount_fen"] == row["amount_fen"]
        assert detail["frozen_adoption"]["amount_fen"] == row["amount_fen"]
        if row["title"] == "个人代扣":
            assert {item["name"] for item in detail["settlements"]["obligations"]} == {
                "employee_social",
                "employee_housing",
                "tax",
            }
    correct(case, deepcopy(wage), period="2026-02")
    capture(case, "2026-02")
    assert len(vouchers(case, "2026-02")["items"]) == 3
    rows = activity(case, "2026-02")["items"]
    assert sum(int(row["amount_fen"]) for row in rows if "代扣" not in row["title"]) == 0
    assert sum(int(row["amount_fen"]) for row in rows if "代扣" in row["title"]) == 0
