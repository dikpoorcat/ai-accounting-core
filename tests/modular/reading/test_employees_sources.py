"""Labor details retain exact source legs, real payments and source-month relationships."""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_application import replacement
from tests.modular.domains.people.test_people import PERSON, SOURCE, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import add, bank_object, correct, payment
from tests.modular.domains.people.test_relationships import relationship

from .test_business_status import progress
from .test_employees import page, publish


@pytest.mark.parametrize(
    "example,classification",
    [("B17Z", "payroll"), ("B17B", "annual_bonus"), ("B17L", "labor"),
     ("B17P", "labor_project_cost")],
)
def test_adopted_remuneration_has_exact_display_kind_and_shared_labor_obligation_keys(
    case, example, classification
):
    case.seed(expense=False)
    remuneration = business_case(example)
    facts = [remuneration]
    if remuneration["data"]["type"] == "wage":
        facts.insert(0, no_contributions())
    add(case, *facts)
    status = progress(case, remuneration["business_id"])["data"]
    assert status["identity"]["kind"] == classification
    assert status["current_business_result"]["amount_fen"] == remuneration["data"]["gross_fen"]
    assert status["current_business_result"]["amount_label"] == "应发金额"
    stored = case.app.accounting_get(
        {"company_id": case.company, "business_ids": [remuneration["business_id"]],
         "include": ["facts"]}
    )["businesses"][0]["fact"]
    assert stored["kind"] == "people.remuneration"
    assert stored["data"] == remuneration["data"]
    if remuneration["data"]["type"] != "labor":
        return
    sources = page(
        case, publish(case), section="labor_sources", employee_id=remuneration["data"]["person_id"]
    )["collections"]["labor_sources"]["items"]
    assert len(sources) == 1
    source = sources[0]
    assert source["capitalized"] is (classification == "labor_project_cost")
    assert source["obligations"][0]["key"] == remuneration["business_id"] + ":net_pay"
    by_key = {item["key"]: item for item in status["settlements"]["obligations"]}
    assert set(by_key) == {item["key"] for item in source["obligations"]}
    for item in source["obligations"]:
        shown = by_key[item["key"]]
        assert shown["name"] == item["name"] and shown["source_period"] == source["period"]
        for field in ("paid_fen", "other_settled_fen", "remaining_fen"):
            assert shown[field] == item[field]
        assert shown["source_amount_fen"] == item["amount_fen"]


def test_historical_remuneration_display_kind_uses_adopted_type_before_later_correction(case):
    case.seed(expense=False)
    labor = business_case("B17L")
    labor["data"]["person_id"] = PERSON
    add(case, no_contributions(), labor)
    assert case.close(case.check())["state"] == "closed"
    changed = deepcopy(labor)
    changed["data"] = business_case("B17Z")["data"]
    correct(case, changed, period="2026-02")
    assert progress(case, labor["business_id"], "2026-01")["data"]["identity"]["kind"] == "labor"
    assert progress(case, labor["business_id"], "2026-02")["data"]["identity"]["kind"] == "payroll"


def test_paid_labor_reduction_keeps_cash_100000_other_minus_20000_and_no_fake_unpaid(case):
    case.seed(expense=False)
    labor, bank = business_case("B17L"), bank_object()
    labor["data"].update(person_id=PERSON, gross_fen="100000")
    paid = payment(bank["object_id"], "2026-01", (labor["business_id"], "100000"))
    relation = relationship(("labor", "2026-01-01"), ("labor_ended", "2026-02-01"))
    add(case, labor, paid, relation, objects=[bank])
    labor["data"]["gross_fen"] = "80000"
    claim = {
        "business_id": str(uuid4()),
        "kind": "operations.overpayment",
        "period": "2026-02",
        "evidence_ids": [SOURCE],
        "data": {
            "source": {"business_id": labor["business_id"], "slot": "net_pay"},
            "amount_fen": "20000",
        },
    }
    command = replacement(case.company, labor)
    command["mutations"].append({"op": "create", "expected_seq": 0, "fact": claim})
    saved = case.app.accounting_apply(command)
    assert saved["completion"] == "complete", saved
    snapshot = publish(case, "2026-02")
    normal = page(case, snapshot)
    assert normal["collections"]["labor_workers"]["items"] == []
    assert normal["outstanding_remuneration_fen"] == "0"
    data = page(case, snapshot, section="labor_sources", employee_id=PERSON)
    source = data["collections"]["labor_sources"]["items"][0]
    assert source["gross_fen"] == source["net_fen"] == "80000"
    assert source["relationship_confirmed"]  # January labor is known although February ended.
    assert len(source["obligations"]) == 1
    net = source["obligations"][0]
    assert (net["amount_fen"], net["paid_fen"], net["other_settled_fen"], net["remaining_fen"]) == (
        "80000",
        "100000",
        "-20000",
        "0",
    )
    assert net["period_paid_fen"] == "0" and net["period_other_settled_fen"] == "-20000"


def test_closed_labor_to_wage_correction_has_negative_labor_once_and_no_permanent_wrong_role(case):
    case.seed(expense=False)
    labor = business_case("B17L")
    labor["data"].update(person_id=PERSON)
    add(case, no_contributions(), labor)
    assert case.close(case.check())["state"] == "closed"
    changed = deepcopy(labor)
    changed["data"] = business_case("B17Z")["data"]
    correct(case, changed, period="2026-02")
    january = page(case, publish(case), employee_filter="all")
    assert january["employees"]["registered_count"] == 0
    assert january["collections"]["labor_workers"]["items"][0]["gross_fen"] == "100000"
    february_snapshot = publish(case, "2026-02")
    february = page(case, february_snapshot, employee_filter="all")
    assert february["employees"]["gross_salary_fen"] == "100000"
    assert february["workforce_cost"] == {
        "total_fen": "0",
        "personal_labor_fen": "-100000",
        "capitalized_labor_fen": "0",
    }
    worker = february["collections"]["labor_workers"]["items"][0]
    assert worker["gross_fen"] == "-100000" and worker["source_count"] == 1
    source = page(case, february_snapshot, section="labor_sources", employee_id=PERSON)[
        "collections"
    ]["labor_sources"]["items"][0]
    assert source["gross_fen"] == "-100000" and source["obligations"] == []
    assert source["source_id"] != source["subject_id"]
    march = page(case, publish(case, "2026-03"), employee_filter="all")
    assert march["collections"]["labor_workers"]["items"] == []
    assert (
        march["employees"]["registered_count"] == 1
        and march["employees"]["gross_salary_fen"] == "0"
    )
