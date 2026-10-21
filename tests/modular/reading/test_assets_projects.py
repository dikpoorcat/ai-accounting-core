"""Project rows retain unconsumed formal costs and bounded identity checks."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from tests.modular.application._working_case import case as case
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_assets import case as asset_case
from tests.modular.domains.assets.test_reading import save
from tests.modular.domains.assets.test_reading_integration import _fact

from .test_assets import page, publish


def test_partial_and_full_project_consumption_keep_summary_and_visible_counts(case):
    case.seed(expense=False)
    first, second, partial, full = (
        asset_case("B18"),
        asset_case("B18"),
        asset_case("B19"),
        asset_case("B19"),
    )
    second["business_id"] = str(uuid4())
    second["data"]["amount_fen"] = "200000"
    partial["data"]["sources"][0]["amount_fen"] = "40000"
    full["business_id"] = str(uuid4())
    full["data"]["sources"] = [
        {"source": {"business_id": second["business_id"], "slot": "cost"}, "amount_fen": "200000"}
    ]
    save(case.app, asset_request(case.company, first, second, partial, full))
    snapshot, _ = publish(case.app, case.company)
    result = page(case.app, snapshot)["data"]
    assert (
        result["project_cost_fen"]
        == result["ledger_cost_fen"]
        == result["ledger_net_fen"]
        == "60000"
    )
    assert not result["checking"]
    projects = result["collections"]["projects"]
    assert projects["page"]["total_count"] == projects["page"]["filtered_count"] == 1
    assert projects["items"][0]["cost_fen"] == "100000"
    assert projects["items"][0]["remaining_fen"] == "60000"
    assert projects["items"][0]["project_id"] == first["data"]["project_id"]
    selected = page(case.app, snapshot, section="projects", project_id=first["data"]["project_id"])
    assert set(selected["data"]["collections"]) == {"projects"}


def test_capitalized_labor_project_rows_keep_source_people_and_full_summary_before_page(case):
    case.seed(expense=False)
    project, person = str(uuid4()), str(uuid4())
    direct = _fact(
        case,
        "assets.project_cost",
        {
            "project_id": project,
            "project_nature": "purchased_intangible",
            "supplier_id": case.party,
            "amount_fen": "30000",
        },
    )
    labor = _fact(
        case,
        "people.remuneration",
        {
            "type": "labor",
            "person_id": person,
            "gross_fen": "60000",
            "destination": {
                "type": "project",
                "project_id": project,
                "project_nature": "purchased_intangible",
            },
            "withholding_state": "not_withheld_not_filed",
        },
    )
    released = _fact(
        case,
        "assets.project_release",
        {
            "project_id": project,
            "expense_class": "administration",
            "sources": [
                {
                    "source": {"business_id": direct["business_id"], "slot": "cost"},
                    "amount_fen": "10000",
                },
                {
                    "source": {"business_id": labor["business_id"], "slot": "cost"},
                    "amount_fen": "30000",
                },
            ],
        },
    )
    save(
        case.app,
        case.request(
            direct,
            labor,
            released,
            objects=[
                {
                    "object_id": project,
                    "kind": "project",
                    "name": "合成项目",
                    "expected_seq": 0,
                    "data": {},
                },
                {
                    "object_id": person,
                    "kind": "person",
                    "name": "合成劳务人员",
                    "expected_seq": 0,
                    "data": {},
                },
            ],
        ),
    )
    snapshot, _ = publish(case.app, case.company)
    result = page(case.app, snapshot, section="projects", limit=1)["data"]
    assert result["project_cost_fen"] == "50000"
    first = result["collections"]["projects"]
    assert first["page"]["total_count"] == first["page"]["filtered_count"] == 2
    assert len(first["items"]) == 1 and first["page"]["has_more"]
    second = page(
        case.app, snapshot, section="projects", limit=500, cursor=first["page"]["next_cursor"]
    )["data"]["collections"]["projects"]
    rows = [*first["items"], *second["items"]]
    assert {item["kind"] for item in rows} == {"assets.project_cost", "people.remuneration"}
    assert {item["party"] for item in rows} == {"合成供应商", "合成劳务人员"}
    assert sum(int(item["remaining_fen"]) for item in rows) == 50000


def test_unrelated_project_does_not_change_projection_and_exact_empty_project_is_valid(
    case, monkeypatch
):
    case.seed(expense=False)
    before, _ = publish(case.app, case.company)
    empty_project = str(uuid4())
    save(
        case.app,
        case.request(
            objects=[
                {
                    "object_id": empty_project,
                    "kind": "project",
                    "name": "另一个没有资产成本的项目",
                    "expected_seq": 0,
                    "data": {},
                }
            ]
        ),
    )
    after, _ = publish(case.app, case.company)
    assert after.version == before.version
    assert after.components == before.components
    save(case.app, case.request(case.expense("700")))
    after, _ = publish(case.app, case.company)
    assert after.version == before.version and after.components == before.components
    calls = []
    original = case.kernel.identity.get_batch

    def selected(ids, read):
        calls.append(tuple(ids))
        return original(ids, read)

    monkeypatch.setattr(case.kernel.identity, "get_batch", selected)
    empty = page(case.app, after, project_id=empty_project, section="projects")["data"][
        "collections"
    ]["projects"]
    assert empty["items"] == [] and calls == [(empty_project,)]
    with pytest.raises(ContractError) as error:
        page(case.app, after, project_id=case.party)
    assert error.value.code == "not_found"
    with pytest.raises(ContractError) as error:
        page(case.app, after, project_id=str(uuid4()))
    assert error.value.code == "not_found"
