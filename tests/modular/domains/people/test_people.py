"""People rules use explicit synthetic amounts, not calculated expected values."""

import json
from copy import deepcopy
from hashlib import sha256
from itertools import permutations
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    EvaluatedSlot,
    canonical_json,
    validate_contract,
)
from ai_accounting_modular.domains.people.public import create_people

_CASES = json.loads(
    (Path(__file__).parents[4] / "docs/design/examples/business-facts-cases.json").read_text(
        encoding="utf-8"
    )
)
CASES = {item["case_id"]: item for item in _CASES["valid_cases"]}
NEGATIVES = {item["case_id"]: item for item in _CASES["negative_cases"]}
PERSON = CASES["B14"]["when"]["data"]["employee_id"]
SOURCE = CASES["B14"]["when"]["evidence_ids"][0]
EMPLOYMENT = CASES["B14"]["when"]["business_id"]
POLICY = CASES["B15"]["when"]["business_id"]
ACTUAL = CASES["B16"]["when"]["business_id"]


class Context:
    """A strict input/result transport; it contains no payroll or ledger formula."""

    read_context = None

    def __init__(self, *facts):
        self.facts = {fact["business_id"]: deepcopy(fact) for fact in facts}
        self.objects = {PERSON: "person"}
        for fact in facts:
            data = fact["data"]
            if "person_id" in data:
                self.objects[data["person_id"]] = "person"
            if data.get("destination", {}).get("type") == "project":
                self.objects[data["destination"]["project_id"]] = "project"
        self.results = {}

    def get_fact(self, business_id):
        return deepcopy(self.facts.get(business_id))

    def get_object(self, object_id):
        kind = self.objects.get(object_id)
        return {"object_id": object_id, "kind": kind} if kind else None

    def resolve_sources(self, ids):
        return [{"source_id": item} for item in ids]

    def get_result(self, business_id, result_slot):
        key = business_id, result_slot
        if key not in self.results:
            if business_id not in self.facts:
                return None
            plan = create_people().evaluate_slot(business_id, result_slot, self)
            self.results[key] = EvaluatedSlot(
                {
                    "business_id": business_id,
                    "result_id": str(uuid4()),
                    "result_slot": result_slot,
                    "result_generation": 1,
                    "content_digest": sha256(canonical_json(plan).encode()).hexdigest(),
                },
                plan["target"],
                plan["basis"],
            )
        return self.results[key]


def case(name):
    return deepcopy(CASES[name]["when"])


def evaluate(fact, *sources):
    return create_people().evaluate_slot(fact["business_id"], "main", Context(*sources, fact))


def no_contributions():
    fact = case("B14")
    data = fact["data"]
    data.update(social_insurance_participating=False, housing_fund_participating=False)
    data.pop("social_insurance_base_fen")
    data.pop("housing_fund_base_fen")
    return fact


@pytest.mark.parametrize(
    "name", ["B14", "B15", "B16", "B17", "B17N", "B17B", "B17Z", "B17A", "B17L", "B17P"]
)
def test_published_business_examples(name):
    fact = case(name)
    sources = [case("B14"), case("B15"), case("B16")]
    if name == "B17Z":
        sources[0] = no_contributions()
    if name == "B17A":
        sources.remove(sources[1])
    plan = evaluate(fact, *sources)
    validate_contract("business-facts.schema.json", "PostingPlan", plan)
    expected = CASES[name]["then"]
    for field, value in expected.get("values_subset", {}).items():
        assert plan["target"]["values"][field] == value
    if expected.get("no_voucher"):
        assert plan["lines"] == []
    if "obligation_slots" in expected:
        assert [item["slot"] for item in plan["obligations"]] == expected["obligation_slots"]
    assert sum(int(item["debit_fen"]) for item in plan["lines"]) == sum(
        int(item["credit_fen"]) for item in plan["lines"]
    )


@pytest.mark.parametrize("name", ["N05", "N06", "N07", "N10", "N13"])
def test_published_invalid_examples(name):
    fact = deepcopy(NEGATIVES[name]["when"])
    with pytest.raises(ContractError):
        evaluate(fact, case("B14"), case("B15"), case("B16"))


