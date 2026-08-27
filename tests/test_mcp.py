from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from ai_accounting import mcp_server
from ai_accounting.agent_contract import (
    AI_OPERATING_PROTOCOL_VERSION,
    EVIDENCE_FIRST_RUNTIME_INSTRUCTION,
)
from ai_accounting.mcp_server import mcp


def test_mcp_exposes_only_domain_tools() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == {
        "finance_get_profile",
        "finance_get_event_schema",
        "finance_register_evidence",
        "finance_import_bank_statement",
        "finance_preview_bank_statement_import",
        "finance_confirm_bank_statement_import",
        "finance_preview_bank_reconciliation_scope",
        "finance_confirm_bank_reconciliation_scope",
        "finance_preview_late_bank_evidence",
        "finance_confirm_late_bank_evidence",
        "finance_preview_bank_reconciliation",
        "finance_confirm_bank_reconciliation",
        "finance_query_bank_statement_state",
        "finance_register_employee",
        "finance_register_employee_profile_version",
        "finance_register_payroll_policy_version",
        "finance_register_payroll_opening_state",
        "finance_register_payroll_first_wage_tax_treatment",
        "finance_register_payroll_contribution_actual",
        "finance_record_payroll_contribution_supplement",
        "finance_preview_payroll",
        "finance_confirm_payroll",
        "finance_get_payroll_batch",
        "finance_register_labor_service_person",
        "finance_end_labor_service_person",
        "finance_preview_labor_remuneration_batch",
        "finance_confirm_labor_remuneration_batch",
        "finance_get_labor_remuneration",
        "finance_preview_unified_payout_run",
        "finance_confirm_unified_payout_run",
        "finance_pay_labor_withholding_tax",
        "finance_confirm_labor_external_declaration",
        "finance_acquire_fixed_asset",
        "finance_activate_fixed_asset",
        "finance_preview_fixed_asset_depreciation",
        "finance_confirm_fixed_asset_depreciation",
        "finance_preview_fixed_asset_depreciation_batch",
        "finance_confirm_fixed_asset_depreciation_batch",
        "finance_dispose_fixed_asset",
        "finance_get_fixed_asset",
        "finance_acquire_intangible_asset",
        "finance_preview_intangible_asset_amortization",
        "finance_confirm_intangible_asset_amortization",
        "finance_retire_intangible_asset",
        "finance_get_intangible_asset",
        "finance_draw_borrowing",
        "finance_preview_borrowing_interest",
        "finance_confirm_borrowing_interest",
        "finance_pay_borrowing_interest",
        "finance_repay_borrowing_principal",
        "finance_get_borrowing",
        "finance_generate_accounting_period",
        "finance_preview_accounting_period_close",
        "finance_confirm_accounting_period_close",
        "finance_get_accounting_periods",
        "finance_preview_quarterly_financial_statements",
        "finance_get_financial_statement_requirements",
        "finance_confirm_financial_statement_classification",
        "finance_confirm_enterprise_income_tax_quarter",
        "finance_query_context",
        "finance_record_event",
        "finance_calculate_tax_period",
        "finance_confirm_tax_period",
        "finance_reverse_event",
        "finance_get_event",
    }
    assert not any("journal_line" in name or "sql" in name for name in names)


def test_every_registered_tool_publishes_a_closed_top_level_envelope() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert tools
    assert all(tool.inputSchema.get("additionalProperties") is False for tool in tools)


def test_ai_operating_contract_is_published_at_runtime_and_in_discovery() -> None:
    schema = mcp_server.finance_get_event_schema()
    protocol = schema["agent_operating_protocol"]

    assert protocol["version"] == AI_OPERATING_PROTOCOL_VERSION
    assert [item["code"] for item in protocol["required_sequence"]] == [
        "inspect_available_materials",
        "derive_when_unique",
        "identify_material_unknowns",
        "separate_contribution_policy_actual_and_cash",
        "apply_first_wage_tax_treatment_only_with_evidence",
        "generate_period_close_management_commentary",
        "ask_minimum_specific_question",
        "submit_or_stop",
    ]
    assert "不得让用户代替AI" in protocol["prohibitions"][-1]
    assert "除已提供并核对的材料外" in protocol["question_policy"]["final_fallback"]
    assert EVIDENCE_FIRST_RUNTIME_INSTRUCTION in mcp.instructions
    assert "agent_operating_protocol" in mcp.instructions
    assert "management_commentary" in mcp.instructions
    assert "不得把看板指标简单拼接" in mcp.instructions


@pytest.mark.parametrize(
    ("tool_name", "arguments", "forbidden_value"),
    [
        (
            "finance_get_event_schema",
            {"actor": "caller-actor"},
            "caller-actor",
        ),
        (
            "finance_get_profile",
            {
                "org_id": "11111111-1111-1111-1111-111111111111",
                "session_token": "profile-token",
            },
            "profile-token",
        ),
        (
            "finance_query_context",
            {
                "org_id": "11111111-1111-1111-1111-111111111111",
                "actor": "query-actor",
            },
            "query-actor",
        ),
        (
            "finance_get_event",
            {
                "org_id": "11111111-1111-1111-1111-111111111111",
                "event_id": "22222222-2222-2222-2222-222222222222",
                "session_token": "event-token",
            },
            "event-token",
        ),
    ],
)
def test_read_tool_runtime_rejects_caller_identity_and_session_extras(
    tool_name: str,
    arguments: dict[str, object],
    forbidden_value: str,
) -> None:
    with pytest.raises(ToolError, match="VALIDATION_ERROR") as caught:
        asyncio.run(mcp.call_tool(tool_name, arguments))
    assert forbidden_value not in str(caught.value)


