"""Strict read responses, exact HTTP money, live consumers and read-only generation."""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from response_samples import http_samples, native_samples
from test_dashboard_transport import authenticated
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.platforms import BankPlatformTransfer
from ai_accounting.kernel.query_semantics import classify_financial_position
from ai_accounting.kernel.response_contracts import (
    RESPONSE_ADAPTERS,
    http_response,
    response_schemas,
    validate_response,
)

ROOT = Path(__file__).resolve().parents[2]
resident = resident_fixture


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return native_samples(tmp_path_factory.mktemp("response-contracts"))


def test_live_shapes_preserve_native_types_omission_and_null(samples):
    for item in samples.values():
        command, response = item["command"], item["response"]
        assert validate_response(command, response) == response
        assert RESPONSE_ADAPTERS[command].dump_python(response) == response
    empty = samples["empty_context"]["response"]
    assert "generated_at" not in empty and empty["current_company"] is None
    assert samples["company_without_period"]["response"]["periods"] == []
    assert samples["funds_without_period"]["response"]["data"] is None
    assert samples["deferred_funds"]["response"]["data"]["period_preparation"] is None
    assert "movement_page" not in samples["page_accounts"]["response"]["data"]
    assert "page" not in samples["page_accounts"]["response"]["data"]["bank_statement"]


@pytest.mark.parametrize("amount", [None, 0, 2**53 + 1, 2**63 - 1, -(2**63)])
def test_native_and_http_int64_money_in_nested_issues(samples, amount):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    value["data"]["total_fen"] = amount
    issue = {
        "field": "materials",
        "message": "synthetic mismatch",
        "expected_fen": amount,
        "actual_fen": 0,
        "pages": 2,
    }
    value["data"]["period_preparation"]["current_followups"]["materials"]["issues"] = [issue]
    native = validate_response("dashboard_funds", value)
    wire = http_response("dashboard_funds", value)
    assert native == value
    assert wire["data"]["total_fen"] == (str(amount) if amount is not None else None)
    wire_issue = wire["data"]["period_preparation"]["current_followups"]["materials"]["issues"][0]
    assert wire_issue["expected_fen"] == (str(amount) if amount is not None else None)
    assert wire_issue["actual_fen"] == "0" and type(wire_issue["pages"]) is int
    assert wire["data"]["movements"][0]["amount_fen"] == str(
        value["data"]["movements"][0]["amount_fen"]
    )


@pytest.mark.parametrize("bad", [1.0, True, False, "1", 2**63, -(2**63) - 1])
def test_invalid_native_money_rejected_without_input_values(samples, bad):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    value["data"]["total_fen"] = bad
    with pytest.raises(KernelError) as failure:
        validate_response("dashboard_funds", value)
    assert failure.value.response() == {
        "status": "rejected",
        "code": "response_contract_mismatch",
        "message": "读取结果不符合接口合同",
        "paths": ["data.total_fen"],
    }


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "version", "float_version", "bool_count", "nested_extra"]
)
def test_contract_mismatch_is_a_program_error(samples, mutation):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    if mutation == "missing":
        del value["data"]["bank_opening_fen"]
    elif mutation == "extra":
        value["private-company-file"] = "secret"
    elif mutation == "version":
        value["schema_version"] = 3
    elif mutation == "float_version":
        value["schema_version"] = 2.0
    elif mutation == "bool_count":
        value["data"]["account_count"] = True
    else:
        value["data"]["movements"][0]["field_sources"]["private-company-file"] = {"amount_fen": 1}
    with pytest.raises(KernelError) as failure:
        validate_response("dashboard_funds", value)
    message = json.dumps(failure.value.response())
    assert failure.value.code == "response_contract_mismatch"
    assert "private-company-file" not in message and "secret" not in message
    assert "needs_information" not in message and "fact_issues" not in failure.value.response()


def test_schema_command_exposes_native_contracts(resident):
    service, _, _, _, _ = resident
    result = service.dispatch("schema", {})["response_schemas"]
    assert result == response_schemas()
    native = result["dashboard_funds"]["$defs"]["FundsData"]["properties"]["inflow_fen"]
    assert native["type"] == "integer" and native["maximum"] == 2**63 - 1
    wire = response_schemas(mode="serialization")["dashboard_funds"]["$defs"]["FundsData"][
        "properties"
    ]["inflow_fen"]
    assert wire["type"] == "string" and wire["x-fen-int64"] is True


