"""Local stdio transport, owned by the launching OS user; no listening network socket."""

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .daemon import ServiceClient
from .diagnostics import error_response


def serve(root: Path):
    service = ServiceClient(root)
    mcp = FastMCP(
        "local-accounting-kernel",
        instructions=(
            "Call finance_local_schema first. Follow its versioned agent_operating_protocol "
            "and the specialized agent_operating_rules returned with selected contracts."
        ),
    )

    @mcp.tool()
    def finance_local_schema(
        view: Literal["overview", "selected", "full"] = "overview",
        fact_kinds: list[str] | None = None,
        commands: list[str] | None = None,
        response_types: list[str] | None = None,
        protocol_topics: list[str] | None = None,
    ) -> dict:
        """Read common rules and the directory, then select contracts and operating topics.

        Selected contracts automatically include related operating rules.
        Use protocol_topics to read additional topics without requesting contracts.
        Follow the overview's discovery instructions for reuse and fallback.
        """
        try:
            return service.dispatch("schema", {
                "view": view,
                "fact_kinds": fact_kinds or [],
                "commands": commands or [],
                "response_types": response_types or [],
                "protocol_topics": protocol_topics or [],
            })
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
        """Execute a command using its discovered contract and operating rules.

        Read finance_local_schema first; command="schema" accepts the same selection payload.
        """
        try:
            result = service.dispatch(command, payload)
            return result if isinstance(result, dict) else {"items": result}
        except Exception as exc:
            return error_response(exc)

    mcp.run(transport="stdio")
