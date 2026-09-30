"""Isolated, packaged CLI/MCP relay for a human-directed synthetic agent replay.

Run this file with the *packaged* runtime/python.exe. Business calls enter the
packaged finance-local CLI MCP process and its HTTP service. The host supplies
only a fresh test company, synthetic owner login and native approval window.
All request/response transcripts remain under an explicit .tmp directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def _recorded_brief_worker(*args):
    from ai_accounting.kernel.brief_parallel import _worker_read

    return {**_worker_read(*args), "test_worker_pid": os.getpid()}


def write_json(path, value):
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def host(directory, package):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pydantic import SecretStr

    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.security.credentials import WindowsCredentialStore
    from ai_accounting.kernel.security.transport import NativeHttpClient
    from ai_accounting.kernel.security.windows import write_protected_json
    from ai_accounting.kernel.service import LocalService

    if not directory.is_relative_to(REPOSITORY / ".tmp"):
        raise ValueError("Use a synthetic harness directory under repository .tmp")
    if not (package / "manifest.json").is_file() or not (package / "runtime/python.exe").is_file():
        raise ValueError("Use a built independent runtime package")
    package_formats = json.loads((package / "manifest.json").read_text(encoding="utf-8"))[
        "runtime"
    ]["database_formats"]
    seed = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
    if seed["status"] != "complete" or Path(seed["root"]).resolve() != directory / "data":
        raise ValueError("The synthetic seed report and company root must match")
    for name in ("requests", "responses", "native_requests", "native_responses"):
        (directory / name).mkdir()
    root = directory / "data"
    app = LocalService(root, enable_read_pool=True, enable_parallel_brief=True)
    password = SecretStr("Synthetic-stage9-package-agent-only-2026")
    app.security.provision("synthetic-stage9-owner", password)
    token = app.security.login("synthetic-stage9-owner", password).session_token
    from ai_accounting.kernel.security.service import credential_target

    credentials = WindowsCredentialStore(
        target_name=credential_target(root / "catalog.sqlite", app.security.catalog_instance_id)
    )
    credentials.save_session_token(token)
    server, capability = create_server(app, port=0)
    app.security_controller = build_native_security_controller(
        app,
        server,
        capability,
        credential_store=credentials,
        window_opener=lambda _request_id: None,
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
    write_protected_json(root / ".service.json", metadata)
    native = NativeHttpClient(
        port=server.server_port,
        capability=capability,
        catalog_instance_id=app.security.catalog_instance_id,
    )
    # Exercise the packaged CLI entry and session credential across processes.
    attached = ServiceClient(root)
    companies = attached.dispatch("companies", {})
    assert len(companies) == 1 and companies[0]["name"] == "阶段九合成规模企业"
    company = companies[0]
    with app.engine(company["id"]).store.connection(read_only=True):
        pass
    assert app.catalog.database_format() == package_formats["catalog"]
    from ai_accounting.kernel.brief_parallel import _BriefParallelAttempt, _worker_read

    open_periods = [
        period for period, snapshot in seed["snapshots"].items() if not snapshot["closed"]
    ]
    assert len(open_periods) == 1
    submitted, accepted = {}, []
    pool = app.brief_parallel.pool
    original_submit = pool.apply_async
    original_finish = _BriefParallelAttempt.finish

    def recorded_submit(function, args):
        assert function is _worker_read and args[0] not in submitted
        job = original_submit(_recorded_brief_worker, args)
        submitted[args[0]] = job
        return job

    def recorded_finish(attempt):
        result = original_finish(attempt)
        accepted.append((attempt.started, attempt.finished))
        return result

    pool.apply_async = recorded_submit
    _BriefParallelAttempt.finish = recorded_finish
    try:
        brief = app.dispatch(
            "dashboard_brief",
            {"company_id": company["id"], "period": open_periods[0]},
            session_token=token,
        )
    finally:
        pool.apply_async = original_submit
        _BriefParallelAttempt.finish = original_finish
    assert accepted == [(True, True)] and brief["schema_version"] == 7
    assert set(submitted) == {"materials", "duplicates", "reports"}
    worker_pids = {}
    for kind, job in submitted.items():
        packet = job.get(timeout=15)
        assert packet["ok"] and packet["test_worker_pid"] > 0
        worker_pids[kind] = packet["test_worker_pid"]
    assert len(set(worker_pids.values())) == 3
    write_json(
        directory / "parallel-brief.json",
        {
            "company_id": company["id"],
            "period": open_periods[0],
            "parallel_finish": accepted[0][1],
            "worker_pids": worker_pids,
            "response_schema_version": brief["schema_version"],
        },
    )
    parameters = StdioServerParameters(
        command=str(package / "runtime/python.exe"),
        args=["-I", "-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", str(root), "mcp"],
    )
    try:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                write_json(
                    directory / "ready.json",
                    {
                        "package": str(package),
                        "root": str(root),
                        "company": company,
                        "seed": {
                            "snapshots": seed["snapshots"],
                            "businesses_per_month": seed["monthly_business_count"],
                        },
                        "package_database_formats": package_formats,
                        "tools": [item.name for item in tools.tools],
                    },
                )
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
                    for path in sorted((directory / "native_requests").glob("*.json")):
                        output = directory / "native_responses" / path.name
                        if output.exists():
                            continue
                        request = json.loads(path.read_text(encoding="utf-8"))
                        if request.get("operation") != "approve":
                            raise ValueError("Only explicit synthetic close approval is supported")
                        value = native.call(
                            "native_execute",
                            request["request_id"],
                            password=password,
                        )
                        native.call("native_update", request["request_id"], status="succeeded")
                        with (directory / "native.jsonl").open("a", encoding="utf-8") as log:
                            log.write(
                                json.dumps(
                                    {"request_id": request["request_id"], "approval": value},
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        write_json(output, value)
                    await asyncio.sleep(0.05)
    finally:
        try:
            app.security.logout(token)
        finally:
            try:
                credentials.delete_session_token()
                server.shutdown()
                thread.join(5)
            finally:
                try:
                    server.server_close()
                finally:
                    app.close()


def submit(directory, channel, arguments_file):
    if not (directory / "ready.json").is_file():
        raise RuntimeError("The packaged MCP relay is not ready")
    request_id = uuid.uuid4().hex
    request = (
        directory
        / ("native_requests" if channel == "native" else "requests")
        / f"{request_id}.json"
    )
    arguments = json.loads(arguments_file.read_text(encoding="utf-8-sig"))
    write_json(request, arguments)
    output = directory / ("native_responses" if channel == "native" else "responses") / request.name
    deadline = time.monotonic() + 300
    while not output.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Await the existing response file: {output}")
        time.sleep(0.05)
    print(output)
    if channel != "schema":
        print(output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "call", "schema", "native"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("argument", nargs="?", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.mode == "host":
        asyncio.run(host(directory, args.argument.resolve()))
    else:
        submit(directory, args.mode, args.argument)
