"""Local stdio transport, owned by the launching OS user; no listening network socket."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .contracts import KernelError
from .service import LocalService


def serve(root: Path):
    service = LocalService(root)
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
        return service.dispatch("schema", {})

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
        except KernelError as exc:
            return exc.response()
        except (ValueError, TypeError, KeyError) as exc:
            return {"status": "rejected", "code": "invalid_command", "message": str(exc)}

    mcp.run(transport="stdio")
