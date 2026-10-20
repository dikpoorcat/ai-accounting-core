"""Independent date/unknown oracles for current correct personnel relationships."""

from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
from itertools import product
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.domains.people._relationships import ordered_bounds, project_relationship
from ai_accounting_modular.domains.people.public import create_people

from .test_people import PERSON, SOURCE, Context, case

READ = SimpleNamespace(company_id=str(uuid4()), database_id=str(uuid4()))


def relationship(*segments, person=PERSON, period="2026-08"):
    return {
        "business_id": str(uuid4()),
        "kind": "people.relationship",
        "period": period,
        "evidence_ids": [SOURCE],
        "data": {
            "person_id": person,
            "segments": [{"state": state, "effective_from": moment} for state, moment in segments],
        },
    }


def project(fact, period):
    validate_contract("business-facts.schema.json", "Fact", fact)
    return project_relationship(PERSON, period, fact, READ)


def test_published_relationship_oracles_and_precision_are_preserved():
    fact = case("B46")
    for period, employee, labor, end in (
        ("2026-02", True, True, "regular"),
        ("2026-03", True, False, "unpaid_leave"),
        ("2026-05", None, False, "departed"),
    ):
        result = project(fact, period)
        assert (result.employee_member, result.labor_member, result.period_end_state) == (
            employee,
            labor,
            end,
        )
    assert result.segments[-1].start == "2026-05"
    assert result.segments[-2].end == "2026-05"
    assert result.period_states == ("departed",)
    assert result.possible_period_states == ("regular",)
    assert result.issues[0]["field"] == "/data/segments/4/effective_from"
    assert all(item["blocking_scope"] == "none" for item in result.issues)
    for item in result.issues:
        validate_contract("core.schema.json", "FactIssue", item)


def test_unknown_prefix_is_not_a_fabricated_not_started_relationship():
    fact = relationship(("regular", "2026-02-15"))
    earlier = project(fact, "2026-01")
    assert earlier.period_end_state == "unknown"
    assert earlier.period_states == ("unknown",)
    assert earlier.employee_member is earlier.labor_member is None
    current = project(fact, "2026-02")
    assert current.employee_member is True
    assert current.labor_member is None
    assert set(current.period_states) == {"regular", "unknown"}
    future = project(fact, "2026-03")
    assert future.employee_member is True
    assert future.labor_member is False
    assert future.issues == ()


def test_same_day_and_same_month_order_do_not_invent_positive_duration():
    exact = project(relationship(("labor", "2026-02-01"), ("regular", "2026-02-01")), "2026-02")
    assert exact.period_states == ("regular",)
    assert exact.labor_member is False
    uncertain = project(relationship(("labor", "2026-02"), ("regular", "2026-02")), "2026-02")
    assert uncertain.period_states == ("regular",)
    assert set(uncertain.possible_period_states) == {"labor", "unknown"}
    assert uncertain.employee_member is True
    assert uncertain.labor_member is None


def test_contiguous_employee_states_can_guarantee_membership_without_fixed_individual_state():
    fact = relationship(
        ("regular", "2026-01-01"),
        ("unpaid_leave", "2026-02"),
        ("labor", "2026-02-15"),
    )
    result = project(fact, "2026-02")
    assert result.period_states == ("labor",)
    assert set(result.possible_period_states) == {"regular", "unpaid_leave"}
    assert result.employee_member is result.labor_member is True
    repeated = relationship(
        ("regular", "2026-01-01"), ("regular", "2026-02"), ("departed", "2026-02-15")
    )
    assert project(repeated, "2026-02").period_states == ("regular", "departed")


@pytest.mark.parametrize(
    "moments",
    [
        ("2026-01-20", "2026-01", "2026-01-10"),
        ("2026-02", "2026-01"),
        ("2026-02-20", "2026-02-10"),
    ],
)
def test_globally_impossible_confirmed_order_has_a_concrete_management_field(moments):
    fact = relationship(*(("regular", moment) for moment in moments))
    with pytest.raises(ContractError) as captured:
        ordered_bounds(fact)
    issue = captured.value.fact_issues[0]
    assert issue["field"] == f"/data/segments/{len(moments) - 1}/effective_from"
    assert issue["business_id"] == fact["business_id"]
    assert issue["allowed_precision"] == ["month", "day"]


def test_day_followed_by_same_month_is_feasible_and_original_precision_unchanged():
    fact = relationship(("regular", "2026-02-20"), ("departed", "2026-02"))
    before = deepcopy(fact)
    result = project(fact, "2026-02")
    assert result.period_end_state == "departed"
    assert result.employee_member is None
    assert result.segments[1].start == "2026-02"
    assert fact == before


