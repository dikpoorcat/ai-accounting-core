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
            "Call finance_local_schema first and follow its versioned agent_operating_protocol. "
            "Bind each request to the selected company. Submit typed facts and evidence, "
            "never free journal entries. Do not infer missing accounting facts."
        ),
    )

    @mcp.tool()
    def finance_local_schema(
        view: Literal["overview", "selected", "full"] = "overview",
        fact_kinds: list[str] | None = None,
        commands: list[str] | None = None,
        response_types: list[str] | None = None,
    ) -> dict:
        """Read the overview first; explicitly select needed contracts or request full.

        The selected view requires fact_kinds for save_fact/amend_fact/save_facts.
        Dedicated facts include their required registration command automatically.
        """
        try:
            return service.dispatch("schema", {
                "view": view,
                "fact_kinds": fact_kinds or [],
                "commands": commands or [],
                "response_types": response_types or [],
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
        """Execute a typed command; confirm accepts a reviewed digest, never journal lines.

        First discover finance_local_schema. Pass company_id for every company operation.
        save_fact confirms latest facts; preview/confirm publish their accounting effects.
        Review each preview item's source_period, posting_period and mode. Supply
        posting_period only when the published contract requires an explicit open month.
        Automatically recalculated open dependencies keep their existing posting month;
        an explicit root that conflicts with the requested month is rejected.
        Missing accounting facts are returned as structured needs_information.
        list/read/save/delete_work_draft resume unsubmitted work by explicit
        company, period and work_area. Save with expected_revision=null only
        when no draft exists; updates/deletion require its current revision.
        A saved draft is not a fact, publication or approval. Recover uncertain
        business writes through request_result using the original request_id.
        """
        try:
            result = service.dispatch(command, payload)
            return result if isinstance(result, dict) else {"items": result}
        except Exception as exc:
            return error_response(exc)

    mcp.run(transport="stdio")
