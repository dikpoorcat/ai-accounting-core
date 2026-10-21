"""Independent repeated real lifecycle samples from one verified synthetic backup."""

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import uuid4

from ai_accounting_modular.contracts.public import CONTRACT

from .._support import COMPANY, ready
from .dataset import describe, open_dataset
from .lifecycle import submission, whole_month
from .measurements import BUDGETS, SAMPLES, WARMUPS, summarize
from .portable_cases import clone_baseline, package, prepare_baseline
from .projections import PAGES, parameters
from .workload import BUSINESSES_PER_MONTH, CONTRACTORS, EMPLOYEES, PEOPLE, SCALES, plan_month


def _checkpoint(destination, report):
    temporary = destination / "lifecycle-progress.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination / "lifecycle-progress.json")


def run(root, output_root, mode, scenario, *, probe=False):
    manifest = describe(root)
    assert scenario in {"commit_to_ready", "month_archive_and_ready"}
    if scenario == "month_archive_and_ready":
        intended = manifest["intended_scale_months"]
        period = manifest["pending_period"]
        assert intended == manifest["months"] + 1
    else:
        intended, period = manifest["months"], manifest["period_to"]
    if not probe:
        assert intended in SCALES and manifest["people"] == PEOPLE
        assert manifest["employees"] == EMPLOYEES and manifest["contractors"] == CONTRACTORS
        assert manifest["businesses_per_month"] == BUSINESSES_PER_MONTH
    destination = Path(output_root).resolve()
    destination.mkdir(parents=False, exist_ok=False)
    warmups, repeats = (1, 1) if probe else (WARMUPS, SAMPLES)
    attempts = []
    report = {
        "format": "modular-c14-lifecycle/1",
        "status": "running",
        "official": not probe,
        "measurement": scenario,
        "scale_months": intended,
        "employees": EMPLOYEES,
        "contractors": CONTRACTORS,
        "total_people": PEOPLE,
        "mode": mode,
        "warmups": warmups,
        "sample_count": repeats,
        "endpoint": "formal commit success to current documents and five page models ready",
        "setup_excluded": [
            "verified backup",
            "one verified isolated restore and closed baseline",
            "per-sample byte copy, complete source/target SHA256 and new catalog registration",
            "fixture planning",
            "prior ready baseline",
        ],
        "month_publication_boundary": "defer prepare_documents; final Apply resumes all facts",
        "attempts": attempts,
    }
    _checkpoint(destination, report)
    with TemporaryDirectory(prefix="portable-", dir=destination) as workspace:
        workspace = Path(workspace).resolve()
        assert workspace.is_relative_to(destination)
        backup = package(root, workspace / "baseline.zip", mode)
        baseline = prepare_baseline(backup, workspace / "baseline", manifest, mode)
        for iteration in range(warmups + repeats):
            kind = "warmup" if iteration < warmups else "sample"
            record = {
                "kind": kind,
                "iteration": iteration + 1,
                "phase": "byte_checked_clone",
                "status": "running",
                "elapsed_ms": None,
            }
            attempts.append(record)
            _checkpoint(destination, report)
            try:
                setup_start = perf_counter_ns()
                with TemporaryDirectory(prefix="sample-", dir=workspace) as sample:
                    clone_baseline(baseline, sample, manifest, mode)
                    with open_dataset(sample, mode, clock_period=period) as (kernel, _):
                        if scenario == "month_archive_and_ready":
                            planned = plan_month(
                                manifest["months"],
                                business_count=manifest["businesses_per_month"],
                                opening_bank=manifest["ending_bank_fen"],
                            )
                        else:
                            # The same one-line unpaid expense is amended from the
                            # same restored baseline in every sample, never a no-op.
                            source = manifest["months_data"][-1]["expense_id"]
                            current = kernel.application.accounting_get(
                                {
                                    "company_id": COMPANY,
                                    "business_ids": [source],
                                    "include": ["facts"],
                                }
                            )["businesses"][0]
                            fact = deepcopy(current["fact"])
                            fact["data"]["amount_fen"] = "1001"
                            fact["data"]["report_details"][0]["amount_fen"] = "1001"
                            request = {
                                "contract": CONTRACT,
                                "company_id": COMPANY,
                                "command_id": str(uuid4()),
                                "mutations": [
                                    {
                                        "op": "replace",
                                        "expected_seq": current["state"]["seq"],
                                        "fact": fact,
                                    }
                                ],
                            }
                            for page in PAGES:
                                ready(kernel, page, parameters(COMPANY, period, page))
                        record["setup_ms"] = (perf_counter_ns() - setup_start) / 1_000_000
                        record["phase"] = "commit_and_finish"
                        _checkpoint(destination, report)
                        result = (
                            whole_month(kernel, planned, observation=record)
                            if scenario == "month_archive_and_ready"
                            else submission(kernel, request, period, observation=record)
                        )
                        record.update(
                            phase="complete",
                            status="complete",
                            elapsed_ms=result["elapsed_ms"],
                            evidence=result.get("evidence", {}),
                        )
            except Exception as error:
                record.update(status="failed", error_type=type(error).__name__, message=str(error))
            finally:
                _checkpoint(destination, report)
    samples = [
        record["elapsed_ms"] if record["status"] == "complete" else None
        for record in attempts
        if record["kind"] == "sample"
    ]
    summary = summarize(samples, BUDGETS[scenario], expected=repeats)
    report.update(
        status=summary["status"]
        if all(record["status"] == "complete" for record in attempts)
        else "failed",
        summary=summary,
    )
    _checkpoint(destination, report)
    return report
