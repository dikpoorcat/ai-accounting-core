"""Synthetic MCP dialogue relay for checkpointed working draft recovery.

The relay delegates transport to the previous reading harness. Its data and
credentials are synthetic, and it never attaches to an existing local service.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import reading_agent_harness as reading

write_json = reading.write_json
value_from_mcp = reading.value_from_mcp


def seed(app, token, directory):
    calls = []

    def command(command_name, **payload):
        result = app.dispatch(command_name, payload, session_token=token)
        calls.append({"command": command_name, "payload": payload, "result": result})
        return result

    companies = [
        command("create_company", taxpayer_id=taxpayer, name=name)
        for taxpayer, name in (
            ("91310000123456789A", "合成工作稿甲公司"),
            ("91310000123456789B", "合成工作稿乙公司"),
        )
    ]
    company = companies[0]["id"]
    confirmation = (
        "全为合成资料。甲公司2026年2月收到两笔月度办公服务，"
        "均属于2026-02、管理费用，供应方为合成办公服务商。"
        "第1笔金额10101分，债权人类别为供应商；"
        "第2笔金额20202分，原资料未说明债权人类别。"
        "两笔尚未登记。资料于2026年3月收到，接收月不是核算所属月。"
    )
    proof = command(
        "evidence",
        company_id=company,
        content_base64=base64.b64encode(confirmation.encode()).decode(),
        media_type="text/plain",
        name="合成业务说明.txt",
        request_id="seed-proof",
    )["digest"]
    for item in companies:
        command(
            "update_company_note",
            company_id=item["id"],
            text=confirmation if item["id"] == company else "乙公司用于验证草稿隔离。",
            expected_revision=0,
            request_id="seed-note-" + item["id"],
            **({"evidence_digest": proof} if item["id"] == company else {}),
        )
    supplier = command(
        "register_entity",
        company_id=company,
        kind="organization",
        data={"display_name": "合成办公服务商"},
        source="合成负责人说明",
        evidence_digest=proof,
        request_id="seed-supplier",
    )["entity_id"]
    raw = (
        "名称,金额,核算所属期,往来方,费用类别,债权人类别\n"
        "办公服务1,101.01,2026-02,合成办公服务商,administration,supplier\n"
        "办公服务2,202.02,2026-02,合成办公服务商,administration,\n"
    ).encode("utf-8-sig")
    original = command(
        "evidence",
        company_id=company,
        content_base64=base64.b64encode(raw).decode(),
        media_type="text/csv",
        name="合成办公服务.csv",
        request_id="seed-original",
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
        subject_id="draft-source",
        data={
            "period": "2026-03",
            "evidence_digest": original,
            "category": "transactions",
            "purpose": "business",
            "specification": specification,
        },
        evidence=[original, proof],
        expected_revision=0,
        request_id="seed-material",
    )
    candidates = []
    for number, amount in ((1, 10101), (2, 20202)):
        data = {
            "period": "2026-02",
            "counterparty_id": supplier,
            "amount_fen": amount,
            "expense_class": "administration",
        }
        if number == 1:
            data["creditor_kind"] = "supplier"
        candidates.append(
            {
                "id": f"expense-{number}",
                "command": "save_fact",
                "payload": {
                    "company_id": company,
                    "kind": "expense",
                    "subject_id": f"draft-expense-{number}",
                    "data": data,
                    "evidence": [original, proof],
                    "expected_revision": 0,
                    "source_locations": [
                        {
                            "source_id": "draft-source",
                            "source_fact_id": source["fact_id"],
                            "evidence_digest": original,
                            "location": f"CSV!B{number + 1}",
                        }
                    ],
                    "request_id": f"submit-draft-expense-{number}",
                },
            }
        )
    info = {
        "synthetic": True,
        "catalog_id": app.security.catalog_instance_id,
        "company_id": company,
        "companies": companies,
        "supplier_id": supplier,
        "period": "2026-02",
        "work_area": "transactions",
        "source_id": "draft-source",
        "source_result": source,
        "evidence_digest": original,
        "confirmation_evidence": proof,
        "specification": specification,
        "candidates": candidates,
        "work_context_payload": {
            "company_id": company,
            "period": "2026-02",
            "work_area": "transactions",
        },
        "inspect_payload": {
            "company_id": company,
            "evidence_digest": original,
            "specification": specification,
        },
        "owner_answer_file": str(directory / "owner-answer.json"),
        "instruction": (
            "两笔均未登记。查已有来源后只询问第2笔 creditor_kind，收到回答后准备输入。"
            "在负责人要求停下时保存接续点（若当前比较允许草稿），不要登记或发布。"
            "重启后核对当前内核；不要把未判明请求当失败或换键重做。"
        ),
    }
    (directory / "synthetic-source.csv").write_bytes(raw)
    write_json(
        directory / "owner-answer.json",
        {
            "owner_stage_only": True,
            "answer": "甲公司2026年2月第2笔办公服务的债权人类别也是供应商。",
            "field": "creditor_kind",
            "value": "supplier",
            "location": "CSV!B3",
        },
    )
    write_json(directory / "seed.json", info)
    write_json(directory / "seed-transcript.json", calls)
    return info


class Workload(reading.Workload):
    def install(self):
        super().install()
        if self.enabled:
            sys.addaudithook(self.file_access)

    def file_access(self, event, args):
        if event != "open" or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0]))
        if ".work-drafts" not in path.parts or path.suffix not in {".json", ".tmp"}:
            return
        flags = args[2] if len(args) > 2 else 0
        key = "draft_file_writes" if flags & (os.O_WRONLY | os.O_RDWR) else "draft_file_reads"
        with self.lock:
            if self.active is not None:
                self.active[key] += 1

    def invoke(self, method, command, *args, **kwargs):
        self.sequence += 1
        metric = {
            "sequence": self.sequence,
            "command": command,
            "instrumented": self.enabled,
            "sql_statements": 0 if self.enabled else None,
            "inspect_bytes_calls": 0 if self.enabled else None,
            "draft_file_reads": 0 if self.enabled else None,
            "draft_file_writes": 0 if self.enabled else None,
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


def parameters(directory, *, source_dir=None, resume=False, instrument=False, cache_build_id=None):
    from mcp import StdioServerParameters

    args = ["-X", "utf8", str(Path(__file__).resolve()), "mcp", str(directory)]
    if source_dir is not None:
        args.extend(["--source-dir", str(source_dir)])
    if resume:
        args.append("--resume")
    if instrument:
        args.append("--instrument")
    if cache_build_id is not None:
        args.extend(["--cache-build-id", str(cache_build_id)])
    return StdioServerParameters(command=sys.executable, args=args)


async def relay(directory, *, source_dir=None, resume=False, instrument=False):
    """Forward explicitly queued tools and retain all workload counters."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    if resume:
        if not (directory / "seed.json").is_file():
            raise RuntimeError("No existing synthetic work draft dialogue to resume")
        (directory / "stop").unlink(missing_ok=True)
        (directory / "stopped.json").unlink(missing_ok=True)
        (directory / "ready.json").unlink(missing_ok=True)
    else:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "requests").mkdir()
        (directory / "responses").mkdir()
    async with stdio_client(
        parameters(directory, source_dir=source_dir, resume=resume, instrument=instrument)
    ) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tool_list = await client.list_tools()
            seed_info = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
            write_json(
                directory / "ready.json",
                {
                    **seed_info,
                    "tools": [tool.name for tool in tool_list.tools],
                    "relay_pid": os.getpid(),
                    "instrumented": instrument,
                    "source_dir": str(source_dir) if source_dir else None,
                    "resumed": resume,
                },
            )
            while not (directory / "stop").exists():
                for path in sorted((directory / "requests").glob("*.json")):
                    output = directory / "responses" / path.name
                    if output.exists():
                        continue
                    request = json.loads(path.read_text(encoding="utf-8"))
                    start = time.perf_counter()
                    result = value_from_mcp(
                        await client.call_tool(request["tool"], request["arguments"])
                    )
                    duration = (time.perf_counter() - start) * 1000
                    metric = json.loads(
                        (directory / "workload.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    with (directory / "tools.jsonl").open("a", encoding="utf-8") as log:
                        log.write(
                            json.dumps(
                                {
                                    "request": request,
                                    "response": result,
                                    "elapsed_ms": duration,
                                    "response_bytes": len(
                                        json.dumps(
                                            result, ensure_ascii=False, separators=(",", ":")
                                        ).encode()
                                    ),
                                    "instrumented": instrument,
                                    **{
                                        key: metric[key]
                                        for key in (
                                            "sql_statements",
                                            "inspect_bytes_calls",
                                            "draft_file_reads",
                                            "draft_file_writes",
                                        )
                                    },
                                    "tokens": None,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    write_json(output, result)
                await asyncio.sleep(0.1)
    write_json(directory / "stopped.json", {"relay_pid": os.getpid(), "stopped": True})


reading.seed = seed
reading.Workload = Workload
reading.parameters = parameters
reading.relay = relay


if __name__ == "__main__":
    reading.main()
