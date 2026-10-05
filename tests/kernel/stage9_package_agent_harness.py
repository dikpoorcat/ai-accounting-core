"""Isolated, packaged CLI/MCP relay for a human-directed synthetic agent replay.

Run this file with the *packaged* runtime/python.exe. Business calls enter the
packaged finance-local CLI MCP process and its HTTP service. The host uses two
explicit draft seed companies, synthetic login and test-only native transport.
It does not exercise a human password window.
All request/response transcripts remain under an explicit .tmp directory.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def validate_draft_seed(directory, package_formats, seed):
    """Validate explicit version-2 seed scope before starting its synthetic host."""
    from ai_accounting.kernel.schema_bundle import valid_database_format
    from ai_accounting.kernel.types import YearMonth

    if set(package_formats) != {"catalog", "company"} or any(
        not valid_database_format(value) or value["kind"] != kind
        or value["family"] != "ai-accounting-kernel/2"
        or value["status"] != "draft" or value["version"] != 0
        for kind, value in package_formats.items()
    ):
        raise ValueError("Only an actual draft/0 package may host this replay")
    if (
        type(seed.get("seed_schema_version")) is not int or seed["seed_schema_version"] != 2
        or seed.get("status") != "complete"
        or seed.get("completion_scope") != "isolated_draft_seed_construction_not_ai_acceptance"
        or seed.get("owner_provisioned") is not False
        or seed.get("ai_acceptance_status") != "not_started"
        or seed.get("database_formats") != package_formats
        or seed.get("root") != str(directory / "data")
        or not isinstance(seed.get("source_snapshot_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", seed["source_snapshot_sha256"])
        or not isinstance(seed.get("source_build_id"), str)
        or not re.fullmatch(r"local-kernel-2:[a-f0-9]{64}", seed["source_build_id"])
        or not isinstance(seed.get("companies"), list) or len(seed["companies"]) != 2
    ):
        raise ValueError(
            "Expected an explicit complete two-company draft seed, not old qualification"
        )
    identities = {key: set() for key in ("id", "taxpayer_id", "database_id")}
    for case in seed["companies"]:
        company = case.get("company", {})
        if set(company) != {"id", "taxpayer_id", "name", "path", "database_id"} or any(
            not isinstance(value, str) or not value for value in company.values()
        ):
            raise ValueError("Seed company identity is incomplete")
        path = directory / "data" / company["taxpayer_id"] / "company.sqlite"
        if (not re.fullmatch(r"[A-Z0-9]{18}", company["taxpayer_id"])
                or company["path"] != str(path)):
            raise ValueError("Seed company path or taxpayer identity escaped the synthetic root")
        for key, seen in identities.items():
            if company[key] in seen:
                raise ValueError("Seed companies must have distinct business identities")
            seen.add(company[key])
        periods = case.get("open_periods")
        if (not isinstance(periods, list) or len(periods) != 1
                or not isinstance(periods[0], str) or str(YearMonth(periods[0])) != periods[0]):
            raise ValueError("Each seed company needs one explicit open month")
        if (not isinstance(case.get("state"), list) or len(case["state"]) != 6
                or any(type(value) is not int for value in case["state"])):
            raise ValueError("Seed company needs its complete observed state")
    return seed["companies"]


def assert_seed_companies(cases, actual):
    expected = {case["company"]["id"]: case["company"] for case in cases}
    if len(actual) != 2 or {row["id"]: row for row in actual} != expected:
        raise ValueError("Actual catalog differs from the two exact seed company identities")


def assert_seed_response_pair(brief, funds, *, company, period):
    assert_owner_brief_result(brief, funds, company_id=company["id"], period=period)
    assert brief["read_context"]["database_id"] == company["database_id"]
    assert brief["read_context"] == funds["read_context"]


async def mcp_value(session, tool, arguments):
    result = await session.call_tool(tool, arguments)
    if result.isError:
        raise ValueError("Packaged MCP preflight returned a transport error")
    return mcp_result_value(result)


def mcp_result_value(result):
    """Preserve MCP tool errors, including SDK validation's non-JSON text."""
    text = "".join(getattr(item, "text", "") for item in getattr(result, "content", ()))
    value = result.structuredContent
    if value is None:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
    if result.isError:
        return {"status": "rejected", "code": "mcp_tool_error", "message": text,
                "mcp_is_error": True, "structured_content": value}
    if value is None:
        return {"status": "unknown", "code": "mcp_non_json_response", "message": text,
                "mcp_is_error": False}
    return value


