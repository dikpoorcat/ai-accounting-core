"""Build a private mixed synthetic replay, then produce one projection per month.

This benchmark is intentionally not an accounting replay command. It accepts
only a new directory below the workspace's private ``.tmp`` root and generates
fictional business facts through the Stage 9 production fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

if __package__:
    from .stage9_source import configure_source, require_source_module, synthetic_path, workspace_root
else:
    from stage9_source import configure_source, require_source_module, synthetic_path, workspace_root


PROBE_MONTHS = (1, 12, 25, 49)
DEFAULT_EMPLOYEES = 2
DEFAULT_BUSINESSES = 40
EXPECTED_MONTHS = 57


def _timing_marker(engine):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT coalesce(max(rowid),0) FROM operation_timing").fetchone()
    return row[0]


def _write_timing_since(engine, marker):
    with engine.store.connection(read_only=True) as connection:
        rows = connection.execute(
            "SELECT request_id,stage,elapsed_ms FROM operation_timing WHERE rowid>? ORDER BY rowid",
            (marker,),
        ).fetchall()
    totals = {}
    for _, stage, elapsed_ms in rows:
        totals[stage] = totals.get(stage, 0) + elapsed_ms
    return {"write_count": len({row[0] for row in rows}), "milliseconds_by_stage": totals}


def _queue_periods(engine):
    with engine.store.connection(read_only=True) as connection:
        return [row[0] for row in connection.execute(
            "SELECT period FROM dashboard_projection_request ORDER BY period"
        )]


def _snapshot_tables(engine):
    with engine.store.connection(read_only=True) as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )}
        result = {}
        for name in (
            "dashboard_projection_publication",
            "dashboard_projection_current",
            "dashboard_read_model_commit",
            "dashboard_read_model_block",
        ):
            if name in names:
                result[name] = connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        return result


def _assert_source(engine, source):
    module_path = Path(sys.modules[engine.__class__.__module__].__file__).resolve()
    expected = (source / "src").resolve()
    if expected not in module_path.parents:
        raise RuntimeError(f"Engine imported outside requested worktree: {module_path}")
    return str(module_path)


def run(args):
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    root = synthetic_path(args.synthetic_root, workspace)
    output = synthetic_path(args.output, workspace)
    if root.exists() or output.exists():
        raise ValueError("Choose fresh private synthetic root and output paths")
    if args.months not in {1, EXPECTED_MONTHS}:
        raise ValueError("Only the one-month trial and the approved 57-month run are supported")
    if args.employees != DEFAULT_EMPLOYEES or args.businesses != DEFAULT_BUSINESSES:
        raise ValueError("The representative benchmark uses exactly 2 employees and 40 monthly businesses")

    source = configure_source(args.source, workspace)
    import ai_accounting.kernel.engine as engine_module

    require_source_module(engine_module, source, "src/ai_accounting/kernel/engine.py")
    from ai_accounting.kernel.backup import create_portable
    from ai_accounting.kernel.dashboard_async import run_pending
    from ai_accounting.kernel.maintenance import Maintenance
    from ai_accounting.kernel.schema_bundle import production_bundle

    # Imported only after source validation so pytest helpers and production
    # APIs resolve to this worktree, never the primary checkout's editable install.
    import stage9_book

    require_source_module(stage9_book, source, "tests/kernel/stage9_book.py")
    from stage9_book import MixedBook, month_at

    bundle = production_bundle()
    if bundle.status != "draft":
        raise RuntimeError("Synthetic replay requires the active development draft contract")

    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "building",
        "source": str(source),
        "engine_module": str(Path(engine_module.__file__).resolve()),
        "synthetic_root": str(root),
        "months_requested": args.months,
        "employees": args.employees,
        "businesses_per_month": args.businesses,
        "business_work": {},
        "projection_builds": [],
        "zip": None,
    }

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    book = MixedBook(root, employees=args.employees, businesses=args.businesses)
    engine = book.engine
    report["engine_module"] = _assert_source(engine, source)
    save()

    for index in range(args.months):
        prior_marker = _timing_marker(engine)
        before = time.perf_counter()
        month = book.add_month(index, close=index != args.months - 1)
        elapsed_ms = (time.perf_counter() - before) * 1000
        completed = index + 1
        if completed in PROBE_MONTHS:
            timings = _write_timing_since(engine, prior_marker)
            report["business_work"][str(completed)] = {
                "period": month["period"],
                "fixture_elapsed_ms": round(elapsed_ms, 3),
                "business_write_count": timings["write_count"],
                "operation_timing_ms": timings["milliseconds_by_stage"],
                "native_builds_during_replay": 0,
                "businesses": month["business_count"],
            }
        report["status"] = "building"
        report["completed_months"] = completed
        save()
        print(json.dumps({"month": completed, "period": month["period"], "elapsed_ms": round(elapsed_ms, 3)}), flush=True)

    expected_periods = [month_at(index) for index in range(args.months)]
    queued = _queue_periods(engine)
    if not set(expected_periods) <= set(queued):
        raise AssertionError({"expected_projection_periods": expected_periods, "queued": queued})
    if args.months == EXPECTED_MONTHS and set(queued) != set(expected_periods):
        raise AssertionError({"expected_projection_periods": expected_periods, "queued": queued})

    # Historical projections are generated only after the complete business
    # replay. run_pending does real native production and its real CAS publish.
    started = time.perf_counter()
    results = run_pending(engine, limit=len(queued))
    projection_elapsed_ms = (time.perf_counter() - started) * 1000
    if len(results) != len(queued) or any(item.get("status") != "ready" for item in results):
        raise AssertionError({"projection_results": results})
    result_periods = [item["period"] for item in results]
    if set(result_periods) != set(queued) or len(set(result_periods)) != len(queued):
        raise AssertionError({"projection_periods": result_periods})
    report["projection_builds"] = results
    report["projection_elapsed_ms"] = round(projection_elapsed_ms, 3)
    report["projection_build_count"] = len(results)
    report["projection_build_median_ms"] = statistics.median(
        item.get("elapsed_ms", 0) for item in results
    )
    report["model_tables"] = _snapshot_tables(engine)

    integrity = Maintenance(engine).verify_integrity()
    if integrity.get("status") != "verified" or integrity.get("limitations"):
        raise AssertionError({"integrity": integrity})
    report["integrity"] = integrity

    backup_dir = root / "verified-backup"
    backup_dir.mkdir()
    zips_before = list(backup_dir.glob("*.zip"))
    if zips_before:
        raise AssertionError("backup destination was not empty")
    backup = create_portable(
        engine.store.path,
        backup_dir,
        request_id="benchmark-replay-fixed-snapshot",
        _bundle=bundle,
    )
    zips_after = list(backup_dir.glob("*.zip"))
    if len(zips_after) != 1 or backup.get("verification", {}).get("status") != "verified":
        raise AssertionError({"backup": backup, "zip_files": [str(path) for path in zips_after]})
    report["zip"] = {
        "path": str(zips_after[0]),
        "verified": True,
        "identity": backup.get("identity"),
        "latest_closed_period": backup.get("latest_closed_period"),
    }
    report["status"] = "complete"
    save()
    print(json.dumps({"status": "complete", "months": args.months, "output": str(output)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path, help="workspace containing the private .tmp directory")
    parser.add_argument("--synthetic-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--months", type=int, default=EXPECTED_MONTHS)
    parser.add_argument("--employees", type=int, default=DEFAULT_EMPLOYEES)
    parser.add_argument("--businesses", type=int, default=DEFAULT_BUSINESSES)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
