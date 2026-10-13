"""Real adapters and self-contained immutable history close/backup/restore."""

import asyncio
import importlib
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
from opening_fixture import adopt_empty_opening
from test_banking import funding, opening
from test_dashboard_model_transactions import state
from test_integrity_content import damage
from test_resident_service import PASSWORD
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel import backup, dashboard_model_reads
from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.dashboard_async import _produce, delivery_state
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.security.credentials import WindowsCredentialStore
from ai_accounting.kernel.security.service import credential_target
from ai_accounting.kernel.security.windows import write_protected_json
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth

resident = resident_fixture
MONTH = "2026-09"
AMOUNT = 9007199254740993
COMPANY_FP = "041fb3c8e4de2cf0bde980a54a061cd66563495c4f06d5f6e6b93978d00dadf8"
MODULES = (
    "cli",
    "mcp",
    "daemon",
    "dashboard_async",
    "dashboard_read_model",
    "dashboard_delivery",
    "response_contracts",
    "source24_history",
    "backup",
    "maintenance",
)
PAGES = ("brief", "funds", "employees", "assets", "quarterly_report")


def source_origins():
    source = Path(backup.__file__).resolve().parents[2]
    origins = {}
    for name in MODULES:
        path = Path(importlib.import_module("ai_accounting.kernel." + name).__file__).resolve()
        assert path.is_relative_to(source)
        origins[name] = str(path)
    return source, origins


CHILD = """
import importlib, json, os, pathlib, sys
source = pathlib.Path(os.environ['P1_ADAPTER_SRC']).resolve()
expected = json.loads(os.environ['P1_ADAPTER_ORIGINS'])
actual = {}
for name in expected:
    path = pathlib.Path(importlib.import_module('ai_accounting.kernel.' + name).__file__).resolve()
    assert path.is_relative_to(source), (name, path)
    actual[name] = str(path)
assert actual == expected
pathlib.Path(sys.argv[1]).write_text(json.dumps(actual), encoding='utf-8')
from ai_accounting.kernel import cli
sys.argv = [cli.__file__, *sys.argv[2:]]
cli.main()
"""


def child_environment():
    source, origins = source_origins()
    return {
        **os.environ,
        "PYTHONPATH": str(source),
        "PYTHONUTF8": "1",
        "P1_ADAPTER_SRC": str(source),
        "P1_ADAPTER_ORIGINS": json.dumps(origins),
    }


def cli_call(book, directory, command, payload):
    request = directory / "p1-adapter-cli-request.json"
    provenance = directory / "p1-adapter-cli-provenance.json"
    request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            CHILD,
            str(provenance),
            "--root",
            str(book["root"]),
            "call",
            command,
            "--input",
            str(request),
        ],
        env=child_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    assert json.loads(provenance.read_text(encoding="utf-8")) == source_origins()[1]
    value = json.loads(result.stdout)
    assert result.returncode == int(value.get("status") == "rejected"), result.stderr
    return value


def stdio_parameters(book, directory):
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-X",
            "utf8",
            "-c",
            CHILD,
            str(directory / "p1-adapter-mcp-provenance.json"),
            "--root",
            str(book["root"]),
            "mcp",
        ],
        env=child_environment(),
    )


def mcp_payload(response):
    assert not response.isError
    assert len(response.content) == 1 and response.content[0].type == "text"
    value = json.loads(response.content[0].text)
    assert isinstance(value, dict)
    if response.structuredContent is not None:
        assert response.structuredContent == value
    return value


async def mcp_call(session, command, payload):
    return mcp_payload(
        await session.call_tool(
            "finance_local_command",
            {"command": command, "payload": payload},
        )
    )


def page_payload(book, page):
    payload = {"company_id": book["company"]["id"]}
    payload.update(
        {"year": 2026, "quarter": 3} if page == "quarterly_report" else {"period": MONTH}
    )
    if page == "funds":
        payload["movement_account_selection"] = "first"
    return payload


def comparable(value):
    if value.get("state") == "ready":
        return value | {"response": stable_payload(value["response"])}
    if "read_context" in value:
        return stable_payload(value)
    return value


