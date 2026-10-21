"""Real employee routes share one saved capture and refresh current supplements."""

from uuid import uuid4

from ai_accounting_modular.contracts.public import validate_contract
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import PERSON, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import add
from tests.modular.domains.people.test_relationships import relationship


def ready(case, parameters):
    with case.kernel.dashboard.get("/api/dashboard/employees", parameters) as response:
        assert response.status == 200 and response.body["state"] == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    with case.kernel.dashboard.get("/api/dashboard/employees", parameters) as response:
        assert response.status == 200 and response.body["state"] == "ready"
        result = response.body["response"]
        assert response.body["model_version"] == result["snapshot_version"]
        validate_contract("dashboard-response-v2.json", "dashboard_employees", result)
        return result


def test_http_hot_sections_and_precise_person_use_one_capture_and_preserve_explicit_filter(
    case, monkeypatch
):
    case.seed(expense=False)
    add(case, no_contributions(), business_case("B17Z"))
    captured = []
    original = case.kernel.reading.people.remuneration_activity

    def capture(scope, read):
        captured.append(scope)
        return original(scope, read)

    monkeypatch.setattr(case.kernel.reading.people, "remuneration_activity", capture)
    parameters = {"company_id": case.company, "period": "2026-01"}
    result = ready(case, parameters)
    assert result["data"]["collections"]["employees"]["items"] == []
    query = {
        **parameters,
        "section": "employees",
        "employee_id": PERSON,
        "expected_version": result["snapshot_version"],
    }
    with case.kernel.dashboard.get("/api/dashboard/employees", query) as response:
        assert response.status == 200
        assert response.body["data"]["employee_filter"] == "employment_active"
        assert (
            response.body["data"]["collections"]["employees"]["items"][0]["gross_salary_fen"]
            == "100000"
        )
    with case.kernel.dashboard.get(
        "/api/dashboard/employees", {**query, "employee_filter": "employment_active"}
    ) as response:
        assert response.status == 200
        assert response.body["data"]["collections"]["employees"]["items"] == []
    with case.kernel.dashboard.get(
        "/api/dashboard/employees", {**query, "employee_id": str(uuid4())}
    ) as response:
        assert response.status == 404
    with case.kernel.dashboard.get(
        "/api/dashboard/employees", {**parameters, "section": "labor_sources"}
    ) as response:
        assert response.status == 400
    assert len(captured) == 1 and case.kernel.reading.run_pending(case.company) == ()


def test_closed_page_relationship_and_name_updates_refresh_without_rewriting_payroll(
    case, monkeypatch
):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    assert case.close(case.check())["state"] == "closed"
    before = case.get(wage["business_id"])["businesses"]
    captured = []
    original = case.kernel.reading.people.remuneration_activity

    def capture(scope, read):
        captured.append(scope)
        return original(scope, read)

    monkeypatch.setattr(case.kernel.reading.people, "remuneration_activity", capture)
    parameters = {"company_id": case.company, "period": "2026-01"}
    initial = ready(case, parameters)
    assert initial["data"]["employees"]["unknown_period_count"] == 1
    relation = relationship(("regular", "2026-01"))
    relation["data"]["segments"][0]["relationship_start"] = True
    saved = add(case, relation)
    assert saved["accounting"] == "unchanged"
    with case.kernel.dashboard.get(
        "/api/dashboard/employees",
        {**parameters, "section": "employees", "expected_version": initial["snapshot_version"]},
    ) as response:
        assert response.status == 409 and response.body["code"] == "dashboard_snapshot_changed"
    related = ready(case, parameters)
    assert related["snapshot_version"] != initial["snapshot_version"]
    card = related["data"]["collections"]["employees"]["items"][0]
    assert card["employment_start_date"] == "2026-01" and card["gross_salary_fen"] == "100000"
    renamed = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": PERSON,
                    "kind": "person",
                    "name": "张某新名",
                    "expected_seq": 1,
                    "data": {},
                }
            ]
        )
    )
    assert renamed["completion"] == "complete" and renamed["accounting"] == "unchanged", renamed
    current = ready(case, parameters)
    assert current["snapshot_version"] != related["snapshot_version"]
    assert current["data"]["collections"]["employees"]["items"][0]["name"] == "张某新名"
    assert current["data"]["employees"] == related["data"]["employees"]
    assert case.get(wage["business_id"])["businesses"] == before
    assert len(captured) == 3


def test_unknown_opening_is_ready_partial_business_data_and_does_not_retry_forever(case):
    add(case, no_contributions(), business_case("B17Z"))
    parameters = {"company_id": case.company, "period": "2026-01", "employee_filter": "all"}
    initial = ready(case, parameters)
    assert initial["data"]["employees"]["gross_salary_fen"] == "100000"
    assert initial["data"]["outstanding_remuneration_fen"] is None
    for _ in range(2):
        with case.kernel.dashboard.get("/api/dashboard/employees", parameters) as response:
            assert response.status == 200 and response.body["state"] == "ready"
            assert response.body["response"]["snapshot_version"] == initial["snapshot_version"]
        assert case.kernel.reading.run_pending(case.company) == ()


def test_unrelated_expense_and_ordinary_person_leave_complete_employee_response_unchanged(case):
    case.seed(expense=False)
    add(case, no_contributions(), business_case("B17Z"), relationship(("regular", "2026-01-01")))
    parameters = {"company_id": case.company, "period": "2026-01"}
    original = ready(case, parameters)
    saved = case.app.accounting_apply(
        case.request(
            case.expense("700"),
            objects=[
                {
                    "object_id": str(uuid4()),
                    "kind": "person",
                    "name": "阿甲普通报销人",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
    )
    assert saved["completion"] == "complete", saved
    current = ready(case, parameters)
    assert current == original
