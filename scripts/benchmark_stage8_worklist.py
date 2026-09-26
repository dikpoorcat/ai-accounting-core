"""Measure work-list selection against 12, 48 and 120 synthetic months.

Two independent fresh-company groups distinguish inventory month markers from
typed expense business sources (and their actual material evidence). No real
company database, service or backup is opened.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "kernel"))

from test_workflow import setup_company  # noqa: E402

from ai_accounting.kernel.domains.transactions import Expense  # noqa: E402
from ai_accounting.kernel.periods import Periods  # noqa: E402
from ai_accounting.kernel.types import YearMonth  # noqa: E402
from ai_accounting.kernel.worklist import Worklist  # noqa: E402


class CountingCursor:
    def __init__(self, cursor, counts):
        self.cursor, self.counts = cursor, counts

    def _record(self, row):
        if row is not None:
            self.counts["loaded_rows"] += 1
            self.counts["loaded_value_bytes"] += sum(
                len(value.encode("utf-8"))
                if isinstance(value, str)
                else len(value)
                if isinstance(value, bytes)
                else 8
                for value in row
            )
        return row

    def fetchone(self):
        return self._record(self.cursor.fetchone())

    def fetchall(self):
        return [self._record(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return self

    def __next__(self):
        return self._record(next(self.cursor))

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


def make_company(root: Path, months: int, group: str):
    root.mkdir(parents=True, exist_ok=True)
    company = setup_company(root)
    first = YearMonth("2016-01").ordinal
    if group == "month_markers":
        evidence = bytes.fromhex(company.owner_confirmation)
        with company.engine.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO material_revision"
                "(period,category,expected,received,no_business,evidence_digest)"
                " VALUES(?,'transactions',0,0,1,?)",
                [(first + index, evidence) for index in range(months)],
            )
            connection.commit()
    elif group == "business_sources":
        for index in range(months):
            company.save(
                Expense(
                    period=str(YearMonth.from_ordinal(first + index)),
                    counterparty_id="supplier",
                    amount_fen=1000 + index,
                    expense_class="administration",
                    creditor_kind="supplier",
                ),
                f"expense-{index}",
            )
    else:
        raise ValueError(group)
    return company


def measure(company, repeats):
    samples = []
    original = Periods.collect_current_readiness
    for _ in range(repeats):
        counts = Counter()

        def counted(self, *args, _counts=counts, **kwargs):
            _counts["full_readiness_calls"] += 1
            return original(self, *args, **kwargs)

        Periods.collect_current_readiness = counted
        try:
            with company.engine.store.connection(read_only=True) as raw:
                connection = CountingConnection(raw, counts)
                connection.execute("BEGIN")
                started = time.perf_counter()
                result = Worklist(company.engine).query(connection, as_of="2026-01-15")
                elapsed_ms = (time.perf_counter() - started) * 1000
        finally:
            Periods.collect_current_readiness = original
        if result["period"] != "2016-01":
            raise RuntimeError("synthetic earliest month was not selected")
        samples.append(
            {
                "elapsed_ms": round(elapsed_ms, 3),
                "sql_calls": counts["sql_calls"],
                "sql_read_calls": counts["sql_read_calls"],
                "loaded_rows": counts["loaded_rows"],
                "loaded_value_bytes": counts["loaded_value_bytes"],
                "full_readiness_calls": counts["full_readiness_calls"],
                "response_bytes": len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
            }
        )
    return {
        "samples": samples,
        "median_elapsed_ms": round(statistics.median(item["elapsed_ms"] for item in samples), 3),
        "max_elapsed_ms": max(item["elapsed_ms"] for item in samples),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    with tempfile.TemporaryDirectory(prefix="stage8-worklist-") as temporary:
        root = Path(temporary)
        results = {
            group: {
                str(months): measure(
                    make_company(root / group / str(months), months, group), args.repeats
                )
                for months in (12, 48, 120)
            }
            for group in ("month_markers", "business_sources")
        }
    output = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
