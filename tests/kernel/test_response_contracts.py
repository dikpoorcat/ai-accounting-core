"""Strict read responses, exact HTTP money, live consumers and read-only generation."""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

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
        assert json.loads(RESPONSE_ADAPTERS[command].dump_json(response)) == http_response(
            command, response
        )
    empty = samples["empty_context"]["response"]
    assert "generated_at" not in empty and empty["current_company"] is None
    assert samples["company_without_period"]["response"]["periods"] == []
    assert samples["funds_without_period"]["response"]["data"] is None
    assert samples["deferred_funds"]["response"]["data"]["period_preparation"] is None
    assert "movement_page" not in samples["page_accounts"]["response"]["data"]
    assert "page" not in samples["page_accounts"]["response"]["data"]["bank_statement"]
    brief = samples["brief"]["response"]["data"]
    assert brief["adopted_basis"]["scope"] == "current_voucher_page"
    assert brief["adopted_basis"]["calculation_ids"] == [
        item["calculation_id"] for item in brief["collections"]["vouchers"]["items"]
    ]


def _personnel_date_value(response, sample):
    if sample == "business_month_dates":
        return response["data"]["display_profiles"]["business"]["values"]
    return response["data"]["collections"]["employees"]["items"][0]


@pytest.mark.parametrize("sample,field", [
    ("employees_month_dates", "employment_start_date"),
    ("employees_month_dates", "employment_end_date"),
    ("employees_month_dates", "tax_withholding_start_date"),
    ("business_month_dates", "employment_start"),
    ("business_month_dates", "employment_end"),
])
@pytest.mark.parametrize(
    "date_value", ["2026-03", "2026-03-20", None, "2026-13", "2026-00", 202603, True]
)
def test_personnel_date_contract_preserves_supported_precision(samples, sample, field, date_value):
    command = samples[sample]["command"]
    response = copy.deepcopy(samples[sample]["response"])
    _personnel_date_value(response, sample)[field] = date_value
    if date_value in ("2026-03", "2026-03-20", None):
        native = validate_response(command, response)
        assert _personnel_date_value(native, sample)[field] == date_value
        assert _personnel_date_value(http_response(command, response), sample)[field] == date_value
    else:
        for validate in (validate_response, http_response):
            with pytest.raises(KernelError) as failure:
                validate(command, response)
            assert failure.value.code == "response_contract_mismatch"


def test_live_personnel_dates_keep_frozen_and_supplemental_precision(samples):
    monthly = _personnel_date_value(
        samples["employees_month_dates"]["response"], "employees_month_dates"
    )
    mixed = _personnel_date_value(
        samples["employees_mixed_dates"]["response"], "employees_mixed_dates"
    )
    assert monthly["employment_start_date"] == "2025-12"
    assert monthly["tax_withholding_start_date"] == "2026-01"
    assert monthly["employment_end_date"] is None
    assert mixed["employment_start_date"] == "2025-12"
    assert mixed["employment_end_date"] == "2026-04-20"
    assert mixed["field_sources"]["employment_start_date"]["basis"] == "frozen"
    assert mixed["field_sources"]["employment_end_date"]["basis"] == "current_supplement"
    conflict = _personnel_date_value(
        samples["employees_date_conflict"]["response"], "employees_date_conflict"
    )
    assert conflict["in_period"] is None
    assert conflict["field_conflicts"] == [{
        "code": "employment_interval_conflict",
        "fields": ["employment_start", "employment_end"],
    }]


@pytest.mark.parametrize("conflict", [
    {"code": "unknown", "fields": ["employment_start", "employment_end"]},
    {"code": "employment_interval_conflict", "fields": ["private-field"]},
    {"fields": ["employment_start", "employment_end"]},
    {"code": "employment_interval_conflict"},
    {"field": "employment_start", "values": [], "sources": []},
])
def test_employee_interval_conflict_rejects_unsupported_shapes(samples, conflict):
    response = copy.deepcopy(samples["employees_date_conflict"]["response"])
    _personnel_date_value(response, "employees_date_conflict")["field_conflicts"] = [conflict]
    for validate in (validate_response, http_response):
        with pytest.raises(KernelError) as failure:
            validate("dashboard_employees", response)
        assert failure.value.code == "response_contract_mismatch"