@pytest.fixture
def p1_transport_book(resident, tmp_path, monkeypatch, record_property):
    service, server, capability, _, _ = resident
    root = service.catalog.root
    assert root.is_relative_to(tmp_path) and server.server_port != 5173
    assert production_bundle().database_format()["fingerprint"] == COMPANY_FP
    company = service.catalog.create_company("91310000123456789A", "合成P1适配器企业")
    engine = service.engine(company["id"])
    adopt_empty_opening(engine, period="2026-08")
    proof = engine.register_evidence(
        b"synthetic P1 transport evidence", "text/plain", "fixture", request_id="p1-transport-proof"
    )["digest"]
    supporting_text(engine, proof)
    sequence = 0

    def save(kind, subject, fields):
        nonlocal sequence
        sequence += 1
        seed_registration_entities(engine, kind, fields)
        return engine.save_fact(
            kind,
            subject,
            fields,
            evidence=(proof,),
            expected_revision=0,
            request_id=f"p1-transport-save-{sequence}",
        )

    def publish(subject):
        preview = engine.preview([subject])
        return engine.confirm(
            [subject],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"p1-transport-publish-{sequence}",
        )

    opening(save, publish, month="2026-08")
    funding(save, publish, subject="p1-history", amount=9, day="2026-08-01")
    funding(save, publish, subject="p1-current", amount=AMOUNT)
    publication = _produce(engine, MONTH)
    assert publication["status"] == "ready"
    assert delivery_state(engine, MONTH)["model_version"] == publication["model_version"]
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
    observed = []
    original_page, original_quarter = (
        dashboard_model_reads.read_page,
        dashboard_model_reads.read_quarterly_report,
    )

    def read_page(*args, **kwargs):
        result = original_page(*args, **kwargs)
        observed.append((args[2], result is not None))
        return result

    def read_quarter(*args, **kwargs):
        result = original_quarter(*args, **kwargs)
        observed.append(("quarterly_report", result is not None))
        return result

    monkeypatch.setattr(dashboard_model_reads, "read_page", read_page)
    monkeypatch.setattr(dashboard_model_reads, "read_quarterly_report", read_quarter)
    token = service.security.login("owner", PASSWORD).session_token
    credentials = WindowsCredentialStore(
        target_name=credential_target(
            root / "catalog.sqlite",
            service.security.catalog_instance_id,
        )
    )
    assert credentials.load_session_token() is None  # a fresh temporary catalogue key only
    record_property("p1_adapter_source", json.dumps(source_origins()[1]))
    record_property("p1_actual_model_publication", json.dumps(publication))
    try:
        credentials.save_session_token(token)
        write_protected_json(
            root / ".service.json",
            {
                "protocol": 2,
                "database_format": service.catalog.database_format(),
                "pid": os.getpid(),
                "port": server.server_port,
                "capability": capability,
                "catalog_id": service.security.catalog_instance_id,
                "build_id": server.build_id,
            },
        )
        yield {
            "service": service,
            "engine": engine,
            "company": company,
            "root": root,
            "token": token,
            "observed": observed,
        }
    finally:
        credentials.delete_session_token()
        service.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows resident credential boundary")
