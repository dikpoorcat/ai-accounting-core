"""A real resident is shared by subprocess CLI, stdio MCP and same-origin HTTP."""

import asyncio
import json
import subprocess
import sys
from hashlib import sha256
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.host.runtime import _state
from ai_accounting_modular.host.runtime._client import Client

from .conftest import business_request, native_login


def test_large_legal_source_batch_crosses_real_ipc_without_request_or_response_truncation(
    local, monkeypatch
):
    from uuid import uuid4

    runtime, company, _ = local

    def sources(request):
        validate_contract("core.schema.json", "SourcesRequest", request)
        result = {
            "company_id": request["company_id"],
            "sources": [
                {
                    "source_id": item["source_id"],
                    "kind": "owner_statement",
                    "text": item["text"],
                    "sha256": sha256(item["text"].encode()).hexdigest(),
                }
                for item in request["sources"]
            ],
            "missing_source_ids": [],
        }
        return validate_contract("core.schema.json", "SourcesResult", result)

    # Exercise transport framing now; Application's atomic import owner is integrated separately.
    monkeypatch.setattr(runtime.kernel.application, "accounting_sources", sources, raising=False)
    runtime.start()
    native_login(runtime)
    batch = [
        {"kind": "owner_statement", "source_id": str(uuid4()), "text": str(index) + "😀" * 19990}
        for index in range(480)
    ]
    result = Client(runtime.catalog_path).dispatch(
        "accounting_sources",
        {
            "company_id": company.company_id,
            "action": "import",
            "sources": batch,
        },
    )
    assert len(result["sources"]) == 480
    assert result["sources"][-1]["text"] == batch[-1]["text"]


def http(runtime, method, path, payload=None, *, cookies="", origin=True, headers=None):
    __tracebackhide__ = True
    connection = HTTPConnection("127.0.0.1", runtime.server.server_port, timeout=5)
    values = {"Content-Type": "application/json", "Cookie": cookies, **(headers or {})}
    if origin:
        values["Origin"] = f"http://127.0.0.1:{runtime.server.server_port}"
    connection.request(method, path, json.dumps(payload) if payload is not None else None, values)
    response = connection.getresponse()
    body = response.read()
    cookie = SimpleCookie()
    for key, value in response.getheaders():
        if key.lower() == "set-cookie":
            cookie.load(value)
            assert "HttpOnly" in value and "SameSite=Strict" in value
    result = (
        response.status,
        body,
        "; ".join(f"{key}={value.value}" for key, value in cookie.items()),
    )
    connection.close()
    return result


def browser(runtime):
    __tracebackhide__ = True
    ticket = parse_qs(urlsplit(runtime.browser_url()).fragment)["ticket"][0]
    status, _, cookies = http(runtime, "POST", "/api/browser-session", {"ticket": ticket})
    assert status == 200
    status, _, _ = http(runtime, "POST", "/api/browser-session", {"ticket": ticket})
    assert status == 401
    return cookies


