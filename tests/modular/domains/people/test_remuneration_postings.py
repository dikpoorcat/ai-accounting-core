"""Formal payroll journals retain gross accruals and every evidenced deduction."""

from collections import Counter
from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError

from .test_people import PERSON, case, evaluate, no_contributions
from .test_read_remuneration import wage_with_actuals

MAX_FEN = 9223372036854775807


def assert_journal(plan, expected, turnover):
    lines = plan["lines"]
    assert Counter(
        (line["account_code"], line["debit_fen"], line["credit_fen"]) for line in lines
    ) == Counter(expected)
    assert [line["line_no"] for line in lines] == list(range(1, len(lines) + 1))
    assert all((int(line["debit_fen"]) > 0) != (int(line["credit_fen"]) > 0) for line in lines)
    assert sum(int(line["debit_fen"]) for line in lines) == turnover
    assert sum(int(line["credit_fen"]) for line in lines) == turnover


def obligations(plan):
    return {
        item["slot"]: (item["amount_fen"], item["account_code"]) for item in plan["obligations"]
    }


@pytest.mark.parametrize(
    "destination,cost_account",
    [
        ("administration", "560201"),
        ("sales", "560101"),
        ("service", "540101"),
    ],
)
def test_complete_wage_accruals_and_deductions_keep_their_own_lines(destination, cost_account):
    wage, employment, actual = wage_with_actuals()
    wage["data"]["expense_area"] = destination
    plan = evaluate(wage, employment, actual)

    assert_journal(
        plan,
        [
            (cost_account, "600000", "0"),
            ("221101", "0", "600000"),
            (cost_account, "60000", "0"),
            ("221102", "0", "60000"),
            (cost_account, "30000", "0"),
            ("221103", "0", "30000"),
            ("221101", "50000", "0"),
            ("224102", "0", "50000"),
            ("221101", "20000", "0"),
            ("224103", "0", "20000"),
            ("221101", "30000", "0"),
            ("222103", "0", "30000"),
        ],
        790000,
    )
    assert {
        key: plan["target"]["values"][key]
        for key in ("gross_fen", "net_fen", "cost_fen", "disbursement_target_fen")
    } == {
        "gross_fen": "600000",
        "net_fen": "500000",
        "cost_fen": "690000",
        "disbursement_target_fen": "500000",
    }
    assert obligations(plan) == {
        "net_pay": ("500000", "221101"),
        "tax": ("30000", "222103"),
        "employee_social": ("50000", "224102"),
        "employee_housing": ("20000", "224103"),
        "employer_social": ("60000", "221102"),
        "employer_housing": ("30000", "221103"),
    }
    wage_lines = [line for line in plan["lines"] if line["account_code"] == "221101"]
    assert all(line["party_id"] == PERSON for line in wage_lines)
    assert any("计提" in line["summary"] for line in plan["lines"])
    assert any("代扣" in line["summary"] for line in plan["lines"])
    assert len({line["summary"] for line in wage_lines}) >= 3
    assert all(
        label in plan["lines"][0]["summary"]
        for label in ("工资", "单位社保", "单位公积金", "计提", "代扣")
    )
    assert plan["cost_lots"] == []
    assert all(
        line["party_id"] == PERSON for line in plan["lines"] if line["account_code"] == cost_account
    )


def test_zero_net_wage_retains_gross_accrual_and_personal_contribution_deductions():
    wage, employment, actual = wage_with_actuals()
    wage["data"]["gross_fen"] = "70000"
    wage["data"].pop("tax_basis")
    plan = evaluate(wage, employment, actual)

    assert_journal(
        plan,
        [
            ("560201", "70000", "0"),
            ("221101", "0", "70000"),
            ("560201", "60000", "0"),
            ("221102", "0", "60000"),
            ("560201", "30000", "0"),
            ("221103", "0", "30000"),
            ("221101", "50000", "0"),
            ("224102", "0", "50000"),
            ("221101", "20000", "0"),
            ("224103", "0", "20000"),
        ],
        230000,
    )
    assert plan["target"]["values"]["net_fen"] == "0"
    assert plan["target"]["values"]["cost_fen"] == "160000"
    assert obligations(plan)["net_pay"] == ("0", "221101")
    assert "tax" not in obligations(plan)