def test_p1_real_cli_and_stdio_mcp_ready_five_pages(p1_transport_book, tmp_path):
    book = p1_transport_book
    selection = {
        "view": "selected",
        "commands": ["dashboard_" + page for page in PAGES],
        "response_types": ["dashboard_" + page for page in PAGES],
    }
    schema = book["service"].dispatch("schema", selection)
    assert cli_call(book, tmp_path, "schema", selection) == schema
    before = state(book["engine"])

    async def scenario():
        async with stdio_client(stdio_parameters(book, tmp_path)) as streams:
            async with ClientSession(
                *streams, read_timeout_seconds=timedelta(seconds=60)
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "finance_local_schema",
                    "finance_local_security",
                    "finance_local_command",
                }
                assert (
                    mcp_payload(await session.call_tool("finance_local_schema", selection))
                    == schema
                )
                for page in PAGES:
                    command, payload = "dashboard_" + page, page_payload(book, page)
                    expected = book["service"].dispatch(
                        command, payload, session_token=book["token"]
                    )
                    assert expected["state"] == "ready"
                    if page == "quarterly_report":
                        assert {item["key"] for item in expected["response"]["statements"]} == {
                            "balance_sheet",
                            "profit_statement",
                            "cash_flow_statement",
                        }
                        assert all(item["rows"] for item in expected["response"]["statements"])
                    else:
                        assert expected["response"]["data"] is not None
                    for adapter in ("cli", "stdio-mcp"):
                        book["observed"].clear()
                        actual = (
                            cli_call(book, tmp_path, command, payload)
                            if adapter == "cli"
                            else await mcp_call(session, command, payload)
                        )
                        assert comparable(actual) == comparable(expected)
                        assert (page, True) in book[
                            "observed"
                        ]  # the real published block was consumed
                        if page == "brief":
                            amount = actual["response"]["data"]["funds_overview"]["bank_fen"]
                            assert type(amount) is int and amount == AMOUNT + 9
                # The actual signature requires str, not a wire integer.
                # Malformed string months have a separately recorded unresolved
                # delivery-boundary defect; this test does not invent its rejection.
                invalid = {"company_id": book["company"]["id"], "period": 7}
                for actual in (
                    cli_call(book, tmp_path, "dashboard_brief", invalid),
                    await mcp_call(session, "dashboard_brief", invalid),
                ):
                    assert actual["status"] == "rejected" and actual["code"] == "invalid_command"
        assert (
            json.loads((tmp_path / "p1-adapter-mcp-provenance.json").read_text())
            == source_origins()[1]
        )

    asyncio.run(asyncio.wait_for(scenario(), timeout=240))
    assert state(book["engine"]) == before


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows resident credential boundary")
def test_p1_real_adapters_native_continuation_and_bad_source_are_independent(
    p1_transport_book,
    tmp_path,
):
    book = p1_transport_book
    engine = book["engine"]
    payload = page_payload(book, "brief") | {"section": "vouchers", "limit": 1}
    expected = book["service"].dispatch("dashboard_brief", payload, session_token=book["token"])
    assert expected["scope"] == "vouchers"
    assert expected["data"]["collections"]["vouchers"]["items"]
    before = state(engine)

    async def scenario():
        async with stdio_client(stdio_parameters(book, tmp_path)) as streams:
            async with ClientSession(
                *streams, read_timeout_seconds=timedelta(seconds=60)
            ) as session:
                await session.initialize()
                await session.call_tool("finance_local_schema", {})
                for adapter in ("cli", "stdio-mcp"):
                    book["observed"].clear()
                    actual = (
                        cli_call(book, tmp_path, "dashboard_brief", payload)
                        if adapter == "cli"
                        else await mcp_call(session, "dashboard_brief", payload)
                    )
                    assert comparable(actual) == comparable(expected)
                    assert ("brief", False) in book[
                        "observed"
                    ]  # v6 continuation is native by contract
                    assert not any(used for _, used in book["observed"])
                assert state(engine) == before
                damage(
                    engine,
                    "fact_funding",
                    "UPDATE fact_funding SET amount_fen=amount_fen+1 "
                    "WHERE revision_id=(SELECT fact_id FROM fact_current "
                    "WHERE subject_id='p1-current')",
                )
                damaged = state(engine)
                for actual in (
                    cli_call(book, tmp_path, "dashboard_brief", payload),
                    await mcp_call(session, "dashboard_brief", payload),
                ):
                    assert (
                        actual["status"] == "rejected"
                        and actual["code"] == "content_integrity_failed"
                    )
                with pytest.raises(KernelError) as full:
                    Maintenance(engine).verify_integrity()
                assert full.value.code == "content_integrity_failed"
                with pytest.raises(KernelError) as repair:
                    Maintenance(engine).rebuild_dashboard_read_model(
                        MONTH, request_id="p1-bad-source-repair"
                    )
                assert repair.value.code == "content_integrity_failed"
                with pytest.raises(backup.BackupError) as portable:
                    backup.create_portable(engine.store.path, tmp_path / "bad-source-packages")
                assert portable.value.code == "backup_content_invalid"
                assert state(engine) == damaged  # no reset/queue/audit/repair mutation on refusal
        assert (
            json.loads((tmp_path / "p1-adapter-mcp-provenance.json").read_text())
            == source_origins()[1]
        )

    asyncio.run(asyncio.wait_for(scenario(), timeout=240))


def rows(connection, table, *, period=None):
    quoted = '"' + table.replace('"', '""') + '"'
    return sorted(
        (
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {quoted}" + (" WHERE period=?" if period is not None else ""),
                () if period is None else (YearMonth(period).ordinal,),
            )
        ),
        key=repr,
    )


