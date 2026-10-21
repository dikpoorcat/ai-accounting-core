"""Complete personnel/source counts survive both public and wire pagination."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.people.public import create_people
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import add

from .test_employees import page, publish


def test_501_people_and_21_same_person_sources_keep_exact_full_totals_and_bounded_pages(
    case, monkeypatch
):
    case.seed(expense=False)
    facts = []
    for _ in range(501):
        labor = business_case("B17L")
        labor["business_id"] = str(uuid4())
        labor["data"].update(person_id=str(uuid4()), gross_fen="1")
        facts.append(labor)
    facts[0]["data"]["gross_fen"] = "9007199254740993"
    for _ in range(20):
        labor = deepcopy(facts[0])
        labor["business_id"] = str(uuid4())
        labor["data"]["gross_fen"] = "1"
        facts.append(labor)
    for offset in range(0, len(facts), 250):
        add(case, *facts[offset : offset + 250])
    owner = create_people(ledger=case.kernel.ledger)
    captured = []
    original = owner.remuneration_activity

    def capture(scope, read):
        captured.append(scope)
        return original(scope, read)

    monkeypatch.setattr(owner, "remuneration_activity", capture)
    snapshot = publish(case, owner=owner)
    assert len(captured) == 1
    monkeypatch.setattr(
        owner, "remuneration_activity", lambda *_: pytest.fail("hot page reloaded payroll")
    )
    first = page(case, snapshot, employee_filter="all")
    assert first["employees"]["registered_count"] == 0
    assert first["workforce_cost"]["personal_labor_fen"] == "9007199254741513"
    assert first["outstanding_remuneration_fen"] == "9007199254741513"
    workers = first["collections"]["labor_workers"]
    assert workers["page"]["total_count"] == workers["page"]["filtered_count"] == 501
    assert len(workers["items"]) == 20
    second = page(
        case,
        snapshot,
        section="labor_workers",
        employee_filter="all",
        cursor=workers["page"]["next_cursor"],
        limit=500,
    )["collections"]["labor_workers"]
    assert len(second["items"]) == 481 and not second["page"]["has_more"]
    assert len({item["person_id"] for item in [*workers["items"], *second["items"]]}) == 501
    assert page(case, snapshot)["collections"]["labor_workers"]["page"]["filtered_count"] == 0
    person = facts[0]["data"]["person_id"]
    focused = page(case, snapshot, employee_id=person)["collections"]["labor_workers"]["items"][0]
    assert focused["source_count"] == 21 and focused["gross_fen"] == "9007199254741013"
    sources = page(case, snapshot, section="labor_sources", employee_id=person)["collections"][
        "labor_sources"
    ]
    assert sources["page"]["total_count"] == 521 and sources["page"]["filtered_count"] == 21
    assert len(sources["items"]) == 20
    last = page(
        case,
        snapshot,
        section="labor_sources",
        employee_id=person,
        cursor=sources["page"]["next_cursor"],
    )["collections"]["labor_sources"]
    assert len(last["items"]) == 1 and not last["page"]["has_more"]
    assert len({item["source_id"] for item in [*sources["items"], *last["items"]]}) == 21
    with pytest.raises(ContractError) as error:
        page(
            case,
            snapshot,
            section="labor_workers",
            employee_filter="unknown",
            cursor=workers["page"]["next_cursor"],
        )
    assert error.value.code == "snapshot_changed"