def test_zero_gross_employer_borne_contributions_do_not_invent_wage_or_deductions():
    wage, employment, actual = wage_with_actuals()
    wage["data"]["gross_fen"] = "0"
    wage["data"].pop("tax_basis")
    employment["data"]["contribution_shortfall"] = "employer_borne"
    plan = evaluate(wage, employment, actual)

    assert_journal(
        plan,
        [
            ("560201", "110000", "0"),
            ("221102", "0", "110000"),
            ("560201", "50000", "0"),
            ("221103", "0", "50000"),
        ],
        160000,
    )
    assert plan["target"]["values"]["cost_fen"] == "160000"
    assert obligations(plan) == {
        "net_pay": ("0", "221101"),
        "employee_social": ("0", "224102"),
        "employee_housing": ("0", "224103"),
        "employer_social": ("110000", "221102"),
        "employer_housing": ("50000", "221103"),
    }


@pytest.mark.parametrize(
    "name,cost_account,payable_account",
    [
        ("B17B", "560101", "221101"),
        ("B17L", "540104", "224104"),
        ("B17P", "189901", "224104"),
    ],
)
def test_bonus_and_labor_keep_gross_accrual_before_initial_tax_deduction(
    name, cost_account, payable_account
):
    fact = case(name)
    fact["data"]["gross_fen"] = "600000"
    fact["data"]["tax_basis"] = deepcopy(case("B17B")["data"]["tax_basis"])
    fact["data"]["tax_basis"]["tax_fen"] = "30000"
    if fact["data"]["type"] == "labor":
        fact["data"]["withholding_state"] = "deduction_established"
    plan = evaluate(fact)

    assert_journal(
        plan,
        [
            (cost_account, "600000", "0"),
            (payable_account, "0", "600000"),
            (payable_account, "30000", "0"),
            ("222103", "0", "30000"),
        ],
        630000,
    )
    assert plan["target"]["values"]["cost_fen"] == "600000"
    assert plan["target"]["values"]["net_fen"] == "570000"
    assert obligations(plan) == {
        "net_pay": ("570000", payable_account),
        "tax": ("30000", "222103"),
    }
    assert all(
        line["party_id"] == fact["data"]["person_id"]
        for line in plan["lines"]
        if line["account_code"] == payable_account
    )
    if name == "B17P":
        project_id = fact["data"]["destination"]["project_id"]
        assert all(
            line["object_id"] == project_id
            for line in plan["lines"]
            if line["account_code"] == cost_account
        )
        assert [(lot["slot"], lot["amount_fen"]) for lot in plan["cost_lots"]] == [
            ("cost", "600000")
        ]
    else:
        assert plan["cost_lots"] == []


def test_no_deductions_do_not_add_cancelling_or_zero_lines():
    wage = case("B17Z")
    plan = evaluate(wage, no_contributions())
    assert_journal(
        plan,
        [("560201", "100000", "0"), ("221101", "0", "100000")],
        100000,
    )


def test_remuneration_rule_revision_preserves_adopted_contribution_policy_sources():
    wage, policy, actual = case("B17"), case("B15"), case("B16")
    wage["data"]["contribution_basis"] = "policy_until_actual"
    actual["data"]["items"] = [actual["data"]["items"][0]]
    plan = evaluate(wage, case("B14"), policy, actual)

    assert plan["calculation_key"] == "people.remuneration"
    assert plan["calculation_revision"] == 2
    assert len(plan["rule_refs"]) == 1
    rule = plan["rule_refs"][0]
    assert (rule["key"], rule["revision"]) == ("people.remuneration", 2)
    assert rule["policy_business_ids"] == [policy["business_id"]]
    assert set(rule["official_urls"]) == {
        "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf",
        policy["data"]["policy"]["official_url"],
    }
    policy_plan = evaluate(policy)
    assert policy_plan["calculation_revision"] == 1
    assert policy_plan["rule_refs"] == [
        {
            "key": "people.contribution_policy",
            "revision": 1,
            "policy_business_ids": [],
            "official_urls": [policy["data"]["policy"]["official_url"]],
        }
    ]


@pytest.mark.parametrize("gross,accepted", [(MAX_FEN - 1, True), (MAX_FEN, False)])
def test_expanded_journal_turnover_has_an_explicit_int64_boundary(gross, accepted):
    wage = case("B17Z")
    wage["data"]["gross_fen"] = str(gross)
    wage["data"]["tax_basis"] = deepcopy(case("B17B")["data"]["tax_basis"])
    wage["data"]["tax_basis"]["tax_fen"] = "1"
    if not accepted:
        with pytest.raises(ContractError) as error:
            evaluate(wage, no_contributions())
        assert error.value.code == "invalid_input"
        return

    plan = evaluate(wage, no_contributions())
    assert_journal(
        plan,
        [
            ("560201", "9223372036854775806", "0"),
            ("221101", "0", "9223372036854775806"),
            ("221101", "1", "0"),
            ("222103", "0", "1"),
        ],
        MAX_FEN,
    )
    assert plan["target"]["values"]["net_fen"] == "9223372036854775805"