async def process_mcp_requests(directory, session):
    """Resume original pending files; completed receipts are never overwritten."""
    for path in sorted((directory / "requests").glob("*.json"),
                       key=lambda item: (item.stat().st_mtime_ns, item.name)):
        output = directory / "responses" / path.name
        if output.exists():
            continue
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            tool, arguments = request["tool"], request["arguments"]
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                raise ValueError("Invalid tool envelope")
        except (ValueError, KeyError, TypeError):
            write_json(output, {"status": "rejected", "code": "relay_invalid_request",
                                "message": "Invalid MCP request file"})
            continue
        try:
            value = mcp_result_value(await session.call_tool(tool, arguments))
        except Exception as error:
            # Delivery/transport failure cannot establish whether a write committed.
            value = {"status": "unknown", "code": "mcp_transport_error",
                     "message": type(error).__name__, "submission_outcome": "unknown"}
        delivered = value
        fault = directory / "lose_next_write_response"
        payload = arguments.get("payload", {})
        if (fault.exists() and isinstance(payload, dict) and payload.get("request_id")
                and isinstance(value, dict)
                and value.get("status") not in {"rejected", "needs_information", "unknown"}):
            fault.unlink()
            delivered = {"status": "rejected", "code": "service_unavailable",
                         "message": "测试传输中断：本次响应未送达"}
        with (directory / "tools.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"request": request, "actual_response": value,
                                  "delivered_response": delivered}, ensure_ascii=False) + "\n")
        write_json(output, delivered)


def process_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x1000, 0, pid)
    if not handle:
        return False
    try:
        status = ctypes.c_uint32()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status))) and status.value == 259
    finally:
        kernel.CloseHandle(handle)


async def preflight_seed_mcp(session, cases, resident_reads, package_formats):
    """Observe both exact companies through production stdio before publishing ready."""
    schema = await mcp_value(session, "finance_local_schema", {})
    if schema.get("database_formats") != package_formats:
        raise ValueError("MCP schema differs from the package's active draft formats")
    companies = await mcp_value(session, "finance_local_command", {
        "command": "companies", "payload": {},
    })
    assert_seed_companies(cases, companies["items"])
    observations = []
    for case in cases:
        company, period = case["company"], case["open_periods"][0]
        values = []
        for command in ("dashboard_brief", "dashboard_funds"):
            values.append(await mcp_value(session, "finance_local_command", {
                "command": command, "payload": {"company_id": company["id"], "period": period},
            }))
        brief, funds = values
        assert_seed_response_pair(brief, funds, company=company, period=period)
        resident_brief, resident_funds = resident_reads[company["id"]]
        assert brief["read_context"] == resident_brief["read_context"]
        assert funds["read_context"] == resident_funds["read_context"]
        assert {key: value for key, value in brief["data"].items() if key != "generated_at"} == {
            key: value for key, value in resident_brief["data"].items() if key != "generated_at"
        }
        assert funds["data"] == resident_funds["data"]
        observations.append({
            "company": company, "period": period, "read_context": brief["read_context"],
            "response_schema_version": brief["schema_version"],
            "response_contract_validated": True, "scope_validated": True,
            "mcp_business_result_equal_to_resident": True,
            "activity_count": brief["data"]["activity_count"],
            "funds_overview": brief["data"]["funds_overview"],
            "position": brief["data"]["position"],
        })
    return observations


