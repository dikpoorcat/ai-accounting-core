"""Real CLI processes and MCP stdio consume one isolated native service."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from dashboard_model_fixture import derived_connection, model_current, produce
from entity_fixture import seed_registration_entities
from material_fixture import supporting_text
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from opening_fixture import adopt_empty_opening
from test_banking import funding, opening
from test_dashboard_model_transactions import raw_read, state
from test_integrity_content import damage
from test_resident_service import PASSWORD
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel import dashboard_model_reads
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.security.credentials import WindowsCredentialStore
from ai_accounting.kernel.security.service import credential_target
from ai_accounting.kernel.security.windows import write_protected_json

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Real Windows credential boundary")
resident = resident_fixture
MONTH = "2026-09"
AMOUNT = 9007199254740993
WORKSPACE = Path(__file__).resolve().parents[2]
PAGES = {
    "brief": "dashboard_brief",
    "funds": "dashboard_funds",
    "employees": "dashboard_employees",
    "assets": "dashboard_assets",
    "reports": "dashboard_quarterly_report",
}


def page_payload(company, page, period=MONTH):
    payload = {"company_id": company["id"]}
    if page == "reports":
        year, month = map(int, period.split("-"))
        payload.update(year=year, quarter=(month - 1) // 3 + 1)
    else:
        payload["period"] = period
        if page == "funds":
            payload["movement_account_selection"] = "first"
    return payload


def native_pages(engine, period=MONTH):
    dashboard = Dashboard(engine)
    with raw_read():
        return {
            "brief": stable_payload(dashboard.brief(period)),
            "funds": stable_payload(dashboard.funds(period, movement_account_selection="first")),
            "employees": stable_payload(dashboard.employees(period)),
            "assets": stable_payload(dashboard.assets(period)),
            "reports": stable_payload(dashboard.quarterly_report(
                int(period[:4]), (int(period[-2:]) - 1) // 3 + 1)),
        }


def cli_call(root, directory, command, payload):
    request = directory / "cli-request.json"
    request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(WORKSPACE / "src")}
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "ai_accounting.kernel.cli",
         "--root", str(root), "call", command, "--input", str(request)],
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    value = json.loads(result.stdout)
    assert result.returncode == int(value.get("status") == "rejected")
    return value


def stdio_parameters(root):
    return StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", str(root), "mcp"],
        env={"PYTHONPATH": str(WORKSPACE / "src")},
    )


async def mcp_call(session, command, payload):
    response = await session.call_tool(
        "finance_local_command", {"command": command, "payload": payload})
    return mcp_payload(response)


def mcp_payload(response):
    assert not response.isError
    assert len(response.content) == 1 and response.content[0].type == "text"
    value = json.loads(response.content[0].text)
    assert isinstance(value, dict)
    if response.structuredContent is not None:
        assert response.structuredContent == value
    return value


@pytest.fixture
def transport_book(resident, tmp_path, monkeypatch):
    service, server, capability, _, _ = resident
    root = service.catalog.root
    assert root.is_relative_to(tmp_path)
    assert server.server_port != 5173
    company = service.catalog.create_company("91310000123456789A", "合成stdio读取企业")
    engine = service.engine(company["id"])
    adopt_empty_opening(engine, period="2026-08")
    proof = engine.register_evidence(
        b"synthetic real transport evidence", "text/plain", "fixture", request_id="transport-proof"
    )["digest"]
    supporting_text(engine, proof)
    sequence = 0

    def save(kind, subject, fields):
        nonlocal sequence
        sequence += 1
        seed_registration_entities(engine, kind, fields)
        return engine.save_fact(kind, subject, fields, evidence=(proof,), expected_revision=0,
                                request_id=f"transport-save-{sequence}")

    def publish(subject):
        preview = engine.preview([subject])
        return engine.confirm([subject], preview_digest=preview["digest"],
                              epochs=preview["epochs"], request_id=f"transport-publish-{sequence}")

    opening(save, publish, month="2026-08")
    funding(save, publish, subject="hidden-history", amount=9, day="2026-08-01")
    funding(save, publish, subject="shown-current", amount=AMOUNT)
    for period in ("2026-08", MONTH):
        produce(engine, period)
    with derived_connection(engine) as connection:
        assert {"2026-08", MONTH} <= {row[0] for row in connection.execute(
            "SELECT period FROM dashboard_read_model_current"
        )}
    expected = native_pages(engine)
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
    observed = []
    actual_page = dashboard_model_reads.read_page
    actual_quarter = dashboard_model_reads.read_quarterly_report

    def page(*args, **kwargs):
        value = actual_page(*args, **kwargs)
        observed.append((args[2], value is not None))
        return value

    def quarter(*args, **kwargs):
        value = actual_quarter(*args, **kwargs)
        observed.append(("reports", value is not None))
        return value

    monkeypatch.setattr(dashboard_model_reads, "read_page", page)
    monkeypatch.setattr(dashboard_model_reads, "read_quarterly_report", quarter)
    token = service.security.login("owner", PASSWORD).session_token
    credentials = WindowsCredentialStore(target_name=credential_target(
        root / "catalog.sqlite", service.security.catalog_instance_id))
    # A newly created catalog derives a fresh key. Never replace another key.
    assert credentials.load_session_token() is None
    try:
        credentials.save_session_token(token)
        write_protected_json(root / ".service.json", {
            "protocol": 2, "database_format": service.catalog.database_format(),
            "pid": os.getpid(), "port": server.server_port, "capability": capability,
            "catalog_id": service.security.catalog_instance_id, "build_id": server.build_id,
        })
        yield service, engine, company, root, expected, observed, save, publish
    finally:
        credentials.delete_session_token()
        service.close()


def test_real_cli_and_mcp_stdio_schema_and_five_registered_model_pages(transport_book, tmp_path):
    service, engine, company, root, expected, observed, _, _ = transport_book
    before = state(engine)
    selection = {"view": "selected", "commands": list(PAGES.values()),
                 "response_types": list(PAGES.values())}
    schema = service.dispatch("schema", selection)
    assert cli_call(root, tmp_path, "schema", selection) == schema

    async def scenario():
        async with stdio_client(stdio_parameters(root)) as streams:
            async with ClientSession(
                *streams, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "finance_local_schema", "finance_local_security", "finance_local_command"}
                overview = await session.call_tool("finance_local_schema", {})
                assert mcp_payload(overview) == service.dispatch("schema", {})
                assert "agent_operating_protocol" in mcp_payload(overview)
                response = await session.call_tool("finance_local_schema", selection)
                assert mcp_payload(response) == schema
                assert set(schema["command_schemas"]) == set(PAGES.values())
                for page, command in PAGES.items():
                    payload = page_payload(company, page)
                    for call in ("cli", "mcp"):
                        observed.clear()
                        value = (cli_call(root, tmp_path, command, payload) if call == "cli"
                                 else await mcp_call(session, command, payload))
                        assert value["state"] == "ready", value
                        assert stable_payload(value["response"]) == expected[page]
                        assert (page, True) in observed
                        if page == "brief":
                            amount = value["response"]["data"]["funds_overview"]["bank_fen"]
                            assert type(amount) is int and amount == AMOUNT + 9
                invalid = {"company_id": company["id"], "period": "bad"}
                for value in (
                    cli_call(root, tmp_path, PAGES["brief"], invalid),
                    await mcp_call(session, PAGES["brief"], invalid),
                ):
                    assert value.get("status") == "rejected", value
                    assert value["code"] == "invalid_command"
                assert state(engine) == before

    asyncio.run(asyncio.wait_for(scenario(), timeout=180))


def test_real_transports_prepare_without_business_writes_and_keep_damage_rejection(
    transport_book, tmp_path,
):
    _, engine, company, root, _, observed, save, publish = transport_book
    funding(save, publish, subject="unproduced-month", amount=1, day="2026-10-01")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM dashboard_projection_current WHERE period='2026-10'"
        ).fetchone() is None

    def authority():
        with engine.store.connection(read_only=True) as connection:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ) if not row[0].startswith(("sqlite_", "dashboard_"))
                and row[0] not in {"audit", "request", "operation_timing"}]
            data = {name: tuple(tuple(row) for row in connection.execute(
                f'SELECT * FROM "{name}" ORDER BY rowid'
            )) for name in names}
            data["business_audit"] = tuple(tuple(row) for row in connection.execute(
                "SELECT * FROM audit WHERE action NOT LIKE 'dashboard_projection_%' ORDER BY id"
            ))
            data["business_requests"] = tuple(tuple(row) for row in connection.execute(
                "SELECT * FROM request WHERE id IN(SELECT request_id FROM audit "
                "WHERE action NOT LIKE 'dashboard_projection_%') ORDER BY id"
            ))
            return data

    async def pages(session, period, *, ready):
        expected = native_pages(engine, period) if ready else None
        before = authority()
        for page, command in PAGES.items():
            payload = page_payload(company, page, period)
            for call in ("cli", "mcp"):
                observed.clear()
                value = (cli_call(root, tmp_path, command, payload) if call == "cli"
                         else await mcp_call(session, command, payload))
                if ready:
                    assert value["state"] == "ready", value
                    assert stable_payload(value["response"]) == expected[page]
                    assert (page, True) in observed
                else:
                    assert value["state"] == "preparing", value
                    assert "response" not in value and value["model_version"] is None
                    assert observed == []
        assert authority() == before

    async def scenario():
        async with stdio_client(stdio_parameters(root)) as streams:
            async with ClientSession(
                *streams, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                await pages(session, "2026-10", ready=False)
                produce(engine, "2026-10")
                await pages(session, "2026-10", ready=True)
                Entities(engine).update_entity_profile(
                    "bank-a", {"display_name": "明确变更后的合成账户"},
                    source="synthetic explicit profile update", expected_revision=1,
                    request_id="source-profile-change",
                )
                await pages(session, MONTH, ready=False)
                produce(engine, MONTH)
                await pages(session, MONTH, ready=True)
                payload = page_payload(company, "brief")
                observed.clear()
                shown = cli_call(root, tmp_path, PAGES["brief"], payload)
                assert shown["state"] == "ready" and ("brief", True) in observed
                damage(engine, "fact_funding", "UPDATE fact_funding SET amount_fen=10 WHERE "
                       "revision_id=(SELECT fact_id FROM fact_current "
                       "WHERE subject_id='hidden-history')")
                before = state(engine)
                for call in ("cli", "mcp"):
                    value = (cli_call(root, tmp_path, PAGES["brief"], payload) if call == "cli"
                             else await mcp_call(session, PAGES["brief"], payload))
                    assert value["state"] == "ready", value
                    assert stable_payload(value["response"]) == stable_payload(shown["response"])
                    rejected = (cli_call(
                        root, tmp_path, "verify_integrity", {"company_id": company["id"]})
                                if call == "cli" else await mcp_call(
                                    session, "verify_integrity", {"company_id": company["id"]}))
                    assert rejected["status"] == "rejected"
                    assert rejected["code"] == "content_integrity_failed"
                assert state(engine) == before
                _, descriptor = model_current(engine, MONTH)
                with derived_connection(engine, read_only=False) as connection:
                    connection.execute(
                        "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
                        (bytes.fromhex(descriptor["root_digest"]),),
                    )
                before = state(engine)
                for call in ("cli", "mcp"):
                    failed = (cli_call(root, tmp_path, PAGES["brief"], payload) if call == "cli"
                              else await mcp_call(session, PAGES["brief"], payload))
                    assert failed["state"] == "failed" and failed["reason"] == "corrupt"
                    assert "response" not in failed and failed["model_version"] is None
                assert state(engine) == before

    asyncio.run(asyncio.wait_for(scenario(), timeout=240))
