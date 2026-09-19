"""Verify a relocated software bundle using only its isolated interpreter.

All business data below is synthetic. Most is written beside the relocated
package; the default-launcher check writes only after the software-only ZIP has
already been generated.
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import hashlib
import http.client
import importlib
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
_PRIVATE_NATIVE = {}


def assert_current_formats(app, company_id):
    """Check installed contracts and real synthetic database formats together."""
    from ai_accounting.kernel.runtime import connect
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.versions import database_format

    bundle = production_bundle()
    expected = {
        kind: {
            "family": contract["family"],
            "kind": contract["kind"],
            "status": contract["status"],
            "version": contract["version"],
            "fingerprint": contract["sha256"],
        }
        for kind in ("catalog", "company")
        for contract in (bundle.current(kind),)
    }
    with app.engine(company_id).store.connection(read_only=True) as connection:
        assert database_format(connection, bundle=bundle, kind="company") == expected["company"]
    connection = connect(app.catalog.path, read_only=True)
    try:
        assert database_format(connection, bundle=bundle, kind="catalog") == expected["catalog"]
    finally:
        connection.close()
    return expected


def business_contract(value):
    """Exclude file-task activity when comparing a backup and its restored source."""
    assert value["read_semantics"]["knowledge"] == "current_knowledge"
    assert value["review"]["status"] == "current"
    obligations = value["settlements"]["obligations"]
    assert len(obligations) == 1
    assert type(obligations[0]["source_amount_fen"]) is int
    assert obligations[0]["source_amount_fen"] == obligations[0]["remaining_fen"] == 123456
    assert obligations[0]["settlement_status"] == "open"
    return {
        key: value[key]
        for key in ("identity", "period", "as_of", "selected_accounting", "settlements")
    }


def readiness_contract(value):
    assert value["as_of_semantics"] == "current_knowledge"
    assert value["closure"]["state"] == "open"
    assert value["current_followups"]["affects_frozen_readiness"] is False
    return {
        key: value[key]
        for key in ("company_id", "database_id", "period", "as_of", "closure", "readiness")
    }


def start_resident(root, *, native_smoke=False):
    """Exercise the exact daemon components using exclusively synthetic owners."""
    from pydantic import SecretStr

    from ai_accounting.kernel.daemon import build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.jobs import JobRunner
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
    controller = build_native_security_controller(
        app, server, capability, window_opener=lambda request_id: None
    )
    app.security_controller = controller
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
    _PRIVATE_NATIVE[root] = (client, password)

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
    _PRIVATE_NATIVE.clear()


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
    required_modules = (
        "business_queries",
        "query_reads",
        "dashboard_reads",
        "read_indexes",
        "versions",
    )
    for name in required_modules:
        module = importlib.import_module("ai_accounting.kernel." + name)
        assert Path(module.__file__).resolve().is_relative_to(package)
        assert "kernel/" + name + ".py" in manifest["application_modules"]
    contracts = package / "app/ai_accounting/kernel/schema_contracts"
    assert sorted(
        path.relative_to(contracts).as_posix() for path in contracts.rglob("*.json")
    ) == ["catalog/draft.json", "company/draft.json"]
    assert not (package / "app/ai_accounting/kernel/migrations").exists()
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

    def wait_for_backup(company_id, queued, *, root=data_root):
        assert queued["status"] == "pending"
        deadline = time.monotonic() + 30
        while True:
            jobs = call("jobs", {"company_id": company_id}, root=root)
            backup_job = next(row for row in jobs if row["id"] == queued["job_id"])
            if backup_job["status"] == "succeeded":
                return backup_job["result"]
            assert backup_job["status"] != "failed", "Automatic backup failed"
            assert time.monotonic() < deadline, "Automatic backup did not complete"
            time.sleep(0.1)

    def approve_close(company, period, preview, *, root=data_root):
        app = _SERVICES[root][0]
        client, password = _PRIVATE_NATIVE[root]
        request = app.security_controller.request(
            kind="approve_period_close",
            company_id=company["id"],
            database_id=company["database_id"],
            period=period,
            calculation_hash=preview["digest"],
            epochs=preview["epochs"],
        )
        approved = client.call("native_execute", request["request_id"], password=password)
        client.call("native_update", request["request_id"], status="succeeded")
        assert app.security_controller.status(request["request_id"])["status"] == "succeeded"
        return approved["approval_id"]

    schema = call("schema", {})
    assert {"expense", "cash_payment", "cash_funding", "labor"} <= schema["facts"].keys()
    assert {"business_status", "period_readiness"} <= schema["command_schemas"].keys()
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
    business_request = {
        **overview_request,
        "subject_id": "synthetic-expense",
        "as_of": "2026-09-30",
    }
    readiness_request = {**overview_request, "as_of": "2026-09-30"}
    business = business_contract(call("business_status", business_request))
    readiness = readiness_contract(call("period_readiness", readiness_request))
    database_formats = assert_current_formats(_SERVICES[data_root][0], company_id)
    assert manifest["runtime"]["database_formats"] == database_formats
    queued = call(
        "backup",
        {
            "company_id": company_id,
            "directory": str(validation / "backups"),
            "request_id": "package-backup",
        },
    )
    backup = wait_for_backup(company_id, queued)
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
    assert assert_current_formats(_SERVICES[restored_root][0], company_id) == database_formats
    restored_business = call("business_status", business_request, root=restored_root)
    restored_readiness = call("period_readiness", readiness_request, root=restored_root)
    assert business_contract(restored_business) == business
    assert readiness_contract(restored_readiness) == readiness

    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES

    close_company = call(
        "create_company", {"taxpayer_id": "91310000123456789B", "name": "运行包合成关账企业"}
    )
    close_company_id = close_company["id"]
    close_period = "2026-08"
    close_proof = call(
        "evidence",
        {
            "company_id": close_company_id,
            "content_base64": base64.b64encode(
                b"Explicit synthetic no-business close confirmation"
            ).decode("ascii"),
            "media_type": "text/plain",
            "name": "合成无业务关账确认",
            "request_id": "package-close-evidence",
        },
    )["digest"]
    for category in MATERIAL_CATEGORIES:
        call(
            "inventory",
            {
                "company_id": close_company_id,
                "period": close_period,
                "category": category,
                "evidence": [],
                "expected": 0,
                "no_business": True,
                "confirmation_evidence": close_proof,
                "request_id": f"package-close-inventory-{category}",
            },
        )
    close_preview = call(
        "preview_close",
        {
            "company_id": close_company_id,
            "period": close_period,
            "owner_confirmation": close_proof,
        },
    )
    approval_id = approve_close(close_company, close_period, close_preview)
    closed = call(
        "close",
        {
            "company_id": close_company_id,
            "period": close_period,
            "owner_confirmation": close_proof,
            "preview_digest": close_preview["digest"],
            "epochs": close_preview["epochs"],
            "approval_id": approval_id,
            "request_id": "package-close",
            "backup_directory": str(validation / "closed-backups"),
        },
    )
    assert closed["status"] == "closed" and closed["backup_job"]
    frozen_close = call(
        "closed_report", {"company_id": close_company_id, "period": close_period}
    )
    close_backup = wait_for_backup(
        close_company_id, {"status": "pending", "job_id": closed["backup_job"]}
    )
    close_restored_root = validation / "closed-restored"
    close_restored = call(
        "restore_company",
        {
            "archive": close_backup["path"],
            "taxpayer_id": close_company["taxpayer_id"],
            "name": "运行包合成关账恢复企业",
        },
        root=close_restored_root,
    )
    assert close_restored["id"] == close_company_id
    assert close_restored["database_id"] == close_company["database_id"]
    assert (
        call(
            "closed_report",
            {"company_id": close_company_id, "period": close_period},
            root=close_restored_root,
        )
        == frozen_close
    )

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
        assert {item["id"] for item in json.loads(result.stdout)} == {
            company_id, close_company_id,
        }

    default_environment = {
        key: value for key, value in os.environ.items() if key != "FINANCE_DATA_ROOT"
    }
    default_root = package / "data/kernel-draft"
    try:
        result = subprocess.run(
            [str(package / "finance-local.cmd"), "call", "schema"],
            cwd=package,
            env=default_environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert json.loads(result.stdout)["database_formats"] == database_formats
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(package / "finance-local.ps1"),
                "service-info",
            ],
            cwd=package,
            env=default_environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        default_metadata = json.loads(result.stdout)
        assert default_metadata["protocol"] == 2
        assert default_metadata["database_format"] == database_formats["catalog"]
    finally:
        subprocess.run(
            [str(package / "finance-local.cmd"), "stop"],
            cwd=package,
            env=default_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    assert (default_root / "catalog.sqlite").is_file()

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
                    for command, payload, extract, expected in (
                        ("business_status", business_request, business_contract, business),
                        ("period_readiness", readiness_request, readiness_contract, readiness),
                    ):
                        result = await session.call_tool(
                            "finance_local_command", {"command": command, "payload": payload}
                        )
                        assert not result.isError
                        value = result.structuredContent
                        if value is None:
                            value = json.loads("".join(item.text for item in result.content))
                        assert extract(value) == expected

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
        for action in (
            "context",
            "brief",
            "funds",
            "employees",
            "assets",
            "quarterly-report",
            "business-status",
        ):
            query = f"company_id={company_id}"
            query += (
                "&year=2026&quarter=3"
                if action == "quarterly-report"
                else ("" if action == "context" else "&period=2026-09")
            )
            if action == "business-status":
                query += "&subject_id=synthetic-expense&as_of=2026-09-30&limit=1"
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request(
                "GET",
                f"/api/dashboard/{action}?{query}",
                headers={"Authorization": "Bearer " + token},
            )
            response = connection.getresponse()
            wire_dashboard = json.loads(response.read())
            assert response.status == 200, (action, wire_dashboard)
            assert wire_dashboard["schema_version"] == (
                1 if action in {"quarterly-report", "business-status"} else 2
            )
            if action == "context":
                assert wire_dashboard["current_company"]["company_id"] == company_id
            elif action == "brief":
                assert isinstance(wire_dashboard["data"]["total_debit_fen"], str)
                assert wire_dashboard["data"]["total_debit_fen"] == "123456"
            if action in {"brief", "funds", "employees", "assets"}:
                assert wire_dashboard["data"]["period_preparation"]["projection"] == (
                    "dashboard_period_preparation"
                )
            if action == "business-status":
                assert wire_dashboard["data"]["identity"] == business["identity"]
                obligation = wire_dashboard["data"]["settlements"]["obligations"][0]
                assert obligation["source_amount_fen"] == obligation["remaining_fen"] == "123456"
            for collection in wire_dashboard.get("data", {}).get("collections", {}).values():
                page = collection["page"]
                assert len(collection["items"]) == page["returned_count"]
                assert page["returned_count"] <= page["filtered_count"] <= page["total_count"]
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
    stop_residents()
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
                "native_approved_close_backup_restored_and_frozen": True,
                "relative_imports_only": True,
                "http_page_and_authenticated_api": True,
                "dashboard_five_routes_and_legacy_entries": True,
                "dashboard_seven_authenticated_queries": True,
                "database_formats": database_formats,
                "cli_mcp_business_status_and_period_readiness": True,
                "dashboard_current_schemas_and_collections": True,
                "dashboard_integer_cent_strings": True,
                "relative_cmd_and_powershell_launchers": True,
                "launchers_default_to_packaged_draft_root": True,
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


if __name__ == "__main__":
    main()
