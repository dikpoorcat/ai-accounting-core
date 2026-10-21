"""Labor details retain exact source legs, real payments and source-month relationships."""

from copy import deepcopy
from uuid import uuid4

from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_application import replacement
from tests.modular.domains.people.test_people import PERSON, SOURCE, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import add, bank_object, correct, payment
from tests.modular.domains.people.test_relationships import relationship

from .test_employees import page, publish


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