def test_labor_source_uses_named_person_without_duplicate_party(samples):
    response = samples["employees_labor_sources"]["response"]
    item = response["data"]["collections"]["labor_sources"]["items"][0]
    assert item["name"] == "合成劳务人员"
    assert "party" not in item
    assert item["field_sources"]["name"]["field"] == "display_name"
    wire = http_response("dashboard_employees", response)
    encoded = wire["data"]["collections"]["labor_sources"]["items"][0]
    assert encoded["name"] == item["name"] and encoded["gross_fen"] == str(item["gross_fen"])
    for value in (None, 123):
        malformed = copy.deepcopy(response)
        malformed["data"]["collections"]["labor_sources"]["items"][0]["name"] = value
        for validate in (validate_response, http_response):
            with pytest.raises(KernelError) as failure:
                validate("dashboard_employees", malformed)
            assert failure.value.code == "response_contract_mismatch"


def test_workflow_preserves_actual_event_without_claiming_a_review(samples):
    before = samples["actual_tax_unreviewed"]["response"]
    after = samples["actual_tax_reviewed"]["response"]
    old = before["sections"]["external"]["obligations"][0]
    new = after["sections"]["external"]["obligations"][0]
    assert old["actual_completion_status"] == new["actual_completion_status"] == "completed"
    assert old["basis_review_status"] == "not_reviewed"
    assert new["basis_review_status"] == "reviewed"
    assert old["recorded_completions"] == new["recorded_completions"]
    bad = copy.deepcopy(before)
    bad["sections"]["external"]["obligations"][0]["basis_review_status"] = "all_done"
    with pytest.raises(KernelError, match="读取结果不符合接口合同"):
        validate_response("workflow", bad)


@pytest.mark.parametrize(
    "command,sample",
    [
        ("workflow", "open_workflow"),
        ("period_readiness", "open_readiness"),
    ],
)
def test_native_work_contracts_enforce_nested_money(samples, command, sample):
    value = copy.deepcopy(samples[sample]["response"])
    summary = (
        value["sections"]["external"]["settlements"]
        if command == "workflow"
        else value["current_followups"]["settlements"]
    )
    for amount in (None, 0, -(2**63), 2**63 - 1):
        summary["remaining_fen"] = amount
        native = validate_response(command, value)
        wire = http_response(command, native)
        encoded = (
            wire["sections"]["external"]["settlements"]
            if command == "workflow"
            else wire["current_followups"]["settlements"]
        )
        assert encoded["remaining_fen"] == (None if amount is None else str(amount))
        assert type(encoded["obligation_count"]) is int
    for amount in (True, 1.0, "1", 2**63, -(2**63) - 1):
        summary["remaining_fen"] = amount
        with pytest.raises(KernelError) as error:
            validate_response(command, value)
        assert error.value.code == "response_contract_mismatch"
        assert all("remaining_fen" in path for path in error.value.details["paths"])


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
    assert wire["data"]["collections"]["movements"]["items"][0]["amount_fen"] == str(
        value["data"]["collections"]["movements"]["items"][0]["amount_fen"]
    )


def test_material_category_and_actual_money_candidate_diagnostics_are_typed(samples):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    issue = {
        "field": "materials.transactions",
        "message": "原件需要归属核对",
        "category": "transactions",
        "code": "material_allocation_required",
        "origin_periods": ["2026-01"],
        "review_period": "2026-09",
        "responsibility": "closed_followup",
        "signals": [
            {
                "code": "same_actual_money_coordinates",
                "matched_fields": ["account_id", "actual_date", "amount_fen"],
                "distinct_locations_proven": False,
            }
        ],
    }
    value["data"]["period_preparation"]["readiness"]["issues"] = [issue]
    assert validate_response("dashboard_funds", value) == value
    assert http_response("dashboard_funds", value)["data"]["period_preparation"]["readiness"][
        "issues"
    ] == [issue]
    issue["unexpected_amount_fen"] = 1
    with pytest.raises(KernelError) as error:
        validate_response("dashboard_funds", value)
    assert error.value.code == "response_contract_mismatch"


