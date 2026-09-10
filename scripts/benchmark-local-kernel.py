"""Reproducible isolated workload for the NEW SQLite kernel, never real companies.

Run with .tmp-kernel-venv/Scripts/python.exe. The history builder is a private
benchmark fixture: it batch-inserts complete fact/calculation/voucher graphs with
all SQLite constraints and durability settings enabled. Measured foreground
operations use the real typed Engine APIs. It is not a business import API.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ai_accounting.kernel.backup import (
    backup_to_file,
    create_portable,
    restore_portable,
    verify_file,
)
from ai_accounting.kernel.contracts import Fact, Line, Outcome, Registry
from ai_accounting.kernel.engine import PROGRAM_VERSION, Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen, YearMonth, canonical, digest

TAXPAYER = "91310000123456789A"
FIRST_MONTH = YearMonth("2016-01").ordinal
PROBE_MONTH = "2026-01"
HISTORY_MONTH = "2025-12"


class BenchmarkCharge(Fact):
    kind: ClassVar[str] = "benchmark_charge"
    amount: PositiveFen


def calculate(version, context):
    amount = version.fact.amount
    return Outcome(
        (Line("5602", debit=amount), Line("1002", credit=amount, cashflow="operating_expense")),
        {"amount": amount},
    )


class _CountingConnection:
    def __init__(self, connection, counts):
        self._connection, self._counts = connection, counts

    def execute(self, statement, parameters=()):
        self._counts["driver_calls"] += 1
        keyword = statement.lstrip().split(None, 1)[0].upper()
        if keyword in {"SELECT", "WITH", "EXPLAIN"}:
            self._counts["read_calls"] += 1
        return self._connection.execute(statement, parameters)

    def executemany(self, statement, parameters):
        self._counts["driver_calls"] += 1
        self._counts["executemany_calls"] += 1
        return self._connection.executemany(statement, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class CountingStore(Store):
    """Count application-issued SQL separately from SQLite trace/trigger events."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.counts = Counter()

    @contextmanager
    def connection(self, *, read_only=False):
        with super().connection(read_only=read_only) as connection:
            yield _CountingConnection(connection, self.counts)


def make_engine(path: Path) -> Engine:
    registry = Registry()
    registry.register(BenchmarkCharge, calculate)
    store = CountingStore.create(path, registry, "benchmark-company", TAXPAYER, "benchmark-db")
    return Engine(store)