def test_stdio_server_initializes_and_lists_tools() -> None:
    async def run() -> set[str]:
        repository_root = Path(__file__).parents[1]
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(repository_root / "src"),
                    str(site_packages),
                    str(site_packages / "win32"),
                    str(site_packages / "win32" / "lib"),
                    str(site_packages / "pywin32_system32"),
                    environment.get("PYTHONPATH"),
                ],
            )
        )
        parameters = StdioServerParameters(
            # On Windows the virtualenv launcher starts a second process that
            # stdio_client cannot reliably reap.  The base interpreter plus the
            # virtualenv dependencies keeps this smoke test self-contained.
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-m", "ai_accounting.mcp_server"],
            cwd=repository_root,
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.list_tools()
                return {tool.name for tool in response.tools}

    names = asyncio.run(run())
    assert "finance_record_event" in names
    assert "finance_get_event" in names
    assert {
        "finance_register_employee",
        "finance_register_employee_profile_version",
        "finance_register_payroll_policy_version",
        "finance_register_payroll_opening_state",
        "finance_preview_payroll",
        "finance_confirm_payroll",
        "finance_get_payroll_batch",
        "finance_acquire_fixed_asset",
        "finance_activate_fixed_asset",
        "finance_preview_fixed_asset_depreciation",
        "finance_confirm_fixed_asset_depreciation",
        "finance_preview_fixed_asset_depreciation_batch",
        "finance_confirm_fixed_asset_depreciation_batch",
        "finance_dispose_fixed_asset",
        "finance_get_fixed_asset",
        "finance_acquire_intangible_asset",
        "finance_preview_intangible_asset_amortization",
        "finance_confirm_intangible_asset_amortization",
        "finance_retire_intangible_asset",
        "finance_get_intangible_asset",
        "finance_draw_borrowing",
        "finance_preview_borrowing_interest",
        "finance_confirm_borrowing_interest",
        "finance_pay_borrowing_interest",
        "finance_repay_borrowing_principal",
        "finance_get_borrowing",
    } <= names


def test_production_real_stdio_keeps_schema_public_and_data_tools_fail_closed() -> None:
    async def run() -> tuple[object, object, set[str], dict[str, object]]:
        repository_root = Path(__file__).parents[1]
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(repository_root / "src"),
                    str(site_packages),
                    str(site_packages / "win32"),
                    str(site_packages / "win32" / "lib"),
                    str(site_packages / "pywin32_system32"),
                    environment.get("PYTHONPATH"),
                ],
            )
        )
        script = """
from contextlib import nullcontext
from types import SimpleNamespace
from ai_accounting import mcp_server

class EmptyCredentialStore:
    def load_session_token(self):
        return None

mcp_server.get_settings = lambda: SimpleNamespace(
    finance_environment="production",
    finance_service_lock_file="injected-test-service.lock",
)
mcp_server.WindowsCredentialStore = EmptyCredentialStore
mcp_server.WindowsCurrentUserOnlyAclVerifier = object
mcp_server.acquire_windows_service_lease = lambda *args, **kwargs: nullcontext()
mcp_server.main()
"""
        parameters = StdioServerParameters(
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-c", script],
            cwd=repository_root,
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                schema = await session.call_tool("finance_get_event_schema", {})
                data = await session.call_tool(
                    "finance_get_profile",
                    {"org_id": str(uuid.uuid4())},
                )
                tools = await session.list_tools()
                by_name = {tool.name: tool for tool in tools.tools}
                return (
                    schema,
                    data,
                    set(by_name),
                    by_name["finance_preview_bank_statement_import"].inputSchema,
                )

    schema, data, names, import_schema = asyncio.run(run())
    assert schema.isError is False
    assert data.isError is False
    assert data.structuredContent == {
        "status": "rejected",
        "errors": ["AUTHENTICATION_REQUIRED"],
    }
    assert "finance_import_bank_statement" not in names
    assert {
        "finance_preview_bank_statement_import",
        "finance_confirm_bank_statement_import",
        "finance_query_bank_statement_state",
    } <= names
    assert import_schema["additionalProperties"] is False
    request_schema = import_schema["$defs"]["PreviewBankStatementFileImportRequest"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["file_format"]["const"] == "csv"
    assert "sheet_name" not in request_schema["properties"]
    assert "file_path" not in request_schema["properties"]
    assert "statement_bytes" not in request_schema["properties"]


def test_formal_server_initializes_store_without_caching_token_inside_service_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_accounting import mcp_server

    calls: list[str] = []

    class _CredentialStore:
        def __init__(self) -> None:
            calls.append("credential-init")

        def load_session_token(self) -> None:
            calls.append("credential-read")
            return None

    class _Lease:
        def __enter__(self) -> None:
            calls.append("lease-enter")

        def __exit__(self, *_args: object) -> None:
            calls.append("lease-exit")

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="production",
            finance_service_lock_file=Path("service.lock"),
        ),
    )
    monkeypatch.setattr(mcp_server, "WindowsCredentialStore", _CredentialStore)
    monkeypatch.setattr(mcp_server, "WindowsCurrentUserOnlyAclVerifier", object)
    monkeypatch.setattr(
        mcp_server,
        "acquire_windows_service_lease",
        lambda *_args, **_kwargs: _Lease(),
    )
    monkeypatch.setattr(
        mcp_server.mcp,
        "run",
        lambda *, transport: calls.append(f"run:{transport}"),
    )

    mcp_server.main()

    assert calls == ["lease-enter", "credential-init", "run:stdio", "lease-exit"]