def test_no_participation_does_not_require_fake_policies_or_bases():
    plan = evaluate(case("B17Z"), no_contributions())
    assert plan["basis"]["policies"] == []
    assert len(plan["basis"]["source_results"]) == 1
    assert len(plan["obligations"]) == 6
    assert [item["amount_fen"] for item in plan["obligations"]] == [
        "100000",
        "0",
        "0",
        "0",
        "0",
        "0",
    ]
    assert [item["account_code"] for item in plan["lines"]] == ["560201", "221101"]
    assert "effective_on" not in plan["basis"]


def test_actual_without_policy_does_not_require_estimation_bases():
    employment = case("B14")
    employment["data"].pop("social_insurance_base_fen")
    employment["data"].pop("housing_fund_base_fen")
    plan = evaluate(case("B17A"), employment, case("B16"))
    assert plan["target"]["values"]["net_fen"] == "830000"
    assert plan["basis"]["policies"] == []


def test_wage_basis_preserves_only_used_employment_bases_and_actual_items():
    fact, employment, actual = case("B17A"), case("B14"), case("B16")
    context = Context(fact, employment, actual)
    plan = create_people().evaluate_slot(fact["business_id"], "main", context)
    quantities = plan["basis"]["adopted_quantities"]
    adopted_employment = next(item for item in quantities if item["type"] == "employment")
    adopted_actual = next(item for item in quantities if item["type"] == "contribution_actual")
    assert "social_insurance_base_fen" not in adopted_employment["parameters"]
    assert "housing_fund_base_fen" not in adopted_employment["parameters"]
    assert adopted_actual["parameters"] == actual["data"]
    context.facts[ACTUAL]["data"]["items"][0]["employee_fen"] = "1"
    assert adopted_actual["parameters"]["items"][0]["employee_fen"] == "100000"
    estimate = case("B17")
    estimate["data"].pop("contribution_actual_id")
    estimate["data"]["contribution_basis"] = "policy_until_actual"
    estimated = evaluate(estimate, employment, case("B15"))
    parameters = estimated["basis"]["adopted_quantities"][0]["parameters"]
    assert parameters["social_insurance_base_fen"] == "1000000"
    assert parameters["housing_fund_base_fen"] == "1000000"


def test_legal_thousand_evidence_ids_can_include_additional_exact_source_locations():
    fact = case("B17B")
    fact["evidence_ids"] = [str(uuid4()) for _ in range(1000)]
    fact["data"]["tax_basis"]["source"] = {"source_id": SOURCE, "location": "有据个税额"}
    plan = evaluate(fact)
    assert len(plan["basis"]["source_locations"]) == 1001
    assert plan["basis"]["source_locations"][-1] == fact["data"]["tax_basis"]["source"]


def test_description_declares_wage_unique_identity_without_merging_bonus_or_labor():
    people = create_people()
    for name in ("B17Z", "B17B", "B17L"):
        fact = case(name)
        described = people.describe_change(
            [
                {
                    "op": "create",
                    "expected_seq": 0,
                    "business_id": fact["business_id"],
                    "fact": fact,
                },
            ],
            Context(fact),
        )
        responsibility = described["businesses"][0]
        assert responsibility["complete_result_slots"] == ["main"]
        expected = f"people:wage:{PERSON}:2026-01" if name == "B17Z" else None
        assert responsibility["natural_key"] == expected
        assert all(item["consumer_result_slot"] == "main" for item in described["dependencies"])


def test_missing_estimation_base_identifies_actual_source_field():
    fact, employment, actual = case("B17"), case("B14"), case("B16")
    fact["data"]["contribution_basis"] = "policy_until_actual"
    employment["data"].pop("housing_fund_base_fen")
    actual["data"]["items"].pop()
    with pytest.raises(ContractError) as error:
        evaluate(fact, employment, actual, case("B15"))
    assert error.value.code == "needs_information"
    assert error.value.fact_issues[0]["business_id"] == EMPLOYMENT
    assert error.value.fact_issues[0]["field"] == "/data/housing_fund_base_fen"
    assert error.value.fact_issues[0]["allowed_precision"] == ["fen"]


