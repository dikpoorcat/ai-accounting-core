"""The existing employee page consumes complete formal payroll and relationships."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.reading import _employees, _monthly
from ai_accounting_modular.reading._context import period
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import PERSON, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import (
    add,
    bank_object,
    correct,
    paid_wage_recovery,
    payment,
    wage_with_actuals,
)
from tests.modular.domains.people.test_relationships import relationship


def publish(case, month="2026-01", *, owner=None):
    scope = _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )
    reader = SimpleNamespace(
        people=owner or create_people(ledger=case.kernel.ledger), identity=case.kernel.identity
    )
    with case.kernel.storage.read_unit(case.company) as read:
        model = _employees.prepare(reader, scope, read)
        selected = period(month, case.kernel.ledger.closed_through(read))
        database = read.database_id
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository.register(_monthly.key(scope), "employees", unit.head, _monthly.FORMAT_VERSION)
        repository.mark(_monthly.key(scope), "employees", unit.head)
        assert repository.publish(
            _monthly.key(scope), "employees", unit.head, _monthly.FORMAT_VERSION, model
        )
    return _monthly.MonthSnapshot(
        _monthly.key(scope),
        scope,
        content_digest(
            {"employees": model.content_digest, "scope": scope, "status": selected["status"]}
        ),
        {"employees": loads_json(model.payload_json)},
        selected,
        case.company,
        database,
    )


def page(case, snapshot, **query):
    with case.kernel.storage.read_unit(case.company) as read:
        result = _employees.build(snapshot, read, query, identity=case.kernel.identity)
    closed = snapshot.selected_period["status"] == "closed"
    envelope = {
        "schema_version": 13,
        **result,
        "read_context": {
            "company_id": case.company,
            "database_id": snapshot.database_id,
            "as_of": "2026-10-21",
            "read_version": snapshot.version,
        },
        "read_semantics": {
            "knowledge": "current_knowledge",
            "accounting": "as_posted",
            "business_basis": "frozen_adoption" if closed else "current_known",
            "display": "frozen_with_current_supplements" if closed else "current",
            "system_time_replay": False,
            "recorded_at": "system_recording_time",
            "recording_period": "business_recording_period",
            "recorded_later": "business_recording_period_after_selected_period",
        },
    }
    validate_contract("dashboard-response-v2.json", "dashboard_employees", envelope)
    return result["data"]


def test_salary_bonus_contributions_and_same_person_capitalized_labor_keep_distinct_costs(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    bonus, labor, capital = (business_case(name) for name in ("B17B", "B17L", "B17P"))
    labor["data"].update(person_id=PERSON, gross_fen="200000")
    capital["data"].update(person_id=PERSON, gross_fen="300000")
    capital["data"]["tax_basis"]["tax_fen"] = "30000"
    relation = relationship(("labor", "2026-01-01"), ("regular", "2026-01-20"))
    relation["data"]["segments"][1]["relationship_start"] = True
    add(case, wage, employment, actual, bonus, labor, capital, relation)
    snapshot = publish(case)
    data = page(case, snapshot)
    assert set(data["collections"]) == {"employees", "labor_workers"}
    summary = data["employees"]
    assert (
        summary["registered_count"]
        == summary["in_period_count"]
        == summary["labor_worker_count"]
        == 1
    )
    assert (
        summary["gross_salary_fen"],
        summary["annual_bonus_fen"],
        summary["ledger_cost_fen"],
    ) == ("600000", "100000", "790000")
    assert summary["personal_deduction_fen"] == "110000"
    assert data["outstanding_remuneration_fen"] == "1060000"
    assert data["workforce_cost"] == {
        "total_fen": "990000",
        "personal_labor_fen": "200000",
        "capitalized_labor_fen": "300000",
    }
    card = data["collections"]["employees"]["items"][0]
    assert card["employment_start_date"] == "2026-01-20" and card["employment_end_date"] is None
    assert card["period_states"] == ["regular", "labor"]
    assert card["company_cost_fen"] == "790000" and card["net_salary_fen"] == "590000"
    worker = data["collections"]["labor_workers"]["items"][0]
    assert worker["source_count"] == 2 and worker["gross_fen"] == "500000"
    sources = page(case, snapshot, section="labor_sources", employee_id=PERSON)["collections"][
        "labor_sources"
    ]
    assert sources["page"]["total_count"] == 2
    by_project = {item["capitalized"]: item for item in sources["items"]}
    assert (
        by_project[True]["gross_fen"] == "300000" and by_project[True]["booked_tax_fen"] == "30000"
    )
    assert by_project[False]["gross_fen"] == "200000"
    assert all(item["relationship_confirmed"] for item in sources["items"])
    assert all(item["source_id"] == item["subject_id"] for item in sources["items"])
    assert all(
        len({obligation["key"] for obligation in item["obligations"]}) == len(item["obligations"])
        for item in sources["items"]
    )


def test_unknown_relationship_keeps_known_wages_and_exact_person_bypasses_default_filter(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    extra = str(uuid4())
    saved = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": extra,
                    "kind": "person",
                    "name": "只有报销身份的自然人",
                    "expected_seq": 0,
                    "data": {},
                }
            ]
        )
    )
    assert saved["completion"] == "complete", saved
    snapshot = publish(case)
    normal = page(case, snapshot)
    assert (
        normal["employees"]["registered_count"] == normal["employees"]["unknown_period_count"] == 1
    )
    assert normal["collections"]["employees"]["page"]["filtered_count"] == 0
    assert normal["employees"]["gross_salary_fen"] == "100000"
    exact = page(case, snapshot, employee_id=PERSON)["collections"]["employees"]["items"][0]
    assert exact["gross_salary_fen"] == exact["outstanding_net_fen"] == "100000"
    assert exact["in_period"] is None and exact["employment_state"] == "unknown"
    assert exact["employment_start_date"] is None and exact["relationship_segments"] == []
    assert (
        page(case, snapshot, employee_id=PERSON, employee_filter="employment_active")[
            "collections"
        ]["employees"]["items"]
        == []
    )
    assert page(case, snapshot, employee_id=extra)["collections"]["employees"]["items"] == []
    with pytest.raises(ContractError) as error:
        page(case, snapshot, employee_id=str(uuid4()))
    assert error.value.code == "not_found"


def test_prior_wage_and_capitalized_labor_payments_do_not_become_this_month_cost(case):
    case.seed(expense=False)
    wage, capital = business_case("B17Z"), business_case("B17P")
    capital["data"].update(person_id=PERSON, gross_fen="80000")
    capital["data"]["tax_basis"]["tax_fen"] = "0"
    bank = bank_object()
    paid = payment(
        bank["object_id"],
        "2026-02",
        (wage["business_id"], "60000"),
        (capital["business_id"], "30000"),
    )
    add(case, no_contributions(), wage, capital, paid, objects=[bank])
    snapshot = publish(case, "2026-02")
    data = page(case, snapshot, employee_filter="all")
    summary = data["employees"]
    assert summary["ledger_cost_fen"] == summary["gross_salary_fen"] == "0"
    assert (
        summary["direct_net_payments_fen"] == "60000" and summary["outstanding_net_fen"] == "40000"
    )
    assert data["outstanding_remuneration_fen"] == "90000"
    assert (
        data["workforce_cost"]["total_fen"]
        == data["workforce_cost"]["capitalized_labor_fen"]
        == "0"
    )
    worker = data["collections"]["labor_workers"]["items"][0]
    assert worker["gross_fen"] == "0" and worker["source_count"] == 1
    source = page(case, snapshot, section="labor_sources", employee_id=PERSON)["collections"][
        "labor_sources"
    ]["items"][0]
    assert source["period"] == "2026-01" and source["gross_fen"] == "80000"
    net = next(item for item in source["obligations"] if item["name"] == "net")
    assert (net["paid_fen"], net["period_paid_fen"], net["remaining_fen"]) == (
        "30000",
        "30000",
        "50000",
    )


def test_no_opening_leaves_only_accumulated_unpaid_unknown(case):
    add(case, no_contributions(), business_case("B17Z"))
    snapshot = publish(case)
    data = page(case, snapshot, employee_filter="all")
    assert data["employees"]["gross_salary_fen"] == data["employees"]["ledger_cost_fen"] == "100000"
    assert data["employees"]["checking"]
    assert data["outstanding_remuneration_fen"] is data["employees"]["outstanding_net_fen"] is None
    card = data["collections"]["employees"]["items"][0]
    assert card["outstanding_net_fen"] is None and card["gross_salary_fen"] == "100000"


def test_closed_labor_project_reclassification_preserves_signed_sources(case):
    case.seed(expense=False)
    capital = business_case("B17P")
    add(case, capital)
    assert case.close(case.check())["state"] == "closed"
    changed = deepcopy(capital)
    changed["data"]["destination"] = {"type": "expense", "area": "administration"}
    correct(case, changed, period="2026-02")
    snapshot = publish(case, "2026-02")
    data = page(case, snapshot, employee_filter="all")
    assert data["workforce_cost"] == {
        "total_fen": "100000",
        "personal_labor_fen": "100000",
        "capitalized_labor_fen": "-100000",
    }
    worker = data["collections"]["labor_workers"]["items"][0]
    assert worker["gross_fen"] == "0" and worker["source_count"] == 2
    sources = page(
        case, snapshot, section="labor_sources", employee_id=capital["data"]["person_id"]
    )["collections"]["labor_sources"]["items"]
    assert len({item["source_id"] for item in sources}) == 2
    assert {item["capitalized"]: item["gross_fen"] for item in sources} == {
        True: "-100000",
        False: "100000",
    }
    assert sum(len(item["obligations"]) for item in sources) == 2


def test_overpayment_recovery_keeps_real_cash_and_does_not_recreate_unpaid_salary(case):
    bank, _, claim = paid_wage_recovery(case)
    returned = payment(bank["object_id"], "2026-03", (claim["business_id"], "5000"))
    returned["data"]["direction"] = "inflow"
    returned["data"]["allocations"][0]["source"]["slot"] = "recovery"
    add(case, returned)
    february = page(case, publish(case, "2026-02"), employee_filter="all")
    assert (
        february["employees"]["outstanding_net_fen"]
        == february["outstanding_remuneration_fen"]
        == "0"
    )
    assert february["employees"]["direct_net_payments_fen"] == "0"
    assert february["employees"]["other_net_settlements_fen"] == "-20000"
    march = page(case, publish(case, "2026-03"), employee_filter="all")
    assert march["employees"]["direct_net_payments_fen"] == "-5000"
    assert (
        march["employees"]["other_net_settlements_fen"]
        == march["employees"]["outstanding_net_fen"]
        == "0"
    )


def test_relationship_only_people_and_filters_keep_month_precision_and_confirmed_join(case):
    case.seed(expense=False)
    active, leave, departed, later = (str(uuid4()) for _ in range(4))
    regular = relationship(("regular", "2026-01"), person=active)
    regular["data"]["segments"][0]["relationship_start"] = True
    unpaid = relationship(("regular", "2025-12-01"), ("unpaid_leave", "2026-01-01"), person=leave)
    unpaid["data"]["segments"][0]["relationship_start"] = True
    ended = relationship(("departed", "2026-01-01"), person=departed)
    future = relationship(("regular", "2026-02-15"), person=later)
    add(case, regular, unpaid, ended, future)
    snapshot = publish(case)
    all_data = page(case, snapshot, employee_filter="all")
    assert all_data["employees"]["registered_count"] == 3
    assert (
        all_data["employees"]["in_period_count"]
        == all_data["employees"]["without_payroll_count"]
        == 2
    )
    assert all_data["employees"]["gross_salary_fen"] == "0"
    normal = page(case, snapshot)["collections"]["employees"]
    assert normal["page"]["filtered_count"] == 1
    assert normal["items"][0]["employee_id"] == active
    assert normal["items"][0]["employment_start_date"] == "2026-01"
    leaves = page(case, snapshot, employee_filter="employment_unpaid_leave")["collections"][
        "employees"
    ]["items"]
    assert len(leaves) == 1 and leaves[0]["employee_id"] == leave
    assert leaves[0]["employment_start_date"] == "2025-12-01"
    assert leaves[0]["employment_end_date"] is None
    ends = page(case, snapshot, employee_filter="employment_departed")["collections"]["employees"][
        "items"
    ]
    assert len(ends) == 1 and ends[0]["employee_id"] == departed
    assert ends[0]["employment_end_date"] == "2026-01-01"
    assert (
        page(case, snapshot, employee_filter="unknown")["collections"]["employees"]["items"] == []
    )


def test_fully_paid_quiet_roster_keeps_people_without_reinterpreting_old_payroll(case):
    case.seed(expense=False)
    wage, bank = business_case("B17Z"), bank_object()
    paid = payment(bank["object_id"], "2026-01", (wage["business_id"], "100000"))
    add(case, no_contributions(), wage, paid, objects=[bank])
    data = page(case, publish(case, "2026-02"), employee_filter="all")
    assert data["employees"]["registered_count"] == data["employees"]["unknown_period_count"] == 1
    card = data["collections"]["employees"]["items"][0]
    assert not card["has_payroll_activity"] and card["batch_count"] == 0
    assert (
        card["gross_salary_fen"]
        == card["outstanding_net_fen"]
        == card["direct_net_payments_fen"]
        == "0"
    )