def seed_to(
    engine: Engine, target: int, *, existing: int = 0, batch_size: int = 2000, progress=None
) -> dict:
    """Append complete synthetic histories, preserving triggers and FULL fsync."""
    if target < existing:
        raise ValueError("history may only grow")
    proof = engine.register_evidence(
        b"Synthetic benchmark history; no real accounting data.",
        "text/plain",
        "benchmark-fixture",
        request_id="benchmark-evidence",
    )["digest"]
    proof_bytes = bytes.fromhex(proof)
    periods = [FIRST_MONTH + index for index in range(120)]
    facts = [
        digest({"period": str(YearMonth.from_ordinal(period)), "amount": 100}) for period in periods
    ]
    outcome = asdict(
        Outcome(
            (Line("5602", debit=100), Line("1002", credit=100, cashflow="operating_expense")),
            {"amount": 100},
        )
    )
    outcome_json, outcome_digest = canonical(outcome), digest(outcome)
    started = time.perf_counter()
    with engine.store.connection() as connection:
        next_number = connection.execute("SELECT next_number FROM state").fetchone()[0]
        for start in range(existing, target, batch_size):
            finish = min(start + batch_size, target)
            rows = [
                (
                    n,
                    f"fixture:{n}",
                    f"ff:{n}",
                    f"fc:{n}",
                    f"fv:{n}",
                    f"fh:{n}",
                    periods[n % 120],
                    next_number + n - existing,
                )
                for n in range(start, finish)
            ]
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "INSERT INTO subject VALUES(?,?)", [(r[1], "benchmark_charge") for r in rows]
                )
                connection.executemany(
                    "INSERT INTO fact_revision VALUES(?,?,?,?,?)",
                    [(r[2], r[1], 1, r[6], facts[r[0] % 120]) for r in rows],
                )
                connection.executemany(
                    "INSERT INTO fact_benchmark_charge VALUES(?,?,?)",
                    [(r[2], r[6], 100) for r in rows],
                )
                connection.executemany(
                    "INSERT INTO fact_evidence VALUES(?,?)", [(r[2], proof_bytes) for r in rows]
                )
                connection.executemany(
                    "INSERT INTO fact_scope VALUES(?,?,?)",
                    [
                        (r[2], "benchmark_charge", key)
                        for r in rows
                        for key in (str(YearMonth.from_ordinal(r[6])), "@" + r[1])
                    ],
                )
                connection.executemany("INSERT INTO fact_seal VALUES(?)", [(r[2],) for r in rows])
                connection.executemany(
                    "INSERT INTO fact_current VALUES(?,?)", [(r[1], r[2]) for r in rows]
                )
                connection.executemany(
                    "INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (
                            r[3],
                            r[1],
                            r[2],
                            "benchmark_charge",
                            r[6],
                            outcome_json,
                            outcome_digest,
                            PROGRAM_VERSION,
                        )
                        for r in rows
                    ],
                )
                connection.executemany(
                    "INSERT INTO calculation_scope VALUES(?,?,?)",
                    [
                        (r[3], "benchmark_charge", key)
                        for r in rows
                        for key in (str(YearMonth.from_ordinal(r[6])), "@" + r[1])
                    ],
                )
                connection.executemany(
                    "INSERT INTO dependency_fact VALUES(?,?)", [(r[3], r[2]) for r in rows]
                )
                connection.executemany(
                    "INSERT INTO voucher VALUES(?,?)", [(r[4], r[7]) for r in rows]
                )
                connection.executemany(
                    "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)",
                    [
                        line
                        for r in rows
                        for line in (
                            (r[5], 1, "5602", 100, 0, None),
                            (r[5], 2, "1002", 0, 100, "operating_expense"),
                        )
                    ],
                )
                connection.executemany(
                    "INSERT INTO voucher_version VALUES(?,?,?,?,?,?)",
                    [(r[5], r[4], r[3], r[6], None, 100) for r in rows],
                )
                connection.executemany(
                    "INSERT INTO voucher_current VALUES(?,?)", [(r[4], r[5]) for r in rows]
                )
                connection.executemany(
                    "INSERT INTO calculation_publication VALUES(?,?,?)",
                    [(r[3], r[6], r[4]) for r in rows],
                )
                connection.executemany(
                    "INSERT INTO calculation_seal VALUES(?)", [(r[3],) for r in rows]
                )
                connection.executemany(
                    "INSERT INTO calculation_current VALUES(?,?)", [(r[1], r[3]) for r in rows]
                )
                connection.execute(
                    "UPDATE state SET accounting=accounting+1,next_number=?",
                    (next_number + finish - existing,),
                )
                connection.execute(
                    "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
                    (
                        f"fixture:{start}:{finish}",
                        "synthetic_benchmark_batch",
                        canonical({"first": start, "stop": finish}),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            if progress and (finish % 10000 == 0 or finish == target):
                progress(f"history {finish:,}/{target:,}, {time.perf_counter() - started:.1f}s")
    engine.rebuild_projections(request_id=f"fixture-rebuild-{target}")
    periods_service = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods_service.inventory(
            HISTORY_MONTH,
            category,
            evidence=[proof] if category == "transactions" else [],
            expected=1 if category == "transactions" else 0,
            no_business=category != "transactions",
            confirmation_evidence=proof,
            request_id=f"fixture-inventory-{target}-{category}",
        )
    return {
        "seconds": time.perf_counter() - started,
        "proof": proof,
        "synthetic_vouchers": target,
        "batch_size": batch_size,
    }


def memory_bytes() -> dict:
    if os.name == "nt":

        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        success = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb
        )
        if not success:
            raise ctypes.WinError()
        return {"rss_bytes": counters.WorkingSetSize, "peak_rss_bytes": counters.PeakWorkingSetSize}
    import resource

    unit = 1 if sys.platform == "darwin" else 1024
    return {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit}


def measure(engine: Engine, operation, *, repetitions: int = 7) -> tuple[dict, object]:
    samples, counts, locks, traces = [], [], [], []
    value = None
    before = memory_bytes()
    for index in range(repetitions):
        engine.store.counts.clear()
        engine.last_metrics = {}
        started = time.perf_counter()
        value = operation(index)
        samples.append(time.perf_counter() - started)
        counts.append(dict(engine.store.counts))
        locks.append(engine.last_metrics.get("lock_seconds", 0.0))
        traces.append(engine.last_metrics.get("sql_statements", 0))
    ordered = sorted(samples)
    return {
        "samples": repetitions,
        "seconds": samples,
        "p50_seconds": statistics.median(samples),
        "p95_seconds": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
        "driver_calls": [c.get("driver_calls", 0) for c in counts],
        "read_calls": [c.get("read_calls", 0) for c in counts],
        "lock_seconds": locks,
        "write_trace_events_including_triggers": traces,
        "memory_before": before,
        "memory_after": memory_bytes(),
    }, value


def foreground_probe(engine: Engine, proof: str, name: str):
    engine.save_fact(
        "benchmark_charge",
        name,
        {"period": PROBE_MONTH, "amount": 100},
        evidence=(proof,),
        expected_revision=0,
        request_id=name + ":fact",
    )
    preview = engine.preview([name])
    return engine.confirm(
        [name],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=name + ":post",
    )


def prepare_history_closes(engine, proof, *, before=HISTORY_MONTH, progress=None):
    """Private synthetic setup: freeze every earlier month using real period APIs."""
    if (engine.store.company_id, engine.store.database_id) != ("benchmark-company", "benchmark-db"):
        raise ValueError("historical checkpoint preparation is restricted to the synthetic company")
    if engine.store.registry.models != {"benchmark_charge": BenchmarkCharge}:
        raise ValueError("historical checkpoint preparation requires the benchmark-only registry")
    started = time.perf_counter()
    periods = Periods(engine)
    count = 0
    for month in range(FIRST_MONTH, YearMonth(before).ordinal):
        label = str(YearMonth.from_ordinal(month))
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                label,
                category,
                evidence=[proof] if category == "transactions" else [],
                expected=1 if category == "transactions" else 0,
                no_business=category != "transactions",
                confirmation_evidence=proof,
                request_id=f"fixture-checkpoint:{label}:{category}",
            )
        preview = periods.preview_close(label, owner_confirmation=proof)
        periods.close(
            label,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"fixture-checkpoint:{label}:close",
        )
        count += 1
        if progress and (count % 12 == 0 or month + 1 == YearMonth(before).ordinal):
            progress(f"historical checkpoints: {count}, through {label}")
    return {"periods": count, "seconds": time.perf_counter() - started}


