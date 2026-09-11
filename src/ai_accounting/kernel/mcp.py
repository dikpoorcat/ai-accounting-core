"""Local stdio transport, owned by the launching OS user; no listening network socket."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .daemon import ServiceClient
from .diagnostics import error_response


def serve(root: Path):
    service = ServiceClient(root)
    mcp = FastMCP(
        "local-accounting-kernel",
        instructions=(
            "Call finance_local_schema first and follow agent_operating_protocol. "
            "Bind each request to the selected company. Submit typed facts and evidence, "
            "never free journal entries. Do not infer missing accounting facts."
        ),
    )

    @mcp.tool()
    def finance_local_schema() -> dict:
        """Get all typed business fact schemas, field meanings and supported commands."""
        try:
            return service.dispatch("schema", {})
        except Exception as exc:
            return error_response(exc)

    @mcp.tool()
    def finance_local_security(action: str, payload: dict) -> dict:
        """Request/status/cancel the native window; never include passwords or recovery codes."""
        try:
            return service.security(action, payload)
        except Exception as exc:
            return error_response(exc)

    @mcp.tool()
    def finance_local_command(command: str, payload: dict) -> dict:
        """Execute a typed command; confirm accepts a reviewed digest, never journal lines.

        First discover finance_local_schema. Pass company_id for every company operation.
        save_fact confirms latest facts; preview/confirm publish their accounting effects.
        Missing accounting facts are returned as structured needs_information.
        """
        try:
            result = service.dispatch(command, payload)
            return result if isinstance(result, dict) else {"items": result}
        except Exception as exc:
            return error_response(exc)

    mcp.run(transport="stdio")