def test_policy_estimates_only_missing_items_and_preserves_adopted_policy():
    fact, actual = case("B17"), case("B16")
    fact["data"]["contribution_basis"] = "policy_until_actual"
    actual["data"]["items"] = [
        {
            "code": "social",
            "base_kind": "social_insurance",
            "state": "declared",
            "employee_fen": "90000",
            "employer_fen": "190000",
        }
    ]
    plan = evaluate(fact, case("B14"), case("B15"), actual)
    assert plan["target"]["values"]["net_fen"] == "840000"
    assert plan["target"]["values"]["cost_fen"] == "1240000"
    adopted_policy = deepcopy(case("B15")["data"])
    adopted_policy["items"] = [adopted_policy["items"][1]]
    assert plan["basis"]["policies"][0]["parameters"] == adopted_policy
    assert plan["rule_refs"][0]["policy_business_ids"] == [POLICY]


def test_actual_override_excludes_unused_policy_rate_from_payroll_basis():
    fact, actual, policy = case("B17"), case("B16"), case("B15")
    fact["data"]["contribution_basis"] = "policy_until_actual"
    actual["data"]["items"] = [actual["data"]["items"][0]]
    before = evaluate(fact, case("B14"), actual, policy)
    policy["data"]["items"][0]["employee_rate"] = "0.99"
    policy["data"]["items"][0]["employer_rate"] = "0.99"
    after = evaluate(fact, case("B14"), actual, policy)
    assert after["target"] == before["target"]
    assert after["basis"]["policies"] == before["basis"]["policies"]
    assert after["basis"]["adopted_quantities"] == before["basis"]["adopted_quantities"]


@pytest.mark.parametrize(
    "rounding,employee,employer", [("half_up", "2", "2"), ("down", "1", "1"), ("up", "2", "2")]
)
def test_each_item_rounds_before_sum_with_capped_base(rounding, employee, employer):
    fact, employment, policy = case("B17"), case("B14"), case("B15")
    fact["data"].pop("contribution_actual_id")
    fact["data"]["contribution_basis"] = "policy_until_actual"
    employment["data"]["housing_fund_participating"] = False
    employment["data"]["social_insurance_base_fen"] = "999999"
    policy["data"]["items"] = [
        {
            "code": code,
            "base_kind": "social_insurance",
            "employee_rate": "0.15",
            "employer_rate": "0.15",
            "minimum_base_fen": "10",
            "maximum_base_fen": "10",
            "rounding": rounding,
            "enabled": True,
        }
        for code in ("a", "b")
    ]
    plan = evaluate(fact, employment, policy)
    assert plan["target"]["values"]["employee_social_fen"] == str(int(employee) * 2)
    assert plan["target"]["values"]["employer_social_fen"] == str(int(employer) * 2)


@pytest.mark.parametrize(
    "edit",
    [
        "missing_mode",
        "missing_category",
        "missing_base",
        "unmatched_item",
        "wrong_person",
        "wrong_month",
        "expired",
        "midmonth",
        "duplicate",
    ],
)
def test_incomplete_or_conflicting_contribution_facts_are_not_guessed(edit):
    fact, employment, policy, actual = case("B17"), case("B14"), case("B15"), case("B16")
    if edit == "missing_mode":
        fact["data"].pop("contribution_basis")
    elif edit == "missing_category":
        actual["data"]["items"].pop()
        fact["data"].pop("contribution_policy_id")
    elif edit == "missing_base":
        employment["data"].pop("housing_fund_base_fen")
        actual["data"]["items"].pop()
        fact["data"]["contribution_basis"] = "policy_until_actual"
    elif edit == "unmatched_item":
        actual["data"]["items"][0]["code"] = "unknown"
    elif edit == "wrong_person":
        actual["data"]["employee_id"] = str(uuid4())
    elif edit == "wrong_month":
        actual["period"] = "2026-02"
    elif edit in {"expired", "midmonth"}:
        fact["data"].pop("contribution_actual_id")
        fact["data"]["contribution_basis"] = "policy_until_actual"
        policy["data"]["policy"]["effective_from"] = (
            "2026-02-01" if edit == "expired" else "2026-01-15"
        )
    elif edit == "duplicate":
        actual["data"]["items"].append(deepcopy(actual["data"]["items"][0]))
    with pytest.raises(ContractError):
        evaluate(fact, employment, policy, actual)


