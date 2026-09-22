"""Measure full-roster payroll no-change confirmation on isolated synthetic companies.

Run with the repository virtualenv, for example:
  .tmp-kernel-venv/Scripts/python.exe -X utf8 scripts/benchmark_stage6_payroll_confirmation.py \
    --output .tmp/stage6-payroll-confirmation.json

The fixture creates new temporary company databases only. It measures the public
prepare and preview paths and records the dependency rows written by publication.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "kernel"))

from test_payroll import (  # noqa: E402
    contribution_policy,
    income_tax_policy,
    opening,
    payroll,
    profile,
)
from test_payroll_corrections import Company  # noqa: E402

from ai_accounting.kernel.payroll_preparation import (  # noqa: E402
    PayrollNoChange,
    PayrollPreparation,
)
from ai_accounting.kernel.types import YearMonth  # noqa: E402


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", default="1,10,50")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timing-context", default="unspecified host load")
    parser.add_argument("--workspace", type=Path)
    return parser.parse_args()


def measured(repeats, operation):
    samples, result = [], None
    for _ in range(repeats):
        started = time.perf_counter()
        result = operation()
        samples.append(time.perf_counter() - started)
    return {
        "seconds": samples,
        "median_seconds": statistics.median(samples),
    }, result


def arrange(directory: Path, size: int):
    directory.mkdir(parents=True, exist_ok=False)
    company = Company(directory / "company.sqlite")
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    subjects = []
    for index in range(size):
        employee = f"employee-{index:04}"
        profile_id = f"profile-{index:04}"
        wage_id = f"january-{index:04}"
        company.save(profile(employee_id=employee), profile_id)
        company.save(opening(employee_id=employee), f"opening-{index:04}")
        company.save(payroll(employee_id=employee, profile_id=profile_id), wage_id)
        company.confirm_payroll(wage_id)
        subjects.append(wage_id)
    company.publish(*subjects)
    return company


def run_size(workspace: Path, size: int, repeats: int):
    company = arrange(workspace / f"employees-{size}", size)
    service = PayrollPreparation(company.engine)
    basis = service.reuse_basis("2026-02")
    if basis["fact_issues"] or len(basis["employees"]) != size:
        raise RuntimeError("synthetic no-change basis is incomplete")
    company.save(
        PayrollNoChange(
            period="2026-02",
            prior_period="2026-01",
            employees=tuple(basis["employees"]),
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "february-no-change",
    )
    prepare_metrics, proposal = measured(repeats, lambda: service.prepare("2026-02"))
    if proposal["status"] != "ready" or len(proposal["candidates"]) != size:
        raise RuntimeError("synthetic payroll preparation is not ready")
    saved = service.confirm(
        "2026-02",
        preview_digest=proposal["digest"],
        request_id=company.request(),
    )
    subjects = saved["publish_subjects"]
    preview_metrics, preview = measured(repeats, lambda: company.engine.preview(subjects))
    company.engine.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    with company.engine.store.connection(read_only=True) as connection:
        current_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT c.calculation_id FROM calculation_current c "
                "JOIN calculation r ON r.id=c.calculation_id "
                "WHERE r.kind IN ('payroll','payroll_bounded') AND r.period=?",
                (YearMonth("2026-02").ordinal,),
            )
        )
        placeholders = ",".join("?" for _ in current_ids)
        dependency_scope_rows = connection.execute(
            f"SELECT count(*) FROM dependency_scope WHERE calculation_id IN ({placeholders})",
            current_ids,
        ).fetchone()[0]
        dependency_fact_rows = connection.execute(
            f"SELECT count(*) FROM dependency_fact WHERE calculation_id IN ({placeholders})",
            current_ids,
        ).fetchone()[0]
        dependency_calculation_rows = connection.execute(
            f"SELECT count(*) FROM dependency_calculation WHERE calculation_id IN ({placeholders})",
            current_ids,
        ).fetchone()[0]
        roster_scope_rows = connection.execute(
            f"SELECT count(*) FROM dependency_scope WHERE calculation_id IN ({placeholders}) "
            "AND source='fact' AND kind='payroll_profile' AND scope_key='*'",
            current_ids,
        ).fetchone()[0]
    return {
        "employees": size,
        "prepare": prepare_metrics,
        "preview": preview_metrics,
        "published_calculations": len(current_ids),
        "dependency_scope_rows": dependency_scope_rows,
        "dependency_fact_rows": dependency_fact_rows,
        "dependency_calculation_rows": dependency_calculation_rows,
        "full_roster_scope_rows": roster_scope_rows,
        "database_bytes": company.engine.store.path.stat().st_size,
    }


def main():
    args = options()
    sizes = sorted({int(value) for value in args.sizes.split(",")})
    if not sizes or sizes[0] < 1 or args.repeats < 1:
        raise ValueError("sizes and repeats must be positive")
    workspace = args.workspace or ROOT / ".tmp" / (
        "stage6-payroll-confirmation-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    workspace.mkdir(parents=True, exist_ok=False)
    results = []
    for size in sizes:
        print(f"Measuring full-roster no-change confirmation for {size} employees", flush=True)
        results.append(run_size(workspace, size, args.repeats))
    output = {
        "generated_at": datetime.now(UTC).isoformat(),
        "timing_context": args.timing_context,
        "workspace": str(workspace),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