def test_cli_business_and_mcp_read_share_one_resident_and_browser_session(local, tmp_path):
    runtime, company, _ = local
    runtime.start()
    native_login(runtime)
    content = business_request(company.company_id)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    command = [
        sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "ai_accounting_modular.host.runtime.cli",
        "--catalog",
        str(runtime.catalog_path),
    ]
    process = subprocess.run(
        [*command, "call", "accounting-apply", "--input", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["accounting"] == "saved"

    async def check_mcp():
        params = StdioServerParameters(command=sys.executable, args=command[1:] + ["mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            resource = await session.read_resource("accounting://contracts")
            catalog = json.loads(resource.contents[0].text)
            guides = await session.read_resource("accounting://guides")
            assert len(json.loads(guides.contents[0].text)["topics"]) == 5
            expense_guide = await session.read_resource(
                "accounting://guides/business/operations.expense"
            )
            assert "实际收付款另记" in expense_guide.contents[0].text
            assert {item["command"] for item in catalog["commands"]} == set(runtime.agent.commands)
            for item in catalog["commands"]:
                Draft202012Validator.check_schema(item["request_schema"])
                assert "core__PostingPlan" not in item["request_schema"]["$defs"]
            schema = next(
                item["request_schema"]
                for item in catalog["commands"]
                if item["command"] == "accounting_apply"
            )
            assert Draft202012Validator(schema).is_valid(content)
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "accounting_schema",
                "accounting_command",
                "accounting_new_ids",
                "accounting_security",
            }
            identifiers = await session.call_tool("accounting_new_ids", {"count": 3})
            assert not identifiers.isError
            ids = identifiers.structuredContent["ids"]
            assert len(ids) == len(set(ids)) == 3
            for identity in ids:
                validate_contract("core.schema.json", "Id", identity)
            result = await session.call_tool(
                "accounting_command",
                {
                    "command": "accounting_get",
                    "payload": {
                        "company_id": company.company_id,
                        "business_ids": [content["mutations"][1]["fact"]["business_id"]],
                        "include": ["facts"],
                    },
                },
            )
            assert not result.isError
            value = result.structuredContent
            assert value["businesses"][0]["fact"]["data"]["amount_fen"] == "100000"

    asyncio.run(check_mcp())
    cookies = browser(runtime)
    status, body, _ = http(runtime, "GET", "/api/dashboard/context", cookies=cookies)
    assert status == 200
    assert json.loads(body)["current_company"]["company_id"] == company.company_id
    info = Client(runtime.catalog_path).info()
    assert "capability" not in info
    assert info["port"] == runtime.server.server_port
    if sys.platform == "win32":
        assert (
            runtime.capability.encode() not in _state.state_path(runtime.catalog_path).read_bytes()
        )


def test_untrusted_post_and_business_web_write_are_rejected_and_tickets_are_bound(local):
    runtime, _, _ = local
    runtime.start()
    native_login(runtime)
    cookies = browser(runtime)
    status, _, _ = http(runtime, "GET", "/api/dashboard/context")
    assert status == 401
    status, _, _ = http(runtime, "POST", "/api/accounting-apply", {}, cookies=cookies)
    assert status == 404
    status, _, _ = http(
        runtime,
        "POST",
        "/api/security-request",
        {"operation": "session_status"},
        cookies=cookies,
        origin=False,
    )
    assert status == 400
    status, _, _ = http(
        runtime,
        "POST",
        "/private/command",
        {
            "command": "accounting_schema",
            "payload": {},
        },
        cookies=cookies,
    )
    assert status == 401
    status, _, _ = http(
        runtime,
        "GET",
        "/api/dashboard/context",
        cookies=cookies,
        headers={"Host": "foreign.invalid"},
    )
    assert status == 400
    with pytest.raises(ContractError, match="已经有一个"):
        with _state.instance_lock(runtime.catalog_path):
            pass
    with pytest.raises(ContractError) as failed:
        Client(runtime.catalog_path).dispatch("accounting_get", {})
    assert failed.value.code == "invalid_input"


def test_browser_native_login_completes_only_its_matching_surface(local):
    from ai_accounting_modular.host.runtime._client import request

    from .conftest import PASSWORD

    runtime, _, _ = local
    runtime.start()
    cookies = browser(runtime)
    status, body, _ = http(
        runtime,
        "POST",
        "/api/security-request",
        {
            "operation": "request",
            "payload": {"kind": "login"},
        },
        cookies=cookies,
    )
    assert status == 200
    window = json.loads(body)
    request(
        runtime.metadata,
        "/private/native",
        {
            "operation": "execute",
            "payload": {
                "request_id": window["request_id"],
                "password": PASSWORD,
            },
        },
    )
    unrelated = browser(runtime)
    _, _, unrelated_update = http(
        runtime,
        "POST",
        "/api/security-request",
        {
            "operation": "status",
            "payload": {"request_id": window["request_id"]},
        },
        cookies=unrelated,
    )
    assert not unrelated_update
    status, _, update = http(
        runtime,
        "POST",
        "/api/security-request",
        {
            "operation": "status",
            "payload": {"request_id": window["request_id"]},
        },
        cookies=cookies,
    )
    assert status == 200 and update
    status, _, _ = http(runtime, "GET", "/api/dashboard/context", cookies=cookies + "; " + update)
    assert status == 200
