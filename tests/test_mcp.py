from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_accounting.mcp_server import mcp


def test_mcp_exposes_only_domain_tools() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == {
        "finance_get_profile",
        "finance_get_event_schema",
        "finance_register_evidence",
        "finance_import_bank_statement",
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
        "finance_query_context",
        "finance_record_event",
        "finance_calculate_tax_period",
        "finance_confirm_tax_period",
        "finance_reverse_event",
        "finance_get_event",
    }
    assert not any("journal_line" in name or "sql" in name for name in names)


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