def test_not_declared_is_explicit_zero_not_missing():
    actual = case("B16")
    for item in actual["data"]["items"]:
        item.update(state="not_declared", employee_fen="0", employer_fen="0")
    plan = evaluate(case("B17A"), case("B14"), actual)
    assert plan["target"]["values"]["cost_fen"] == "1000000"
    assert plan["target"]["values"]["net_fen"] == "980000"


def test_evidenced_actual_overrides_disabled_estimation_rule():
    policy = case("B15")
    for item in policy["data"]["items"]:
        item["enabled"] = False
    plan = evaluate(case("B17"), case("B14"), policy, case("B16"))
    assert plan["target"]["values"]["net_fen"] == "830000"
    assert plan["target"]["values"]["cost_fen"] == "1250000"


def test_disabled_policy_items_need_no_bases_but_the_policy_is_still_adopted():
    fact, policy, employment = case("B17"), case("B15"), case("B14")
    fact["data"].pop("contribution_actual_id")
    fact["data"]["contribution_basis"] = "policy_until_actual"
    employment["data"].pop("social_insurance_base_fen")
    employment["data"].pop("housing_fund_base_fen")
    for item in policy["data"]["items"]:
        item["enabled"] = False
    plan = evaluate(fact, employment, policy)
    assert plan["target"]["values"]["net_fen"] == "980000"
    assert plan["basis"]["policies"][0]["parameters"] == policy["data"]
    policy["data"]["policy"]["effective_to"] = "2025-12-31"
    with pytest.raises(ContractError):
        evaluate(fact, employment, policy)


@pytest.mark.parametrize("treatment", ["reject", "employer_borne"])
def test_shortfall_preserves_agency_total_and_never_makes_negative_net(treatment):
    fact, employment = case("B17A"), case("B14")
    fact["data"]["gross_fen"] = "100000"
    fact["data"]["tax_basis"]["tax_fen"] = "20000"
    employment["data"]["contribution_shortfall"] = treatment
    if treatment == "reject":
        with pytest.raises(ContractError, match="差额"):
            evaluate(fact, employment, case("B16"))
        return
    plan = evaluate(fact, employment, case("B16"))
    values = plan["target"]["values"]
    assert values["net_fen"] == "0"
    assert values["cost_fen"] == "420000"
    assert values["employee_social_fen"] == "80000"
    assert values["employee_housing_fen"] == "0"
    assert values["employer_social_fen"] == "220000"
    assert values["employer_housing_fen"] == "100000"
    assert sum(int(item["amount_fen"]) for item in plan["obligations"]) == 420000


def test_zero_wage_can_have_explicit_employer_borne_contributions():
    fact, employment = case("B17A"), case("B14")
    fact["data"]["gross_fen"] = "0"
    fact["data"]["tax_basis"]["tax_fen"] = "0"
    employment["data"]["contribution_shortfall"] = "employer_borne"
    plan = evaluate(fact, employment, case("B16"))
    assert plan["target"]["values"]["cost_fen"] == "400000"
    assert plan["target"]["values"]["net_fen"] == "0"