def measure_close_copy(engine, proof, scale, *, samples=3, progress=print):
    preparation = prepare_history_closes(engine, proof, progress=progress)
    periods = Periods(engine)
    preview_metrics, preview = measure(
        engine,
        lambda _: periods.preview_close(HISTORY_MONTH, owner_confirmation=proof),
        repetitions=samples,
    )
    confirm_metrics, confirmed = measure(
        engine,
        lambda _: periods.close(
            HISTORY_MONTH,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"measured-close-{scale}",
        ),
        repetitions=1,
    )
    if (
        confirmed["status"] != "closed"
        or periods.closed_report(HISTORY_MONTH) != preview["manifest"]
    ):
        raise RuntimeError("committed period snapshot differs from reviewed preview")
    return {
        "historical_checkpoint_setup": preparation,
        "close_manifest_vouchers": len(preview["manifest"]["vouchers"]),
        "close_manifest_bytes": len(canonical(preview["manifest"]).encode()),
        "closed_snapshot_verified": True,
        "metrics": {
            "close_preview": preview_metrics,
            "close_confirm_on_restored_copy": confirm_metrics,
        },
    }


def measure_scale(
    engine: Engine,
    scale: int,
    proof: str,
    workspace: Path,
    *,
    samples=7,
    include_backups=True,
    progress=print,
) -> dict:
    result = {"synthetic_vouchers": scale, "metrics": {}}
    metrics = result["metrics"]
    metrics["record_save_preview_confirm"], _ = measure(
        engine, lambda n: foreground_probe(engine, proof, f"probe:{scale}:{n}"), repetitions=samples
    )
    metrics["overview"], _ = measure(
        engine, lambda _: engine.overview(HISTORY_MONTH), repetitions=samples
    )
    metrics["ledger_first_page"], first = measure(
        engine, lambda _: engine.ledger(HISTORY_MONTH, limit=100), repetitions=samples
    )
    after = first[-1]["number"] if first else 0
    metrics["ledger_cursor_page"], _ = measure(
        engine,
        lambda _: engine.ledger(HISTORY_MONTH, after_number=after, limit=100),
        repetitions=samples,
    )
    metrics["ledger_recent_sparse_period"], _ = measure(
        engine, lambda _: engine.ledger(PROBE_MONTH, limit=100), repetitions=samples
    )
    progress(f"{scale:,}: foreground, overview and cursor measured")
    metrics["rebuild_projections"], _ = measure(
        engine,
        lambda _: engine.rebuild_projections(request_id=f"measured-rebuild-{scale}"),
        repetitions=1,
    )
    progress(f"{scale:,}: projection rebuild measured")
    if include_backups:
        directory = workspace / f"backup-{scale}"
        metrics["portable_backup"], archive = measure(
            engine, lambda _: create_portable(engine.store.path, directory), repetitions=1
        )
        metrics["restore_and_verify"], restored = measure(
            engine,
            lambda _: restore_portable(
                archive["path"],
                directory / "restored.sqlite",
                expected_company_id="benchmark-company",
            ),
            repetitions=1,
        )
        result["backup_bytes"] = Path(archive["path"]).stat().st_size
        result["restored_evidence_count"] = restored["evidence_count"]
        progress(f"{scale:,}: portable backup, restore and verification measured")
        close_path = directory / "restored.sqlite"
    else:
        close_path = workspace / f"close-copy-{scale}.sqlite"
        backup_to_file(engine.store.path, close_path)
    restored_engine = Engine(
        CountingStore(
            close_path, engine.store.registry, engine.store.company_id, engine.store.database_id
        )
    )
    close_result = measure_close_copy(
        restored_engine, proof, scale, samples=min(3, samples), progress=progress
    )
    metrics.update(close_result.pop("metrics"))
    result.update(close_result)
    progress(f"{scale:,}: period close committed on isolated restored copy")
    with engine.store.connection() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result["actual_vouchers"] = connection.execute(
            "SELECT count(*) FROM voucher_current"
        ).fetchone()[0]
        result["actual_lines"] = connection.execute("SELECT count(*) FROM voucher_line").fetchone()[
            0
        ]
        result["query_plan_ledger"] = [
            r[3]
            for r in connection.execute(
                "EXPLAIN QUERY PLAN SELECT v.number,h.id,h.period FROM voucher v "
                "JOIN voucher_current a ON a.voucher_id=v.id "
                "JOIN voucher_version h ON h.id=a.version_id "
                "WHERE h.period=? AND v.number>? ORDER BY v.number LIMIT 100",
                (YearMonth(HISTORY_MONTH).ordinal, 0),
            )
        ]
    result["database_bytes"] = engine.store.path.stat().st_size
    result["source_verification"] = verify_file(
        engine.store.path, expected_company_id=engine.store.company_id
    )
    result["integrity"] = "ok"
    result["foreign_keys_ok"] = True
    return result


