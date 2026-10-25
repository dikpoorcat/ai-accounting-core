"""Input references and typed owner failures must not become owner questionnaires."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from tests.modular.domains.assets.test_assets import case as asset_fact
from tests.modular.domains.people.test_people import case as people_fact

from ._working_case import case as case


@pytest.mark.parametrize("family", ["operations", "people", "assets"])
@pytest.mark.parametrize("registered", [False, True])
def test_object_identity_error_is_typed_at_the_owner_before_description_aggregation(
    case, family, registered
):
    case.seed(expense=False)
    identity = case.party if registered else str(uuid4())
    if family == "operations":
        fact = case.expense()
        fact["data"].update(creditor_id=identity, creditor_kind="employee")
        field = "/data/creditor_id"
    elif family == "people":
        fact = people_fact("B14")
        fact["data"]["employee_id"] = identity
        field = "/data/employee_id"
    else:
        fact = asset_fact("B20")
        fact["data"]["asset"]["asset_id"] = identity
        fact["data"]["supplier_id"] = case.party
        field = "/data/asset/asset_id"
    fact["evidence_ids"] = [case.source_id]
    with case.kernel.storage.read_unit(case.company) as read:
        before = read.head
    result = case.app.accounting_apply(case.request(fact))
    assert result["error"] == "invalid_input", result
    assert result["completion"] == "not_saved" and result["changes"] == "none"
    problem = result["fact_issues"][0]
    assert problem["field"] == field
    assert problem["business_id"] == fact["business_id"]
    assert problem["code"] == (
        "incompatible_object_kind" if registered else "reference_not_found"
    )
    assert case.get(fact["business_id"])["businesses"] == []
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before


@pytest.mark.parametrize("code", ["invalid_input", "unsupported_business", "dependency_conflict"])
def test_description_propagates_owner_error_without_reclassifying_it(case, monkeypatch, code):
    case.seed(expense=False)
    fact = case.expense()
    problem = {
        "code": "synthetic_typed_problem",
        "business_id": fact["business_id"],
        "field": "/data/creditor_id",
        "meaning": "原业务引用",
        "message": "合成领域给出的精确原因。",
        "allowed_precision": [],
        "reusable_sources": [{"source_id": case.source_id}],
        "blocking_scope": "business",
    }

    def fail(*_):
        raise ContractError(code, problem["message"], fact_issues=[problem])

    monkeypatch.setattr(case.app.registry.owner(fact["kind"]), "describe_change", fail)
    result = case.app.accounting_apply(case.request(fact))
    assert result["error"] == code, result
    assert result["fact_issues"] == [problem]
    assert result["changes"] == "none"