@pytest.mark.parametrize("amount", [0, 2**53 + 1, 2**63 - 1])
def test_tax_file_followup_has_explicit_money_and_never_changes_close_readiness(samples, amount):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    preparation = value["data"]["period_preparation"]
    readiness = copy.deepcopy(preparation["readiness"])
    preparation["current_followups"]["tax_import_mapping"] = {
        "status": "unsupported",
        "blocking_scope": "tax_import_file",
        "mapping_fact_ids": [],
        "calculation_ids": ["synthetic-wage-result"],
        "issues": [
            {
                "code": "tax_import_format_unsupported",
                "category": "capability",
                "field": "tax_import_mapping",
                "message": "个税文件列无法容纳已发布的个人扣款项目",
                "component_codes": ["a", "b", "c", "d"],
                "amount_fen": amount,
            }
        ],
    }
    assert validate_response("dashboard_funds", value) == value
    wire = http_response("dashboard_funds", value)
    assert wire["data"]["period_preparation"]["current_followups"]["tax_import_mapping"]["issues"][
        0
    ]["amount_fen"] == str(amount)
    assert preparation["readiness"] == readiness
    for invalid in (True, 1.0, 2**63, "1"):
        preparation["current_followups"]["tax_import_mapping"]["issues"][0]["amount_fen"] = invalid
        with pytest.raises(KernelError, match="读取结果不符合接口合同"):
            validate_response("dashboard_funds", value)


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
        value["schema_version"] = 2
    elif mutation == "float_version":
        value["schema_version"] = 3.0
    elif mutation == "bool_count":
        value["data"]["account_count"] = True
    else:
        value["data"]["collections"]["movements"]["items"][0]["field_sources"][
            "private-company-file"
        ] = {"amount_fen": 1}
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
    bad = {"schema_version": 3, "data": {"private-company-file": "secret"}}
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


def test_dashboard_http_dispatch_validates_once_and_keeps_native_money(
    resident, samples, monkeypatch
):
    import ai_accounting.kernel.response_contracts as contracts

    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "合成响应边界公司")["id"]
    original = contracts.validate_response
    checked = []

    def counted(command, value):
        checked.append(command)
        return original(command, value)

    monkeypatch.setattr(contracts, "validate_response", counted)
    native = service.dispatch("dashboard_funds", {"company_id": company}, session_token=token)
    assert type(native["schema_version"]) is int
    checked.clear()
    adapter = RESPONSE_ADAPTERS["dashboard_funds"]
    original_json = type(adapter).dump_json
    original_python = type(adapter).dump_python
    serialized = {"json": 0, "python": 0}

    def counted_json(self, *args, **kwargs):
        if self is adapter:
            serialized["json"] += 1
        return original_json(self, *args, **kwargs)

    def counted_python(self, *args, **kwargs):
        if self is adapter:
            serialized["python"] += 1
        return original_python(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(adapter), "dump_json", counted_json)
        patch.setattr(type(adapter), "dump_python", counted_python)
        status, _, _, response = http.request(
            f"/api/dashboard/funds?company_id={company}", headers=headers
        )
    assert status == 200
    assert checked == ["dashboard_funds"]
    assert serialized == {"json": 1, "python": 0}
    assert response["data"] is None
    with pytest.raises(KernelError) as failure:
        service.dispatch(
            "workflow", {"company_id": company}, session_token=token, response_format="http"
        )
    assert failure.value.code == "invalid_command"
    assert checked == ["dashboard_funds"]
    for amount in (None, 2**63 - 1):
        synthetic = copy.deepcopy(samples["cash_funds"]["response"])
        synthetic["data"]["total_fen"] = amount
        monkeypatch.setattr(Dashboard, "funds", lambda *args, result=synthetic, **kwargs: result)
        native = service.dispatch("dashboard_funds", {"company_id": company}, session_token=token)
        wire = service.dispatch(
            "dashboard_funds",
            {"company_id": company},
            session_token=token,
            response_format="http",
        )
        wire_json = service.dispatch(
            "dashboard_funds",
            {"company_id": company},
            session_token=token,
            response_format="http_json",
        )
        assert json.loads(wire_json) == wire
        assert native["data"]["total_fen"] == amount
        assert wire["data"]["total_fen"] == (None if amount is None else str(amount))


