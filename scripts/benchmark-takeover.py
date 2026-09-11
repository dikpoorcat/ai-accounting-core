"""Measure takeover boundaries using isolated synthetic facts and real kernel APIs."""

from __future__ import annotations

import argparse
import ctypes
import gc
import http.client
import json
import random
import sqlite3
import statistics
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from ai_accounting.kernel.build import calculator_build_id
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import PROGRAM_VERSION, Engine
from ai_accounting.kernel.http import create_server
from ai_accounting.kernel.jobs import JobRunner
from ai_accounting.kernel.materials import Column, Materials, Specification, inspect_bytes
from ai_accounting.kernel.service import LocalService, default_registry

MIB = 1024 * 1024


def rss_bytes():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak",
                "working",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("Psapi.dll", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError()
    return counters.working


def measure(operation):
    gc.collect()
    start_rss = rss_bytes()
    peak = [start_rss]
    stopped = threading.Event()

    def sample():
        while not stopped.wait(0.01):
            peak[0] = max(peak[0], rss_bytes())

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started = time.perf_counter()
    try:
        result = operation()
    finally:
        seconds = time.perf_counter() - started
        peak[0] = max(peak[0], rss_bytes())
        stopped.set()
        thread.join()
    return {
        "seconds": seconds,
        "rss_before_bytes": start_rss,
        "sampled_peak_rss_bytes": peak[0],
        "sampled_rss_growth_bytes": max(0, peak[0] - start_rss),
    }, result


class SQLCounter:
    def __init__(self, store):
        self.store, self.original = store, store.connection
        self.count = 0
        self.hold_seconds = []

    @contextmanager
    def installed(self):
        @contextmanager
        def connection(*, read_only=False):
            with self.original(read_only=read_only) as raw:
                parent = self

                class Proxy:
                    begin = None

                    def execute(self, sql, parameters=()):
                        parent.count += 1
                        result = raw.execute(sql, parameters)
                        if sql.strip().upper() == "BEGIN IMMEDIATE":
                            self.begin = time.perf_counter()
                        return result

                    def executemany(self, sql, parameters):
                        parent.count += 1
                        return raw.executemany(sql, parameters)

                    def commit(self):
                        try:
                            return raw.commit()
                        finally:
                            if self.begin is not None:
                                parent.hold_seconds.append(time.perf_counter() - self.begin)
                                self.begin = None

                    def __getattr__(self, name):
                        return getattr(raw, name)

                yield Proxy()

        self.store.connection = connection
        try:
            yield self
        finally:
            self.store.connection = self.original


def make_engine(root):
    catalog = Catalog(root, default_registry())
    company = catalog.create_company("91310000123456789A", "合成接管性能验证")
    return catalog, Engine(catalog.bind(company["id"]))


def records(count, evidence):
    return [
        {
            "kind": "cash_funding",
            "subject_id": f"funding-{index}",
            "expected_revision": 0,
            "evidence": [evidence],
            "data": {
                "period": "2026-03",
                "owner_id": "synthetic-owner",
                "amount_fen": 100,
                "funding_kind": "capital",
                "actual_date": "2026-03-01",
                "cash_account_id": "cash",
            },
        }
        for index in range(count)
    ]


def evidence_boundary(engine, size):
    # Deterministic incompressible content exercises actual snapshot/ZIP I/O.
    content = random.Random(20260911).randbytes(size)
    metrics, proof = measure(
        lambda: engine.register_evidence(
            content, "application/octet-stream", "合成容量依据", request_id="large-proof"
        )
    )
    metrics.update({"bytes": size, "status": proof["status"]})
    try:
        engine.register_evidence(
            b"x" * (20 * MIB + 1), "application/octet-stream", "超界", request_id="over-proof"
        )
    except KernelError as exc:
        metrics["over_limit_code"] = exc.code
    else:
        raise AssertionError("Evidence size bound disappeared")
    return metrics, proof["digest"]


def material_boundary(rows, *, source_bytes=None, engine=None):
    spec = Specification(
        format="csv",
        columns=tuple(
            Column(
                column=letter,
                role="amount"
                if letter == "E"
                else "recognition_period"
                if letter == "A"
                else "context",
            )
            for letter in "ABCDEFGH"
        ),
        total_rows={"CSV": (rows + 2,)},
    )
    header = "日期,银行流水号,账户,对方,金额,余额,摘要,币种\n"
    control = (
        f"2026-03-31,total,bank-main,synthetic-party,{rows * 123 // 100}."
        f"{rows * 123 % 100:02d},100000.00,control-total,CNY\n"
    )

    def content(padding=0, extra=0):
        return (
            header
            + "".join(
                f"2026-03-01,txn-{index},bank-main,synthetic-party,1.23,100000.00,"
                f"receipt{'x' * (padding + (index < extra))},CNY\n"
                for index in range(rows)
            )
            + control
        ).encode("utf-8")

    raw = content()
    if source_bytes is not None:
        assert source_bytes >= len(raw)
        raw = content(*divmod(source_bytes - len(raw), rows))
        assert len(raw) == source_bytes
    metrics, result = measure(lambda: inspect_bytes(raw, spec))
    assert len(result["items"]) == rows and not result["issues"]
    assert result["control_totals"][0]["actual_fen"] == rows * 123
    metrics.update(
        {
            "data_rows": rows,
            "columns": 8,
            "source_bytes": len(raw),
            "header_rows": 1,
            "control_rows": 1,
            "nonempty_cells": len(result["coverage"]),
            "normalized_items": len(result["items"]),
            "total_fen": sum(x["amount_fen"] for x in result["items"]),
        }
    )
    del result
    if engine is not None:
        proof = engine.register_evidence(
            raw, "text/csv", "合成十万行银行流水", request_id="material-proof"
        )["digest"]
        api = Materials(engine)
        public_metrics, page = measure(lambda: api.inspect(proof, spec.model_dump(mode="json")))
        public_metrics.update(
            {
                "items_returned": len(page["items"]),
                "locations_returned": len(page["coverage"]),
                "items_total": page["summary"]["item_count"],
                "json_bytes": len(json.dumps(page, ensure_ascii=False).encode("utf-8")),
                "has_next_cursor": page["next_cursor"] is not None,
            }
        )
        assert len(page["items"]) == min(rows, 100)
        assert page["summary"]["item_count"] == rows
        assert page["status"] == "ready"
        metrics["public_first_page"] = public_metrics
    boundary_spec = Specification(
        format="csv", columns=(Column(column="A", role="amount"),), header_rows={"CSV": 0}
    )
    rejected, response = measure(lambda: material_rejection(b"1.23\n" * 100_001, boundary_spec))
    metrics["over_limit_rows"] = {**rejected, "status": response, "data_rows": 100_001}
    metrics["over_limit_width"] = material_rejection(b",".join([b"1"] * 65), boundary_spec)
    return metrics


def material_rejection(raw, spec):
    try:
        inspect_bytes(raw, spec)
    except KernelError as exc:
        assert exc.code == "material_too_large"
        return exc.code
    raise AssertionError("Material cell count bound disappeared")


def batch_boundary(engine, evidence, count):
    batch = records(count, evidence)
    with SQLCounter(engine.store).installed() as counter:
        metrics, result = measure(lambda: engine.save_facts(batch, request_id="large-batch"))
    assert len(result["results"]) == count
    metrics.update(
        {
            "facts": count,
            "sql_driver_calls": counter.count,
            "write_lock_seconds": sum(counter.hold_seconds),
        }
    )
    before = engine.overview("2026-03")
    try:
        engine.save_facts(records(5001, evidence), request_id="over-batch")
    except ValueError:
        metrics["over_limit_rejected"] = True
    else:
        raise AssertionError("Fact batch count bound disappeared")
    assert engine.overview("2026-03") == before
    subjects = [item["subject_id"] for item in batch]
    preview_metrics, preview = measure(lambda: engine.preview(subjects))
    with SQLCounter(engine.store).installed() as counter:
        publish_metrics, published = measure(
            lambda: engine.confirm(
                subjects,
                preview_digest=preview["digest"],
                epochs=preview["epochs"],
                request_id="publish-batch",
            )
        )
    publish_metrics.update(
        {
            "sql_driver_calls": counter.count,
            "write_lock_seconds": sum(counter.hold_seconds),
            "vouchers": len(published["results"]),
        }
    )
    return {"save": metrics, "preview": preview_metrics, "publish": publish_metrics}


def report_facts(engine, evidence):
    for kind, subject, data in (
        (
            "report_profile",
            "profile",
            {
                "period": "2026-01",
                "company_name": "合成接管性能验证",
                "accounting_standard": "small_enterprise",
                "bookkeeping_start": "2026-01",
                "newly_established_zero_opening_confirmed": True,
            },
        ),
        (
            "report_income_tax_confirmation",
            "income-tax",
            {
                "period": "2026-03",
                "treatment": "zero",
                "cumulative_assessed_fen": 0,
                "explanation": "合成场景仅有出资，无应税所得",
            },
        ),
    ):
        engine.save_fact(
            kind=kind,
            subject_id=subject,
            data=data,
            evidence=[evidence],
            expected_revision=0,
            request_id=subject,
        )


def foreground_during_backup(catalog, engine, directory, *, repetitions=3):
    service = LocalService(catalog.root)
    password = SecretStr("Synthetic-benchmark-owner-20260911")
    service.security.provision("benchmark-owner", password)
    token = service.security.login("benchmark-owner", password).session_token
    server, capability = create_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(command, payload):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=60)
        try:
            connection.request(
                "POST",
                "/api/command",
                json.dumps({"command": command, "payload": payload}),
                {
                    "Content-Type": "application/json",
                    "X-Local-Capability": capability,
                    "Authorization": "Bearer " + token.get_secret_value(),
                },
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            assert response.status == 200, result
            return result
        finally:
            connection.close()

    operations = (
        (
            "overview",
            lambda: request(
                "overview",
                {
                    "company_id": engine.store.company_id,
                    "period": "2026-03",
                },
            ),
        ),
        (
            "quarter_report",
            lambda: request(
                "report",
                {
                    "company_id": engine.store.company_id,
                    "year": 2026,
                    "quarter": 1,
                },
            ),
        ),
    )
    try:
        return _foreground_measurements(catalog, engine, directory, operations, repetitions)
    finally:
        service.security.logout(token)
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _foreground_measurements(catalog, engine, directory, operations, repetitions):
    baseline, reference = {}, {}
    for name, operation in operations:
        baseline[name] = []
        for _ in range(repetitions):
            metrics, value = measure(operation)
            baseline[name].append(metrics)
        reference[name] = value
    assert reference["quarter_report"]["status"] == "ready", reference["quarter_report"].get(
        "fact_issues"
    )
    queued = engine.queue_backup(str(directory), request_id="background-backup")
    runner = JobRunner(catalog, interval=0.05)
    started = time.perf_counter()
    runner.start()
    concurrent = {"overview": [], "quarter_report": []}

    def job():
        return next(item for item in engine.jobs() if item["id"] == queued["job_id"])

    try:
        deadline = time.monotonic() + 60
        while job()["status"] == "pending":
            assert time.monotonic() < deadline
            time.sleep(0.005)
        while job()["status"] in {"pending", "running"}:
            for name, operation in operations:
                active = job()["status"] == "running"
                metrics, value = measure(operation)
                assert value == reference[name]
                metrics["backup_active_at_start"] = active
                metrics["backup_active_at_end"] = job()["status"] == "running"
                concurrent[name].append(metrics)
            assert time.monotonic() < deadline
        completed = job()
        assert completed["status"] == "succeeded", completed
    finally:
        runner.stop()
    return {
        "transport": "authenticated loopback HTTP /api/command including JSON serialization",
        "baseline": baseline,
        "during_backup": concurrent,
        "backup_seconds_including_worker_start": time.perf_counter() - started,
        "backup_attempts": completed["attempts"],
        "foreground_results_unchanged": True,
    }


def run(root, *, evidence_size=20 * MIB, material_rows=100_000, batch_size=5000):
    root.mkdir(parents=True, exist_ok=False)
    result = {
        "build_before": calculator_build_id(),
        "process_program_version": PROGRAM_VERSION,
        "sqlite": sqlite3.sqlite_version,
        "python": sys.version.split()[0],
        "created_at": datetime.now(UTC).isoformat(),
        "method": (
            "real typed Engine/Materials/Reports/JobRunner; RSS sampled every 10ms; "
            "SQL counts exclude schema verification and trigger internals"
        ),
    }
    catalog, engine = make_engine(root / "data")
    result["evidence"], evidence = evidence_boundary(engine, evidence_size)
    print("evidence completed", flush=True)
    result["materials"] = material_boundary(material_rows, source_bytes=20 * MIB, engine=engine)
    print("material parsing completed", flush=True)
    result["fact_batch"] = batch_boundary(engine, evidence, batch_size)
    print("fact batch publication completed", flush=True)
    report_facts(engine, evidence)
    result["foreground"] = foreground_during_backup(catalog, engine, root / "backups")
    result["build_after"] = calculator_build_id()
    result["database_bytes"] = engine.store.path.stat().st_size
    return result


def write_report(result, json_path):
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 本地服务接管性能实测",
        "",
        f"构建：`{result['build_before']}`。",
        "",
        "所有数据均为隔离目录中的合成数据。RSS 每 10 ms 抽样，不是分配量或严格内存上界；"
        "耗时包含相应 API 的真实工作。",
        result.get("measurement_environment", "运行时未记录其他进程的活动；不宣称独占主机。"),
        "",
        "| 场景 | 规模 | 秒 | RSS 峰值 MiB |",
        "|---|---:|---:|---:|",
    ]
    for name, size, metrics in (
        ("证据登记", result["evidence"]["bytes"], result["evidence"]),
        (
            "银行 CSV 解析（8列，另含表头与控制行）",
            result["materials"]["data_rows"],
            result["materials"],
        ),
        ("事实批量保存", result["fact_batch"]["save"]["facts"], result["fact_batch"]["save"]),
        (
            "公开资料首页（全量解析后分页）",
            result["materials"]["public_first_page"]["items_returned"],
            result["materials"]["public_first_page"],
        ),
        ("批量计算预览", result["fact_batch"]["save"]["facts"], result["fact_batch"]["preview"]),
        (
            "批量发布凭证",
            result["fact_batch"]["publish"]["vouchers"],
            result["fact_batch"]["publish"],
        ),
    ):
        lines.append(
            f"| {name} | {size:,} | {metrics['seconds']:.3f} | "
            f"{metrics['sampled_peak_rss_bytes'] / MIB:.1f} |"
        )
    lines += [
        "",
        f"材料公开首页返回{result['materials']['public_first_page']['items_returned']}条，"
        f"JSON {result['materials']['public_first_page']['json_bytes']:,}字节。"
        "游标绑定原件、映射及解析程序版本；目前每页重新解析完整原件，属于大文件预处理成本，"
        "不是亚秒交互读取。内部接收与完整性核验仍覆盖全部原行。",
        "",
        "资料按十万条数据行计数，表头与控制行另计；100,001条数据行和65列超宽资料明确拒绝。"
        "安全边界还包括200万个非空单元格、32张表、每表100行表头、1000个控制行与200 MiB解压内容。"
        "20 MiB + 1 字节的证据和 5,001 条事实批次也明确拒绝。",
        "",
        "| 写操作 | 应用 SQL 调用 | 持写锁秒 |",
        "|---|---:|---:|",
    ]
    for name in ("save", "publish"):
        metric = result["fact_batch"][name]
        lines.append(
            f"| {name} | {metric['sql_driver_calls']:,} | {metric['write_lock_seconds']:.3f} |"
        )
    lines += [
        "",
        "SQL 调用数按驱动 execute / executemany 调用计，排除连接结构校验与触发器内部 SQL。"
        "持锁时间从 BEGIN IMMEDIATE 成功到 COMMIT 完成。",
        "",
        "| 前台查询 | 空闲中位秒 | 备份中最大秒 | 开始时备份运行的样本数 |",
        "|---|---:|---:|---:|",
    ]
    for name in ("overview", "quarter_report"):
        baseline = result["foreground"]["baseline"][name]
        concurrent = result["foreground"]["during_backup"][name]
        active = [item for item in concurrent if item["backup_active_at_start"]]
        maximum = f"{max(item['seconds'] for item in active):.3f}" if active else "无重叠样本"
        lines.append(
            f"| {name} | {statistics.median(item['seconds'] for item in baseline):.3f} | "
            f"{maximum} | {len(active)} |"
        )
    lines += [
        "",
        "备份期间前台返回内容与空闲基线逐项一致。这里报告本机实测值，不将亚秒目标当作保证。"
        "数据库主文件大小不包含活动 WAL；完整数据只存在隔离测试目录。",
        "前台查询通过真实 loopback HTTP，包含目录身份校验、公司连接绑定和 JSON 编解码。"
        "5000事实批次使用无上游依赖的现金出资；不据此推定复杂工资批次具有相同耗时。",
        "",
        f"开始与结束源码构建一致：`{result['build_before'] == result['build_after']}`。"
        "若不一致，正式结论需冻结源码后复测。",
        "",
    ]
    json_path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_report(run(args.root), args.output)
