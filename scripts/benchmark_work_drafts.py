"""Compare synthetic checkpoint/restart recovery through the production MCP stack.

This is a scripted transport comparison, not an AI dialogue evaluation. SQL and
file-open workload passes are separate from uninstrumented elapsed-time passes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import shutil
import statistics
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "kernel"))

from benchmark_ai_reading import extract_baseline  # noqa: E402
from work_draft_agent_harness import parameters, value_from_mcp, write_json  # noqa: E402


@asynccontextmanager
async def session(directory, source, instrument, *, resume=False):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(
        parameters(directory, source_dir=source, instrument=instrument, resume=resume)
    ) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            yield client


async def worker(directory, source, *, instrument, repeats):
    directory.mkdir(parents=True, exist_ok=False)
    samples, checks = [], {}

    async def invoke(client, phase, command, payload, *, tool="finance_local_command"):
        started = time.perf_counter()
        arguments = (
            payload
            if tool == "finance_local_schema"
            else {
                "command": command,
                "payload": payload,
            }
        )
        result = value_from_mcp(await client.call_tool(tool, arguments))
        duration = (time.perf_counter() - started) * 1000
        metric = json.loads(
            (directory / "workload.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        sample = {
            "phase": phase,
            "command": command,
            "tool_calls": 1,
            "response_bytes": len(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            ),
            "elapsed_ms": None if instrument else duration,
            **{
                key: metric[key]
                for key in (
                    "sql_statements",
                    "sql_connections",
                    "inspect_bytes_calls",
                    "draft_file_reads",
                    "draft_file_writes",
                )
            },
        }
        samples.append(sample)
        write_json(directory / f"response-{len(samples):03}.json", result)
        if result.get("status") in {"rejected", "needs_information"}:
            raise AssertionError(f"Unexpected result for {command}: {result}")
        return result

    async with session(directory, source, instrument) as client:
        first_instance = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        seed = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
        schema = await invoke(client, "discovery", "schema", {}, tool="finance_local_schema")
        modern = "save_work_draft" in schema["commands"]
        selected = None
        for _ in range(repeats):
            selected = await invoke(
                client,
                "selected_contract",
                "schema",
                {
                    "view": "selected",
                    "commands": ["work_context"] + (["save_work_draft"] if modern else []),
                    "response_types": ["work_context"],
                },
                tool="finance_local_schema",
            )
            assert selected["view"] == "selected"
        combined = "include_work_draft" in selected["command_schemas"]["work_context"]["properties"]
        scope = seed["work_context_payload"]
        if modern and not combined:
            await invoke(client, "initial_read", "read_work_draft", scope)
        await invoke(
            client,
            "initial_read",
            "work_context",
            {**scope, **({"include_work_draft": True} if combined else {})},
        )
        await invoke(client, "cold_inspection", "inspect_material", seed["inspect_payload"])
        for _ in range(repeats):
            await invoke(client, "hot_inspection", "inspect_material", seed["inspect_payload"])
            page = await invoke(client, "hot_context", "work_context", scope)
            assert "work_draft" not in page
        draft = {
            "candidates": seed["candidates"],
            "questions": [{"id": "creditor-2", "question": "第2笔债权人类别？"}],
            "resume_note": "尚未登记；先查明第2笔债权人类别。",
        }
        revision = None
        if modern:
            saved = await invoke(
                client,
                "checkpoint",
                "save_work_draft",
                {**scope, "expected_revision": revision, "draft": draft},
            )
            revision = saved["revision"]
        answer = json.loads((directory / "owner-answer.json").read_text(encoding="utf-8"))
        draft = copy.deepcopy(draft)
        draft["candidates"][1]["payload"]["data"]["creditor_kind"] = answer["value"]
        draft["questions"][0]["answer"] = answer["answer"]
        draft["resume_note"] = "已收到第2笔答案，两笔候选待登记。负责人要求先停下。"
        if modern:
            saved = await invoke(
                client,
                "checkpoint",
                "save_work_draft",
                {**scope, "expected_revision": revision, "draft": draft},
            )
            revision = saved["revision"]

    async with session(directory, source, instrument, resume=True) as client:
        second_instance = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        checks["new_service_process_same_catalog"] = (
            first_instance["pid"] != second_instance["pid"]
            and first_instance["catalog_id"] == second_instance["catalog_id"]
        )
        if modern:
            if combined:
                context = await invoke(
                    client,
                    "restart_recovery",
                    "work_context",
                    {**scope, "include_work_draft": True},
                )
                restored = context["work_draft"]
            else:
                restored = await invoke(client, "restart_recovery", "read_work_draft", scope)
                await invoke(client, "restart_recovery", "work_context", scope)
            checks["candidate_and_answer_restored"] = restored["draft"] == draft
            assert restored["revision"] == revision
            recovered = restored["draft"]
            for _ in range(repeats):
                await invoke(client, "hot_read", "read_work_draft", scope)
        else:
            recovered = None
            checks["candidate_and_answer_restored"] = False
            checks["missing_after_restart"] = ["unsaved_owner_answer", "candidate_request_ids"]
        if not modern:
            await invoke(client, "restart_recovery", "work_context", scope)
        await invoke(client, "restart_inspection", "inspect_material", seed["inspect_payload"])
        if not modern:
            await invoke(client, "restart_recovery", "inspect_material", seed["inspect_payload"])
        checks["scripted_owner_requestion_required"] = 0 if recovered else 1
        if modern:
            original = recovered["candidates"][0]["payload"]
            recovered["pending_requests"] = [
                {
                    "command": "save_fact",
                    "payload": candidate["payload"],
                    "request_id": candidate["payload"]["request_id"],
                }
                for candidate in recovered["candidates"]
            ]
            saved = await invoke(
                client,
                "formal_request_checkpoint",
                "save_work_draft",
                {**scope, "expected_revision": revision, "draft": recovered},
            )
            revision = saved["revision"]
            receipt = await invoke(client, "formal_registration", "save_fact", original)
            await invoke(
                client, "formal_registration", "save_fact", recovered["candidates"][1]["payload"]
            )

        # A separate synthetic company provides a larger unresolved worklist.
        company = seed["companies"][1]["id"]
        raw = (
            "名称,金额,核算所属期\n" + "".join(f"合成服务{i},1.00,2026-02\n" for i in range(250))
        ).encode("utf-8-sig")
        proof = await invoke(
            client,
            "workflow_fixture",
            "evidence",
            {
                "company_id": company,
                "content_base64": base64.b64encode(raw).decode(),
                "media_type": "text/csv",
                "name": "250行合成服务.csv",
                "request_id": "large-proof",
            },
        )
        await invoke(
            client,
            "workflow_fixture",
            "receive_material",
            {
                "company_id": company,
                "subject_id": "large-source",
                "data": {
                    "period": "2026-02",
                    "category": "transactions",
                    "purpose": "business",
                    "evidence_digest": proof["digest"],
                    "specification": {
                        "format": "csv",
                        "columns": [
                            {"column": "A", "role": "context", "label": "名称"},
                            {"column": "B", "role": "amount", "label": "金额"},
                            {"column": "C", "role": "recognition_period", "label": "核算所属期"},
                        ],
                    },
                },
                "evidence": [proof["digest"]],
                "expected_revision": 0,
                "request_id": "large-source",
            },
        )
        for _ in range(repeats):
            listing = await invoke(
                client,
                "workflow_250_rows",
                "workflow",
                {
                    "company_id": company,
                    "period": "2026-02",
                    "as_of": "2026-03-31",
                },
            )
        checks["workflow_schema_version"] = listing["schema_version"]

    if modern:
        async with session(directory, source, instrument, resume=True) as client:
            restored = await invoke(client, "lost_response_recovery", "read_work_draft", scope)
            request = restored["draft"]["pending_requests"][0]
            query = {
                "company_id": scope["company_id"],
                "submitted_request_id": request["request_id"],
            }
            result = await invoke(client, "lost_response_recovery", "request_result", query)
            assert result["status"] == "committed" and result["result"] == receipt
            replayed = await invoke(
                client, "original_key_replay", request["command"], request["payload"]
            )
            assert replayed == receipt
            facts = await invoke(
                client,
                "original_key_replay",
                "find_facts",
                {
                    "company_id": scope["company_id"],
                    "kind": "expense",
                    "period_from": "2026-02",
                    "period_to": "2026-02",
                },
            )
            assert len(facts["items"]) == 2
            checks["committed_request_recovered_without_duplicate"] = True
            checks["recovered_work_draft_did_not_publish"] = facts["items"][0]["pending"]
            recovered = restored["draft"]
            recovered["result_refs"] = [
                {"request_id": item["request_id"]} for item in recovered.pop("pending_requests")
            ]
            await invoke(
                client,
                "multiple_receipt_checkpoint",
                "save_work_draft",
                {
                    **scope,
                    "expected_revision": revision,
                    "draft": recovered,
                },
            )
            checks["multiple_receipts_preserved_and_resolved"] = True

    grouped = {}
    for phase in sorted({row["phase"] for row in samples}):
        rows = [row for row in samples if row["phase"] == phase]
        durations = [row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None]
        grouped[phase] = {
            "tool_calls": len(rows),
            "response_bytes": sum(row["response_bytes"] for row in rows),
            "elapsed_ms_total": sum(durations) if durations else None,
            "elapsed_ms_median": statistics.median(durations) if durations else None,
            **{
                key: sum(row[key] for row in rows) if instrument else None
                for key in (
                    "sql_statements",
                    "sql_connections",
                    "inspect_bytes_calls",
                    "draft_file_reads",
                    "draft_file_writes",
                )
            },
        }
    report = {
        "synthetic_only": True,
        "scripted_comparison_not_ai_dialogue": True,
        "source_dir": str(source),
        "build_id": first_instance["build_id"],
        "drafts_available": modern,
        "combined_recovery_available": combined,
        "instrumented": instrument,
        "timing_instrumentation_separated": True,
        "transport": "production MCP -> ServiceClient -> HTTP -> LocalService",
        "file_metric": (
            "Python .json/.tmp open events in .work-drafts, classified by read/write open flags; "
            "not byte counts or logical saves; excludes locks and directory opens"
        ),
        "tokens": None,
        "checks": checks,
        "phases": grouped,
        "samples": samples,
    }
    write_json(directory / "report.json", report)
    return report


def compare(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = extract_baseline(args.baseline_archive.resolve(), output / "baseline-source")
    current = output / "current-source"
    shutil.copytree(
        args.current_source.resolve(),
        current,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    phases = (False, True) if args.phase == "both" else (args.phase == "workload",)
    reports = {}
    for instrument in phases:
        phase = "workload" if instrument else "timing"
        for name, source in (("baseline", baseline), ("current", current)):
            directory = output / f"{name}-{phase}"
            command = [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "worker",
                "--output",
                str(directory),
                "--source-dir",
                str(source),
                "--repeats",
                str(args.repeats),
            ]
            if instrument:
                command.append("--instrument")
            with (output / f"{name}-{phase}.log").open("w", encoding="utf-8") as log:
                subprocess.run(command, cwd=REPO, stdout=log, stderr=log, check=True)
            reports[f"{name}-{phase}"] = json.loads(
                (directory / "report.json").read_text(encoding="utf-8")
            )
    write_json(
        output / "comparison.json",
        {
            "synthetic_only": True,
            "baseline_reference": args.baseline_reference,
            "scripted_comparison_not_ai_dialogue": True,
            "reports": reports,
            "note": (
                "Checkpoint cost, restart reads and formal verification are reported separately."
            ),
        },
    )
    print(output / "comparison.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("worker", "compare"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--current-source", type=Path, default=REPO / "src")
    parser.add_argument("--baseline-archive", type=Path)
    parser.add_argument("--baseline-reference", default="ece2a8d")
    parser.add_argument("--phase", choices=("both", "timing", "workload"), default="both")
    parser.add_argument("--instrument", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    if args.mode == "worker":
        if args.source_dir is None:
            parser.error("worker requires --source-dir")
        asyncio.run(
            worker(
                args.output.resolve(),
                args.source_dir.resolve(),
                instrument=args.instrument,
                repeats=args.repeats,
            )
        )
    elif args.baseline_archive is None:
        parser.error("compare requires --baseline-archive")
    else:
        compare(args)


if __name__ == "__main__":
    main()
