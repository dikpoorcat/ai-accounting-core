"""Compare isolated production MCP reading runs, timing and work in separate passes."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import statistics
import subprocess
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "kernel"))

from reading_agent_harness import parameters, value_from_mcp, write_json  # noqa: E402


@asynccontextmanager
async def mcp_session(directory, source_dir, instrument, *, resume=False, cache_build_id=None):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(
        parameters(
            directory,
            source_dir=source_dir,
            resume=resume,
            instrument=instrument,
            cache_build_id=cache_build_id,
        )
    ) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def worker(directory, source_dir, *, instrument, repeats):
    directory.mkdir(parents=True, exist_ok=False)
    original_source_dir = source_dir
    source_dir = directory / "source-snapshot"
    shutil.copytree(
        original_source_dir, source_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    responses = directory / "responses"
    responses.mkdir()
    samples, checks, skipped = [], {}, []

    async def invoke(session, label, command, payload=None, *, tool="finance_local_command"):
        arguments = (
            payload if tool == "finance_local_schema" else {"command": command, "payload": payload}
        )
        start = time.perf_counter()
        result = value_from_mcp(await session.call_tool(tool, arguments))
        elapsed_ms = (time.perf_counter() - start) * 1000
        workload_path = directory / "workload.jsonl"
        metric = json.loads(workload_path.read_text(encoding="utf-8").splitlines()[-1])
        number = len(samples) + 1
        write_json(responses / f"{number:03}-{label}.json", result)
        samples.append(
            {
                "scenario": label,
                "command": command,
                "tool_calls": 1,
                "elapsed_ms": None if instrument else elapsed_ms,
                "response_bytes": len(
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
                ),
                "sql_statements": metric["sql_statements"],
                "inspect_bytes_calls": metric["inspect_bytes_calls"],
                "tokens": None,
                "status": result.get("status") if isinstance(result, dict) else None,
            }
        )
        return result

    async def command(session, label, command_name, **payload):
        result = await invoke(session, label, command_name, payload)
        if isinstance(result, dict) and result.get("status") in {"rejected", "needs_information"}:
            raise AssertionError(f"Unexpected kernel result for {label}: {result}")
        return result

    async with mcp_session(directory, source_dir, instrument) as session:
        first_instance = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        tools = await session.list_tools()
        tool_schema = next(
            tool.inputSchema for tool in tools.tools if tool.name == "finance_local_schema"
        )
        modern = "view" in tool_schema.get("properties", {})
        seed = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
        company = seed["company_id"]
        inspect_payload = seed["inspect_payload"]
        for _ in range(repeats):
            default_schema = await invoke(
                session, "schema.default", "schema", {}, tool="finance_local_schema"
            )
            await invoke(
                session,
                "schema.full",
                "schema",
                {"view": "full"} if modern else {},
                tool="finance_local_schema",
            )
            if modern:
                await invoke(
                    session,
                    "schema.selected",
                    "schema",
                    {
                        "view": "selected",
                        "fact_kinds": ["expense"],
                        "commands": ["save_fact", "preview", "confirm"],
                    },
                    tool="finance_local_schema",
                )
        if not modern:
            skipped.append("schema.selected: baseline exposes only the complete schema")
        available_commands = set(default_schema["commands"])
        first = await command(session, "reading.cold_page1", "inspect_material", **inspect_payload)
        cursor = first["next_cursor"]
        page2 = await command(
            session, "reading.page2", "inspect_material", **inspect_payload, after=cursor
        )
        page3 = await command(
            session,
            "reading.page3",
            "inspect_material",
            **inspect_payload,
            after=page2["next_cursor"],
        )
        checks["page_rows"] = [len(page["items"]) for page in (first, page2, page3)]
        assert checks["page_rows"] == [100, 100, 50]
        assert page3["items"][-1]["location"] == "CSV!B251"
        assert page3["next_cursor"] is None
        for _ in range(repeats):
            hot = await command(session, "reading.hot_page1", "inspect_material", **inspect_payload)
            assert hot == first
        fact_filters = {"company_id": company, "period_from": "2026-03", "period_to": "2026-03"}
        equivalent = None
        for _ in range(repeats):
            await command(session, "context.separate", "company_context", company_id=company)
            parts = []
            for kind in (
                "expense",
                "material_source_v2",
                "material_period_allocation",
                "material_resolution_v2",
            ):
                parts.append(
                    await command(
                        session, "context.separate", "find_facts", **fact_filters, kind=kind
                    )
                )
            await command(
                session,
                "context.separate",
                "find_entities",
                company_id=company,
                query=seed["supplier_id"],
                kind="organization",
            )
            equivalent = {item["fact_id"] for part in parts for item in part["items"]}
            if "work_context" in available_commands:
                context = await command(
                    session, "context.aggregate", "work_context", **seed["work_context_payload"]
                )
                assert {item["fact_id"] for item in context["items"]} == equivalent
                assert {item["entity_id"] for item in context["entities"]} == {seed["supplier_id"]}
        checks["existing_facts"] = len(equivalent)
        if "work_context" not in available_commands:
            skipped.append("context.aggregate: baseline has no work_context")

    async with mcp_session(directory, source_dir, instrument, resume=True) as session:
        restarted_instance = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        assert restarted_instance["build_id"] == first_instance["build_id"]
        assert restarted_instance["catalog_id"] == first_instance["catalog_id"]
        restarted = await command(session, "reading.restart", "inspect_material", **inspect_payload)
        assert restarted == first
        checks["restart_same_result"] = True
        changed_map = json.loads(json.dumps(seed["specification"]))
        changed_map["columns"][0]["label"] = "服务项目"
        stale = await invoke(
            session,
            "reading.changed_map_stale",
            "inspect_material",
            {
                **inspect_payload,
                "specification": changed_map,
                "after": cursor,
            },
        )
        assert stale["code"] == "material_cursor_stale"
        await command(
            session,
            "reading.changed_map",
            "inspect_material",
            **{
                **inspect_payload,
                "specification": changed_map,
            },
        )
        raw = (
            Path(seed["source_bytes_file"])
            .read_bytes()
            .replace(b"0001", b"9001", 1)
        )
        new_evidence = await command(
            session,
            "reading.new_source_register",
            "evidence",
            company_id=company,
            content_base64=base64.b64encode(raw).decode(),
            media_type="text/csv",
            name="合成新增办公服务资料.csv",
            request_id="benchmark-new-source",
        )
        await command(
            session,
            "reading.new_source",
            "inspect_material",
            **{
                **inspect_payload,
                "evidence_digest": new_evidence["digest"],
            },
        )
        # This formal registration must inspect original bytes independently,
        # even though the same original was warmed before the restart.
        formal = await command(
            session,
            "reading.formal_receive",
            "receive_material",
            company_id=company,
            subject_id=seed["source_id"],
            data={
                "period": seed["period"],
                "evidence_digest": seed["evidence_digest"],
                "category": "transactions",
                "purpose": "business",
                "specification": seed["specification"],
            },
            evidence=[seed["evidence_digest"], seed["confirmation_evidence"]],
            expected_revision=seed["source_result"]["revision"],
            request_id="benchmark-formal-receive",
        )
        checks["formal_receive_status"] = formal["status"]
        replay = await command(
            session,
            "recovery.saved_request",
            "request_result",
            company_id=company,
            submitted_request_id="seed-expense-1",
        )
        checks["saved_request_found"] = replay.get("status")
    if modern:
        async with mcp_session(
            directory,
            source_dir,
            instrument,
            resume=True,
            cache_build_id="synthetic-reading-parser-build-2",
        ) as session:
            stale = await invoke(
                session,
                "reading.changed_build_stale",
                "inspect_material",
                {
                    **inspect_payload,
                    "after": cursor,
                },
            )
            assert stale["code"] == "material_cursor_stale"
            await command(session, "reading.changed_build", "inspect_material", **inspect_payload)
    else:
        skipped.append("reading.changed_build: baseline has no persistent cache build identity")
    grouped = {}
    for scenario in sorted({sample["scenario"] for sample in samples}):
        rows = [sample for sample in samples if sample["scenario"] == scenario]
        durations = [sample["elapsed_ms"] for sample in rows if sample["elapsed_ms"] is not None]
        width = 6 if scenario == "context.separate" else 1
        retrieval_durations = [
            sum(durations[index : index + width]) for index in range(0, len(durations), width)
        ]
        grouped[scenario] = {
            "samples": len(rows),
            "tool_calls": len(rows),
            "tool_calls_per_retrieval": width,
            "retrievals": len(rows) // width,
            "response_bytes_total": sum(row["response_bytes"] for row in rows),
            "response_bytes_mean": statistics.mean(row["response_bytes"] for row in rows),
            "elapsed_ms_median": (
                statistics.median(retrieval_durations) if retrieval_durations else None
            ),
            "sql_statements_total": sum(row["sql_statements"] for row in rows)
            if instrument
            else None,
            "inspect_bytes_calls_total": sum(row["inspect_bytes_calls"] for row in rows)
            if instrument
            else None,
        }
    report = {
        "source_dir": str(original_source_dir),
        "source_snapshot": str(source_dir),
        "build_id": first_instance["build_id"],
        "catalog_id": first_instance["catalog_id"],
        "seed_evidence_digest": seed["evidence_digest"],
        "instrumented": instrument,
        "repeats": repeats,
        "transport": (
            "production MCP SDK -> production stdio tools -> ServiceClient -> HTTP -> LocalService"
        ),
        "tokens": None,
        "timing_instrumentation_separated": True,
        "checks": checks,
        "skipped": skipped,
        "scenarios": grouped,
        "samples": samples,
    }
    write_json(directory / "report.json", report)
    return report


def extract_baseline(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(
                    "Baseline archive contains a path outside its extraction directory"
                )
        zipped.extractall(destination)
    if not (destination / "src" / "ai_accounting" / "kernel" / "mcp.py").is_file():
        raise ValueError("Baseline archive has no accounting source")
    return destination / "src"


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
    versions = (("baseline", baseline), ("current", current))
    phases = (False, True) if args.phase == "both" else (args.phase == "workload",)
    reports = {}
    # Sequential child processes keep source imports and timing workloads apart.
    for instrument in phases:
        phase = "workload" if instrument else "timing"
        for name, source in versions:
            run_dir = output / f"{name}-{phase}"
            command = [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "worker",
                "--output",
                str(run_dir),
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
                (run_dir / "report.json").read_text(encoding="utf-8")
            )
    write_json(
        output / "comparison.json",
        {
            "synthetic_only": True,
            "baseline_reference": args.baseline_reference,
            "timing_note": (
                "Timing runs exclude SQL and parser instrumentation; "
                "workload runs do not report timing."
            ),
            "cache_build_note": (
                "Changed-build case uses an explicit synthetic parser identity "
                "in the isolated service."
            ),
            "reports": reports,
        },
    )
    print(output / "comparison.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("compare", "worker"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--current-source", type=Path, default=REPO / "src")
    parser.add_argument("--baseline-archive", type=Path)
    parser.add_argument("--baseline-reference", default="457fefa")
    parser.add_argument("--phase", choices=("timing", "workload", "both"), default="both")
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
    else:
        if args.baseline_archive is None:
            parser.error("compare requires --baseline-archive")
        compare(args)


if __name__ == "__main__":
    main()
