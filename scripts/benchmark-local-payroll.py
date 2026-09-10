"""Measure real Engine payroll batches and dense correction graphs in isolated companies.

All source facts, payroll publications and corrections use typed Engine APIs.
No synthetic voucher, dependency or calculation row is inserted with raw SQL.
Run with the repository .tmp-kernel-venv Python (SQLite 3.53.1 or newer).
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import sqlite3
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.domains.payroll import (
    ContributionActualItem,
    ContributionRuleFact,
    Payroll,
    PayrollContributionActual,
    PayrollContributionPolicy,
    PayrollIncomeTaxPolicy,
    PayrollOpeningState,
    PayrollProfile,
    TaxBracketFact,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.payroll import CumulativeIncomeTaxPolicy

PERIODS = ("2026-01", "2026-02", "2026-03")


def memory_bytes():
    if os.name == "nt":

        class Counters(ctypes.Structure):
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

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(process),
            ctypes.byref(counters),
            counters.cb,
        ):
            raise ctypes.WinError()
        return {
            "rss_bytes": counters.WorkingSetSize,
            "process_peak_rss_bytes": counters.PeakWorkingSetSize,
        }
    import resource

    factor = 1 if sys.platform == "darwin" else 1024
    result = {"process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * factor}
    if Path("/proc/self/statm").is_file():
        result["rss_bytes"] = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
            "SC_PAGE_SIZE"
        )
    return result


class SQLConnection:
    def __init__(self, connection, counts):
        self.connection, self.counts = connection, counts

    def execute(self, sql, args=()):
        self.counts["driver_calls"] += 1
        if sql.lstrip().split(None, 1)[0].upper() in {"SELECT", "WITH", "EXPLAIN"}:
            self.counts["read_calls"] += 1
        return self.connection.execute(sql, args)

    def executemany(self, sql, args):
        self.counts["driver_calls"] += 1
        self.counts["executemany_calls"] += 1
        return self.connection.executemany(sql, args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class CountingStore(Store):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.counts = Counter()

    @contextmanager
    def connection(self, *, read_only=False):
        with super().connection(read_only=read_only) as connection:
            yield SQLConnection(connection, self.counts)


class CountingEngine(Engine):
    def __init__(self, store):
        super().__init__(store)
        self.transactions = []

    def _write(self, *args, **kwargs):
        value = super()._write(*args, **kwargs)
        self.transactions.append(dict(self.last_metrics))
        return value


def measure(engine, operation):
    engine.store.counts.clear()
    engine.transactions.clear()
    before = memory_bytes()
    maximum = [before.get("rss_bytes", 0)]
    stopped = threading.Event()

    def sample_memory():
        while not stopped.wait(0.025):
            maximum[0] = max(maximum[0], memory_bytes().get("rss_bytes", 0))

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        result = operation()
        elapsed = time.perf_counter() - started
    finally:
        stopped.set()
        sampler.join()
    after = memory_bytes()
    maximum[0] = max(maximum[0], after.get("rss_bytes", 0))
    return {
        "seconds": elapsed,
        "sql": dict(engine.store.counts),
        "transactions": len(engine.transactions),
        "total_lock_seconds": sum(item.get("lock_seconds", 0) for item in engine.transactions),
        "max_lock_seconds": max(
            (item.get("lock_seconds", 0) for item in engine.transactions), default=0
        ),
        "sqlite_trace_events_including_triggers": sum(
            item.get("sql_statements", 0) for item in engine.transactions
        ),
        "memory_before": before,
        "memory_after": after,
        "sampled_peak_rss_bytes": maximum[0],
        "rss_sampling_seconds": 0.025,
    }, result


def source_facts(employee):
    identity = f"employee-{employee:04}"
    yield (
        f"profile-{employee:04}",
        PayrollProfile(
            period="2026-01",
            employee_id=identity,
            effective_from="2026-01",
            effective_to=None,
            withholding_start_date="2026-01-01",
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=None,
            social_insurance_participating=True,
            housing_fund_participating=False,
            contribution_shortfall="reject",
        ),
    )
    yield (
        f"opening-{employee:04}",
        PayrollOpeningState(
            period="2026-01",
            employee_id=identity,
            through_period=None,
            cumulative_income_fen=0,
            cumulative_tax_exempt_income_fen=0,
            cumulative_standard_deduction_fen=0,
            cumulative_employee_contributions_fen=0,
            cumulative_special_additional_deduction_fen=0,
            cumulative_other_legal_deduction_fen=0,
            cumulative_tax_relief_fen=0,
            cumulative_withheld_tax_fen=0,
        ),
    )
    for period in PERIODS:
        yield (
            subject(employee, period),
            Payroll(
                period=period,
                employee_id=identity,
                profile_id=f"profile-{employee:04}",
                contribution_policy_id="contributions",
                income_tax_policy_id="income-tax",
                accounting_gross_salary_fen=1_000_000 + (employee % 5) * 50_000,
                tax_reported_salary_fen=1_000_000 + (employee % 5) * 50_000,
                tax_exempt_income_fen=0,
                special_additional_deduction_fen=0,
                other_legal_deduction_fen=0,
                tax_relief_fen=0,
                expense_class="management",
                contribution_basis="policy_until_actual",
            ),
        )


def subject(employee, period):
    return f"payroll-{employee:04}-{period}"


def policies():
    yield (
        "contributions",
        PayrollContributionPolicy(
            period="2026-01",
            version="synthetic-benchmark-contributions",
            jurisdiction="synthetic",
            effective_from="2026-01-01",
            effective_to="2026-12-31",
            primary_source_url="https://www.mof.gov.cn/",
            rules=(
                ContributionRuleFact(
                    code="pension",
                    base_kind="social_insurance",
                    employee_rate="0.08",
                    employer_rate="0.16",
                    minimum_base_fen=0,
                    maximum_base_fen=10_000_000,
                    rounding="half_up",
                    enabled=True,
                ),
            ),
        ),
    )
    policy = CumulativeIncomeTaxPolicy.china_resident_wage_withholding()
    yield (
        "income-tax",
        PayrollIncomeTaxPolicy(
            period="2026-01",
            version=policy.version,
            effective_from=policy.effective_from.isoformat(),
            effective_to=None,
            primary_source_url=policy.primary_source_url,
            legal_basis_source_url=policy.legal_basis_source_url,
            monthly_standard_deduction_fen=policy.monthly_standard_deduction_fen,
            brackets=tuple(
                TaxBracketFact(
                    upper_bound_fen=bracket.upper_bound_fen,
                    rate=str(bracket.rate),
                    quick_deduction_fen=bracket.quick_deduction_fen,
                )
                for bracket in policy.brackets
            ),
        ),
    )


def source_hashes():
    repository = Path(__file__).resolve().parents[1]
    paths = list((repository / "src/ai_accounting/kernel").rglob("*.py"))
    paths += list((repository / "src/ai_accounting/payroll").rglob("*.py"))
    paths.append(Path(__file__).resolve())
    return {
        str(path.relative_to(repository)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(paths)
    }


def current_results(engine):
    with engine.store.connection(read_only=True) as connection:
        calculations = {
            row["subject_id"]: row["id"]
            for row in connection.execute(
                "SELECT c.subject_id,c.id FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id "
                "WHERE c.kind='payroll'"
            )
        }
        numbers = {
            row["subject_id"]: row["number"]
            for row in connection.execute(
                "SELECT c.subject_id,s.number FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id "
                "JOIN voucher_version v ON v.calculation_id=c.id "
                "JOIN voucher_current h ON h.version_id=v.id "
                "JOIN voucher s ON s.id=v.voucher_id WHERE c.kind='payroll'"
            )
        }
    return calculations, numbers


def run_scale(size, workspace, progress):
    stage = {
        "employees": size,
        "months": len(PERIODS),
        "status": "running",
        "metrics": {},
        "source_sha256": source_hashes(),
    }
    engine = CountingEngine(
        CountingStore.create(
            workspace / f"employees-{size}.sqlite",
            default_registry(),
            f"payroll-benchmark-{size}",
            "91310000123456789A",
            f"payroll-benchmark-db-{size}",
        )
    )
    metrics = stage["metrics"]
    with engine.store.connection(read_only=True) as connection:
        schema = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            )
        )
    stage["schema_sha256"] = hashlib.sha256(schema.encode()).hexdigest()
    proof = engine.register_evidence(
        b"Synthetic payroll benchmark register: explicit employee profiles, "
        b"known-zero openings, wages and assessment revisions.",
        "text/plain",
        "synthetic-benchmark-evidence",
        request_id="proof",
    )["digest"]
    revisions = {}
    requests = [0]

    def save(sid, fact):
        requests[0] += 1
        result = engine.save_fact(
            fact.kind,
            sid,
            fact.model_dump(mode="json"),
            evidence=(proof,),
            expected_revision=revisions.get(sid, 0),
            request_id=f"source-{requests[0]}",
        )
        revisions[sid] = result["revision"]
        return result

    def prepare_sources():
        for sid, fact in policies():
            save(sid, fact)
        for employee in range(size):
            for sid, fact in source_facts(employee):
                save(sid, fact)
            if (employee + 1) % 100 == 0:
                progress(f"{size} employees: confirmed typed sources for {employee + 1}")

    metrics["source_confirmation"], _ = measure(engine, prepare_sources)
    metrics["monthly_batches"] = []
    for period in PERIODS:
        roots = [subject(employee, period) for employee in range(size)]
        preview_metrics, preview = measure(engine, lambda roots=roots: engine.preview(roots))
        confirm_metrics, result = measure(
            engine,
            lambda roots=roots, preview=preview, period=period: engine.confirm(
                roots,
                preview_digest=preview["digest"],
                epochs=preview["epochs"],
                request_id=f"publish-{period}",
            ),
        )
        if len(result["results"]) != size:
            raise AssertionError("monthly batch did not publish exactly its employees")
        metrics["monthly_batches"].append(
            {
                "period": period,
                "calculations": size,
                "preview": preview_metrics,
                "confirm": confirm_metrics,
            }
        )
        progress(
            f"{size} employees {period}: preview {preview_metrics['seconds']:.3f}s, "
            f"confirm {confirm_metrics['seconds']:.3f}s, "
            f"lock {confirm_metrics['max_lock_seconds']:.3f}s"
        )
        del preview, result

    def correct(employees, name, increment):
        before, before_numbers = current_results(engine)
        expected = {subject(employee, period) for employee in employees for period in PERIODS}

        def change_sources():
            for employee in employees:
                save(
                    f"actual-{employee:04}",
                    PayrollContributionActual(
                        period="2026-01",
                        employee_id=f"employee-{employee:04}",
                        items=(
                            ContributionActualItem(
                                code="pension",
                                base_kind="social_insurance",
                                state="declared",
                                employee_amount_fen=100_000 + increment + (employee % 3) * 1_000,
                                employer_amount_fen=200_000
                                + increment * 2
                                + (employee % 3) * 2_000,
                            ),
                        ),
                    ),
                )

        source_metrics, _ = measure(engine, change_sources)
        with engine.store.connection(read_only=True) as connection:
            pending = {
                row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM pending")
            }
        if pending != expected:
            raise AssertionError(
                f"{name}: pending scope {len(pending)} differs from expected {len(expected)}"
            )
        roots = [f"actual-{employee:04}" for employee in employees]
        preview_metrics, preview = measure(engine, lambda: engine.preview(roots))
        if set(preview["subjects"]) != expected:
            raise AssertionError(f"{name}: source correction included the wrong payroll subjects")
        confirm_metrics, _ = measure(
            engine,
            lambda: engine.confirm(
                roots,
                preview_digest=preview["digest"],
                epochs=preview["epochs"],
                request_id=name,
            ),
        )
        after, after_numbers = current_results(engine)
        changed = {sid for sid in before if before[sid] != after[sid]}
        if changed != expected or before_numbers != after_numbers:
            raise AssertionError(
                f"{name}: unrelated calculations changed or original voucher numbers were lost"
            )
        with engine.store.connection(read_only=True) as connection:
            if connection.execute("SELECT count(*) FROM pending").fetchone()[0]:
                raise AssertionError(
                    f"{name}: stale dependency obligations remain after publication"
                )
        metric = {
            "affected_employees": len(employees),
            "affected_calculations": len(expected),
            "unrelated_calculations_unchanged": len(before) - len(expected),
            "all_original_voucher_numbers_preserved": True,
            "source_confirmation": source_metrics,
            "preview": preview_metrics,
            "confirm": confirm_metrics,
        }
        progress(
            f"{size} employees {name} ({len(expected)} calculations): "
            f"preview {preview_metrics['seconds']:.3f}s, "
            f"confirm {confirm_metrics['seconds']:.3f}s, "
            f"lock {confirm_metrics['max_lock_seconds']:.3f}s"
        )
        return metric

    metrics["single_employee_backfill"] = correct([0], "single-backfill", 0)
    affected = list(range(0, size, 2))
    metrics["dense_correction_first"] = correct(affected, "dense-first", 10_000)
    metrics["dense_correction_repeated"] = correct(affected, "dense-repeated", 20_000)
    stage["verification"] = verify_file(engine.store.path)
    with engine.store.connection(read_only=True) as connection:
        stage["row_counts"] = {
            name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (
                "fact_revision",
                "calculation",
                "dependency_fact",
                "dependency_calculation",
                "dependency_scope",
                "voucher",
                "voucher_version",
                "voucher_line",
                "pending",
            )
        }
        stage["wal_and_durability"] = {
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
        }
    stage["database_bytes"] = engine.store.path.stat().st_size
    final_hashes = source_hashes()
    stage["source_files_changed_during_measurement"] = sorted(
        path
        for path in set(stage["source_sha256"]) | set(final_hashes)
        if stage["source_sha256"].get(path) != final_hashes.get(path)
    )
    stage["final_source_sha256"] = final_hashes
    stage["status"] = "complete"
    return stage


def write_report(report, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 本地 SQLite 工资批次与密集更正性能验收",
        "",
        f"状态：{report['status']}。",
        "",
        "每档使用独立合成公司，所有员工档案、明确零期初、工资和社保实际数"
        "均通过真实类型化 Engine API 保存；",
        "每人连续计算三个月工资，复用实际工资／社保／累计个税算法，不直接插入凭证、计算或依赖关系。",
        "当地社保比例是明确标记的合成测试政策，不代表任何地区实际执行政策。全部事实共用一份合成资料清单。",
        "",
        f"运行时：Python {report['runtime']['python']}；SQLite {report['runtime']['sqlite']}。",
        f"机器：{report['runtime']['platform']}；逻辑 CPU：{report['runtime']['cpu_count']}。",
        "",
        "| 员工数 | 操作 | 受影响计算数 | 预览 s | 确认 s | 确认持锁 s | "
        "预览 SELECT | 确认 SQL 调用 | 采样峰值 RSS MiB |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in report["scales"]:
        measurements = [
            (item["period"] + " 整月工资", item["calculations"], item)
            for item in stage["metrics"]["monthly_batches"]
        ]
        measurements += [
            (label, stage["metrics"][name]["affected_calculations"], stage["metrics"][name])
            for name, label in (
                ("single_employee_backfill", "单员工来源后补"),
                ("dense_correction_first", "半数员工首次更正"),
                ("dense_correction_repeated", "半数员工再次更正"),
            )
        ]
        for label, count, metric in measurements:
            preview, confirm = metric["preview"], metric["confirm"]
            peak = (
                max(preview["sampled_peak_rss_bytes"], confirm["sampled_peak_rss_bytes"]) / 1024**2
            )
            lines.append(
                f"| {stage['employees']} | {label} | {count} | {preview['seconds']:.3f} | "
                f"{confirm['seconds']:.3f} | {confirm['max_lock_seconds']:.3f} | "
                f"{preview['sql'].get('read_calls', 0)} | "
                f"{confirm['sql'].get('driver_calls', 0)} | {peak:.1f} |"
            )
    lines += [
        "",
        "确认耗时包含重新计算与预览核验，持锁时间只含发布事务；二者不能混同。",
        "SQL 调用数统计应用显式 execute／executemany，后者按一次驱动调用计；"
        "不包括连接初始化和固定公司身份检查。",
        "JSON 另记 SQLite trace 事件（含触发器重复事件）、来源确认时间与事务累计持锁、"
        "进程内存、数据库大小及行数。",
        "RSS 每 25 毫秒采样，短峰值可能漏采；操作系统累计峰值另外记录。"
        "每个操作只做一次，结果是单次测量，不宣称 p95 或容量上限。",
        "单员工后补必须只影响其三个月工资；两轮密集更正各影响半数员工及其三个月链，"
        "验证其余员工计算 ID 不变、所有原凭证号保留、待更正项清空。",
        "该验收不包含真实付款、闭期冲正、代发 Excel、长期政策变更与多人并发；"
        "不能据此推断这些场景的性能。",
        "代码和实际 SQLite schema 的 SHA-256 随每档结果保存；如测量期间源码变化，"
        "JSON 会明确列出，不掩盖开发并行带来的测量限制。",
        "",
    ]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", type=int, nargs="+", default=[100, 500])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/local-payroll-benchmark.json"))
    args = parser.parse_args()
    if args.scales != sorted(set(args.scales)) or min(args.scales) < 2:
        parser.error("scales must be distinct increasing employee counts, each at least two")
    workspace = (
        args.workspace
        or Path(".tmp") / ("payroll-benchmark-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    ).resolve()
    workspace.mkdir(parents=True, exist_ok=False)
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
        "method": (
            "real typed Engine source confirmation, payroll preview/confirm and "
            "dependency corrections; no direct SQL data seeding"
        ),
    }
    write_report(report, args.output)
    try:
        for size in args.scales:
            print(f"Starting real payroll benchmark: {size} employees x 3 months", flush=True)
            stage = run_scale(size, workspace, lambda message: print(message, flush=True))
            report["scales"].append(stage)
            write_report(report, args.output)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        write_report(report, args.output)
        raise
    report["status"] = "complete"
    report["finished_at"] = datetime.now(UTC).isoformat()
    write_report(report, args.output)
    print(f"Complete: {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
