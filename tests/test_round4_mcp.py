from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SENTINEL = (
    "R4-SECRET-UNKNOWN-987654 postgresql://user:password@db.internal/payroll "
    "SELECT * FROM employees WHERE id='110101199001011234' "
    "6222020202020202 "
    + "X" * 4096
)
SQL_SENTINEL = f"SELECT * FROM payroll WHERE secret = {SENTINEL}"


def _stdio_environment() -> dict[str, str]:
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
    return environment


def _raising_stdio_program(exception_statement: str) -> str:
    function_source = "def boom(event_type=None):\n    " + exception_statement
    # ``-c`` receives one Windows command-line argument; semicolons keep this
    # program syntactically single-line while the injected function itself is
    # still compiled with a real newline by ``exec``.
    return "; ".join(
        [
            "from ai_accounting import mcp_server",
            "tool = mcp_server.mcp._tool_manager.get_tool('finance_get_event_schema')",
            "namespace = {}",
            f"exec({function_source!r}, namespace)",
            "object.__setattr__(tool, 'fn', namespace['boom'])",
            "mcp_server.main()",
        ]
    )


def test_r4_010_stdio_prevalidation_keeps_paths_but_hides_request_sentinels() -> None:
    async def run() -> str:
        parameters = StdioServerParameters(
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-m", "ai_accounting.mcp_server"],
            cwd=Path(__file__).parents[1],
            env=_stdio_environment(),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                response = await client.call_tool(
                    "finance_preview_payroll",
                    {
                        "request": {
                            "org_id": "00000000-0000-0000-0000-000000000000",
                            "unexpected": SENTINEL,
                        }
                    },
                )
                assert response.isError is True
                return "\\n".join(item.text for item in response.content)

    response_text = asyncio.run(run())

    assert "VALIDATION_ERROR" in response_text
    assert "request.unexpected" in response_text
    for forbidden in (SENTINEL, "input_value", "postgresql://", "6222020202020202"):
        assert forbidden not in response_text


@pytest.mark.parametrize(
    ("exception_statement", "expected_code"),
    [
        (
            "from sqlalchemy.exc import IntegrityError; raise IntegrityError("
            f"{SQL_SENTINEL!r}, "
            f"{{'connection': {SENTINEL!r}}}, Exception({SENTINEL!r}))",
            "CONSTRAINT_VIOLATION",
        ),
        (f"raise ValueError({SENTINEL!r})", "INVALID_REQUEST"),
        (f"raise OSError({SENTINEL!r})", "INPUT_UNAVAILABLE"),
        (f"raise RuntimeError({SENTINEL!r})", "INTERNAL_ERROR"),
    ],
    ids=["database", "value-error", "os-error", "unknown-runtime"],
)
def test_r4_010_stdio_outer_tool_boundary_never_leaks_unknown_exceptions(
    exception_statement: str,
    expected_code: str,
) -> None:
    """Exercise FastMCP's real STDIO error serializer with hostile exception text."""

    async def run() -> str:
        parameters = StdioServerParameters(
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-c", _raising_stdio_program(exception_statement)],
            cwd=Path(__file__).parents[1],
            env=_stdio_environment(),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                response = await client.call_tool(
                    "finance_get_event_schema", {"event_type": "expense_cash"}
                )
                assert response.isError is True
                return "\\n".join(item.text for item in response.content)

    response_text = asyncio.run(run())

    assert response_text == expected_code
    for forbidden in (
        SENTINEL,
        "postgresql://",
        "password",
        "SELECT",
        "110101199001011234",
        "6222020202020202",
        "RuntimeError",
        "ValueError",
        "OSError",
        "IntegrityError",
        "Traceback",
    ):
        assert forbidden not in response_text
