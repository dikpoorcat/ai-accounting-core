"""CLI and MCP deliver one resident-owned report without opening a browser."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openpyxl import load_workbook

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host import _desktop_export
from ai_accounting_modular.host.runtime._client import Client

from .conftest import business_request, native_login


def command(runtime):
    return [
        sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "ai_accounting_modular.host.runtime.cli",
        "--catalog",
        str(runtime.catalog_path),
    ]


def generate_via_cli(runtime, payload, request_path):
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        process = subprocess.run(
            [*command(runtime), "call", "accounting-report-export", "--input", str(request_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        result = json.loads(process.stdout)
        if result["state"] != "preparing":
            assert result["state"] in {"pending", "running", "succeeded"}, result
            return result
        time.sleep(0.05)
    pytest.fail("合成报表的读取准备未在期限内完成")


def assert_workbook(path, company_name):
    with path.open("rb") as stream:
        book = load_workbook(stream, data_only=True)
        try:
            assert book.worksheets[0]["H3"].value == company_name
            assert book.worksheets[1]["D19"].value == 1000
            assert book.worksheets[1]["D37"].value == -1000
        finally:
            book.close()


@pytest.mark.skipif(sys.platform != "win32", reason="真实桌面交付仅支持 Windows")
@pytest.mark.parametrize(
    "company_name,file_prefix",
    [
        pytest.param("合成运行公司", "合成运行公司", id="ordinary"),
        pytest.param("CON.技术公司", "_CON.技术公司", id="reserved-con"),
        pytest.param("COM².技术公司", "_COM².技术公司", id="reserved-superscript-com"),
    ],
)
def test_cli_generation_and_mcp_delivery_share_verified_task_without_business_writes(
    local, tmp_path, monkeypatch, company_name, file_prefix
):
    runtime, company, _ = local
    with closing(sqlite3.connect(company.database_path)) as connection:
        connection.execute(
            "UPDATE company_profile SET legal_name=?,profile_seq=profile_seq+1", (company_name,)
        )
        connection.commit()
    with runtime.kernel.storage.read_unit(company.company_id) as read:
        company = runtime.kernel.storage.company_metadata(read)
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    monkeypatch.setattr(_desktop_export, "desktop_directory", lambda: desktop)
    submitted = business_request(company.company_id)
    expense = submitted["mutations"][1]["fact"]
    expense["data"]["report_details"] = [{"code": "management_other", "amount_fen": "100000"}]
    app = runtime.kernel.application
    assert app.accounting_apply(submitted)["completion"] == "complete"
    facts_request = {
        "company_id": company.company_id,
        "business_ids": [item["fact"]["business_id"] for item in submitted["mutations"]],
        "include": ["facts", "results", "sources"],
    }
    before = app.accounting_get(facts_request)["businesses"]
    with runtime.kernel.storage.read_unit(company.company_id) as read:
        head = read.head
    runtime.start()
    native_login(runtime)
    payload = {
        "action": "generate",
        "company_id": company.company_id,
        "year": 2026,
        "quarter": 1,
    }
    request_path = tmp_path / "report-request.json"
    generated = generate_via_cli(runtime, payload, request_path)
    assert generated["company_id"] == company.company_id
    assert (generated["year"], generated["quarter"]) == (2026, 1)
    task_id = generated["task"]["task_id"]

    async def check_mcp():
        params = StdioServerParameters(command=sys.executable, args=command(runtime)[1:] + ["mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            contracts = json.loads(
                (await session.read_resource("accounting://contracts")).contents[0].text
            )
            assert "accounting_report_export" in {item["command"] for item in contracts["commands"]}

            async def call(name, body):
                response = await session.call_tool(
                    "accounting_command", {"command": name, "payload": body}
                )
                assert not response.isError, response
                return response.structuredContent

            for _ in range(200):
                finished = await call(
                    "accounting_jobs", {"company_id": company.company_id, "task_id": task_id}
                )
                if finished["state"] not in {"pending", "running"}:
                    break
                await asyncio.sleep(0.05)
            assert finished["state"] == "succeeded", finished
            assert finished["task_id"] == task_id
            assert finished["company_id"] == company.company_id
            assert finished["kind"] == "report_export"
            save = {
                "action": "save",
                "company_id": company.company_id,
                "task_id": task_id,
                "destination": "desktop",
            }
            saved = await call("accounting_report_export", save)
            assert saved["state"] == "copied", saved
            assert saved["company_id"] == finished["company_id"]
            assert saved["database_id"] == company.database_id
            assert saved["task_id"] == finished["task_id"]
            output = Path(saved["path"])
            assert output.parent == desktop
            assert output.name == (
                f"{file_prefix}_{company.credit_code}_2026年第1季度财务报表.xlsx"
            )
            raw = output.read_bytes()
            assert len(raw) == saved["byte_size"] == finished["byte_size"]
            assert sha256(raw).hexdigest() == saved["sha256"] == finished["sha256"]
            assert_workbook(output, company.legal_name)
            again = await asyncio.to_thread(generate_via_cli, runtime, payload, request_path)
            assert again["task"]["task_id"] == task_id
            repeated = await call("accounting_report_export", save)
            assert repeated == saved | {"state": "reused"}
            assert list(desktop.iterdir()) == [output]

    asyncio.run(check_mcp())
    assert app.accounting_get(facts_request)["businesses"] == before
    with runtime.kernel.storage.read_unit(company.company_id) as read:
        assert read.head == head


def test_generate_and_save_require_native_owner_login(local, tmp_path, monkeypatch):
    runtime, company, _ = local
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    monkeypatch.setattr(_desktop_export, "desktop_directory", lambda: desktop)
    runtime.start()
    client = Client(runtime.catalog_path, autostart=False)
    for payload in (
        {"action": "generate", "company_id": company.company_id, "year": 2026, "quarter": 1},
        {
            "action": "save",
            "company_id": company.company_id,
            "task_id": str(uuid4()),
            "destination": "desktop",
        },
    ):
        with pytest.raises(ContractError) as rejected:
            client.dispatch("accounting_report_export", payload)
        assert rejected.value.code == "owner_session_required"
    assert not list(desktop.iterdir())
    with runtime.kernel.storage.read_unit(company.company_id) as read:
        assert read.head == 0
