"""Test-only persistent MCP relay for an independent accounting agent.

The production MCP handlers, service client, HTTP server and kernel all run.
Only owner credentials and the data root are replaced with isolated synthetic
ones. Dialogue and tool transcripts stay in the chosen test directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path


def write_json(path, value):
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def mcp_process(directory):
    from pydantic import SecretStr

    from ai_accounting.kernel import mcp as production_mcp
    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
    from ai_accounting.kernel.service import LocalService

    root = directory / "data"
    if root.exists():
        raise RuntimeError("The dialogue harness requires a new synthetic root")
    app = LocalService(root)
    password = SecretStr("Synthetic-agent-evaluation-only-2026")
    app.security.provision("synthetic-owner", password)
    credentials = InMemoryCredentialStore()
    credentials.save_session_token(app.security.login("synthetic-owner", password).session_token)
    server, capability = create_server(app, port=0)
    app.security_controller = build_native_security_controller(
        app, server, capability, credential_store=credentials, window_opener=lambda _: None
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    metadata = {
        "protocol": 2,
        "database_format": app.catalog.database_format(),
        "pid": os.getpid(),
        "port": server.server_port,
        "capability": capability,
        "catalog_id": app.security.catalog_instance_id,
        "build_id": server.build_id,
    }
    client = ServiceClient(root, metadata=metadata, credential_store=credentials)
    # Inject the existing credential boundary; every business request still
    # crosses the real HTTP and MCP handlers and their production validation.
    production_mcp.ServiceClient = lambda _root: client
    app.catalog.create_company("91310000123456789A", "合成甲公司")
    app.catalog.create_company("91310000123456789B", "合成乙公司")
    try:
        production_mcp.serve(root)
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


async def relay(directory):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    directory.mkdir(parents=True, exist_ok=False)
    (directory / "requests").mkdir()
    (directory / "responses").mkdir()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", str(Path(__file__).resolve()), "mcp", str(directory)],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            write_json(directory / "ready.json", {"tools": [t.name for t in tool_list.tools]})
            while not (directory / "stop").exists():
                for path in sorted((directory / "requests").glob("*.json")):
                    output = directory / "responses" / path.name
                    if output.exists():
                        continue
                    request = json.loads(path.read_text(encoding="utf-8"))
                    result = await session.call_tool(request["tool"], request["arguments"])
                    value = result.structuredContent
                    if value is None:
                        value = json.loads("".join(item.text for item in result.content))
                    delivered = value
                    fault = directory / "lose_next_write_response"
                    payload = request.get("arguments", {}).get("payload", {})
                    if (
                        fault.exists()
                        and payload.get("request_id")
                        and value.get("status") not in {"rejected", "needs_information"}
                    ):
                        fault.unlink()
                        delivered = {
                            "status": "rejected",
                            "code": "service_unavailable",
                            "message": "测试传输中断：本次响应未送达",
                        }
                    with (directory / "tools.jsonl").open("a", encoding="utf-8") as log:
                        log.write(
                            json.dumps(
                                {
                                    "request": request,
                                    "actual_response": value,
                                    "delivered_response": delivered,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    write_json(output, delivered)
                await asyncio.sleep(0.1)


def call(directory, tool, arguments_file):
    if not (directory / "ready.json").is_file():
        raise RuntimeError("MCP evaluation relay is not ready")
    request_id = uuid.uuid4().hex
    request = directory / "requests" / f"{request_id}.json"
    arguments = json.loads(arguments_file.read_text(encoding="utf-8-sig"))
    write_json(request, {"tool": tool, "arguments": arguments})
    output = directory / "responses" / request.name
    deadline = time.monotonic() + 55
    while not output.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Await the existing response file: {output}")
        time.sleep(0.05)
    print(output)
    if tool != "finance_local_schema":
        print(output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "mcp", "call"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("tool", nargs="?")
    parser.add_argument("arguments_file", nargs="?", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.mode == "mcp":
        mcp_process(directory)
    elif args.mode == "host":
        asyncio.run(relay(directory))
    else:
        call(directory, args.tool, args.arguments_file)
