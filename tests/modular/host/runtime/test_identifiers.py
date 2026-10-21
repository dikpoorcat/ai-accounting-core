"""Candidate UUIDs need neither a company nor durable work preparation."""

import asyncio
import json
import subprocess
import sys
from uuid import UUID

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from ai_accounting_modular.agent.public import generate_ids
from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.host.runtime import _client, cli
from ai_accounting_modular.host.runtime._mcp import create_mcp

from .conftest import business_request


def check_ids(ids, count):
    assert len(ids) == len(set(ids)) == count
    for identity in ids:
        validate_contract("core.schema.json", "Id", identity)
        assert UUID(identity).version == 4


def test_generator_default_and_complete_legal_request_capacity():
    check_ids(generate_ids(), 1)
    check_ids(generate_ids(1001), 1001)


@pytest.mark.parametrize("count", [True, False, 0, -1, 1002, 1.0, "1", None])
def test_generator_rejects_non_integer_or_out_of_range_count(count):
    with pytest.raises(ContractError) as failed:
        generate_ids(count)
    assert failed.value.code == "invalid_input"


def test_mcp_generator_needs_no_client_and_does_not_coerce_counts():
    async def check():
        # No methods exist: generating candidates must never dispatch a business call.
        mcp = create_mcp(object())
        tool = next(tool for tool in await mcp.list_tools() if tool.name == "accounting_new_ids")
        assert tool.inputSchema["properties"]["count"]["type"] == "integer"
        for arguments, count in (({}, 1), ({"count": 1001}, 1001)):
            _, result = await mcp.call_tool("accounting_new_ids", arguments)
            assert set(result) == {"ids"}
            check_ids(result["ids"], count)
        for invalid in (True, False, 0, -1, 1002, 1.0, "1", None):
            with pytest.raises(ToolError):
                await mcp.call_tool("accounting_new_ids", {"count": invalid})

    asyncio.run(check())


@pytest.mark.parametrize("count", [None, 3, 1001])
def test_cli_generator_ignores_nonexistent_catalog_and_never_starts_service(
    count, tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("candidate IDs must not connect to or start a resident")

    monkeypatch.setattr(_client, "Client", forbidden)
    monkeypatch.setattr(_client.subprocess, "Popen", forbidden)
    arguments = ["finance-modular", "--catalog", str(tmp_path / "absent.sqlite3"), "ids"]
    if count is not None:
        arguments.extend(("--count", str(count)))
    monkeypatch.setattr(sys, "argv", arguments)
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"ids"}
    check_ids(result["ids"], 1 if count is None else count)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("count", [0, -1, 1002])
def test_cli_rejects_invalid_count_without_catalog(count, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["finance-modular", "ids", "--count", str(count)])
    with pytest.raises(SystemExit) as failed:
        cli.main()
    assert failed.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "rejected" and result["code"] == "invalid_input"


def test_actual_cli_can_generate_without_catalog_or_files(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "ai_accounting_modular.host.runtime.cli",
            "ids",
            "--count",
            "3",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    check_ids(json.loads(result.stdout)["ids"], 3)
    assert list(tmp_path.iterdir()) == []


def test_generated_ids_follow_original_business_on_retry_and_correction(local):
    runtime, company, _ = local
    app = runtime.kernel.application
    request = business_request(company.company_id)
    command, supplier, opening, expense = generate_ids(4)
    request["command_id"] = command
    request["objects"][0]["object_id"] = supplier
    request["mutations"][0]["fact"]["business_id"] = opening
    fact = request["mutations"][1]["fact"]
    fact["business_id"] = expense
    fact["data"]["creditor_id"] = supplier
    assert app.accounting_apply(request)["accounting"] == "saved"
    assert app.accounting_apply(request)["accounting"] == "unchanged"
    fact["data"]["amount_fen"] = "90000"
    corrected = app.accounting_apply(
        {
            "contract": request["contract"],
            "company_id": company.company_id,
            "command_id": generate_ids()[0],
            "mutations": [{"op": "replace", "expected_seq": 1, "fact": fact}],
        }
    )
    assert corrected["accounting"] == "saved"
    business = app.accounting_get(
        {"company_id": company.company_id, "business_ids": [expense], "include": ["facts"]}
    )["businesses"][0]
    assert business["state"]["seq"] == 2
    assert business["fact"]["business_id"] == expense
    assert business["fact"]["data"]["amount_fen"] == "90000"