def test_true_relationship_start_is_distinct_from_an_unknown_first_status_or_resume():
    fact = relationship(("unpaid_leave", "2026-02"), ("regular", "2026-03-10"))
    fact["data"]["segments"][0]["relationship_start"] = True
    result = project(fact, "2026-03")
    assert result.segments[0].relationship_start is True
    assert result.segments[1].relationship_start is None
    invalid = deepcopy(fact)
    invalid["data"]["segments"][1]["relationship_start"] = True
    with pytest.raises(ContractError) as captured:
        ordered_bounds(invalid)
    assert captured.value.fact_issues[0]["field"] == "/data/segments/1/relationship_start"
    cross_type = relationship(("labor", "2026-01"), ("regular", "2026-02"))
    cross_type["data"]["segments"][1]["relationship_start"] = True
    project(cross_type, "2026-02")


@pytest.mark.parametrize(
    "change",
    [
        lambda fact: fact.update(effective_on="2026-02-01"),
        lambda fact: fact["data"]["segments"][0].update(effective_from="2026-02-29"),
        lambda fact: fact["data"]["segments"][0].update(relationship_start=False),
        lambda fact: fact["data"]["segments"][0].update(state="departed", relationship_start=True),
    ],
)
def test_strict_fact_rejects_fake_overall_day_bad_day_or_a_false_start_flag(change):
    fact = relationship(("regular", "2026-02"))
    change(fact)
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "Fact", fact)


def test_maximum_calendar_month_needs_no_overflowing_invented_next_date():
    result = project(relationship(("regular", "9999-12")), "9999-12")
    assert result.employee_member is True and result.labor_member is None
    assert result.period_end_state == "regular"
    assert result.segments[0].start == "9999-12"


def test_owner_declares_one_person_relation_and_no_result_or_financial_dependency():
    fact = relationship(("departed", "2026-02"))
    context = Context(fact)
    change = {"op": "create", "expected_seq": 0, "business_id": fact["business_id"], "fact": fact}
    owner = create_people()
    description = owner.describe_change([change], context)
    assert description["businesses"][0]["complete_result_slots"] == []
    assert description["businesses"][0]["natural_key"] == "people:relationship:" + PERSON
    assert description["dependencies"] == description["publications"] == []
    assert owner.declare_relations([change], context) == (
        {
            "consumer_business_id": fact["business_id"],
            "producer_kind": "object",
            "producer_id": PERSON,
            "relation_kind": "identity",
        },
    )
    with pytest.raises(ContractError, match="没有核算结果槽"):
        owner.evaluate_slot(fact["business_id"], "main", context)


def _calendar_oracle(fact, period):
    """Enumerate real day placements and observe daily state; no interval formulas."""
    year, month = map(int, period.split("-"))
    days = [date(year, month, day) for day in range(1, monthrange(year, month)[1] + 1)]
    options = []
    for item in fact["data"]["segments"]:
        moment = item["effective_from"]
        if len(moment) == 10:
            options.append([date.fromisoformat(moment)])
        else:
            start = date.fromisoformat(moment + "-01")
            options.append(
                [
                    start + timedelta(days=index)
                    for index in range(monthrange(start.year, start.month)[1])
                ]
            )
    observed, employee, labor, ends = [], [], [], set()
    for actual in product(*options):
        if list(actual) != sorted(actual):
            continue
        daily = []
        for day in days:
            state = "unknown"
            for item, when in zip(fact["data"]["segments"], actual, strict=True):
                if when <= day:
                    state = item["state"]
            daily.append(state)
        observed.append(set(daily))
        employee.append(any(state in {"regular", "unpaid_leave"} for state in daily))
        labor.append("labor" in daily)
        ends.add(daily[-1])
    certain, all_states = set.intersection(*observed), set.union(*observed)
    unknown = "unknown" in all_states

    def membership(values):
        return True if all(values) else None if any(values) or unknown else False

    return certain, all_states - certain, membership(employee), membership(labor), ends


@pytest.mark.parametrize(
    "segments",
    [
        (("labor", "2026-02"), ("regular", "2026-02")),
        (("regular", "2026-01-01"), ("unpaid_leave", "2026-02"), ("labor", "2026-02-15")),
        (("regular", "2026-02-20"), ("departed", "2026-02")),
        (("regular", "2026-02"), ("regular", "2026-02"), ("departed", "2026-02-15")),
        (("labor", "2026-02"), ("regular", "2026-02"), ("labor", "2026-02")),
    ],
)
def test_month_projection_matches_independent_real_day_enumeration(segments):
    fact = relationship(*segments)
    certain, possible, employee, labor, ends = _calendar_oracle(fact, "2026-02")
    result = project(fact, "2026-02")
    assert set(result.period_states) == certain
    assert set(result.possible_period_states) == possible
    assert (result.employee_member, result.labor_member) == (employee, labor)
    assert {result.period_end_state} == ends
