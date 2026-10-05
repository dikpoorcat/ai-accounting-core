"""Build and measure new synthetic Stage 9 books; never opens configured data roots."""

from __future__ import annotations

import argparse
import atexit
import json
import statistics
import sys
import time
from contextlib import ExitStack
from pathlib import Path

if __package__:
    from .stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
    from .stage9_verified_open_preview import verify_book_open_preview
else:
    from stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
    from stage9_verified_open_preview import verify_book_open_preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path, help="workspace containing the synthetic .tmp")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--employees", type=int, default=50)
    parser.add_argument("--businesses", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--read-existing", action="store_true")
    parser.add_argument(
        "--verify-only", action="store_true",
        help="verify a completed checkpoint without timing reads",
    )
    parser.add_argument(
        "--resume", action="store_true", help="continue a completed synthetic month"
    )
    parser.add_argument("--instrument", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-build", action="store_true")
    parser.add_argument(
        "--stop-file", type=Path, help="stop safely after the current complete month"
    )
    parser.add_argument(
        "--build-only", action="store_true", help="defer measurement and full verification"
    )
    parser.add_argument(
        "--defer-historical-verification", action="store_true",
        help="test fixture only: require later independent full verification before measurement",
    )
    args = parser.parse_args()
    if args.resume and args.read_existing:
        parser.error("--resume and --read-existing are mutually exclusive")
    if args.verify_only and not args.read_existing:
        parser.error("--verify-only requires --read-existing")
    if args.defer_historical_verification and (not args.build_only or args.read_existing):
        parser.error("--defer-historical-verification is only for --build-only construction")
    try:
        workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
        root = synthetic_path(args.root, workspace)
        synthetic_path(args.output, workspace)
        if args.stop_file is not None:
            synthetic_path(args.stop_file, workspace)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output.exists():
        parser.error("preserve the previous result; choose a new output file")
    if args.months < 1 or args.repeats < 1:
        parser.error("months and repeats must be positive")
    source = configure_source(args.source, workspace)
    import stage9_book

    import ai_accounting

    require_source_module(ai_accounting, source, "src/ai_accounting/__init__.py")
    require_source_module(stage9_book, source, "tests/kernel/stage9_book.py")
    from ai_accounting.kernel.daemon import _prepare_static_runtime

    # Match resident startup before constructing a fixture book. Only static
    # package models are frozen; no company object or business result exists.
    _prepare_static_runtime()
    from stage9_book import MixedBook

    from ai_accounting.kernel.dashboard import Dashboard
    from ai_accounting.kernel.types import canonical

    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "creating",
        "source": str(source),
        "workspace": str(workspace),
        "root": str(args.root.resolve()),
        "requested_months": args.months,
        "default_page_limit": 20,
        "python": sys.version,
        "measurements": {},
    }

    def write():
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if args.read_existing:
        book = MixedBook.resume(root)
        if (
            book.employees_count != args.employees
            or book.businesses != args.businesses
            or len(book.month_stats) != args.months
        ):
            raise ValueError("completed synthetic sample differs from requested dimensions")
        engine = book.engine
        report.update(book.describe(), status="measuring")
    else:
        book = (
            MixedBook.resume(root)
            if args.resume
            else MixedBook(
                root, employees=args.employees, businesses=args.businesses
            )
        )
        if book.employees_count != args.employees or book.businesses != args.businesses:
            raise ValueError("resumed sample dimensions differ from the checkpoint")
        if len(book.month_stats) > args.months:
            raise ValueError("resumed sample contains more months than requested")
        engine = book.engine
        # Like the resident service, retain one private idle SQLite connection.
        # It holds no transaction and avoids repeatedly rebuilding the WAL index
        # between independent production write commands during sample creation.
        connections = ExitStack()
        connections.enter_context(engine.store.connection(read_only=True))
        atexit.register(connections.close)
        report.update(book.describe())
        if args.defer_historical_verification:
            connections.enter_context(book.defer_historical_verification_for_construction())
        if args.resume and len(book.month_stats) < args.months:
            book.close_last_month()
        for index in range(len(book.month_stats), args.months):
            if args.stop_file is not None and args.stop_file.exists():
                report.update(status="construction_stopped")
                write()
                connections.close()
                return
            try:
                if args.profile_build:
                    import cProfile

                    profiler = cProfile.Profile()
                    try:
                        stats = profiler.runcall(
                            book.add_month, index, close=index != args.months - 1
                        )
                    finally:
                        profiler.dump_stats(
                            str(args.output.with_suffix(f".build-{index + 1}.prof"))
                        )
                else:
                    stats = book.add_month(index, close=index != args.months - 1)
            except Exception as exc:
                report.update(
                    book.describe(),
                    status="creation_failed",
                    error={"type": type(exc).__name__, "message": str(exc), "details": vars(exc)},
                )
                write()
                raise
            report.update(book.describe())
            write()
            print(json.dumps(stats), flush=True)
        if args.build_only:
            report.update(status="built_not_verified")
            write()
            connections.close()
            return
        report["status"] = "measuring"
    # Do not label a shortened, partially written, or wholly closed book as the
    # completed main sample. Preserve its actual shape in the verification report.
    import benchmark_stage9_browser
    from benchmark_stage9_browser import validate_book_report

    require_source_module(benchmark_stage9_browser, source, "scripts/benchmark_stage9_browser.py")

    validate_book_report(
        report,
        company_name="阶段九合成规模企业",
        require_verified=False,
    )
    write()
    # The response summary above retains the required sample evidence. The
    # construction book also holds all historical fixture inputs/results,
    # which have no role in page timing or independent integrity verification.
    del book
    dashboard = Dashboard(engine)
    period = str(__import__("stage9_book").month_at(args.months - 1))
    from ai_accounting.kernel.business_queries import _today_china

    def preparation():
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            context = dashboard._read_context(connection, _today_china())
        return dashboard.period_preparation(
            period, expected_read_version=context["read_version"], as_of=context["as_of"]
        )

    operations = {
        name: (
            lambda name=name: getattr(dashboard, name)(period, limit=20, preparation="deferred")
        )
        for name in ("funds", "employees", "assets")
    }
    operations.update(
        context=dashboard.context,
        brief=lambda: dashboard.brief(period, limit=20, preparation="deferred"),
        preparation=preparation,
        reports=lambda: dashboard.quarterly_report(
            int(period[:4]), (int(period[5:]) - 1) // 3 + 1, preparation="deferred"
        ),
    )
    if args.verify_only:
        operations = {}
    for name, call in operations.items():
        try:
            call()
            samples, result = [], None
            for _ in range(args.repeats):
                start = time.perf_counter()
                result = call()
                samples.append((time.perf_counter() - start) * 1000)
        except Exception as exc:
            report.update(
                status="measurement_failed",
                error={"operation": name, "type": type(exc).__name__, "message": str(exc)},
            )
            write()
            raise
        report["measurements"][name] = {
            "milliseconds": samples,
            "median_ms": statistics.median(samples),
            "max_ms": max(samples),
            "response_bytes": len(canonical(result).encode()),
        }
        write()
        if args.instrument:
            import stage9_metrics
            from stage9_metrics import measure_work

            require_source_module(stage9_metrics, source, "tests/kernel/stage9_metrics.py")

            report["measurements"][name]["work"], _ = measure_work(engine, call)
            write()
        if args.profile:
            import cProfile

            profiler = cProfile.Profile()
            profiler.runcall(call)
            profiler.dump_stats(str(args.output.with_suffix(f".{name}.prof")))
    try:
        report.update(
            verify_book_open_preview(
                engine,
                checkpoint_path=root / "stage9-builder.json",
                company=report["company"],
                snapshots=report["snapshots"],
                period=period,
                source=source,
            )
        )
    except Exception as exc:
        report.update(
            status="verification_failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        write()
        raise
    report["status"] = "complete"
    write()
    print(json.dumps({"status": "complete", "output": str(args.output)}), flush=True)
    if not args.read_existing:
        connections.close()


if __name__ == "__main__":
    main()