def write_report(report: dict, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 本地 SQLite 内核性能验收",
        "",
        f"状态：{report['status']}。",
        "",
        "使用隔离的合成公司。历史通过基准内部批量写入完整事实、计算、证据依赖和凭证图；",
        "STRICT、外键、封存触发器、WAL 和 FULL 同步始终启用。普通入账使用真实类型化 API。",
        "样本分布于 2016-01 至 2025-12 共 120 个月，每笔 100 分、两条分录，"
        "共用一份明确标识的合成证据。",
        "历史构建不是业务导入接口，不衡量百万次业务请求的端到端耗时。",
        "",
        f"运行时：Python {report['runtime']['python']}；SQLite {report['runtime']['sqlite']}。",
        f"机器：{report['runtime']['platform']}；逻辑 CPU：{report['runtime']['cpu_count']}。",
        "",
        "| 合成凭证数 | 普通入账 p95 ms | SELECT 调用数 | 总览 p95 ms | "
        "游标页 p95 ms | 关账预览 p95 ms | 重建 s |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if report.get("hotfix_refresh"):
        refresh = report["hotfix_refresh"]
        lines[4:4] = [
            f"修复后复测状态：{refresh['status']}，开始时间：{refresh['started_at']}。",
            "表中普通入账与关账两项采用修复后的复测；其余指标保留原三档测量。",
            "复测使用已有合成快照的 schema 和当前 Python 内核；两者 SHA-256 分别记录，"
            "未把历史快照 DDL 改写成当前版本，也不是正式数据库迁移。",
            "",
        ]
    for scale in report["scales"]:
        m = scale["metrics"]
        post = m["record_save_preview_confirm"]
        lines.append(
            f"| {scale['synthetic_vouchers']:,} | {post['p95_seconds'] * 1000:.2f} | "
            f"{min(post['read_calls'])}–{max(post['read_calls'])} | "
            f"{m['overview']['p95_seconds'] * 1000:.2f} | "
            f"{m['ledger_cursor_page']['p95_seconds'] * 1000:.2f} | "
            f"{m['close_preview']['p95_seconds'] * 1000:.2f} | "
            f"{m['rebuild_projections']['p50_seconds']:.2f} |"
        )
    lines.extend(
        [
            "",
            "| 合成凭证数 | 普通提交最大持锁 ms | 关账确认 s | 备份及校验 s | "
            "恢复及校验 s | 数据库 MiB | ZIP MiB | 累计峰值 RSS MiB |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scale in report["scales"]:
        m = scale["metrics"]
        post = m["record_save_preview_confirm"]
        optional = [
            f"{m[key]['p50_seconds']:.2f}" if key in m else "—"
            for key in (
                "close_confirm_on_restored_copy",
                "portable_backup",
                "restore_and_verify",
            )
        ]
        peak = max(metric["memory_after"]["peak_rss_bytes"] for metric in m.values())
        zip_size = f"{scale['backup_bytes'] / 1024**2:.1f}" if "backup_bytes" in scale else "—"
        lines.append(
            f"| {scale['synthetic_vouchers']:,} | {max(post['lock_seconds']) * 1000:.2f} | "
            f"{' | '.join(optional)} | {scale['database_bytes'] / 1024**2:.1f} | "
            f"{zip_size} | {peak / 1024**2:.1f} |"
        )
    lines.extend(
        [
            "",
            "普通入账包含确认事实、预览、确认发布；锁时间单独记录最后的凭证提交事务。",
            "driver_calls/read_calls 计应用显式发出的 SQL，executemany 算一次调用；"
            "不包括连接工厂的固定配置和身份查询。",
            "SQLite trace 次数另列，包含触发器重复 trace 事件，不能当成应用往返次数。",
            "p95 是有限样本的最近秩统计，不是容量保证；备份、恢复、重建仅测一次。",
            "关账预览测完整只读构建；关账确认另外在隔离恢复副本执行一次，"
            "验证正式封存快照与预览相同，不阻断主样本下一档追加历史。",
            "关账复测先在隔离副本通过真实资料清单、预览和关账 API 顺序冻结前 119 个月，"
            "每月保留原事实/计算/凭证版本与累计余额。准备时间单列，不计入目标月关账延迟。",
            "RSS 是操作系统进程工作集，峰值是累计进程峰值，不能当作单次操作分配量。",
            "百万规模仍只测试一个简单无依赖费用规则；复杂工资、深层更正图及大文件证据须单独验收。",
            "流水采用编号游标，无 OFFSET；当前执行计划仍先筛当月版本后按编号排序，"
            "故单页成本受该月记录数影响，不能宣称严格只访问一页记录。",
            "本轮为持续进程下的本机开发环境测量，没有做冷启动、独占机器或长期并发容量验收。",
            "本轮从空库构建；事实和计算均先写关系、再写 seal 后发布。"
            "代码与实际 schema 的 SHA-256 记录在 JSON，便于辨认开发中的测量版本。",
            "",
            "详细时序、SQL 次数、锁时间、工作集、文件大小与查询计划见同名 JSON。",
            "",
        ]
    )
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def refresh_existing_report(output: Path):
    """Remeasure only known synthetic snapshots; never alter historical schema DDL."""
    report = json.loads(output.read_text(encoding="utf-8"))
    repository = Path(__file__).resolve().parents[1]
    workspace = Path(report["workspace"]).resolve()
    if not workspace.is_relative_to(repository / ".tmp") or not workspace.name.startswith(
        "kernel-benchmark-"
    ):
        raise ValueError("refresh is restricted to this repository's synthetic benchmark workspace")
    tag = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destination = workspace / ("hotfix-refresh-" + tag)
    destination.mkdir(exist_ok=False)
    registry = Registry()
    registry.register(BenchmarkCharge, calculate)
    sources = sorted((repository / "src/ai_accounting/kernel").rglob("*.py"))
    sources.append(Path(__file__).resolve())
    refresh = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "workspace": str(destination),
        "scales": [],
        "source_sha256": {
            str(p.relative_to(repository)).replace("\\", "/"): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sources
        },
    }
    report["hotfix_refresh"] = refresh
    write_report(report, output)
    for scale in report["scales"]:
        count = scale["synthetic_vouchers"]
        directory = workspace / f"backup-{count}"
        proof = scale["construction"]["proof"]
        engine = Engine(
            CountingStore(
                directory / "restored.sqlite", registry, "benchmark-company", "benchmark-db"
            )
        )
        with engine.store.connection(read_only=True) as connection:
            schema_sql = "\n".join(
                row[0]
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
                )
            )
        post_metrics, _ = measure(
            engine,
            lambda n, engine=engine, proof=proof, count=count: foreground_probe(
                engine, proof, f"refresh:{tag}:{count}:{n}"
            ),
        )
        print(f"{count:,}: post-fix ordinary pipeline measured", flush=True)
        close_path = destination / f"close-{count}.sqlite"
        restore_portable(
            directory / f"{TAXPAYER}.finance-company.zip",
            close_path,
            expected_company_id="benchmark-company",
            expected_database_id="benchmark-db",
        )
        close_engine = Engine(
            CountingStore(close_path, registry, "benchmark-company", "benchmark-db")
        )
        close_result = measure_close_copy(
            close_engine,
            proof,
            count,
            progress=lambda message, count=count: print(f"{count:,}: {message}", flush=True),
        )
        current_metrics = close_result.pop("metrics")
        current_metrics["record_save_preview_confirm"] = post_metrics
        original = scale.setdefault("initial_metrics_before_cutoff_fix", {})
        for key, value in current_metrics.items():
            original.setdefault(key, scale["metrics"][key])
            scale["metrics"][key] = value
        scale.update(close_result)
        refresh["scales"].append(
            {
                "synthetic_vouchers": count,
                "snapshot_schema_sha256": hashlib.sha256(schema_sql.encode()).hexdigest(),
                "historical_checkpoint_setup": close_result["historical_checkpoint_setup"],
                "closed_snapshot_verified": close_result["closed_snapshot_verified"],
            }
        )
        write_report(report, output)
        print(f"{count:,}: post-fix period preview and close measured", flush=True)
    refresh["status"] = "complete"
    refresh["finished_at"] = datetime.now(UTC).isoformat()
    write_report(report, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", type=int, nargs="+", default=[10000, 100000, 1000000])
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--no-backups", action="store_true")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/local-kernel-benchmark.json"))
    parser.add_argument(
        "--refresh-existing-report",
        action="store_true",
        help="Remeasure ordinary posting and chronological close on known synthetic snapshots",
    )
    args = parser.parse_args()
    if args.refresh_existing_report:
        refresh_existing_report(args.output)
        return
    if args.samples < 1 or args.scales != sorted(set(args.scales)) or min(args.scales) < 1:
        parser.error("positive samples and strictly increasing positive scales are required")
    workspace = (
        args.workspace
        or Path(".tmp") / ("kernel-benchmark-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    ).resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    engine = make_engine(workspace / "company.sqlite")
    repository = Path(__file__).resolve().parents[1]
    source_paths = sorted((repository / "src/ai_accounting/kernel").rglob("*.py"))
    source_paths.append(Path(__file__).resolve())
    source_hashes = {
        str(path.relative_to(repository)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in source_paths
    }
    with engine.store.connection(read_only=True) as connection:
        schema_sql = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            )
        )
    report = {
        "format_version": 1,
        "status": "running",
        "workspace": str(workspace),
        "started_at": datetime.now(UTC).isoformat(),
        "scales": [],
        "runtime": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "source_sha256": source_hashes,
        "schema_sha256": hashlib.sha256(schema_sql.encode()).hexdigest(),
        "method": "trusted synthetic fixture batch insert; constraints and FULL sync enabled; "
        "foreground operations use public typed Engine calls; no real company data",
    }
    write_report(report, args.output)
    existing = 0
    for scale in args.scales:
        print(f"Starting {scale:,} voucher stage", flush=True)
        construction = seed_to(
            engine, scale, existing=existing, progress=lambda message: print(message, flush=True)
        )
        result = measure_scale(
            engine,
            scale,
            construction["proof"],
            workspace,
            samples=args.samples,
            include_backups=not args.no_backups,
            progress=lambda message: print(message, flush=True),
        )
        result["construction"] = construction
        report["scales"].append(result)
        write_report(report, args.output)
        existing = scale
    report["status"] = "complete"
    report["finished_at"] = datetime.now(UTC).isoformat()
    # No write follows the final stage's source verification.
    report["final_verification"] = report["scales"][-1]["source_verification"]
    write_report(report, args.output)
    print(f"Complete: {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
