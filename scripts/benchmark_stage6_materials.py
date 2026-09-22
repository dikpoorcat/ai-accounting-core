"""Measure authoritative material follow-up checks on isolated synthetic companies.

Run with the repository virtualenv, for example:
  .tmp-kernel-venv/Scripts/python.exe -X utf8 scripts/benchmark_stage6_materials.py \
    --output .tmp/stage6-materials.json

Each size creates a new company database and one CSV spanning every historical
month. The fixture uses public material, fact and publication APIs. It inserts
only a minimal synthetic period-close boundary because the measured operation
reads that boundary but does not consume close manifests. No configured company
or real material is opened.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "kernel"))

from test_materials import Company  # noqa: E402

from ai_accounting.kernel import materials  # noqa: E402
from ai_accounting.kernel.types import YearMonth, canonical, digest  # noqa: E402


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", default="12,48,120")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--large-rows", type=int, default=6000)
    parser.add_argument("--timing-context", default="unspecified host load")
    return parser.parse_args()


def byte_size(value):
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return len(value)
    return 0 if value is None else 8


class CountingCursor:
    def __init__(self, cursor, counts):
        self.cursor, self.counts = cursor, counts

    def record(self, row):
        if row is not None:
            self.counts["returned_sql_rows"] += 1
            self.counts["returned_sql_value_bytes"] += sum(byte_size(item) for item in row)
        return row

    def fetchone(self):
        return self.record(self.cursor.fetchone())

    def fetchall(self):
        return [self.record(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return self

    def __next__(self):
        return self.record(next(self.cursor))

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class CountingConnection:
    def __init__(self, connection, counts):
        self.connection, self.counts = connection, counts

    def execute(self, statement, parameters=()):
        self.counts["sql_calls"] += 1
        if statement.lstrip().lower().startswith(("select", "with", "pragma")):
            self.counts["sql_read_calls"] += 1
        return CountingCursor(self.connection.execute(statement, parameters), self.counts)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def month(index):
    return YearMonth.from_ordinal(YearMonth("2016-01").ordinal + index)


def arrange_company(directory, size):
    directory.mkdir(parents=True)
    company = Company(directory)
    rows = ["name,amount,period"]
    for index in range(size):
        rows.append(f"row-{index + 1},1.00,{month(index)}")
    raw = ("\n".join(rows) + "\n").encode()
    source, _ = company.source(raw, period=str(month(0)))
    for index in range(size):
        period = str(month(index))
        link = company.expense(f"expense-{index + 1}", 100, period)
        company.resolve(
            source,
            f"CSV!B{index + 2}",
            [link],
            subject=f"resolution-{index + 1}",
            period=period,
            recognition_period=period,
        )
    closed_through = month(size - 1).ordinal
    manifest = {"synthetic_benchmark_boundary": str(month(size - 1))}
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (closed_through, canonical(manifest), digest(manifest)),
        )
        connection.commit()
    return company, source, raw


def measure(company, source, raw, size, repeats):
    review_months = (month(size).ordinal, month(size + 1).ordinal)
    samples = []
    original = materials.inspect_bytes
    try:
        for _ in range(repeats):
            counts = Counter()

            def counted_inspection(content, specification, _counts=counts):
                _counts["parse_calls"] += 1
                _counts["parsed_bytes"] += len(content)
                return original(content, specification)

            materials.inspect_bytes = counted_inspection
            with company.engine.store.connection(read_only=True) as raw_connection:
                connection = CountingConnection(raw_connection, counts)
                connection.execute("BEGIN")
                started = time.perf_counter()
                results = materials.check_completeness_many(
                    connection, review_months, company.engine.store.registry
                )
                elapsed = time.perf_counter() - started
            if any(result["status"] != "complete" for result in results.values()):
                raise RuntimeError("synthetic complete materials unexpectedly failed")
            if counts["parse_calls"] != 1 or counts["parsed_bytes"] != len(raw):
                raise RuntimeError("a cross-period source was not parsed exactly once")
            samples.append(
                {
                    "seconds": elapsed,
                    **dict(counts),
                    "coverage_rows": sum(len(result["coverage"]) for result in results.values()),
                    "fact_dependencies": len(
                        set().union(*(set(result["fact_ids"]) for result in results.values()))
                    ),
                    "source_version": source["fact_id"],
                }
            )
    finally:
        materials.inspect_bytes = original
    stable = {key: samples[0][key] for key in samples[0] if key not in {"seconds"}}
    for sample in samples[1:]:
        if {key: sample[key] for key in stable} != stable:
            raise RuntimeError("non-timing counters changed between identical snapshots")
    return {
        **stable,
        "seconds": [sample["seconds"] for sample in samples],
        "median_seconds": statistics.median(sample["seconds"] for sample in samples),
    }


def arrange_large_file(directory, row_count):
    directory.mkdir(parents=True)
    company = Company(directory)
    closed_rows = row_count // 2
    future_rows = row_count // 4
    unknown_rows = row_count - closed_rows - future_rows
    rows = ["name,amount,period"]
    for index in range(closed_rows):
        rows.append(f"closed-{index + 1},1.00,{month(index % 120)}")
    for index in range(future_rows):
        rows.append(f"future-{index + 1},1.00,2027-{index % 12 + 1:02d}")
    for index in range(unknown_rows):
        rows.append(f"unknown-{index + 1},1.00,")
    raw = ("\n".join(rows) + "\n").encode()
    source, _ = company.source(raw, subject="large-cross-period", period=str(month(0)))
    manifest = {"synthetic_benchmark_boundary": str(month(119))}
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (month(119).ordinal, canonical(manifest), digest(manifest)),
        )
        connection.commit()
    return company, source, raw, (closed_rows, future_rows, unknown_rows)


def measure_large_file(company, source, raw, row_counts, repeats):
    review_months = (YearMonth("2026-01").ordinal, YearMonth("2026-02").ordinal)
    samples = []
    original = materials.inspect_bytes
    try:
        for _ in range(repeats):
            counts = Counter()

            def counted_inspection(content, specification, _counts=counts):
                _counts["parse_calls"] += 1
                _counts["parsed_bytes"] += len(content)
                return original(content, specification)

            materials.inspect_bytes = counted_inspection
            with company.engine.store.connection(read_only=True) as raw_connection:
                connection = CountingConnection(raw_connection, counts)
                connection.execute("BEGIN")
                started = time.perf_counter()
                results = materials.check_completeness_many(
                    connection, review_months, company.engine.store.registry
                )
                elapsed = time.perf_counter() - started
            if any(result["status"] != "needs_information" for result in results.values()):
                raise RuntimeError("unresolved synthetic material unexpectedly passed")
            if counts["parse_calls"] != 1 or counts["parsed_bytes"] != len(raw):
                raise RuntimeError("the large cross-period source was not parsed exactly once")
            responsibilities = Counter(
                issue["responsibility"] for result in results.values() for issue in result["issues"]
            )
            if not responsibilities["closed_followup"] or not responsibilities["unassigned"]:
                raise RuntimeError("closed and unknown rows did not retain their responsibility")
            if responsibilities["direct"]:
                raise RuntimeError("future rows became direct issues before their period")
            samples.append(
                {
                    "seconds": elapsed,
                    **dict(counts),
                    "coverage_rows": sum(len(result["coverage"]) for result in results.values()),
                    "issue_responsibilities": dict(sorted(responsibilities.items())),
                    "fact_dependencies": len(
                        set().union(*(set(result["fact_ids"]) for result in results.values()))
                    ),
                    "source_version": source["fact_id"],
                }
            )
    finally:
        materials.inspect_bytes = original
    stable = {key: samples[0][key] for key in samples[0] if key != "seconds"}
    for sample in samples[1:]:
        if {key: sample[key] for key in stable} != stable:
            raise RuntimeError("non-timing large-file counters changed between snapshots")
    closed_rows, future_rows, unknown_rows = row_counts
    return {
        "source_files": 1,
        "source_rows": sum(row_counts),
        "closed_rows": closed_rows,
        "future_rows": future_rows,
        "unknown_rows": unknown_rows,
        "source_bytes": len(raw),
        **stable,
        "seconds": [sample["seconds"] for sample in samples],
        "median_seconds": statistics.median(sample["seconds"] for sample in samples),
        "database_bytes": company.engine.store.path.stat().st_size,
    }


def main():
    args = options()
    sizes = sorted({int(value) for value in args.sizes.split(",")})
    if not sizes or sizes[0] < 1 or args.repeats < 1 or args.large_rows < 1000:
        raise ValueError("sizes/repeats must be positive and large-rows must be at least 1000")
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    companies = output.with_suffix("")
    if companies.exists():
        raise FileExistsError(f"refusing to reuse {companies}")
    companies.mkdir(parents=True)
    results = []
    for size in sizes:
        print(f"Arranging {size} months", flush=True)
        company, source, raw = arrange_company(companies / str(size), size)
        measured = measure(company, source, raw, size, args.repeats)
        results.append(
            {
                "historical_months": size,
                "review_months": 2,
                "source_files": 1,
                "source_rows": size,
                "source_bytes": len(raw),
                **measured,
                "database_bytes": company.engine.store.path.stat().st_size,
            }
        )
        print(f"Measured {size} months: {measured['median_seconds']:.6f}s", flush=True)
    print(f"Arranging one {args.large_rows}-row cross-period file", flush=True)
    large = arrange_large_file(companies / "large-file", args.large_rows)
    large_file = measure_large_file(*large, args.repeats)
    print(f"Measured large file: {large_file['median_seconds']:.6f}s", flush=True)
    report = {
        "fixture": "isolated synthetic companies; one cross-period CSV; one row per month",
        "measurement": (
            "Two open review months in one snapshot after the latest synthetic close boundary; "
            "SQL counters are application-issued calls and rows/bytes returned to Python"
        ),
        "timing_context": args.timing_context,
        "repeats": args.repeats,
        "results": results,
        "large_file": large_file,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