def test_service_and_http_fail_closed_for_bad_reads(resident, monkeypatch):
    service, _, capability, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "合成错误响应公司")["id"]
    bad = {"schema_version": 2, "data": {"private-company-file": "secret"}}
    monkeypatch.setattr(Dashboard, "funds", lambda *args, **kwargs: bad)
    with pytest.raises(KernelError, match="读取结果") as failure:
        service.dispatch("dashboard_funds", {"company_id": company}, session_token=token)
    assert failure.value.code == "response_contract_mismatch"
    status, _, _, result = http.request(
        f"/api/dashboard/funds?company_id={company}", headers=headers
    )
    assert status == 500 and result["code"] == "response_contract_mismatch"
    assert "secret" not in json.dumps(result) and "private-company-file" not in json.dumps(result)
    status, _, _, result = http.request(
        "/api/command",
        {"command": "dashboard_funds", "payload": {"company_id": company}},
        headers={
            "X-Local-Capability": capability,
            "Authorization": "Bearer " + token.get_secret_value(),
        },
    )
    assert status == 500 and result["code"] == "response_contract_mismatch"


def test_invalid_numeric_mapping_keys_are_not_reported_as_list_positions(samples):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    value["data"]["accounts"][0]["field_sources"] = {6222021234567890123: {}}
    with pytest.raises(KernelError) as failure:
        validate_response("dashboard_funds", value)
    assert "6222021234567890123" not in json.dumps(failure.value.response())
    assert all(
        path.startswith("data.accounts.0.field_sources.<key>")
        for path in failure.value.details["paths"]
    )


def test_existing_report_and_material_diagnostics_remain_business_issues(samples):
    from ai_accounting.kernel.materials import _issue

    value = copy.deepcopy(samples["cash_funds"]["response"])
    position = classify_financial_position(
        [{"account": "1122", "amount": 100, "party_state": "unresolved"}]
    )
    transfer = BankPlatformTransfer.model_validate(
        {
            "period": "2026-01",
            "actual_date": "2026-01-03",
            "direction": "bank_to_platform",
            "bank_account_id": "bank",
            "platform_account_id": "platform",
            "amount_fen": 100,
            "movement_ids": ("movement",),
        }
    )
    with pytest.raises(KernelError) as failure:
        transfer.validate_material_amount("fact.amount_fen", 100, source_amounts=(100,))
    error = failure.value
    materials = [_issue(error.code, str(error), **error.details)]
    followups = value["data"]["period_preparation"]["current_followups"]
    followups["close_requirements"]["issues"] = position["issues"]
    followups["materials"]["issues"] = materials
    response = http_response("dashboard_funds", value)
    actual = response["data"]["period_preparation"]["current_followups"]
    assert actual["close_requirements"]["issues"] == position["issues"]
    assert actual["materials"]["issues"] == materials
    assert materials[0]["dimension"] == "bank" and position["issues"][0]["line_no"] is None


@pytest.mark.parametrize("part,key", [("closure", "digest"), ("frozen_readiness", "readiness")])
def test_frozen_branch_cannot_omit_its_required_basis(samples, part, key):
    value = copy.deepcopy(samples["frozen_funds"]["response"])
    del value["data"]["period_preparation"][part][key]
    with pytest.raises(KernelError) as failure:
        validate_response("dashboard_funds", value)
    assert failure.value.code == "response_contract_mismatch"


def test_migrated_http_bypasses_name_based_money_converter(resident, monkeypatch):
    import ai_accounting.kernel.http as transport

    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "合成HTTP公司")["id"]
    monkeypatch.setattr(
        transport, "wire_money", lambda value: pytest.fail("heuristic converter used")
    )
    for endpoint in ("context", "funds"):
        status, _, _, _ = http.request(
            f"/api/dashboard/{endpoint}?company_id={company}", headers=headers
        )
        assert status == 200


def test_today_backend_samples_pass_generated_browser_validators(samples, tmp_path):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is supplied in frontend CI and local frontend development")
    target = tmp_path / "samples.json"
    target.write_text(json.dumps(http_samples(samples)), encoding="utf-8")
    result = subprocess.run(
        [executable, str(ROOT / "frontend/scripts/check-contract-samples.mjs"), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_schema_check_detects_changed_model_without_writing(tmp_path, monkeypatch):
    script = ROOT / "scripts/export_dashboard_response_schemas.py"
    target = tmp_path / "schema.json"
    subprocess.run(
        [sys.executable, str(script), "--output", str(target)], check=True, capture_output=True
    )
    initial = target.read_bytes()
    module_spec = importlib.util.spec_from_file_location("export_response_contracts", script)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(script), "--check", "--output", str(target)])
    module.main()
    monkeypatch.setattr(
        module, "response_schemas", lambda **_: {"changed_model": {"type": "object"}}
    )
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 1
    assert target.read_bytes() == initial
