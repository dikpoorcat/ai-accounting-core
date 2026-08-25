from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.exc import IntegrityError, OperationalError

from ai_accounting import mcp_server
from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.mcp_server import mcp
from ai_accounting.schemas import RecordEventRequest, RegisterEmployeeRequest


def _tool_schema(tool_name: str) -> dict[str, Any]:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    return tools[tool_name].model_dump(by_alias=True)["inputSchema"]


def _definition(schema: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    ref = reference["$ref"]
    assert ref.startswith("#/$defs/")
    return schema["$defs"][ref.removeprefix("#/$defs/")]


def test_payroll_mcp_list_tools_exposes_strict_typed_schemas() -> None:
    payroll_tools = {
        "finance_register_employee",
        "finance_register_employee_profile_version",
        "finance_register_payroll_policy_version",
        "finance_register_payroll_opening_state",
        "finance_preview_payroll",
        "finance_confirm_payroll",
        "finance_get_payroll_batch",
    }
    schemas = {tool_name: _tool_schema(tool_name) for tool_name in payroll_tools}

    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["finance_get_payroll_batch"]["required"] == ["org_id", "batch_id"]

    preview = schemas["finance_preview_payroll"]
    preview_request = _definition(preview, preview["properties"]["request"])
    assert preview_request["additionalProperties"] is False
    assert {
        "org_id",
        "idempotency_key",
        "batch_kind",
        "payroll_period",
        "posting_date",
        "payment_date",
        "employee_items",
    } <= set(preview_request["required"])

    batch_kind = _definition(preview, preview_request["properties"]["batch_kind"])
    assert set(batch_kind["enum"]) == {"regular", "annual_bonus"}
    employee_items = _definition(preview, preview_request["properties"]["employee_items"]["items"])
    assert employee_items["additionalProperties"] is False
    assert employee_items["properties"]["tax_reported_salary_fen"]["anyOf"][0]["type"] == (
        "integer"
    )
    for removed in (
        "base_salary_fen",
        "performance_pay_fen",
        "taxable_allowance_fen",
        "tax_exempt_income_fen",
        "attendance_deduction_fen",
    ):
        assert removed not in employee_items["properties"]

    schema_text = json.dumps(schemas, ensure_ascii=False)
    assert "debit_fen" not in schema_text
    assert "credit_fen" not in schema_text
    assert "account_code" not in schema_text


def test_payroll_mcp_rejects_extra_float_and_missing_arguments() -> None:
    valid_preview = {
        "org_id": "00000000-0000-0000-0000-000000000000",
        "idempotency_key": "strict-payroll-preview",
        "batch_kind": "regular",
        "payroll_period": "2026-08",
        "posting_date": "2026-08-31",
        "payment_date": "2026-08-31",
        "employee_items": [{"employee_id": "00000000-0000-0000-0000-000000000001"}],
    }

    async def call(arguments: dict[str, Any]) -> None:
        await mcp.call_tool("finance_preview_payroll", arguments)

    with pytest.raises(ToolError):
        asyncio.run(call({"request": valid_preview, "unrecognized": "must-fail"}))
    with pytest.raises(ToolError):
        asyncio.run(
            call(
                {
                    "request": {
                        **valid_preview,
                        "employee_items": [
                            {
                                "employee_id": "00000000-0000-0000-0000-000000000001",
                                "tax_reported_salary_fen": 12.5,
                            }
                        ],
                    }
                }
            )
        )
    with pytest.raises(ToolError):
        asyncio.run(call({"request": valid_preview}))
    with pytest.raises(ToolError):
        asyncio.run(
            call(
                {
                    "request": {
                        **valid_preview,
                        "employee_items": [
                            {
                                "employee_id": "00000000-0000-0000-0000-000000000001",
                                "tax_reported_salary_fen": 100,
                                "base_salary_fen": 100,
                            }
                        ],
                    }
                }
            )
        )
    with pytest.raises(ToolError):
        asyncio.run(call({"request": {"org_id": valid_preview["org_id"]}}))


def test_r7_003_evidence_and_bank_import_contracts_are_typed_and_strict() -> None:
    """MCP must publish the real request contracts, not untyped dictionaries."""

    evidence_schema = _tool_schema("finance_register_evidence")
    bank_schema = _tool_schema("finance_import_bank_statement")
    evidence_request = _definition(evidence_schema, evidence_schema["properties"]["request"])
    bank_request = _definition(bank_schema, bank_schema["properties"]["request"])
    bank_mapping = _definition(bank_schema, bank_request["properties"]["column_mapping"])

    assert evidence_schema["additionalProperties"] is False
    assert bank_schema["additionalProperties"] is False
    assert evidence_request["additionalProperties"] is False
    assert bank_request["additionalProperties"] is False
    assert {"org_id", "source"} == set(evidence_request["required"])
    assert {"org_id", "file_path", "column_mapping"} == set(bank_request["required"])
    assert evidence_request["properties"]["org_id"]["format"] == "uuid"
    assert bank_request["properties"]["file_path"]["format"] == "path"
    assert "oneOf" not in evidence_request
    assert {
        "org_id",
        "source",
        "file_path",
        "content_base64",
        "original_name",
        "media_type",
        "metadata",
    } == set(evidence_request["properties"])
    assert "必须且只能提供一个" in evidence_request["properties"]["file_path"]["description"]
    assert "必须且只能提供一个" in evidence_request["properties"]["content_base64"]["description"]
    assert bank_mapping["additionalProperties"] is False
    assert set(bank_mapping["properties"]) == {
        "booking_date",
        "amount",
        "debit",
        "credit",
        "counterparty",
        "memo",
        "external_id",
        "currency",
    }
    assert bank_mapping["required"] == ["booking_date"]
    assert bank_mapping["allOf"][0]["anyOf"] == [
        {
            "required": ["amount"],
            "properties": {"amount": {"type": "string", "minLength": 1}},
        },
        {
            "required": ["debit", "credit"],
            "properties": {
                "debit": {"type": "string", "minLength": 1},
                "credit": {"type": "string", "minLength": 1},
            },
        },
    ]

    invalid_calls = (
        (
            "finance_register_evidence",
            {
                "request": {
                    "org_id": "00000000-0000-0000-0000-000000000000",
                    "source": "r7-contract",
                    "content_base64": "YQ==",
                    "undeclared": "sensitive-value-must-not-leak",
                }
            },
        ),
        (
            "finance_import_bank_statement",
            {
                "request": {
                    "org_id": "00000000-0000-0000-0000-000000000000",
                    "file_path": "C:/not-used.csv",
                    "column_mapping": {"booking_date": "date"},
                    "undeclared": "sensitive-value-must-not-leak",
                }
            },
        ),
        (
            "finance_register_evidence",
            {
                "request": {
                    "org_id": "00000000-0000-0000-0000-000000000000",
                    "source": "r7-contract",
                    "file_path": "C:/not-used.txt",
                    "content_base64": "YQ==",
                }
            },
        ),
        (
            "finance_import_bank_statement",
            {
                "request": {
                    "org_id": "00000000-0000-0000-0000-000000000000",
                    "file_path": "C:/not-used.csv",
                    "column_mapping": {
                        "booking_date": "date",
                        "amount": "amount",
                        "not_canonical": "sentinel",
                    },
                }
            },
        ),
    )

    async def call(name: str, arguments: dict[str, Any]) -> None:
        await mcp.call_tool(name, arguments)

    for name, arguments in invalid_calls:
        with pytest.raises(ToolError) as error:
            asyncio.run(call(name, arguments))
        assert "VALIDATION_ERROR" in str(error.value)
        assert "sensitive-value-must-not-leak" not in str(error.value)


def test_r2_007_record_reverse_and_policy_contracts_are_real_and_strict() -> None:
    record_schema = _tool_schema("finance_record_event")
    reverse_schema = _tool_schema("finance_reverse_event")
    policy_schema = _tool_schema("finance_register_payroll_policy_version")
    record_request = _definition(record_schema, record_schema["properties"]["request"])
    reverse_request = _definition(reverse_schema, reverse_schema["properties"]["request"])
    policy_request = _definition(policy_schema, policy_schema["properties"]["request"])
    details = _definition(record_schema, record_request["properties"]["details"])
    parameters = _definition(policy_schema, policy_request["properties"]["parameters"])
    income_tax = _definition(policy_schema, parameters["properties"]["income_tax"])
    annual_bonus = _definition(policy_schema, parameters["properties"]["annual_bonus"]["anyOf"][0])

    assert record_schema["additionalProperties"] is False
    assert reverse_schema["additionalProperties"] is False
    assert record_request["additionalProperties"] is False
    assert reverse_request["additionalProperties"] is False
    assert details["additionalProperties"] is False
    assert parameters["additionalProperties"] is False
    assert {"org_id", "idempotency_key", "event_type", "business_dates", "amounts"} <= set(
        record_request["required"]
    )
    assert {"org_id", "event_id", "idempotency_key", "reason", "posting_date"} == set(
        reverse_request["required"]
    )
    assert {"contribution_rules", "income_tax", "payment_targets"} <= set(parameters["required"])
    assert "effective_from" in income_tax["required"]
    assert "effective_from" in annual_bonus["required"]
    assert mcp_server.finance_get_event_schema()["record_event_schema"] == record_schema

    invalid_float_amount = {
        "request": {
            "org_id": "00000000-0000-0000-0000-000000000000",
            "idempotency_key": "r2-record-strict-float",
            "event_type": "expense_cash",
            "business_dates": {
                "business_date": "2026-07-05",
                "posting_date": "2026-07-05",
                "payment_date": "2026-07-05",
            },
            "amounts": {"amount_fen": 12.0},
        }
    }
    invalid_freeform_detail = {
        "request": {
            **invalid_float_amount["request"],
            "idempotency_key": "r2-record-strict-detail",
            "amounts": {"amount_fen": 12},
            "details": {"untyped_client_payload": "not accepted"},
        }
    }

    async def call(name: str, arguments: dict[str, Any]) -> None:
        await mcp.call_tool(name, arguments)

    with pytest.raises(ToolError):
        asyncio.run(call("finance_record_event", invalid_float_amount))
    with pytest.raises(ToolError):
        asyncio.run(call("finance_record_event", invalid_freeform_detail))
    with pytest.raises(ToolError):
        asyncio.run(call("finance_reverse_event", {"request": {"org_id": "bad"}}))


def test_pay_019_payroll_capability_is_discoverable_but_not_a_free_event() -> None:
    capability = mcp_server.finance_get_event_schema("payroll")

    assert capability["status"] == "ok"
    assert "payroll" not in capability["disabled_event_types"]
    assert "payroll" in capability["internal_event_types"]
    assert capability["module_capabilities"]["payroll"] == {
        "status": "enabled",
        "entry_tools": [
            "finance_register_employee",
            "finance_register_employee_profile_version",
            "finance_register_payroll_policy_version",
            "finance_register_payroll_opening_state",
            "finance_preview_payroll",
            "finance_confirm_payroll",
            "finance_get_payroll_batch",
        ],
        "generic_event_writer": "not_available",
        "accrual_entry": "finance_confirm_payroll",
    }

    request = {
        "org_id": "00000000-0000-0000-0000-000000000000",
        "idempotency_key": "must-not-post-payroll-directly",
        "event_type": "payroll",
        "business_dates": {"business_date": "2026-07-05", "posting_date": "2026-07-05"},
        "amounts": {"amount_fen": 1},
    }
    assert mcp_server.finance_record_event(RecordEventRequest.model_validate(request)) == {
        "status": "rejected",
        "errors": ["PAYROLL_REQUIRES_SPECIALIZED_WORKFLOW"],
    }


def _stdio_payroll_policy_parameters() -> dict[str, object]:
    return {
        "contribution_rules": [
            {
                "code": "pension",
                "base_kind": "social_insurance",
                "employee_rate": "0.08",
                "employer_rate": "0.16",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
            {
                "code": "housing_fund",
                "base_kind": "housing_fund",
                "employee_rate": "0.07",
                "employer_rate": "0.07",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
        ],
        "income_tax": {
            "version": "stdio-income-tax-2026",
            "primary_source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            "legal_basis_source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "monthly_standard_deduction_fen": 500_000,
            "brackets": [
                {"upper_bound_fen": 3_600_000, "rate": "0.03", "quick_deduction_fen": 0},
                {
                    "upper_bound_fen": None,
                    "rate": "0.45",
                    "quick_deduction_fen": 18_192_000,
                },
            ],
        },
        "annual_bonus": {
            "version": "stdio-annual-bonus-2026",
            "primary_source_url": "https://m.mof.gov.cn/czxw/202308/t20230828_3904328.htm",
            "effective_from": "2023-01-01",
            "effective_to": "2027-12-31",
            "brackets": [
                {
                    "upper_monthly_average_fen": 3_000_000,
                    "rate": "0.03",
                    "quick_deduction_fen": 0,
                },
                {
                    "upper_monthly_average_fen": None,
                    "rate": "0.45",
                    "quick_deduction_fen": 18_192_000,
                },
            ],
        },
        "payment_targets": {
            "social_insurance": {"agency_code": "SOCIAL-01", "agency_name": "社保局"},
            "housing_fund": {"agency_code": "HOUSING-01", "agency_name": "公积金中心"},
            "individual_income_tax": {"agency_code": "TAX-01", "agency_name": "税务局"},
        },
    }


def test_pay_020_stdio_payroll_register_preview_confirm_uses_isolated_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stdio-payroll.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as database_session:
        organization = seed_organization(
            database_session,
            taxpayer_identification_number="91330106MA1234567T",
            name="工资 STDIO 回归企业",
        )
        organization.accounting_period_control_enabled = False
        database_session.flush()
        org_id = str(organization.id)
    engine.dispose()

    async def run() -> dict[str, dict[str, Any]]:
        environment = os.environ.copy()
        source_directory = str(Path(__file__).parents[1] / "src")
        virtualenv_site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        environment["DATABASE_URL"] = database_url
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    source_directory,
                    str(virtualenv_site_packages),
                    str(virtualenv_site_packages / "win32"),
                    str(virtualenv_site_packages / "win32" / "lib"),
                    str(virtualenv_site_packages / "pywin32_system32"),
                    environment.get("PYTHONPATH"),
                ],
            )
        )
        parameters = StdioServerParameters(
            # The virtualenv launcher on Windows starts a second interpreter
            # process.  Launch the base interpreter directly so stdio_client
            # owns the real server process and can terminate it reliably.
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-m", "ai_accounting.mcp_server"],
            cwd=Path(__file__).parents[1],
            env=environment,
        )

        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    response = await client.call_tool(name, arguments)
                    assert response.isError is False
                    assert len(response.content) == 1
                    return json.loads(response.content[0].text)

                employee = await call(
                    "finance_register_employee",
                    {
                        "request": {
                            "org_id": org_id,
                            "employee_code": "STDIO-E-001",
                            "name": "工资回归员工",
                            "employment_start_date": "2026-07-01",
                            "tax_withholding_start_date": "2026-07-01",
                            "status": "active",
                        }
                    },
                )
                assert employee["status"] == "registered"
                employee_id = employee["employee_id"]

                profile = await call(
                    "finance_register_employee_profile_version",
                    {
                        "request": {
                            "org_id": org_id,
                            "employee_id": employee_id,
                            "effective_from": "2026-07-01",
                            "expense_role": "payroll_management_expense",
                            "social_insurance_base_fen": 1_000_000,
                            "housing_fund_base_fen": 1_000_000,
                            "resident_employee": True,
                        }
                    },
                )
                assert profile["status"] == "registered"

                policy = await call(
                    "finance_register_payroll_policy_version",
                    {
                        "request": {
                            "org_id": org_id,
                            "region": "STDIO 测试地区",
                            "effective_from": "2026-01-01",
                            "effective_to": "2026-12-31",
                            "version": "stdio-2026",
                            "source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
                            "parameters": _stdio_payroll_policy_parameters(),
                        }
                    },
                )
                assert policy["status"] == "registered"

                preview = await call(
                    "finance_preview_payroll",
                    {
                        "request": {
                            "org_id": org_id,
                            "idempotency_key": "stdio-payroll-preview",
                            "batch_kind": "regular",
                            "payroll_period": "2026-07",
                            "posting_date": "2026-07-05",
                            "payment_date": "2026-07-05",
                            "employee_items": [
                                {
                                    "employee_id": employee_id,
                                    "tax_reported_salary_fen": 1_000_000,
                                    "special_additional_deduction_fen": 0,
                                    "other_legal_deduction_fen": 0,
                                }
                            ],
                        }
                    },
                )
                assert preview["status"] == "calculated"

                confirmed = await call(
                    "finance_confirm_payroll",
                    {
                        "request": {
                            "org_id": org_id,
                            "batch_id": preview["batch_id"],
                            "calculation_hash": preview["calculation_hash"],
                            "idempotency_key": "stdio-payroll-confirm",
                        }
                    },
                )
                assert confirmed["status"] == "posted"
                lifecycle = await call(
                    "finance_get_payroll_batch",
                    {"org_id": org_id, "batch_id": preview["batch_id"]},
                )
                return {"preview": preview, "confirmed": confirmed, "lifecycle": lifecycle}

    try:
        result = asyncio.run(run())
    finally:
        engine.dispose()

    assert result["confirmed"]["event_id"] is not None
    assert result["confirmed"]["voucher_id"] is not None
    assert result["lifecycle"]["status"] == "posted"
    assert result["lifecycle"]["lifecycle"]["calculation"]["request_payload_hash"]


