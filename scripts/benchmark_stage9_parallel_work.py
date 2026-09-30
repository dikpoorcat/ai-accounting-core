"""Diagnose complete brief work in the serial and resident parallel service paths.

This is an instrumented workload report, never a page-latency acceptance run.
Only explicitly verified Stage 9 synthetic books under workspace/.tmp are read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

if __package__:
    from .stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
else:
    from stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )


KINDS = ("materials", "duplicates", "reports")


def _measured_worker(kind, binding, period, as_of, send_pipe=None):
    """Test-only wrapper: run the production worker with its own measured Store."""
    from stage9_metrics import measure_work

    from ai_accounting.kernel import brief_parallel, storage
    from ai_accounting.kernel.engine import Engine

    path, company_id, database_id, taxpayer_id = binding
    store = storage.Store(
        path,
        brief_parallel._WORKER_BUNDLE,
        company_id,
        database_id,
        taxpayer_id=taxpayer_id,
        read_pool=brief_parallel._WORKER_POOL,
    )
    engine = Engine(store)
    original_store = storage.Store

    def operation():
        # _worker_read dynamically imports Store. Only this process is patched,
        # only for this one task, and its original validation/read path runs.
        storage.Store = lambda *args, **kwargs: store
        try:
            return brief_parallel._worker_read(kind, binding, period, as_of, send_pipe)
        finally:
            storage.Store = original_store

    work, packet = measure_work(engine, operation)
    return {**packet, "diagnostic_work": work, "diagnostic_pid": os.getpid()}


def _sum_work(reports):
    counters = Counter()
    sql = {}
    for report in reports:
        counters.update(report["counters"])
        for row in report["sql"]:
            statement = row["statement"]
            total = sql.setdefault(statement, {"statement": statement})
            for key, value in row.items():
                if key != "statement":
                    total[key] = total.get(key, 0) + value
    return {
        "counters": dict(counters),
        "sql": sorted(sql.values(), key=lambda row: (-row["calls"], row["statement"])),
    }


def _comparable_brief(response):
    # A generated display timestamp is expected to differ between two calls.
    # Every business, issue, source and version field remains in the comparison.
    comparable = {**response, "data": {**response["data"]}}
    comparable["data"].pop("generated_at", None)
    return comparable


def measure_brief_pair(root, company_id, period, static_runtime):
    """Return full-response parity and separate work for parent and each worker."""
    from stage9_metrics import measure_work

    from ai_accounting.kernel import brief_parallel
    from ai_accounting.kernel.dashboard import Dashboard
    from ai_accounting.kernel.service import LocalService
    from ai_accounting.kernel.types import canonical

    data = {"period": period, "limit": 100, "preparation": "complete"}
    serial = LocalService(root, enable_read_pool=True, _static_runtime=static_runtime)
    parallel = LocalService(
        root,
        enable_read_pool=True,
        enable_parallel_brief=True,
        _static_runtime=static_runtime,
    )
    try:
        # Instrumentation can be much slower than an ordinary request. Keep the
        # formal acceptance/fallback path while allowing the diagnostic to finish.
        parallel.brief_parallel.timeout_seconds = 120.0
        serial_engine = serial.engine(company_id, dashboard_read=True)
        serial_work, serial_result = measure_work(
            serial_engine,
            lambda: serial._dashboard_brief(Dashboard(serial_engine), serial_engine, data),
        )

        parallel_engine = parallel.engine(company_id, dashboard_read=True)
        pool = parallel.brief_parallel.pool
        original_apply_async = pool.apply_async
        original_finish = brief_parallel._BriefParallelAttempt.finish
        submitted = {}
        accepted = []

        def instrumented_apply_async(
            function, args=(), kwds=None, callback=None, error_callback=None
        ):
            if function is not brief_parallel._worker_read or args[0] not in KINDS:
                raise ValueError("Unexpected parallel brief worker submission")
            kind = args[0]
            if kind in submitted:
                raise ValueError("Duplicate parallel brief worker submission")
            job = original_apply_async(
                _measured_worker,
                args,
                kwds or {},
                callback,
                error_callback,
            )
            submitted[kind] = job
            return job

        pool.apply_async = instrumented_apply_async

        def observed_finish(attempt):
            result = original_finish(attempt)
            accepted.append(attempt.finished)
            return result

        brief_parallel._BriefParallelAttempt.finish = observed_finish
        try:

            def parent_operation():
                return parallel._dashboard_brief(Dashboard(parallel_engine), parallel_engine, data)

            # The commit guard now borrows Store.connection too, so the same
            # wrapper counts its PRAGMAs. A separate wrapper would double-count.
            parent_work, parallel_result = measure_work(parallel_engine, parent_operation)
        finally:
            pool.apply_async = original_apply_async
            brief_parallel._BriefParallelAttempt.finish = original_finish

        if set(submitted) != set(KINDS) or accepted != [True]:
            raise ValueError("Formal service did not accept all three parallel worker proofs")
        worker_work = {}
        worker_pids = {}
        for kind in KINDS:
            packet = submitted[kind].get(timeout=15)
            if not packet.get("ok") or "diagnostic_work" not in packet:
                raise ValueError(f"Parallel {kind} worker did not complete with work metrics")
            worker_work[kind] = packet["diagnostic_work"]
            worker_pids[kind] = packet["diagnostic_pid"]
        if canonical(_comparable_brief(serial_result)) != canonical(
            _comparable_brief(parallel_result)
        ):

            def differing(a, b, prefix=""):
                if isinstance(a, dict) and isinstance(b, dict):
                    for key in sorted(set(a) | set(b)):
                        yield from differing(a.get(key), b.get(key), f"{prefix}.{key}")
                elif isinstance(a, list) and isinstance(b, list):
                    for index, (left, right) in enumerate(zip(a, b, strict=False)):
                        yield from differing(left, right, f"{prefix}[{index}]")
                    if len(a) != len(b):
                        yield f"{prefix}.length: {len(a)} != {len(b)}"
                elif a != b:
                    yield f"{prefix}: {a!r} != {b!r}"

            raise ValueError(
                "Serial and formal parallel brief responses differ: "
                + "; ".join(
                    list(
                        differing(
                            _comparable_brief(serial_result), _comparable_brief(parallel_result)
                        )
                    )[:5]
                )
            )
        return {
            "response_equal": True,
            "response_comparison_excludes": ["data.generated_at"],
            "response_bytes": len(canonical(serial_result).encode("utf-8")),
            "serial": {
                "parent": serial_work,
                "total": _sum_work((serial_work,)),
                "python_peaks_by_process_bytes": {"parent": serial_work["python_peak_bytes"]},
            },
            "parallel": {
                "parent": parent_work,
                "guard_work_included_in_parent": True,
                "workers": worker_work,
                "worker_pids": worker_pids,
                "total": _sum_work((parent_work, *worker_work.values())),
                "python_peaks_by_process_bytes": {
                    "parent": parent_work["python_peak_bytes"],
                    **{kind: work["python_peak_bytes"] for kind, work in worker_work.items()},
                },
            },
            "measurement_scope": (
                "Instrumented diagnostic work, not page latency. SQLite VM steps are sampled "
                "in hundreds; returned bytes are values delivered to Python, not disk I/O. "
                "Per-process tracemalloc peaks include instrumentation and cannot be summed "
                "as simultaneous RSS. Connection validation before Store-bound queries is "
                "outside measure_work; the parallel guard's own SQL is counted in the parent."
            ),
        }
    finally:
        parallel.close()
        serial.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--book-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
        book_path = synthetic_path(args.book_report, workspace)
        output = synthetic_path(args.output, workspace)
        source = configure_source(args.source, workspace)
    except ValueError as exc:
        parser.error(str(exc))
    if output.exists():
        parser.error("Preserve previous work report; choose a new output file")
    if Path(sys.prefix).resolve() != workspace / ".tmp-kernel-venv":
        parser.error("Use workspace virtual environment")

    import benchmark_stage9_browser
    import stage9_metrics

    import ai_accounting

    require_source_module(ai_accounting, source, "src/ai_accounting/__init__.py")
    require_source_module(benchmark_stage9_browser, source, "scripts/benchmark_stage9_browser.py")
    require_source_module(stage9_metrics, source, "tests/kernel/stage9_metrics.py")
    from ai_accounting.kernel.daemon import _prepare_static_runtime

    static_runtime = _prepare_static_runtime()
    book = json.loads(book_path.read_text(encoding="utf-8"))
    benchmark_stage9_browser.require_report_source(book, source)
    expected_name = (
        "阶段九合成独立业务企业"
        if book.get("distribution") == "independent_local_pairs"
        else "阶段九合成规模企业"
    )
    period = benchmark_stage9_browser.validate_book_report(
        book,
        company_name=expected_name,
    )
    root = synthetic_path(Path(book["root"]), workspace)
    result = measure_brief_pair(root, book["company"]["id"], period, static_runtime)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "status": "diagnostic",
                "source": str(source),
                "measurement_script_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "workspace": str(workspace),
                "book_report": str(book_path),
                "root": str(root),
                "period": period,
                **result,
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
        stream.write("\n")
    print(output)


if __name__ == "__main__":
    main()