@pytest.mark.parametrize(
    "action,sample,extra",
    [
        ("context", "company_with_period", {}),
        ("brief", "brief", {"period": "2026-01"}),
        ("funds", "cash_funds", {"period": "2026-01"}),
        ("employees", "employees", {"period": "2026-01"}),
        ("assets", "assets", {"period": "2026-01"}),
        ("business-status", "business_status", {"period": "2026-01", "subject_id": "sample"}),
        ("quarterly-report", "quarterly_report", {"year": 2026, "quarter": 1}),
        ("period-preparation", "period_preparation", {
            "period": "2026-01", "expected_read_version": "sample", "as_of": "2026-02-25",
        }),
        ("close-review", None, {"period": "2026-01"}),
    ],
)
def test_every_dashboard_http_route_checks_once_and_rejects_bad_payload(
    resident, samples, monkeypatch, action, sample, extra,
):
    """Observe the common HTTP boundary with current kernel-generated responses."""
    import ai_accounting.kernel.response_contracts as contracts

    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "合成全页合同公司")["id"]
    command = "dashboard_" + action.replace("-", "_")
    payload = {"company_id": company, **extra}
    value = (
        copy.deepcopy(samples[sample]["response"])
        if sample else service.dispatch(command, payload, session_token=token)
    )
    original_validate = contracts.validate_response
    adapter_type = type(RESPONSE_ADAPTERS[command])
    original_dump = adapter_type.dump_json
    observed = []

    def read(actual_command, actual_payload, **kwargs):
        assert actual_command == command
        assert {key: actual_payload[key] for key in payload} == payload
        assert kwargs["authority"] is not None
        return value

    def checked(actual_command, actual_value):
        observed.append(("validate", actual_command))
        return original_validate(actual_command, actual_value)

    def encoded(self, *args, **kwargs):
        if self is RESPONSE_ADAPTERS[command]:
            observed.append(("encode", command))
        return original_dump(self, *args, **kwargs)

    monkeypatch.setattr(service, "_dispatch", read)
    monkeypatch.setattr(contracts, "validate_response", checked)
    monkeypatch.setattr(adapter_type, "dump_json", encoded)
    native = service.dispatch(command, payload, session_token=token)
    assert native == value and observed == [("validate", command)]
    expected_wire = json.loads(original_dump(RESPONSE_ADAPTERS[command], native))
    observed.clear()
    status, _, _, result = http.request(
        f"/api/dashboard/{action}?{urlencode(payload)}", headers=headers,
    )
    assert status == 200 and result == expected_wire
    assert observed == [("validate", command), ("encode", command)]

    # An invalid response must never reach serialization or become a business question.
    value["schema_version"] = 999
    value["private-data"] = "do-not-disclose"
    observed.clear()
    status, _, _, result = http.request(
        f"/api/dashboard/{action}?{urlencode(payload)}", headers=headers,
    )
    assert status == 500 and result["code"] == "response_contract_mismatch"
    assert "do-not-disclose" not in json.dumps(result)
    assert "fact_issues" not in result
    assert observed == [("validate", command)]


def test_invalid_numeric_mapping_keys_are_not_reported_as_list_positions(samples):
    value = copy.deepcopy(samples["cash_funds"]["response"])
    value["data"]["collections"]["accounts"]["items"][0]["field_sources"] = {
        6222021234567890123: {}
    }
    with pytest.raises(KernelError) as failure:
        validate_response("dashboard_funds", value)
    assert "6222021234567890123" not in json.dumps(failure.value.response())
    assert all(
        path.startswith("data.collections.accounts.items.0.field_sources.<key>")
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
