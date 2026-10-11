"""Real CLI processes and MCP stdio consume one isolated native service."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from entity_fixture import seed_registration_entities
from material_fixture import supporting_text
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_banking import funding, opening
from test_dashboard_model_integrity import current_content
from test_dashboard_model_transactions import raw_read, state
from test_integrity_content import damage
from test_resident_service import PASSWORD
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel import dashboard_model_reads, dashboard_model_runtime
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
    with engine.store.connection(read_only=True) as connection:
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
                        assert stable_payload(value) == expected[page]
                        assert (page, True) in observed
                        if page == "brief":
                            amount = value["data"]["funds_overview"]["bank_fen"]
                            assert type(amount) is int and amount == AMOUNT + 9
                invalid = {"company_id": company["id"], "period": "bad"}
                for value in (
                    cli_call(root, tmp_path, PAGES["brief"], invalid),
                    await mcp_call(session, PAGES["brief"], invalid),
                ):
                    assert value["status"] == "rejected" and value["code"] == "invalid_command"
                assert state(engine) == before

    asyncio.run(asyncio.wait_for(scenario(), timeout=180))


def test_real_transports_fallback_readonly_and_keep_independent_damage_rejection(
    transport_book, tmp_path, monkeypatch
):
    _, engine, company, root, _, observed, save, publish = transport_book

    # A legitimate earlier producer writes an ordinary typed fact in a new
    # month without creating a model. A nonexistent month is not a page.
    with monkeypatch.context() as earlier_writer:
        earlier_writer.setattr(
            dashboard_model_runtime, "supports_native_production", lambda _: False)
        funding(save, publish, subject="unregistered-month", amount=1, day="2026-10-01")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM dashboard_read_model_current WHERE period='2026-10'"
        ).fetchone() is None

    async def pages(session, period, *, modeled):
        expected = native_pages(engine, period)
        before = state(engine)
        for page, command in PAGES.items():
            payload = page_payload(company, page, period)
            for call in ("cli", "mcp"):
                observed.clear()
                value = (cli_call(root, tmp_path, command, payload) if call == "cli"
                         else await mcp_call(session, command, payload))
                assert stable_payload(value) == expected[page]
                assert (page, modeled) in observed
                if not modeled:
                    assert not any(used for _, used in observed)
        assert state(engine) == before

    async def scenario():
        async with stdio_client(stdio_parameters(root)) as streams:
            async with ClientSession(
                *streams, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                await pages(session, "2026-10", modeled=False)
                # A typed write by an earlier producer leaves an authentic,
                # stale model. Readers must use native data without repairing it.
                with monkeypatch.context() as earlier_writer:
                    earlier_writer.setattr(
                        dashboard_model_runtime, "supports_native_production", lambda _: False)
                    Entities(engine).update_entity_profile(
                        "bank-a", {"display_name": "明确变更后的合成账户"},
                        source="synthetic explicit profile update", expected_revision=1,
                        request_id="earlier-producer-profile")
                await pages(session, MONTH, modeled=False)
                Maintenance(engine).rebuild_dashboard_read_model(
                    MONTH, request_id="current-producer-rebuild")
                observed.clear()
                shown = cli_call(root, tmp_path, PAGES["brief"], page_payload(company, "brief"))
                assert ("brief", True) in observed
                damage(engine, "fact_funding", "UPDATE fact_funding SET amount_fen=10 WHERE "
                       "revision_id=(SELECT fact_id FROM fact_current "
                       "WHERE subject_id='hidden-history')")
                before = state(engine)
                payload = page_payload(company, "brief")
                for call in ("cli", "mcp"):
                    value = (cli_call(root, tmp_path, PAGES["brief"], payload) if call == "cli"
                             else await mcp_call(session, PAGES["brief"], payload))
                    assert stable_payload(value) == stable_payload(shown)
                    rejected = (cli_call(
                        root, tmp_path, "verify_integrity", {"company_id": company["id"]})
                                if call == "cli" else await mcp_call(
                                    session, "verify_integrity", {"company_id": company["id"]}))
                    assert rejected["status"] == "rejected"
                    assert rejected["code"] == "content_integrity_failed"
                assert state(engine) == before
                _, root_digest, _ = current_content(engine)
                damage(engine, "dashboard_read_model_block",
                       "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
                       (root_digest,))
                before = state(engine)
                for call in ("cli", "mcp"):
                    rejected = (cli_call(root, tmp_path, PAGES["brief"], payload) if call == "cli"
                                else await mcp_call(session, PAGES["brief"], payload))
                    assert rejected["status"] == "rejected"
                    assert rejected["code"] == "content_integrity_failed"
                assert state(engine) == before

    asyncio.run(asyncio.wait_for(scenario(), timeout=180))
