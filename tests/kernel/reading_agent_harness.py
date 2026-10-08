"""Isolated production MCP relay for an independent accounting reading dialogue.

Only synthetic roots and in-memory owner credentials are used. Instrumented
workload runs are separate from timing runs; no token counts are estimated.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

OWNER = "synthetic-reading-owner"
PASSWORD = "Synthetic-reading-evaluation-only-2026"


def write_json(path, value):
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def value_from_mcp(result):
    if result.structuredContent is not None:
        return result.structuredContent
    text = "".join(item.text for item in result.content if item.type == "text")
    try:
        return json.loads(text)
    except ValueError:
        if not result.isError:
            raise
        return {"status": "rejected", "code": "mcp_tool_error", "message": text}


def seed(app, token, directory):
    """Seed the same disclosed accounting facts in each source version."""
    calls = []

    def command(command_name, **payload):
        result = app.dispatch(command_name, payload, session_token=token)
        calls.append({"command": command_name, "payload": payload, "result": result})
        return result

    companies = [
        command("create_company", taxpayer_id=taxpayer, name=name)
        for taxpayer, name in (
            ("91310000123456789A", "合成甲公司"),
            ("91310000123456789B", "合成乙公司"),
        )
    ]
    company = companies[0]["id"]
    confirmation = (
        "全为合成资料。合成甲公司2026年3月收到250笔月度办公服务，"
        "各金额不同且均属于2026-03，全部管理费用。"
        "前249笔债权人是合成办公服务商，属于供应商。"
        "第250笔原资料未确认债权人类别，应先查已有资料再问真正缺项。"
        "第1、2笔已登记但尚未正式发布，继续处理时复用已有事实。"
    )
    evidence = command(
        "evidence",
        company_id=company,
        content_base64=base64.b64encode(confirmation.encode()).decode(),
        media_type="text/plain",
        name="合成负责人确认.txt",
        request_id="seed-owner-evidence",
    )["digest"]
    for item in companies:
        command(
            "update_company_note",
            company_id=item["id"],
            text=(confirmation if item["id"] == company else "合成乙公司：用于验证公司切换隔离。"),
            expected_revision=0,
            request_id="seed-company-note-" + item["id"],
            **({"evidence_digest": evidence} if item["id"] == company else {}),
        )
    supplier = command(
        "register_entity",
        company_id=company,
        kind="organization",
        data={
            "display_name": "合成办公服务商",
            "external_identifiers": {"test-code": "reading-supplier"},
        },
        source="合成负责人确认",
        evidence_digest=evidence,
        request_id="seed-supplier",
    )["entity_id"]
    rows = ["名称,金额,核算所属期,往来方,费用类别,债权人类别"]
    for number in range(1, 251):
        amount = 10000 + number
        creditor = "supplier" if number < 250 else ""
        rows.append(
            f"办公服务第{number:04}笔,{amount // 100}.{amount % 100:02},"
            f"2026-03,合成办公服务商,administration,{creditor}"
        )
    raw = ("\n".join(rows) + "\n").encode("utf-8-sig")
    original = command(
        "evidence",
        company_id=company,
        content_base64=base64.b64encode(raw).decode(),
        media_type="text/csv",
        name="合成250笔办公服务.csv",
        request_id="seed-material-evidence",
    )["digest"]
    specification = {
        "format": "csv",
        "columns": [
            {"column": "A", "role": "context", "label": "名称"},
            {"column": "B", "role": "amount", "label": "金额"},
            {"column": "C", "role": "recognition_period", "label": "核算所属期"},
            {"column": "D", "role": "context", "label": "往来方"},
            {"column": "E", "role": "context", "label": "费用类别"},
            {"column": "F", "role": "context", "label": "债权人类别"},
        ],
    }
    source = command(
        "receive_material",
        company_id=company,
        subject_id="reading-source",
        data={
            "period": "2026-03",
            "evidence_digest": original,
            "category": "transactions",
            "purpose": "business",
            "specification": specification,
        },
        evidence=[original, evidence],
        expected_revision=0,
        request_id="seed-material-source",
    )
    facts = []
    for number in (1, 2):
        payload = {
            "company_id": company,
            "kind": "expense",
            "subject_id": f"reading-expense-{number}",
            "data": {
                "period": "2026-03",
                "counterparty_id": supplier,
                "amount_fen": 10000 + number,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            "evidence": [original, evidence],
            "expected_revision": 0,
            "source_locations": [
                {
                    "source_id": "reading-source",
                    "source_fact_id": source["fact_id"],
                    "evidence_digest": original,
                    "location": f"CSV!B{number + 1}",
                }
            ],
            "request_id": f"seed-expense-{number}",
        }
        facts.append({"payload": payload, "result": command("save_fact", **payload)})
    seed_info = {
        "synthetic": True,
        "catalog_id": app.security.catalog_instance_id,
        "period": "2026-03",
        "work_area": "transactions",
        "companies": companies,
        "company_id": company,
        "supplier_id": supplier,
        "confirmation_evidence": evidence,
        "source_id": "reading-source",
        "source_result": source,
        "evidence_digest": original,
        "specification": specification,
        "seed_facts": facts,
        "inspect_payload": {
            "company_id": company,
            "evidence_digest": original,
            "specification": specification,
        },
        "work_context_payload": {
            "company_id": company,
            "period": "2026-03",
            "work_area": "transactions",
        },
        "missing": {"location": "CSV!B251", "field": "creditor_kind", "amount_fen": 10250},
        "owner_answer_file": str(directory / "owner-answer.json"),
        "source_bytes_file": str(directory / "synthetic-source.csv"),
        "instruction": (
            "原件250笔；已有第1、2笔登记未发布。核对并复用已有来源，仅询问剩余真实缺项。"
        ),
    }
    (directory / "synthetic-source.csv").write_bytes(raw)
    write_json(
        directory / "owner-answer.json",
        {
            "owner_stage_only": True,
            "answer": (
                "合成甲公司2026年3月第250笔办公服务也是合成办公服务商应收，"
                "债权人类别为供应商。"
            ),
            "field": "creditor_kind",
            "value": "supplier",
            "location": "CSV!B251",
        },
    )
    write_json(directory / "seed.json", seed_info)
    write_json(directory / "seed-transcript.json", calls)
    return seed_info


class Workload:
    """Count work only, without recording SQL text or credential values."""

    def __init__(self, directory, enabled):
        self.directory, self.enabled = directory, enabled
        self.active = None
        self.sequence = 0
        self.lock = threading.Lock()

    def install(self):
        if not self.enabled:
            return
        from ai_accounting.kernel import materials, runtime

        original_init = runtime._PrivateConnection.__init__

        def connection_init(connection, *args, **kwargs):
            original_init(connection, *args, **kwargs)
            connection.set_trace_callback(self.trace)

        runtime._PrivateConnection.__init__ = connection_init
        original_inspect = materials.inspect_bytes

        def inspect_bytes(*args, **kwargs):
            with self.lock:
                if self.active is not None:
                    self.active["inspect_bytes_calls"] += 1
            return original_inspect(*args, **kwargs)

        materials.inspect_bytes = inspect_bytes

    def trace(self, statement):
        with self.lock:
            if self.active is not None:
                self.active["sql_statements"] += 1

    def invoke(self, method, command, *args, **kwargs):
        self.sequence += 1
        metric = {
            "sequence": self.sequence,
            "command": command,
            "instrumented": self.enabled,
            "sql_statements": 0 if self.enabled else None,
            "inspect_bytes_calls": 0 if self.enabled else None,
        }
        self.active = metric
        start = time.perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            metric["elapsed_ms"] = (time.perf_counter() - start) * 1000
            self.active = None
            with (self.directory / "workload.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(metric) + "\n")


def mcp_process(directory, *, resume=False, instrument=False, cache_build_id=None):
    from pydantic import SecretStr

    from ai_accounting.kernel import mcp as production_mcp
    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
    from ai_accounting.kernel.service import LocalService

    root = directory / "data"
    if root.exists() and not resume:
        raise RuntimeError("A fresh synthetic root is required; use --resume for this harness root")
    if resume and not (directory / "seed.json").is_file():
        raise RuntimeError("Only an existing synthetic reading harness can be resumed")
    workload = Workload(directory, instrument)
    workload.install()
    app = LocalService(root)
    if not resume:
        app.security.provision(OWNER, SecretStr(PASSWORD))
    token = app.security.login(OWNER, SecretStr(PASSWORD)).session_token
    credentials = InMemoryCredentialStore()
    credentials.save_session_token(token)
    seed_info = (
        json.loads((directory / "seed.json").read_text(encoding="utf-8"))
        if resume
        else seed(app, token, directory)
    )
    if resume:
        if (
            seed_info.get("catalog_id", app.security.catalog_instance_id)
            != app.security.catalog_instance_id
        ):
            raise RuntimeError("Synthetic catalog identity changed on resume")
        actual = app.catalog.companies()
        expected = {(item["id"], item["database_id"]) for item in seed_info["companies"]}
        if expected != {(item["id"], item["database_id"]) for item in actual}:
            raise RuntimeError("Synthetic company identities changed on resume")
    if cache_build_id is not None:
        if not hasattr(app, "inspection_cache"):
            raise RuntimeError("The selected source has no persistent reading cache")
        app.inspection_cache.build_id = cache_build_id
    server, capability = create_server(app, port=0)
    app.security_controller = build_native_security_controller(
        app, server, capability, credential_store=credentials, window_opener=lambda _: None
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    metadata = {
        "protocol": 2,
        "database_format": app.catalog.database_format(),
        "pid": os.getpid(),
        "port": server.server_port,
        "capability": capability,
        "catalog_id": app.security.catalog_instance_id,
        "build_id": server.build_id,
    }
    client = ServiceClient(root, metadata=metadata, credential_store=credentials)
    original_dispatch, original_security = client.dispatch, client.security
    client.dispatch = lambda command, payload: workload.invoke(
        original_dispatch, command, command, payload
    )
    client.security = lambda action, payload: workload.invoke(
        original_security, "security:" + action, action, payload
    )
    production_mcp.ServiceClient = lambda _root: client
    write_json(
        directory / "instance.json",
        {
            "pid": os.getpid(),
            "catalog_id": app.security.catalog_instance_id,
            "build_id": server.build_id,
            "instrumented": instrument,
            "cache_build_id": cache_build_id,
            "resumed": resume,
        },
    )
    try:
        production_mcp.serve(root)
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        app.close()


def parameters(directory, *, source_dir=None, resume=False, instrument=False, cache_build_id=None):
    from mcp import StdioServerParameters

    args = ["-X", "utf8", str(Path(__file__).resolve()), "mcp", str(directory)]
    for flag, value in (("--source-dir", source_dir), ("--cache-build-id", cache_build_id)):
        if value is not None:
            args.extend([flag, str(value)])
    if resume:
        args.append("--resume")
    if instrument:
        args.append("--instrument")
    return StdioServerParameters(command=sys.executable, args=args)


async def relay(directory, *, source_dir=None, resume=False, instrument=False):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    if resume:
        if not (directory / "seed.json").is_file():
            raise RuntimeError("No existing synthetic reading dialogue to resume")
        (directory / "stop").unlink(missing_ok=True)
        (directory / "stopped.json").unlink(missing_ok=True)
    else:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "requests").mkdir()
        (directory / "responses").mkdir()
    async with stdio_client(
        parameters(directory, source_dir=source_dir, resume=resume, instrument=instrument)
    ) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            seed_info = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
            write_json(
                directory / "ready.json",
                {
                    **seed_info,
                    "tools": [tool.name for tool in tool_list.tools],
                    "relay_pid": os.getpid(),
                    "instrumented": instrument,
                    "source_dir": str(source_dir) if source_dir else None,
                },
            )
            while not (directory / "stop").exists():
                for path in sorted((directory / "requests").glob("*.json")):
                    output = directory / "responses" / path.name
                    if output.exists():
                        continue
                    request = json.loads(path.read_text(encoding="utf-8"))
                    started = time.perf_counter()
                    result = await session.call_tool(request["tool"], request["arguments"])
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    value = value_from_mcp(result)
                    size = len(
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    metric = json.loads(
                        (directory / "workload.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    with (directory / "tools.jsonl").open("a", encoding="utf-8") as log:
                        log.write(
                            json.dumps(
                                {
                                    "request": request,
                                    "response": value,
                                    "elapsed_ms": elapsed_ms,
                                    "response_bytes": size,
                                    "instrumented": instrument,
                                "sql_statements": metric["sql_statements"],
                                "inspect_bytes_calls": metric["inspect_bytes_calls"],
                                    "tokens": None,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    write_json(output, value)
                await asyncio.sleep(0.1)
    write_json(directory / "stopped.json", {"relay_pid": os.getpid(), "stopped": True})


def call(directory, tool, arguments_file):
    if not (directory / "ready.json").is_file():
        raise RuntimeError("MCP reading relay is not ready")
    request = directory / "requests" / (uuid.uuid4().hex + ".json")
    arguments = json.loads(arguments_file.read_text(encoding="utf-8-sig"))
    write_json(request, {"tool": tool, "arguments": arguments})
    output = directory / "responses" / request.name
    deadline = time.monotonic() + 55
    while not output.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Await the existing response file: {output}")
        time.sleep(0.05)
    print(output)
    print(output.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "mcp", "call", "stop"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("tool", nargs="?")
    parser.add_argument("arguments_file", nargs="?", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--instrument", action="store_true")
    parser.add_argument("--cache-build-id")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.source_dir:
        sys.path.insert(0, str(args.source_dir.resolve()))
    if args.mode == "mcp":
        mcp_process(
            directory,
            resume=args.resume,
            instrument=args.instrument,
            cache_build_id=args.cache_build_id,
        )
    elif args.mode == "host":
        asyncio.run(
            relay(
                directory,
                source_dir=args.source_dir,
                resume=args.resume,
                instrument=args.instrument,
            )
        )
    elif args.mode == "stop":
        (directory / "stop").touch()
        deadline = time.monotonic() + 55
        while not (directory / "stopped.json").is_file():
            if time.monotonic() > deadline:
                raise TimeoutError("Synthetic relay stop is pending; inspect its existing process")
            time.sleep(0.1)
        print("Synthetic reading relay stopped")
    else:
        call(directory, args.tool, args.arguments_file)


if __name__ == "__main__":
    main()