@pytest.mark.parametrize("mode", ["actual", "policy", "mixed"])
def test_shortfall_allocation_is_stable_across_actual_and_policy_item_order(mode):
    fact, employment, actual, policy = case("B17"), case("B14"), case("B16"), case("B15")
    fact["data"]["gross_fen"] = "100000"
    employment["data"]["contribution_shortfall"] = "employer_borne"
    actual["data"]["items"] = [
        {
            "code": code,
            "base_kind": kind,
            "state": "declared",
            "employee_fen": employee,
            "employer_fen": employer,
        }
        for code, kind, employee, employer in (
            ("z", "social_insurance", "40000", "90000"),
            ("h", "housing_fund", "50000", "50000"),
            ("a", "social_insurance", "60000", "110000"),
        )
    ]
    policy["data"]["items"] = [
        {
            "code": code,
            "base_kind": kind,
            "employee_rate": employee,
            "employer_rate": employer,
            "minimum_base_fen": "0",
            "maximum_base_fen": "2000000",
            "rounding": "half_up",
            "enabled": True,
        }
        for code, kind, employee, employer in (
            ("z", "social_insurance", "0.04", "0.09"),
            ("h", "housing_fund", "0.05", "0.05"),
            ("a", "social_insurance", "0.06", "0.11"),
        )
    ]
    if mode == "actual":
        fact["data"].pop("contribution_policy_id")
    else:
        fact["data"]["contribution_basis"] = "policy_until_actual"
        if mode == "policy":
            fact["data"].pop("contribution_actual_id")
        else:
            actual["data"]["items"] = [actual["data"]["items"][1]]
    baseline = evaluate(fact, employment, actual, policy)
    source_baseline = evaluate(actual if mode == "actual" else policy)
    values = baseline["target"]["values"]
    assert values["employee_social_fen"] == "80000"
    assert values["employee_housing_fen"] == "0"
    assert values["employer_social_fen"] == "220000"
    assert values["employer_housing_fen"] == "100000"
    assert values["cost_fen"] == "420000"
    assert (
        sum(
            int(values[field])
            for field in (
                "employee_social_fen",
                "employee_housing_fen",
                "employer_social_fen",
                "employer_housing_fen",
            )
        )
        == 400000
    )
    for order in permutations(range(3)):
        shuffled_actual, shuffled_policy = deepcopy(actual), deepcopy(policy)
        if mode == "actual":
            shuffled_actual["data"]["items"] = [
                deepcopy(actual["data"]["items"][index]) for index in order
            ]
        else:
            shuffled_policy["data"]["items"] = [
                deepcopy(policy["data"]["items"][index]) for index in order
            ]
        assert evaluate(shuffled_actual if mode == "actual" else shuffled_policy) == source_baseline
        result = evaluate(fact, employment, shuffled_actual, shuffled_policy)
        for field in ("target", "lines", "obligations"):
            assert result[field] == baseline[field]
        for field in ("adopted_quantities", "policies"):
            assert result["basis"][field] == baseline["basis"][field]


def test_management_and_unknown_withholding_dates_do_not_enter_accounting_basis():
    fact = case("B17B")
    fact["effective_on"] = "2026-01-27"
    fact["data"]["tax_basis"]["basis_type"] = "actual_withholding"
    plan = evaluate(fact)
    fact["data"]["tax_basis"]["withheld_on"] = "2026-02-03"
    changed = evaluate(fact)
    assert changed["basis"] == plan["basis"]
    assert changed["target"] == plan["target"]
    assert plan["obligations"][1]["business_period"] == "2026-01"


def test_labor_deduction_replaces_same_gross_cost_and_project_cost_is_public():
    fact = case("B17L")
    before = evaluate(fact)
    fact["data"].update(
        withholding_state="deduction_established", tax_basis=case("B17B")["data"]["tax_basis"]
    )
    after = evaluate(fact)
    assert (
        before["target"]["values"]["cost_fen"] == after["target"]["values"]["cost_fen"] == "100000"
    )
    assert [item["amount_fen"] for item in before["obligations"]] == ["100000"]
    assert [item["amount_fen"] for item in after["obligations"]] == ["90000", "10000"]
    project = evaluate(case("B17P"))
    assert project["lines"][0]["account_code"] == "189901"
    assert project["cost_lots"][0]["amount_fen"] == "100000"
    assert project["obligations"][0]["cashflow_weights"] == [
        {"category": "asset_acquisition", "weight": "1"}
    ]


