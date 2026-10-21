"""Restart resumes real owner targets and captured files without applying business twice."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import CompanySpec

from .conftest import business_request


def eventually(check, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("合成后台没有在期限内接续当前目标")


def test_restart_finishes_documents_and_reading_without_reapplying_business(local, monkeypatch):
    runtime, company, _ = local
    app = runtime.kernel.application
    request = business_request(company.company_id)

    def unavailable(_):
        raise OSError("synthetic document write unavailable")

    monkeypatch.setattr(runtime.kernel.records, "prepare_documents", unavailable)
    saved = app.accounting_apply(request)
    assert saved["completion"] == "saved_documents_pending"
    query = {
        "company_id": company.company_id,
        "year": "2026",
        "quarter": "1",
        "preparation": "deferred",
    }
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "preparing"
    runtime.close()
    with create_runtime(runtime.catalog_path, window_opener=lambda _: None) as restarted:
        eventually(
            lambda: (
                restarted.kernel.application.accounting_documents(
                    {
                        "company_id": company.company_id,
                        "action": "status",
                    }
                )["state"]
                == "ready"
            )
        )

        def ready():
            with restarted.kernel.dashboard.get(
                "/api/dashboard/quarterly-report", query
            ) as response:
                return response.body if response.body["state"] == "ready" else None

        page = eventually(ready)
        assert page["response"]["summary"]["current_net_profit_fen"] == "-100000"
        current = restarted.kernel.application.accounting_get(
            {
                "company_id": company.company_id,
                "business_ids": [request["mutations"][1]["fact"]["business_id"]],
                "include": ["facts"],
            }
        )
        assert current["businesses"][0]["state"]["seq"] == 1
        assert not restarted.security.status(local=True)["authenticated"]


def test_restart_recovers_captured_backup_and_delivers_verified_zip(local):
    runtime, company, _ = local
    app = runtime.kernel.application
    assert app.accounting_apply(business_request(company.company_id))["completion"] == "complete"
    receipt = app.accounting_backup(
        {"company_id": company.company_id, "expected_database_id": company.database_id}
    )
    assert receipt["state"] == "pending"
    runtime.close()
    with create_runtime(runtime.catalog_path, window_opener=lambda _: None) as restarted:

        def complete():
            value = restarted.kernel.files.get(company.company_id, receipt["task_id"])
            assert value["state"] != "failed"
            return value if value["state"] == "succeeded" else None

        result = eventually(complete)
        assert Path(result["path"]).is_file()
        assert result["path"].endswith(".zip")
        with restarted.kernel.files.verified_download(
            company.company_id, result["task_id"]
        ) as download:
            assert download.stream.read(2) == b"PK"
        manifest = restarted.kernel.application.accounting_documents(
            {
                "company_id": company.company_id,
                "action": "status",
            }
        )["manifest_path"]
    # Offline operation has no catalog at the original position and no running source resident.
    runtime.catalog_path.rename(runtime.catalog_path.with_suffix(".preserved"))
    for action, source in (("restore", result["path"]), ("rebuild", manifest)):
        target = runtime.catalog_path.parent / ("new-" + action)
        request_path = runtime.catalog_path.parent / (action + ".json")
        request_path.write_text(
            json.dumps(
                {
                    "action": action,
                    "source_path": source,
                    "new_target_directory": str(target),
                    "mode": "check",
                }
            ),
            encoding="utf-8",
        )
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting_modular.host.runtime.cli",
                action,
                "--input",
                str(request_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert process.returncode == 0
        assert json.loads(process.stdout)["state"] == "checked"
        assert not target.exists()

    async def offline_mcp():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting_modular.host.runtime.cli",
                "mcp",
            ],
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {item.name for item in tools.tools} == {
                "accounting_restore",
                "accounting_rebuild",
            }
            contracts = await session.read_resource("accounting://contracts")
            assert {
                item["command"] for item in json.loads(contracts.contents[0].text)["commands"]
            } == {"accounting_restore", "accounting_rebuild"}
            for action, source in (("restore", result["path"]), ("rebuild", manifest)):
                target = runtime.catalog_path.parent / ("mcp-new-" + action)
                value = await session.call_tool(
                    "accounting_" + action,
                    {
                        "payload": {
                            "action": action,
                            "source_path": source,
                            "new_target_directory": str(target),
                            "mode": "check",
                        }
                    },
                )
                assert value.structuredContent["state"] == "checked"
                assert not target.exists()

    asyncio.run(offline_mcp())


def test_document_failure_does_not_hold_independent_reading_target(local, monkeypatch):
    runtime, company, _ = local
    assert (
        runtime.kernel.application.accounting_apply(business_request(company.company_id))[
            "completion"
        ]
        == "complete"
    )
    query = {
        "company_id": company.company_id,
        "year": "2026",
        "quarter": "1",
        "preparation": "deferred",
    }
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "preparing"

    def bad_documents(_):
        raise OSError("synthetic document failure")

    monkeypatch.setattr(runtime.kernel.application, "accounting_documents", bad_documents)
    runtime.background.cycle()
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "ready"


def test_one_company_failure_does_not_stop_another_company_target(local, monkeypatch):
    runtime, company, _ = local
    bad = str(uuid4())
    runtime.kernel.storage.create_company(
        CompanySpec(
            bad,
            str(uuid4()),
            "913100000000000002",
            "AAA合成失败公司",
            "2026-01",
            runtime.catalog_path.parent / "second-company",
        )
    )
    assert (
        runtime.kernel.application.accounting_apply(business_request(company.company_id))[
            "completion"
        ]
        == "complete"
    )
    query = {
        "company_id": company.company_id,
        "year": "2026",
        "quarter": "1",
        "preparation": "deferred",
    }
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "preparing"
    original = runtime.kernel.application.accounting_documents
    failures = []

    def documents(request):
        if request["company_id"] == bad:
            failures.append(bad)
            raise OSError("synthetic isolated company failure")
        return original(request)

    monkeypatch.setattr(runtime.kernel.application, "accounting_documents", documents)
    runtime.background.cycle()
    assert failures
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "ready"
