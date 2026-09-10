"""Verify a relocated software bundle using only its isolated interpreter.

All business data below is synthetic and written beside, never inside, the
software package. The generated ZIP therefore contains software only.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path


def main():
    package = Path(__file__).resolve().parents[1]
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert sys.flags.isolated == 1 and sys.flags.ignore_environment == 1
    assert Path(sys.base_prefix).resolve() == package / "runtime"
    assert all(Path(entry).resolve().is_relative_to(package) for entry in sys.path)
    for relative, expected in manifest["files"].items():
        source = package / relative
        assert source.resolve().is_relative_to(package)
        assert source.stat().st_size == expected["bytes"], relative
        with source.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == expected["sha256"], relative

    from ai_accounting.financial_statement_template import _template_bytes
    from ai_accounting.kernel.build import calculator_build_id
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.mcp import serve  # noqa: F401 - validate optional entry dependencies
    from ai_accounting.kernel.service import LocalService

    assert calculator_build_id() == manifest["runtime"]["build_id"]
    assert sqlite3.sqlite_version == manifest["runtime"]["sqlite"] == "3.53.1"
    assert sys.version.split()[0] == manifest["runtime"]["python"] == "3.12.13"
    template_bytes = len(_template_bytes())
    validation = package.with_name(package.name + "-validation")
    validation.mkdir()  # Never overwrite an earlier verification or company.
    inputs = validation / "inputs"
    inputs.mkdir()
    data_root = validation / "companies"
    calls = 0

    def call(command, payload, *, root=data_root):
        nonlocal calls
        calls += 1
        request = inputs / f"{calls:02d}-{command}.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting.kernel.cli",
                "--root",
                str(root),
                "call",
                command,
                "--input",
                str(request),
            ],
            cwd=package,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        response = json.loads(result.stdout)
        assert not isinstance(response, dict) or response.get("status") not in {
            "rejected",
            "needs_information",
        }, response
        return response

    schema = call("schema", {})
    assert {"expense", "cash_payment", "cash_funding", "labor"} <= schema["facts"].keys()
    company = call(
        "create_company", {"taxpayer_id": "91310000123456789A", "name": "运行包合成验证企业"}
    )
    company_id = company["id"]
    proof = call(
        "evidence",
        {
            "company_id": company_id,
            "content_base64": base64.b64encode(b"Synthetic package verification expense").decode(
                "ascii"
            ),
            "media_type": "text/plain",
            "name": "合成验证资料",
            "request_id": "package-evidence",
        },
    )
    call(
        "save_fact",
        {
            "company_id": company_id,
            "kind": "expense",
            "subject_id": "synthetic-expense",
            "data": {
                "period": "2026-09",
                "amount_fen": 123456,
                "counterparty_id": "synthetic-supplier",
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            "evidence": [proof["digest"]],
            "expected_revision": 0,
            "request_id": "package-fact",
        },
    )
    preview = call("preview", {"company_id": company_id, "subjects": ["synthetic-expense"]})
    confirmation = {
        "company_id": company_id,
        "subjects": ["synthetic-expense"],
        "preview_digest": preview["digest"],
        "epochs": preview["epochs"],
        "request_id": "package-publish",
    }
    published = call("confirm", confirmation)
    assert call("confirm", confirmation) == published
    overview_request = {"company_id": company_id, "period": "2026-09"}
    overview = call("overview", overview_request)
    assert sum(row["debit"] for row in overview["accounts"]) == 123456
    assert sum(row["credit"] for row in overview["accounts"]) == 123456
    backup = call(
        "backup",
        {
            "company_id": company_id,
            "directory": str(validation / "backups"),
            "request_id": "package-backup",
        },
    )
    restored_root = validation / "restored"
    restored = call(
        "restore_company",
        {
            "archive": backup["path"],
            "taxpayer_id": company["taxpayer_id"],
            "name": "运行包合成恢复验证企业",
        },
        root=restored_root,
    )
    assert restored["id"] == company_id and restored["database_id"] == company["database_id"]
    assert call("overview", overview_request, root=restored_root) == overview

    for launcher in (
        [str(package / "finance-local.cmd")],
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(package / "finance-local.ps1"),
        ],
    ):
        result = subprocess.run(
            [*launcher, "--root", str(data_root), "call", "companies"],
            cwd=package,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert json.loads(result.stdout)[0]["id"] == company_id

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check_stdio_mcp():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting.kernel.cli",
                "--root",
                str(restored_root),
                "mcp",
            ],
        )
        with anyio.fail_after(30):
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tool_list = await session.list_tools()
                    assert {tool.name for tool in tool_list.tools} == {
                        "finance_local_schema",
                        "finance_local_command",
                    }
                    result = await session.call_tool(
                        "finance_local_command",
                        {
                            "command": "overview",
                            "payload": overview_request,
                        },
                    )
                    assert not result.isError
                    value = result.structuredContent
                    if value is None:
                        value = json.loads("".join(item.text for item in result.content))
                    assert value["accounts"] == overview["accounts"]

    anyio.run(check_stdio_mcp)

    app = LocalService(restored_root)
    server, token = create_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/local.html")
        response = connection.getresponse()
        assert response.status == 200 and b"<html" in response.read()
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "GET",
            f"/api/local/overview?company_id={company_id}&period=2026-09",
            headers={"Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        assert response.status == 200
        wire_overview = json.loads(response.read())
        assert all(isinstance(row["debit"], str) for row in wire_overview["accounts"])
        assert sum(int(row["debit"]) for row in wire_overview["accounts"]) == 123456
        connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    outside = {
        name: module.__file__
        for name, module in tuple(sys.modules.items())
        if getattr(module, "__file__", None)
        and not Path(module.__file__).resolve().is_relative_to(package)
    }
    assert not outside, outside
    print(
        json.dumps(
            {
                "status": "passed",
                "package": str(package),
                "validation_data": str(validation),
                "runtime": manifest["runtime"],
                "verified_files": len(manifest["files"]),
                "cli_calls": calls,
                "fact_kinds": len(schema["facts"]),
                "published_vouchers": len(published["results"]),
                "debit_fen": 123456,
                "credit_fen": 123456,
                "backup_verified_and_restored": True,
                "relative_imports_only": True,
                "http_page_and_authenticated_api": True,
                "relative_cmd_and_powershell_launchers": True,
                "stdio_mcp_handshake_and_query": True,
                "template_bytes": template_bytes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
