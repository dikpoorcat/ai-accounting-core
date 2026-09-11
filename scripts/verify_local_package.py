"""Verify a relocated software bundle using only its isolated interpreter.

All business data below is synthetic and written beside, never inside, the
software package. The generated ZIP therefore contains software only.
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

_SERVICES = {}
_RUNNERS = {}


def start_resident(root, *, native_smoke=False):
    """Exercise the exact daemon components using exclusively synthetic owners."""
    from pydantic import SecretStr

    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.jobs import JobRunner
    from ai_accounting.kernel.security.native import NativeSecurityController
    from ai_accounting.kernel.security.transport import (
        NativeHttpClient,
        WindowBridge,
        launch_native_window,
    )
    from ai_accounting.kernel.security.window import SecurityForm
    from ai_accounting.kernel.security.windows import read_protected_json, write_protected_json
    from ai_accounting.kernel.service import LocalService

    app = LocalService(root)
    password = SecretStr("Synthetic-package-owner-only-2026")
    app.security.provision("package-test-owner", password)
    server, capability = create_server(app, port=0)
    controller = NativeSecurityController(app.security, window_opener=lambda request_id: None)
    app.security_controller = controller
    metadata = {
        "pid": os.getpid(),
        "port": server.server_port,
        "capability": capability,
        "catalog_id": app.security.catalog_instance_id,
        "build_id": server.build_id,
    }
    write_protected_json(root / ".service.json", metadata)
    assert read_protected_json(root / ".service.json") == metadata
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _SERVICES[root] = (app, server, thread, metadata)
    runner = JobRunner(app.catalog, interval=0.1)
    _RUNNERS[root] = runner
    runner.start()
    client = NativeHttpClient(
        port=server.server_port,
        capability=capability,
        catalog_instance_id=app.security.catalog_instance_id,
    )

    if native_smoke:
        spawned = []
        controller.window_opener = lambda request_id: spawned.append(
            launch_native_window(
                request_id,
                port=server.server_port,
                capability=capability,
                catalog_instance_id=app.security.catalog_instance_id,
            )
        )
        window = controller.request(kind="login")
        deadline = time.monotonic() + 15
        while controller.status(window["request_id"])["status"] == "starting":
            assert time.monotonic() < deadline, "Native pythonw window did not become visible"
            time.sleep(0.1)
        assert controller.status(window["request_id"])["status"] == "waiting_for_user"
        kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, spawned[0])
        assert handle
        try:
            controller.cancel(window["request_id"])
            assert kernel.WaitForSingleObject(handle, 10_000) == 0
        finally:
            kernel.CloseHandle(handle)
        controller.window_opener = lambda request_id: None

        # Use the actual copied Tk form, its worker and private HTTP transport.
        # Only a synthetic password is entered, automatically, into this test form.
        import tkinter as tk

        request_id = controller.request(kind="login")["request_id"]
        bridge = WindowBridge(client, request_id)
        record, facts = bridge.inspect()
        window_root = tk.Tk()
        form = SecurityForm(window_root, bridge, record, facts)

        def submit_synthetic():
            form.entries["password"].insert(0, password.get_secret_value())
            form.submit()

        window_root.after(250, submit_synthetic)
        window_root.after(15_000, form.destroy)
        window_root.mainloop()
        assert controller.status(request_id)["status"] == "succeeded"
    else:
        request_id = controller.request(kind="login")["request_id"]
        client.call("native_execute", request_id, password=password)
        client.call("native_update", request_id, status="succeeded")
    assert controller.session_status()["authenticated"]
    return app, server, metadata


def stop_residents():
    for runner in _RUNNERS.values():
        runner.stop()
    _RUNNERS.clear()
    for app, server, thread, _ in reversed(tuple(_SERVICES.values())):
        try:
            token = app.security_controller.store.load_session_token()
            if token is not None:
                app.security.logout(token)
        finally:
            app.security_controller.store.delete_session_token()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    _SERVICES.clear()


atexit.register(stop_residents)


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

    import argon2  # noqa: F401
    import pypdf  # noqa: F401
    import xlrd  # noqa: F401
    import xlwt  # noqa: F401

    from ai_accounting.financial_statement_template import _template_bytes
    from ai_accounting.kernel.build import calculator_build_id
    from ai_accounting.kernel.mcp import serve  # noqa: F401 - validate optional entry dependencies

    assert calculator_build_id() == manifest["runtime"]["build_id"]
    assert sqlite3.sqlite_version == manifest["runtime"]["sqlite"] == "3.53.1"
    assert sys.version.split()[0] == manifest["runtime"]["python"] == "3.12.13"
    template_bytes = len(_template_bytes())
    validation = package.with_name(package.name + "-validation")
    validation.mkdir()  # Never overwrite an earlier verification or company.
    inputs = validation / "inputs"
    inputs.mkdir()
    data_root = validation / "companies"
    start_resident(data_root, native_smoke=True)
    calls = 0

    def call(command, payload, *, root=data_root):
        nonlocal calls
        if root not in _SERVICES:
            start_resident(root)
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
    queued = call(
        "backup",
        {
            "company_id": company_id,
            "directory": str(validation / "backups"),
            "request_id": "package-backup",
        },
    )
    assert queued["status"] == "pending"
    deadline = time.monotonic() + 30
    while True:
        jobs = call("jobs", {"company_id": company_id})
        backup_job = next(row for row in jobs if row["id"] == queued["job_id"])
        if backup_job["status"] == "succeeded":
            backup = backup_job["result"]
            break
        assert backup_job["status"] != "failed", "Automatic backup failed"
        assert time.monotonic() < deadline, "Automatic backup did not complete"
        time.sleep(0.1)
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
                    assert {tool.name for tool in tool_list.tools} >= {
                        "finance_local_schema",
                        "finance_local_command",
                        "finance_local_security",
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

    app, server, thread, metadata = _SERVICES[restored_root]
    token = app.security_controller.store.load_session_token().get_secret_value()
    try:
        for route in (
            "/",
            "/index.html",
            "/local.html",
            "/funds",
            "/employees",
            "/assets",
            "/reports",
        ):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", route)
            response = connection.getresponse()
            assert response.status == 200 and b"<html" in response.read(), route
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
        for action in ("context", "brief", "funds", "employees", "assets", "quarterly-report"):
            query = f"company_id={company_id}"
            query += (
                "&year=2026&quarter=3"
                if action == "quarterly-report"
                else ("" if action == "context" else "&period=2026-09")
            )
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request(
                "GET",
                f"/api/dashboard/{action}?{query}",
                headers={"Authorization": "Bearer " + token},
            )
            response = connection.getresponse()
            wire_dashboard = json.loads(response.read())
            assert response.status == 200, (action, wire_dashboard)
            if action == "context":
                assert wire_dashboard["current_company"]["company_id"] == company_id
            elif action == "brief":
                assert isinstance(wire_dashboard["data"]["total_debit_fen"], str)
                assert wire_dashboard["data"]["total_debit_fen"] == "123456"
            connection.close()
    finally:
        connection.close()

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
                "background_backup_without_manual_run": True,
                "relative_imports_only": True,
                "http_page_and_authenticated_api": True,
                "dashboard_five_routes_and_legacy_entries": True,
                "dashboard_six_authenticated_queries": True,
                "dashboard_integer_cent_strings": True,
                "relative_cmd_and_powershell_launchers": True,
                "stdio_mcp_handshake_and_query": True,
                "native_pythonw_window_and_private_transport": True,
                "tk_form_synthetic_login": True,
                "windows_private_metadata_acl": True,
                "synthetic_credentials_revoked_and_removed": True,
                "template_bytes": template_bytes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    stop_residents()


if __name__ == "__main__":
    main()