def test_p1_current_close_backup_restore_preserves_four_historical_bodies_and_v4(
    tmp_path,
    monkeypatch,
    record_property,
):
    from historical_book_fixture import HISTORY_TABLES, MONTHS, historical_book

    company, january = historical_book(tmp_path, monkeypatch)
    engine = company.engine
    copied = engine.store.path
    bundle = production_bundle()
    assert backup.verify_file(copied)["verification"]["status"] == "verified"
    assert january["format_version"] == 4 and "fund_details" not in january
    history_tables = HISTORY_TABLES
    frozen_tables = (
        "period_close",
        "close_storage_root",
        "close_storage_subroot",
        "close_storage_directory",
        "close_storage_block",
    )
    with engine.store.connection(read_only=True) as connection:
        preserved = {table: rows(connection, table) for table in history_tables}
        assert all(preserved.values()), "The input must exercise all four real original fact bodies"
        frozen = {table: rows(connection, table, period="2026-01") for table in frozen_tables}
        financial = {
            table: rows(connection, table)
            for table in ("calculation", "voucher_version", "voucher_line")
        }
        # Actual exact-one body decoding, including seven-field saved material links.
        for table in history_tables:
            if table.endswith("_links"):
                continue
            for (fact_id,) in connection.execute(f"SELECT revision_id FROM {table}"):
                assert engine.store.fact(connection, fact_id).id == fact_id
    february = company.close(MONTHS[1])
    assert february["format_version"] == 5 and "fund_details" in february
    package = Path(backup.create_portable(copied, tmp_path / "p1-history-packages")["path"])
    original_zip = package.read_bytes()
    assert backup.verify_portable(package)["verification"]["status"] == "verified"
    restored = tmp_path / "p1-history-restored.sqlite"
    assert backup.restore_portable(package, restored)["verification"]["status"] == "verified"
    restored_engine = Engine(
        Store(restored, bundle, engine.store.company_id, engine.store.database_id)
    )
    assert Periods(restored_engine).closed_report("2026-01") == january
    assert Periods(restored_engine).closed_report("2026-02") == february
    for active in (engine, restored_engine):
        with active.store.connection(read_only=True) as connection:
            assert {table: rows(connection, table) for table in history_tables} == preserved
            assert {
                table: rows(connection, table, period="2026-01") for table in frozen_tables
            } == frozen
            for table, original_rows in financial.items():
                assert set(original_rows) <= set(rows(connection, table))
    assert package.read_bytes() == original_zip
    for active in (engine, restored_engine):
        with active.store.connection(read_only=True) as connection:
            for month, amount in (("2026-01", 5_167), ("2026-02", 4_667)):
                wage = active.store.select(
                    connection, Read("calculation", "payroll", "@wage-" + month)
                )[0]
                interest = active.store.select(
                    connection, Read("calculation", "loan_interest", "@interest-" + month)
                )[0]
                assert (
                    wage.values["gross_fen"],
                    wage.values["net_fen"],
                    wage.values["tax_fen"],
                ) == (600_000, 597_000, 3_000)
                assert interest.values["interest_fen"] == amount
            assert (
                connection.execute("SELECT count(*) FROM fact_payroll_confirmed").fetchone()[0] == 0
            )
            assert (
                connection.execute("SELECT count(*) FROM fact_remuneration_tax_basis").fetchone()[0]
                == 0
            )
    record_property(
        "p1_adapter_history_input",
        json.dumps(
            {
                "origin": "self-contained exact 0c8abb synthetic source SQL",
                "company_fingerprint": bundle.database_format()["fingerprint"],
                "historical_bodies": {key: len(value) for key, value in preserved.items()},
                "sqlite_bytes": copied.stat().st_size,
                "zip_bytes": len(original_zip),
                "frozen_original_version": 4,
                "new_close_version": 5,
            }
        ),
    )
    record_property("p1_adapter_source", json.dumps(source_origins()[1]))
    damage(
        engine,
        "fact_payroll_pre_revision",
        "UPDATE fact_payroll_pre_revision SET "
        "accounting_gross_salary_fen=accounting_gross_salary_fen+1",
    )
    damaged = state(engine)
    with pytest.raises(KernelError) as full:
        Maintenance(engine).verify_integrity()
    assert full.value.code == "content_integrity_failed"
    with pytest.raises(backup.BackupError) as invalid:
        backup.create_portable(copied, tmp_path / "bad-history-packages")
    assert invalid.value.code == "backup_content_invalid"
    assert state(engine) == damaged
    assert package.read_bytes() == original_zip