def declaration(fact, amount="30000", *, allocations=None):
    identifier, row_id = str(uuid4()), str(uuid4())
    tax_basis = fact["data"]["tax_basis"]
    data = {
        "type": "payroll",
        "tax_period": tax_basis["tax_period"],
        "source": {"source_id": SOURCE, "location": "实际申报资料"},
        "rows": [
            {
                "row_id": row_id,
                "person_id": fact["data"]["person_id"],
                "income_category": fact["data"]["type"],
                "tax_period": tax_basis["tax_period"],
                "declared_tax_fen": amount,
                "source": {"source_id": SOURCE, "location": "申报明细行"},
                "allocations": allocations
                or [{"remuneration_id": fact["business_id"], "tax_fen": amount}],
            }
        ],
    }
    return {
        "business_id": identifier,
        "kind": "taxes.actual_declaration",
        "period": "2026-02",
        "evidence_ids": [SOURCE],
        "data": data,
    }, {"declaration_id": identifier, "row_id": row_id}


def test_pooled_row_uses_only_current_remuneration_allocation_without_64_limit():
    fact = case("B17B")
    allocations = [{"remuneration_id": str(uuid4()), "tax_fen": "100"} for _ in range(75)]
    allocations.append({"remuneration_id": fact["business_id"], "tax_fen": "10000"})
    declared, reference = declaration(fact, "17500", allocations=allocations)
    fact["data"]["tax_basis"]["pooled_source"] = reference
    plan = evaluate(fact, declared)
    assert plan["target"]["values"]["tax_fen"] == "10000"
    assert plan["target"]["values"]["net_fen"] == "90000"


@pytest.mark.parametrize(
    "edit", ["person", "type", "period", "total", "missing_allocation", "duplicate", "own_amount"]
)
def test_pooled_row_must_match_person_period_and_explicit_amount(edit):
    fact = case("B17B")
    declared, reference = declaration(fact, "10000")
    fact["data"]["tax_basis"]["pooled_source"] = reference
    row = declared["data"]["rows"][0]
    if edit == "person":
        row["person_id"] = str(uuid4())
    elif edit == "type":
        row["income_category"] = "labor"
    elif edit == "period":
        row["tax_period"] = "2026-02"
    elif edit == "total":
        row["declared_tax_fen"] = "10001"
    elif edit == "missing_allocation":
        row["allocations"][0]["remuneration_id"] = str(uuid4())
    elif edit == "duplicate":
        row["allocations"].append(deepcopy(row["allocations"][0]))
    elif edit == "own_amount":
        row["allocations"][0]["tax_fen"] = row["declared_tax_fen"] = "9999"
    with pytest.raises(ContractError):
        evaluate(fact, declared)


@pytest.mark.parametrize(
    "declared_tax,held,target",
    [("20000", "0", "830000"), ("30000", "10000", "820000"), ("850000", "830000", "0")],
)
def test_disbursement_hold_is_not_new_tax_or_reduced_formal_obligation(declared_tax, held, target):
    fact = case("B17A")
    declared, reference = declaration(fact, declared_tax)
    fact["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    plan = evaluate(fact, case("B14"), case("B16"), declared)
    values = plan["target"]["values"]
    assert values["held_fen"] == held
    assert values["disbursement_target_fen"] == target
    assert values["tax_fen"] == "20000"
    assert plan["obligations"][0]["amount_fen"] == "830000"
    assert plan["obligations"][1]["amount_fen"] == "20000"


@pytest.mark.parametrize("amount", ["19999", "850001"])
def test_conflicting_disbursement_does_not_guess_how_to_clear_difference(amount):
    fact = case("B17A")
    declared, reference = declaration(fact, amount)
    fact["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    with pytest.raises(ContractError):
        evaluate(fact, case("B14"), case("B16"), declared)


def test_person_role_cannot_be_a_bank_account():
    fact = case("B17B")
    context = Context(fact)
    context.objects[PERSON] = "bank_account"
    with pytest.raises(ContractError) as error:
        create_people().evaluate_slot(fact["business_id"], "main", context)
    assert error.value.fact_issues[0]["field"] == "/data/person_id"


def test_money_overflow_is_structured_not_python_integer_spill():
    fact = case("B17A")
    fact["data"]["gross_fen"] = "9223372036854775807"
    with pytest.raises(ContractError, match="64"):
        evaluate(fact, case("B14"), case("B16"))
