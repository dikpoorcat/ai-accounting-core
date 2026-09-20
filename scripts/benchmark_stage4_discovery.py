"""Measure stage-4 fact discovery against an isolated source tree.

The source may be the approved baseline checkout (d810a8b) or the current tree.
Every run creates new synthetic company databases below the output stem; it never
opens configured companies or real business material.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sqlite3
import statistics
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", default="12,48,120")
    parser.add_argument("--facts-per-month", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timing-context", default="unspecified host load")
    parser.add_argument(
        "--read-existing",
        type=Path,
        help="Remeasure the isolated companies from an earlier completed run",
    )
    return parser.parse_args()


def byte_size(value):
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return len(value)
    return 0 if value is None else 8


class Cursor:
    def __init__(self, cursor, counts):
        self.cursor, self.counts = cursor, counts

    def record(self, row):
        if row is not None:
            self.counts["returned_sql_rows"] += 1
            self.counts["returned_sql_value_bytes"] += sum(byte_size(value) for value in row)
        return row

    def fetchone(self):
        return self.record(self.cursor.fetchone())

    def fetchall(self):
        return [self.record(row) for row in self.cursor.fetchall()]

    def fetchmany(self, *args):
        return [self.record(row) for row in self.cursor.fetchmany(*args)]

    def __iter__(self):
        return self

    def __next__(self):
        return self.record(next(self.cursor))

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class Connection:
    def __init__(self, connection, counts):
        self.connection, self.counts = connection, counts

    def execute(self, statement, parameters=()):
        self.counts["sql_calls"] += 1
        if statement.lstrip().lower().startswith(("select", "with", "pragma")):
            self.counts["sql_read_calls"] += 1
        if (
            "discovery_fact_" in statement.lower() and "fact_id desc limit " in statement.lower()
        ) or (
            " order by f.id limit " in statement.lower() and "fact_revision f" in statement.lower()
        ):
            self.counts["candidate_sql"] = statement
            self.counts["candidate_parameters"] = tuple(parameters)
        return Cursor(self.connection.execute(statement, parameters), self.counts)

    def executemany(self, statement, parameters):
        self.counts["sql_calls"] += 1
        return Cursor(self.connection.executemany(statement, parameters), self.counts)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def month(index):
    year, offset = divmod(index, 12)
    return f"{2020 + year:04d}-{offset + 1:02d}"


def main():
    args = options()
    source = args.source.resolve()
    sizes = sorted({int(value) for value in args.sizes.split(",")})
    if not sizes or sizes[0] < 1 or args.facts_per_month < 2 or args.repeats < 1:
        raise ValueError("sizes/repeats must be positive and facts-per-month must be at least 2")
    sys.path.insert(0, str(source / "src"))

    from ai_accounting.kernel.discovery import Discovery
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.storage import Store

    try:
        from ai_accounting.kernel.entities import Entities
    except ImportError:
        Entities = None

    class CountingStore(Store):
        def __init__(self, *values, **kwargs):
            super().__init__(*values, **kwargs)
            self.counts = Counter()

        @contextmanager
        def connection(self, *, read_only=False):
            with super().connection(read_only=read_only) as connection:
                connection.set_progress_handler(
                    lambda: self.counts.update(sqlite_vm_steps=1000) or 0, 1000
                )
                try:
                    yield Connection(connection, self.counts)
                finally:
                    connection.set_progress_handler(None, 0)

        def fact(self, *values, **kwargs):
            self.counts["scalar_fact_loads"] += 1
            return super().fact(*values, **kwargs)

        def fact_data_many(self, connection, fact_ids):
            self.counts["raw_batch_calls"] += 1
            self.counts["raw_batch_facts"] += len(set(fact_ids))
            return super().fact_data_many(connection, fact_ids)

    root = args.output.resolve().with_suffix("")
    companies = (
        args.read_existing.resolve()
        if args.read_existing
        else root.parent / (root.name + "-companies")
    )
    if not args.read_existing:
        companies.mkdir(parents=True, exist_ok=False)
    signature = inspect.signature(Discovery.find_facts).parameters
    current_contract = "cursor" in signature
    results = {
        "source": str(source),
        "baseline": "d810a8b",
        "timing_context": args.timing_context,
        "measurement_scope": {
            "elapsed": (
                "complete Discovery.find_facts call including private open and schema validation"
            ),
            "sql_counters": "query connection after Store.connection validation has completed",
            "sqlite_vm_steps": "1000-instruction progress-handler granularity",
            "storage": (
                "main database plus present sidecars after explicit WAL checkpoint(TRUNCATE)"
            ),
        },
        "facts_per_month": args.facts_per_month,
        "read_existing": str(companies) if args.read_existing else None,
        "runs": [],
    }

    for size in sizes:
        path = companies / f"discovery-{size}.sqlite"
        if args.read_existing:
            with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as raw:
                company_id, database_id = raw.execute(
                    "SELECT company_id,database_id FROM identity WHERE id=1"
                ).fetchone()
            store = CountingStore(path, production_bundle(), company_id, database_id)
        else:
            store = CountingStore.create(
                path,
                production_bundle(),
                f"stage4-{size}",
                f"911100000000{size:06d}"[-18:],
                f"stage4-db-{size}",
            )
        engine = Engine(store)
        people = [f"employee-{index}" for index in range(args.facts_per_month)]
        if args.read_existing and Entities is not None:
            with store.connection(read_only=True) as connection:
                people = [
                    row[0]
                    for row in connection.execute(
                        "SELECT entity_id FROM entity_profile_revision "
                        "ORDER BY json_extract(content,'$.display_name')"
                    )
                ]
        elif not args.read_existing and Entities is not None:
            registered = []
            entities = Entities(engine)
            for index in range(args.facts_per_month):
                registered.append(
                    entities.register_entity(
                        "person",
                        {"display_name": f"Employee {index}"},
                        source="stage4 synthetic benchmark",
                        request_id=f"entity-{index}",
                    )["entity_id"]
                )
            people = registered
        if not args.read_existing:
            proof = engine.register_evidence(
                b"stage4 synthetic profile",
                "text/plain",
                "stage4-profile-source",
                request_id="profile-proof",
            )["digest"]
            for period_index in range(size):
                period = month(period_index)
                for index, employee_id in enumerate(people):
                    subject = f"profile-{period_index:04d}-{index:03d}"
                    data = {
                        "period": period,
                        "employee_id": employee_id,
                        "effective_from": period,
                        "effective_to": period,
                        "withholding_start_date": period + "-01",
                        "social_insurance_base_fen": None,
                        "housing_fund_base_fen": None,
                        "social_insurance_participating": False,
                        "housing_fund_participating": False,
                        "contribution_shortfall": "reject",
                    }
                    engine.save_fact(
                        "payroll_profile",
                        subject,
                        data,
                        evidence=(proof,),
                        expected_revision=0,
                        request_id=subject,
                    )
                    if index == 0 and period_index % 4 == 0:
                        engine.save_fact(
                            "payroll_profile",
                            subject,
                            data | {"withholding_start_date": period + "-02"},
                            evidence=(proof,),
                            expected_revision=1,
                            request_id=subject + "-revision",
                        )

        discovery = Discovery(engine)
        last_year = month(max(0, size - 12))
        workloads = [
            ("current_no_period", {"kind": "payroll_profile", "status": "current"}),
            ("history_no_period", {"kind": "payroll_profile", "status": "history"}),
            (
                "current_last_12_months",
                {"kind": "payroll_profile", "status": "current", "period_from": last_year},
            ),
        ]
        if current_contract:
            entity_filter = {
                "kind": "payroll_profile",
                "status": "history",
                "entity_id": people[0],
                "role": "employee",
                "identity_match": "current",
            }
            workloads.extend(
                (
                    ("target_entity", entity_filter),
                    (
                        "target_entity_last_12_months",
                        entity_filter | {"period_from": last_year},
                    ),
                )
            )
        else:
            entity_filter = {
                "kind": "payroll_profile",
                "status": "history",
                "person_id": people[0],
            }
            workloads.extend(
                (
                    ("target_entity", entity_filter),
                    (
                        "target_entity_last_12_months",
                        entity_filter | {"period_from": last_year},
                    ),
                )
            )
        for name, base in workloads:
            for limit in (1, 100, 500):
                samples, counters, page = [], [], None
                # Remove cold-open and first-plan bias from the timed repeats.
                discovery.find_facts(**base, limit=limit)
                for _ in range(args.repeats):
                    store.counts.clear()
                    started = time.perf_counter_ns()
                    page = discovery.find_facts(**base, limit=limit)
                    samples.append((time.perf_counter_ns() - started) / 1_000_000)
                    counters.append(dict(store.counts))
                candidate_sql = store.counts.get("candidate_sql")
                candidate_parameters = store.counts.get("candidate_parameters", ())
                query_plan = []
                if candidate_sql:
                    with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as raw:
                        query_plan = [
                            row[3]
                            for row in raw.execute(
                                "EXPLAIN QUERY PLAN " + candidate_sql, candidate_parameters
                            )
                        ]
                for counter in counters:
                    counter.pop("candidate_sql", None)
                    counter.pop("candidate_parameters", None)
                results["runs"].append(
                    {
                        "months": size,
                        "workload": name,
                        "limit": limit,
                        "returned_items": len(page["items"]),
                        "has_next": bool(page.get("next_cursor") or page.get("next_after_id")),
                        "median_ms": statistics.median(samples),
                        "min_ms": min(samples),
                        "max_ms": max(samples),
                        "query_plan": query_plan,
                        "counters": counters,
                    }
                )
        with store.connection() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        storage = {
            candidate.name: candidate.stat().st_size
            for candidate in (
                path,
                Path(str(path) + "-wal"),
                Path(str(path) + "-shm"),
                Path(str(path) + "-journal"),
            )
            if candidate.exists()
        }
        results.setdefault("database_files", {})[str(size)] = storage
        results.setdefault("database_bytes", {})[str(size)] = sum(storage.values())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.output)


if __name__ == "__main__":
    main()
