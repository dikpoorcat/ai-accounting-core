"""The coordinator knows the actual failing slot, not the first submitted business."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError

from .conftest import initial_request
from .test_isolated_errors import run_failure


def test_evaluation_failure_locates_the_business_and_remains_atomic(assembled, monkeypatch):
    app, company = assembled
    request = initial_request(company)
    first = request["mutations"][0]["fact"]
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    request["mutations"].append({"op": "create", "expected_seq": 0, "fact": second})
    owner = app.registry.owner(first["kind"])
    evaluate = owner.evaluate_slot
    diagnostic = str(uuid4())

    def fail(business_id, result_slot, context):
        if business_id == second["business_id"]:
            raise ContractError("dependency_conflict", "合成核验失败。", diagnostic_id=diagnostic)
        return evaluate(business_id, result_slot, context)

    monkeypatch.setattr(owner, "evaluate_slot", fail)
    rejected = app.accounting_apply(request)
    assert rejected["completion"] == "not_saved", rejected
    assert rejected["error"] == "dependency_conflict"
    assert rejected["message"] == "合成核验失败。"
    assert rejected["diagnostic_id"] == diagnostic
    assert rejected["fact_issues"][0]["business_id"] == second["business_id"]
    assert rejected["fact_issues"][0]["field"] == "/"
    assert rejected["fact_issues"][0]["allowed_precision"] == []
    with app.storage.read_unit(company) as read:
        assert read.head == 0


def test_existing_offline_failure_mapping_exposes_the_business_without_new_response_fields(
    tmp_path, monkeypatch
):
    business = str(uuid4())
    error = ContractError("dependency_conflict", "业务核验失败。").at_business(business)
    response = run_failure(tmp_path, monkeypatch, error)
    assert response["error"] == "dependency_conflict"
    assert response["message"] == error.message
    assert response["checks"] == [
        {
            "code": "dependency_conflict",
            "state": "fail",
            "message": f"业务：{business}\n字段：/\n业务核验失败。",
        }
    ]