class _DatabaseOriginal(Exception):
    def __init__(self, *, sqlstate: str) -> None:
        self.sqlstate = sqlstate


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_: object) -> bool:
        return False


class _SessionFactory:
    def begin(self) -> _SessionContext:
        return _SessionContext()


@pytest.mark.parametrize(
    ("database_error", "expected_code"),
    [
        (
            IntegrityError(
                "INSERT INTO employees (employee_code) VALUES (:employee_code)",
                {"employee_code": "secret-employee-code"},
                _DatabaseOriginal(sqlstate="23505"),
            ),
            "UNIQUE_CONFLICT",
        ),
        (
            OperationalError(
                "SELECT * FROM payroll_batches WHERE org_id = :org_id",
                {"org_id": "postgresql://sensitive-user:password@db/internal"},
                _DatabaseOriginal(sqlstate="40001"),
            ),
            "CONCURRENCY_CONFLICT",
        ),
        (
            IntegrityError(
                "INSERT INTO payroll_lines (employee_id) VALUES (:employee_id)",
                {"employee_id": "sensitive-foreign-key"},
                _DatabaseOriginal(sqlstate="23503"),
            ),
            "CONSTRAINT_VIOLATION",
        ),
    ],
)
def test_payroll_mcp_database_errors_are_stable_and_do_not_leak_details(
    database_error: Exception,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingFinanceService:
        def __init__(self, _: object) -> None:
            pass

        def register_employee(self, _: RegisterEmployeeRequest) -> dict[str, Any]:
            raise database_error

    monkeypatch.setattr(mcp_server, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(mcp_server, "FinanceService", FailingFinanceService)
    request = RegisterEmployeeRequest.model_validate(
        {
            "org_id": "00000000-0000-0000-0000-000000000000",
            "employee_code": "E-001",
            "name": "测试员工",
            "employment_start_date": "2026-01-01",
        }
    )

    response = mcp_server.finance_register_employee(request)

    assert response == {"status": "rejected", "errors": [expected_code]}
    response_text = json.dumps(response, ensure_ascii=False)
    for forbidden in (
        "INSERT",
        "SELECT",
        "employee_code",
        "secret-employee-code",
        "sensitive-foreign-key",
        "postgresql://",
        "password",
    ):
        assert forbidden not in response_text
