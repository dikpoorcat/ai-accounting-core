"""Measure approved T5 read seams on fresh synthetic companies, never installed data.

Run with .tmp-kernel-venv/Scripts/python.exe scripts/verify_t5_read_costs.py.
T4 evidence is retained. A failed phase can be repeated with --phase and a new
--output path; independent results must not be combined as one successful run.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "kernel"))

from test_dashboard_bounded_reads import layered_book  # noqa: E402
from test_deletion_boundaries import book as settlement_book  # noqa: E402
from test_reports import book as report_book  # noqa: E402
from test_reports import cit  # noqa: E402
from test_unresolved_settlement_reads import mismatched_frozen_payment  # noqa: E402

from ai_accounting.kernel.business_queries import BusinessQueries  # noqa: E402
from ai_accounting.kernel.dashboard import Dashboard  # noqa: E402
from ai_accounting.kernel.query_reads import QueryReads  # noqa: E402
from ai_accounting.kernel.read_indexes import (  # noqa: E402
    sync_audit,
    sync_job,
    verify_read_indexes,
)
from ai_accounting.kernel.reports import Reports, check_report_readiness  # noqa: E402
from ai_accounting.kernel.types import YearMonth, canonical  # noqa: E402
from ai_accounting.kernel.versions import current_version, verify_schema  # noqa: E402

INTERVAL = 100
PERIOD = "2026-01"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_inventory():
    paths = {Path(__file__).resolve()}
    for directory in (ROOT / "src" / "ai_accounting", ROOT / "tests" / "kernel"):
        paths.update(directory.rglob("*.py"))
    paths.update((ROOT / "src" / "ai_accounting" / "kernel" / "migrations").glob("*.json"))
    return {path.relative_to(ROOT).as_posix(): sha(path) for path in sorted(paths)}


def used_sources():
    paths = {Path(__file__).resolve()}
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(ROOT / "src") or path.is_relative_to(ROOT / "tests"):
                paths.add(path)
    paths.update((ROOT / "src" / "ai_accounting" / "kernel" / "migrations").glob("*.json"))
    return sorted(path.relative_to(ROOT).as_posix() for path in paths)


class Paths:
    def __init__(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="t5-read-costs-", dir=ROOT / ".tmp"))

    def mktemp(self, name):
        path = self.root / name
        path.mkdir()
        return path


def verify(engine):
    """Explicit full checks are outside instrumented requests."""
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        version = verify_schema(connection, registry=engine.store.registry)
        assert version == current_version("business")
        return {"business_schema": version, "read_indexes": verify_read_indexes(connection)}


def inventory(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "subject",
                "fact_revision",
                "calculation",
                "voucher_version",
                "voucher_line",
                "period_close",
                "jobs",
                "job_reference",
                "audit",
                "audit_reference",
            )
        }


def category(sql):
    lower = sql.lower()
    if "fact_report_classification" in lower:
        return "classification_scalars"
    if "$.balances" in lower:
        return "financial_effects"
    if "voucher_line" in lower and "sum(" in lower:
        return "ledger_aggregate"
    return "other"


def measure(engine, operation, *, excluded_subjects=(), fixed_connection=None):
    """T4 measurement method, with compact output and caller-owned read support."""
    original_connection, original_loads = engine.store.connection, json.loads
    original_init = QueryReads.__init__
    excluded = set(excluded_subjects)
    with original_connection(read_only=True) as connection:
        raw_outcomes = {
            row[0]
            for row in connection.execute(
                "SELECT outcome FROM calculation "
                "WHERE subject_id IN (SELECT value FROM json_each(?))",
                (canonical(sorted(excluded)),),
            )
        }
    counts, statements, readers = Counter(), [], []

    def loads(value, *args, **kwargs):
        counts["json_loads"] += 1
        if isinstance(value, str) and value in raw_outcomes:
            counts["excluded_complete_outcome_decodes"] += 1
        return original_loads(value, *args, **kwargs)

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        readers.append(self)

    def trace(sql):
        statements.append({"sql": sql, "category": category(sql), "vm_steps_estimate": 0})

    def progress():
        counts["vm_steps_estimate"] += INTERVAL
        if statements:
            statements[-1]["vm_steps_estimate"] += INTERVAL
        return 0

    @contextmanager
    def instrument(connection):
        connection.set_trace_callback(trace)
        connection.set_progress_handler(progress, INTERVAL)
        try:
            yield connection
        finally:
            connection.set_trace_callback(None)
            connection.set_progress_handler(None, 0)

    @contextmanager
    def opened(*, read_only=False):
        with original_connection(read_only=read_only) as connection:
            counts["read_connections" if read_only else "write_connections"] += 1
            counts["connection_setup_peak_bytes"] = max(
                counts["connection_setup_peak_bytes"], tracemalloc.get_traced_memory()[1]
            )
            tracemalloc.reset_peak()
            with instrument(connection):
                yield connection

    engine.store.connection, json.loads, QueryReads.__init__ = opened, loads, initialize
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        if fixed_connection is None:
            response = operation()
        else:
            assert fixed_connection.in_transaction
            with instrument(fixed_connection):
                response = operation()
        elapsed = time.perf_counter() - started
        retained, body_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        engine.store.connection, json.loads, QueryReads.__init__ = (
            original_connection,
            original_loads,
            original_init,
        )
    caches = {
        "facts": {key: value for reader in readers for key, value in reader._fact_versions.items()},
        "calculations": {
            key: value for reader in readers for key, value in reader._calculations.items()
        },
        "metadata": {key: value for reader in readers for key, value in reader._metadata.items()},
        "voucher_lines": {key: value for reader in readers for key, value in reader._lines.items()},
    }
    excluded_cache = {
        "facts": sum(value.subject_id in excluded for value in caches["facts"].values()),
        "calculations": sum(
            value["subject_id"] in excluded for value in caches["calculations"].values()
        ),
        "metadata": sum(value["subject_id"] in excluded for value in caches["metadata"].values()),
    }
    top = [
        dict(row)
        for row in sorted(statements, key=lambda row: row["vm_steps_estimate"], reverse=True)[:5]
    ]
    with original_connection(read_only=True) as connection:
        for row in top:
            query_connection = fixed_connection if fixed_connection is not None else connection
            row["query_plan"] = (
                [item[3] for item in query_connection.execute("EXPLAIN QUERY PLAN " + row["sql"])]
                if row["sql"].lstrip().upper().startswith(("SELECT", "WITH"))
                else []
            )
            row["sql_sha256"] = hashlib.sha256(row["sql"].encode()).hexdigest()
            row["sql_prefix"] = " ".join(row.pop("sql").split())[:500]
    begin_count = sum(row.get("sql") == "BEGIN" for row in statements)
    assert counts["write_connections"] == 0
    if fixed_connection is None:
        assert counts["read_connections"] == begin_count == 1
    else:
        assert counts["read_connections"] == begin_count == 0
    result = {
        "sql_count": len(statements),
        "begin_count": begin_count,
        "read_connections": counts["read_connections"],
        "write_connections": counts["write_connections"],
        "caller_owned_transaction": fixed_connection is not None,
        "sqlite_vm_steps_estimate": counts["vm_steps_estimate"],
        "sqlite_progress_interval": INTERVAL,
        "sql_counts_by_category": dict(Counter(row["category"] for row in statements)),
        "vm_steps_by_category_estimate": {
            key: sum(row["vm_steps_estimate"] for row in statements if row["category"] == key)
            for key in {row["category"] for row in statements}
        },
        "json_loads_count": counts["json_loads"],
        "excluded_complete_outcome_decodes": counts["excluded_complete_outcome_decodes"],
        "cache_counts": {key: len(value) for key, value in caches.items()},
        "cache_ids": {key: sorted(value) for key, value in caches.items()},
        "excluded_cached_subjects": excluded_cache,
        "request_readers": len(readers),
        "python_request_body_peak_bytes": body_peak,
        "python_retained_bytes": retained,
        "python_connection_setup_peak_bytes": counts["connection_setup_peak_bytes"],
        "elapsed_seconds_instrumented": round(elapsed, 6),
        "response_utf8_bytes": len(canonical(response).encode()),
        "highest_vm_queries": top,
    }
    return result, response


def grow_dashboard(book, before):
    engine = book["engine"]
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT hex(digest) FROM evidence LIMIT 1").fetchone()[0].lower()
    subjects, fact_ids = [], []
    for index in range(48, 480):
        subject = f"history-funding-{index:03}"
        month = str(YearMonth.from_ordinal(YearMonth("2024-02").ordinal + index % 23))
        saved = engine.save_fact(
            "cash_funding",
            subject,
            {
                "period": month,
                "actual_date": month + "-01",
                "cash_account_id": "cash",
                "owner_id": "owner",
                "amount_fen": 100,
                "funding_kind": "capital",
            },
            evidence=(proof,),
            expected_revision=0,
            request_id=f"t5-save-{index}",
        )
        subjects.append(subject)
        fact_ids.append(saved["fact_id"])
    preview = engine.preview(subjects)
    engine.confirm(
        subjects, preview_digest=preview["digest"], epochs=preview["epochs"], request_id="t5-grow"
    )
    book["funding_subjects"].update(subjects)
    payload = canonical(
        {
            "plan": {
                "report_fact_ids": [fact_ids[0]],
                "source_closes": [],
                "period": {"quarter_start": "2024-01-01", "quarter_end": "2024-03-31"},
            }
        }
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        for index in range(
            connection.execute("SELECT count(*) FROM jobs").fetchone()[0], before["jobs"] * 10
        ):
            ident = f"t5-job-{index}"
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,'report_export',?,'pending')",
                (ident, payload),
            )
            sync_job(connection, ident)
        for index in range(
            connection.execute("SELECT count(*) FROM audit").fetchone()[0], before["audit"] * 10
        ):
            at = index % len(subjects)
            payload = canonical(
                {
                    "status": "confirmed",
                    "subject_id": subjects[at],
                    "fact_id": fact_ids[at],
                    "revision": 1,
                    "pending": [],
                }
            )
            row = connection.execute(
                "INSERT INTO audit(request_id,action,payload,created_at) "
                "VALUES(?,'confirm_fact',?,'2025-12-31T00:00:00.000Z')",
                (f"t5-audit-{index}", payload),
            )
            sync_audit(connection, row.lastrowid)
        connection.commit()


def dashboard_phase(paths, result):
    book = layered_book.__wrapped__(paths)
    engine = book["engine"]
    baseline = inventory(engine)
    result.update({"database": str(book["path"]), "sizes": {}})
    for size in (48, 480):
        if size == 480:
            grow_dashboard(book, baseline)
        sample = {
            "inventory": inventory(engine),
            "verification_before": verify(engine),
            "requests": {},
        }
        result["sizes"][str(size)] = sample
        for endpoint in ("brief", "funds", "employees", "assets"):
            metrics, response = measure(
                engine,
                lambda endpoint=endpoint: getattr(Dashboard(engine), endpoint)(PERIOD, limit=1),
                excluded_subjects=book["funding_subjects"],
            )
            sample["requests"][endpoint] = metrics
            assert metrics["excluded_complete_outcome_decodes"] == 0
            assert not any(metrics["excluded_cached_subjects"].values())
            data = response["data"]
            metrics["collections"] = {
                name: item["page"] for name, item in data["collections"].items()
            }
            for item in data["collections"].values():
                assert item["page"]["returned_count"] == len(item["items"]) <= 1
            metrics["money"] = {key: value for key, value in data.items() if key.endswith("_fen")}
        sample["verification_after"] = verify(engine)
    small, large = (result["sizes"][str(size)]["requests"] for size in (48, 480))
    for endpoint in small:
        assert large[endpoint]["sql_count"] == small[endpoint]["sql_count"]
        assert large[endpoint]["json_loads_count"] == small[endpoint]["json_loads_count"]
        assert large[endpoint]["cache_counts"] == small[endpoint]["cache_counts"]
    for key in ("opening_fen", "total_fen", "cash_fen"):
        assert large["funds"]["money"][key] - small["funds"]["money"][key] == 43200
    result["assertions"] = (
        "One read transaction/request; zero excluded hydration; fixed SQL/JSON/cache counts; "
        "pages <=1; real historical cash grows exactly 43200 fen."
    )
    return result


def classification_phase(paths, result):
    engine, save, publish, _ = report_book.__wrapped__(paths.mktemp("classifications"))
    save(
        "report_profile",
        "profile",
        {
            "period": "2025-01",
            "company_name": "T5 synthetic classification company",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2025-01",
            "newly_established_zero_opening_confirmed": True,
        },
    )
    cit(save, publish)
    subjects, references, calculations, vouchers = set(), set(), set(), set()
    result.update({"database": str(engine.store.path), "samples": {}})

    def sample(name, *, invalid=False):
        item = {"verification_before": verify(engine), "classification_count": len(references)}
        result["samples"][name] = item
        with engine.store.connection(read_only=True) as connection:
            item["normalized_rows"] = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "fact_report_classification",
                    "fact_report_classification_cash_details",
                )
            }
        metrics, report = measure(
            engine, lambda: Reports(engine).report(2026, 1), excluded_subjects=subjects
        )
        item.update(
            {
                "report": metrics,
                "status": report["status"],
                "report_fact_count": len(report["report_fact_ids"]),
                "fact_issues": report["fact_issues"],
            }
        )
        assert report["status"] == ("needs_information" if invalid else "ready"), report[
            "fact_issues"
        ]
        assert references <= set(report["report_fact_ids"])
        assert not references.intersection(metrics["cache_ids"]["facts"])
        assert not calculations.intersection(metrics["cache_ids"]["calculations"])
        assert not vouchers.intersection(metrics["cache_ids"]["voucher_lines"])
        assert metrics["excluded_complete_outcome_decodes"] == 0
        if invalid:
            assert any(
                issue.get("voucher_version_id") == "zz-t5-missing-voucher"
                for issue in report["fact_issues"]
            )

        def readiness():
            with engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                return check_report_readiness(engine.store, connection, YearMonth("2026-03"))

        readiness_metrics, problems = measure(engine, readiness, excluded_subjects=subjects)
        item.update({"readiness": readiness_metrics, "readiness_issues": problems})
        assert (
            not problems
            if not invalid
            else any(
                issue.get("voucher_version_id") == "zz-t5-missing-voucher" for issue in problems
            )
        )
        assert not references.intersection(readiness_metrics["cache_ids"]["facts"])
        assert not calculations.intersection(readiness_metrics["cache_ids"]["calculations"])
        assert not vouchers.intersection(readiness_metrics["cache_ids"]["voucher_lines"])
        assert readiness_metrics["excluded_complete_outcome_decodes"] == 0
        item.update(
            {
                "report": metrics,
                "readiness": readiness_metrics,
                "status": report["status"],
                "report_fact_count": len(report["report_fact_ids"]),
                "fact_issues": report["fact_issues"],
                "readiness_issues": problems,
                "verification_after": verify(engine),
            }
        )

    for lower, upper in ((0, 48), (48, 480)):
        added = []
        for index in range(lower, upper):
            subject = f"old-capital-{index:03}"
            save(
                "cash_funding",
                subject,
                {
                    "period": "2025-01",
                    "actual_date": "2025-01-03",
                    "owner_id": "owner",
                    "funding_kind": "capital",
                    "amount_fen": 100,
                    "cash_account_id": "cash",
                },
            )
            subjects.add(subject)
            added.append(subject)
        publish(*added)
        with engine.store.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT v.id,v.calculation_id,c.subject_id FROM voucher_version v "
                "JOIN calculation c ON c.id=v.calculation_id "
                "WHERE c.subject_id IN (SELECT value FROM json_each(?))",
                (canonical(added),),
            ).fetchall()
        assert len(rows) == upper - lower
        for row in rows:
            references.add(
                save(
                    "report_classification",
                    "classification-" + row["subject_id"],
                    {
                        "period": "2025-01",
                        "voucher_version_id": row["id"],
                        "cash_details": [{"line_no": 1, "category": 15, "amount_fen": 100}],
                    },
                )["fact_id"]
            )
            calculations.add(row["calculation_id"])
            vouchers.add(row["id"])
        sample(str(upper))
    references.add(
        save(
            "report_classification",
            "zz-t5-invalid-reference",
            {
                "period": "2025-01",
                "voucher_version_id": "zz-t5-missing-voucher",
                "cash_details": [{"line_no": 1, "category": 15, "amount_fen": 100}],
            },
        )["fact_id"]
    )
    sample("480_plus_invalid", invalid=True)
    result["assertions"] = (
        "All 48/480 applicable references retained; scalar history validates without typed "
        "classification/outcome/line hydration; appended missing reference blocks report/readiness."
    )
    return result


def mismatch_phase(paths, result):
    book = settlement_book.__wrapped__(paths.mktemp("mismatch"))
    engine = book[0]
    generator = mismatched_frozen_payment.__wrapped__(book)
    _, connection, identifiers, original_key = next(generator)
    try:
        result.update({"database": str(engine.store.path), "verification_before": verify(engine)})

        def control():
            queries = BusinessQueries(engine, reads=QueryReads(engine, connection))
            page = queries.business_collection(
                connection, "expense", PERIOD, section="settlement_events", limit=1, current=True
            )
            summary = queries.settlement_summary(
                connection, PERIOD, subject_ids={"expense"}, current=True
            )
            return {"page": page, "summary": summary}

        metrics, response = measure(engine, control, fixed_connection=connection)
        page, summary = response["page"], response["summary"]
        result.update({"measurement": metrics, "page": page, "summary": summary})
        movement = page["items"][0]
        assert page["page"]["total_count"] == 2 and page["page"]["returned_count"] == 1
        assert movement["state"] == "unresolved" and movement["issues"]
        assert movement["source_calculation_id"] == identifiers["expense-b"]
        assert movement["source_business"]["subject_id"] == "expense-b"
        assert summary["movement_count"] == 2 and summary["complete"] is False
        obligation = summary["obligations"][0]
        assert obligation["key"] == original_key and obligation["source_amount_fen"] == 100
        assert obligation["paid_fen"] is obligation["remaining_fen"] is None
        result.update(
            {
                "measurement": metrics,
                "page": page,
                "summary": summary,
                "verification_after": verify(engine),
                "assertions": (
                    "Declared A finds unresolved frozen B; exact B trace retained; "
                    "two slots counted once, one returned; A paid/remaining remain null."
                ),
                "boundary": (
                    "Synthetic in-memory TEMP-table corruption; main company remains valid. "
                    "One caller-owned transaction with fresh QueryReads; "
                    "no growth matrix or normal-state claim."
                ),
            }
        )
        return result
    finally:
        generator.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "t5-read-costs.json")
    parser.add_argument(
        "--phase", choices=("all", "dashboard", "classifications", "mismatch"), default="all"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".tmp") or output.exists():
        parser.error(
            "output must be a new file under repository .tmp; "
            "retained evidence is never overwritten"
        )
    before = source_inventory()
    paths = Paths()
    record = {
        "task": "T5",
        "status": "running",
        "phase": args.phase,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "command": subprocess.list2cmdline([sys.executable, *sys.argv]),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "libraries": {
                name: importlib.metadata.version(name)
                for name in ("pydantic", "openpyxl", "pytest")
            },
        },
        "synthetic_directory": str(paths.root),
        "results": {},
        "limits": [
            "One instrumented request per case with fresh request/connection/QueryReads, except "
            "caller-owned mismatch control; not OS cold cache or a latency SLA.",
            "SQL/VM exclude Store connection setup and explicit integrity verification. "
            "VM interval is 100; category attribution and statement quantization are approximate.",
            "Python tracemalloc includes instrumentation, response and caches; excludes SQLite "
            "native memory, OS cache and outcome observation sets. Elapsed time descriptive only.",
            "Cash effects, all applicable classification scalars/issues, complete relevant "
            "manifests and source graphs retain necessary growth. No truncation or invented state.",
            "Synthetic companies only; no installation, production database, deployment or "
            "formal backup. T4 evidence and test matrices are not rerun or combined.",
        ],
    }
    failure = None
    try:
        for name, action in (
            ("dashboard", dashboard_phase),
            ("classifications", classification_phase),
            ("mismatch", mismatch_phase),
        ):
            if args.phase in ("all", name):
                print(f"T5 {name}: starting", flush=True)
                record["results"][name] = {}
                action(paths, record["results"][name])
                output.write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(f"T5 {name}: passed", flush=True)
        record["status"] = "passed"
    except Exception as exc:
        record["status"] = "failed"
        record["failure"] = {"phase": name, "type": type(exc).__name__, "message": str(exc)}
        failure = exc
    finally:
        after = source_inventory()
        dependencies = used_sources()
        record["source_hashes_before"] = {path: before.get(path) for path in dependencies}
        record["source_hashes_after"] = {path: after.get(path) for path in dependencies}
        record["dependency_state_unchanged"] = (
            record["source_hashes_before"] == record["source_hashes_after"]
        )
        if not record["dependency_state_unchanged"]:
            record["status"] = "invalidated_by_source_change"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"T5 evidence: {output} ({record['status']})", flush=True)
    if failure:
        raise failure
    if record["status"] != "passed":
        raise SystemExit("Actual dependencies changed during measurement; see recorded hashes.")


if __name__ == "__main__":
    main()