def assert_owner_brief_result(brief, funds, *, company_id, period):
    """Keep the packaged relay's owner response and money checks executable."""
    from ai_accounting.kernel.response_contracts import RESPONSE_ADAPTERS, validate_response

    for command, value in (("dashboard_brief", brief), ("dashboard_funds", funds)):
        checked = validate_response(command, value)
        assert RESPONSE_ADAPTERS[command].dump_python(checked, mode="python") == value
        assert value["read_context"]["company_id"] == company_id
        assert value["selected_period"]["key"] == period
        for collection in value["data"]["collections"].values():
            page = collection["page"]
            assert len(collection["items"]) == page["returned_count"]
            assert page["returned_count"] <= page["filtered_count"] <= page["total_count"]
            assert page["has_more"] == (page["next_cursor"] is not None)
    assert brief["data"]["activity_count"] > 0
    for field in ("total_fen", "bank_fen", "cash_fen", "payment_platform_fen"):
        assert brief["data"]["funds_overview"][field] == funds["data"][field]


def write_json(path, value):
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def host(directory, package, *, resume=False):
    with ExitStack() as cleanup:
        await _host(directory, package, cleanup, resume=resume)


async def _host(directory, package, cleanup, *, resume=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pydantic import SecretStr

    from ai_accounting.kernel import service as service_module
    from ai_accounting.kernel.build import calculator_build_id
    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.permissions import reject_reparse_path
    from ai_accounting.kernel.runtime import private_file_lock
    from ai_accounting.kernel.security.credentials import WindowsCredentialStore
    from ai_accounting.kernel.security.transport import NativeHttpClient
    from ai_accounting.kernel.security.windows import write_protected_json
    from ai_accounting.kernel.service import LocalService
    from ai_accounting.kernel.types import YearMonth

    if not directory.is_relative_to(REPOSITORY / ".tmp"):
        raise ValueError("Use a synthetic harness directory under repository .tmp")
    if not (package / "manifest.json").is_file() or not (package / "runtime/python.exe").is_file():
        raise ValueError("Use a built independent runtime package")
    if Path(sys.executable).resolve() != (package / "runtime/python.exe").resolve():
        raise ValueError("Run the host with the selected packaged interpreter")
    if not Path(service_module.__file__).resolve().is_relative_to(package / "app"):
        raise ValueError("The host imported service code outside the selected package")
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    package_formats = manifest["runtime"]["database_formats"]
    seed = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
    cases = validate_draft_seed(directory, package_formats, seed)
    if not (seed["source_build_id"] == manifest["source_build_id"]
            == manifest["runtime"]["build_id"] == calculator_build_id()):
        raise ValueError("Seed, package manifest and actual imported calculator build differ")
    root = directory / "data"
    for path in (directory, root, root / "catalog.sqlite", package):
        reject_reparse_path(path)
    if not (root / "catalog.sqlite").is_file() or any(
        not Path(case["company"]["path"]).is_file() for case in cases
    ):
        raise ValueError("The existing synthetic catalog and both company files are required")
    relay_lock = cleanup.enter_context(private_file_lock(root / ".agent-relay.lock"))
    if not relay_lock:
        raise ValueError("This synthetic relay root already has an active host")
    previous = None
    run_directory = directory
    if resume:
        previous_ready = json.loads((directory / "ready.json").read_text("utf-8"))
        previous = json.loads((root / ".service.json").read_text("utf-8"))
        if (previous_ready["package"] != str(package)
                or previous_ready["root"] != str(root)
                or previous_ready["seed_source_snapshot_sha256"] != seed["source_snapshot_sha256"]
                or previous_ready["package_database_formats"] != package_formats
                or previous["build_id"] != calculator_build_id()
                or previous["database_format"] != package_formats["catalog"]
                or previous["protocol"] != 2 or process_alive(previous["pid"])):
            raise ValueError("Resume identity differs or previous host is still active")
        assert_seed_companies(cases, [row["company"] for row in previous_ready["companies"]])
        run_directory = directory / ("resume-" + uuid.uuid4().hex)
        run_directory.mkdir()
        write_json(run_directory / "previous-ready.json", previous_ready)
    for name in ("requests", "responses", "native_requests", "native_responses"):
        if resume:
            if not (directory / name).is_dir():
                raise ValueError("Resume requires every original relay channel")
        else:
            (directory / name).mkdir()
    app = LocalService(root, enable_read_pool=True)
    cleanup.callback(app.close)
    password = SecretStr("Synthetic-stage9-package-agent-only-2026")
    if resume:
        status = app.security.status()
        if (app.security.catalog_instance_id != previous["catalog_id"]
                or not status["provisioned"] or not status["active"]
                or status["login_name"] != "synthetic-stage9-owner"):
            raise ValueError("Resume must retain the original active synthetic owner/catalog")
    else:
        app.security.provision("synthetic-stage9-owner", password)
    token = app.security.login("synthetic-stage9-owner", password).session_token
    cleanup.callback(app.security.logout, token)
    from ai_accounting.kernel.security.service import credential_target

    credentials = WindowsCredentialStore(
        target_name=credential_target(root / "catalog.sqlite", app.security.catalog_instance_id)
    )
    credentials.save_session_token(token)
    cleanup.callback(credentials.delete_session_token)
    server, capability = create_server(app, port=0)
    cleanup.callback(server.server_close)
    app.security_controller = build_native_security_controller(
        app,
        server,
        capability,
        credential_store=credentials,
        window_opener=lambda _request_id: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cleanup.callback(thread.join, 5)
    cleanup.callback(server.shutdown)
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
    assert_seed_companies(cases, companies)
    assert app.catalog.database_format() == package_formats["catalog"]
    resident_reads = {}
    for case in cases:
        company, period = case["company"], case["open_periods"][0]
        with app.engine(company["id"]).store.connection(read_only=True) as connection:
            identity = tuple(connection.execute("SELECT * FROM identity").fetchone())
            assert identity == (1, company["id"], company["taxpayer_id"], company["database_id"])
            current_state = list(connection.execute("SELECT * FROM state").fetchone())
            assert len(current_state) == 6 and all(type(value) is int for value in current_state)
            if not resume:
                assert current_state == case["state"]
                assert connection.execute("SELECT 1 FROM period_close WHERE period=?", (
                    YearMonth(period).ordinal,
                )).fetchone() is None
        payload = {"company_id": company["id"], "period": period}
        brief = attached.dispatch("dashboard_brief", payload)
        funds = attached.dispatch("dashboard_funds", payload)
        assert_seed_response_pair(brief, funds, company=company, period=period)
        resident_reads[company["id"]] = (brief, funds)
    parameters = StdioServerParameters(
        command=str(package / "runtime/python.exe"),
        args=["-I", "-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", str(root), "mcp"],
    )
    try:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                observations = await preflight_seed_mcp(
                    session, cases, resident_reads, package_formats,
                )
                write_json(run_directory / "owner-brief.json", {
                    "scope": "packaged_draft_seed_preflight_not_ai_acceptance",
                    "companies": observations,
                })
                write_json(
                    directory / "ready.json",
                    {
                        "package": str(package),
                        "host_pid": os.getpid(),
                        "resume": resume,
                        "resume_observations": str(run_directory / "owner-brief.json"),
                        "root": str(root),
                        "seed_schema_version": 2,
                        "companies": [{"company": case["company"],
                                       "open_periods": case["open_periods"]} for case in cases],
                        "seed_source_snapshot_sha256": seed["source_snapshot_sha256"],
                        "native_transport_scope": "synthetic_only_not_human_password_window",
                        "package_database_formats": package_formats,
                        "tools": [item.name for item in tools.tools],
                    },
                )
                while not (directory / "stop").exists():
                    await process_mcp_requests(directory, session)
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
        cleanup.close()


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
    parser.add_argument("mode", choices=("host", "resume", "call", "schema", "native"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("argument", nargs="?", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.mode in {"host", "resume"}:
        asyncio.run(host(directory, args.argument.resolve(), resume=args.mode == "resume"))
    else:
        submit(directory, args.mode, args.argument)
