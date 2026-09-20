"""Compare stage-3 history with an isolated source tree and synthetic companies.

Run with the repository virtualenv, for example:
  .tmp-kernel-venv/Scripts/python.exe scripts/benchmark_stage3_history.py \
    --source . --output .tmp/stage3-benchmark/current.json

The same program also accepts a git-archive checkout through --source. It uses
real Engine publication, Periods close and full integrity APIs. Counters describe
application-issued SQL and rows actually returned to Python, not SQLite's hidden
virtual-machine scans. Timings include identical instrumentation in both runs.

Use a distinct --output for each creation run: existing companies are never
overwritten. To remeasure a completed 120-month dataset in an idle window, add
--read-existing <prior-output-stem>-companies --sizes 120 and a new --output.
This recheck executes only read-only business queries and complete verification;
it does not recreate or republish facts, or claim new close timings.
Multiple --sizes recheck historical read cutoffs in that final database. Its
storage and full verification still cover database_months, not just the cutoff.
"""

# ruff: noqa: B023 -- Measurement closures are invoked synchronously in their loop iteration.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", default="12,48,120")
    parser.add_argument("--workload", choices=("both", "independent", "cumulative"), default="both")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timing-context", default="unspecified host load")
    parser.add_argument(
        "--read-existing",
        type=Path,
        help="Remeasure read cutoffs in a prior final synthetic companies folder",
    )
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help="Explicitly repair derived projections before remeasuring; record the repair",
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
            self.counts["returned_sql_value_bytes"] += sum(byte_size(item) for item in row)
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

    def __getattr__(self, key):
        return getattr(self.cursor, key)


class Connection:
    def __init__(self, connection, counts):
        self.connection, self.counts = connection, counts

    def execute(self, statement, parameters=()):
        self.counts["sql_calls"] += 1
        lowered = statement.lower()
        if lowered.lstrip().startswith(("select", "with", "pragma")):
            self.counts["sql_read_calls"] += 1
        for label, table in (
            ("dependency_sql", "dependency_calculation"),
            ("manifest_sql", "period_close"),
            ("outcome_sql", "outcome"),
        ):
            if table in lowered:
                self.counts[label] += 1
        return Cursor(self.connection.execute(statement, parameters), self.counts)

    def executemany(self, statement, parameters):
        self.counts["sql_calls"] += 1
        self.counts["sql_executemany_calls"] += 1
        return Cursor(self.connection.executemany(statement, parameters), self.counts)

    def __getattr__(self, key):
        return getattr(self.connection, key)


@contextmanager
def instrumentation(counts, query_reads):
    """Count actual outcome decoding and request-local cache fills, not estimates."""
    original_loads = json.loads

    def loads(value, *args, **kwargs):
        result = original_loads(value, *args, **kwargs)
        if isinstance(result, dict) and {"lines", "values", "balances"} <= result.keys():
            counts["outcome_decodes"] += 1
            counts["outcome_decode_bytes"] += byte_size(value)
        return result

    originals = {}
    for method, cache in (
        ("calculations", "_calculations"),
        ("metadata", "_metadata"),
        ("facts", "_facts"),
        ("prime_parents", "_parents"),
        ("relations", "_relations"),
        ("relations_many", "_relations"),
    ):
        original = getattr(query_reads, method, None)
        if original is None:
            continue
        originals[method] = original

        def wrapper(self, *args, _method=method, _cache=cache, _original=original, **kwargs):
            counts["queryreads_" + _method + "_calls"] += 1
            before = len(getattr(self, _cache, {}))
            result = _original(self, *args, **kwargs)
            counts["queryreads_" + _method + "_loaded"] += len(getattr(self, _cache, {})) - before
            return result

        setattr(query_reads, method, wrapper)
    json.loads = loads
    try:
        yield
    finally:
        json.loads = original_loads
        for method, original in originals.items():
            setattr(query_reads, method, original)


def main():
    args = options()
    source = args.source.resolve()
    sizes = sorted({int(value) for value in args.sizes.split(",")})
    if not sizes or sizes[0] < 1 or args.repeats < 1:
        raise ValueError("sizes and repeats must be positive")
    if args.repair_existing and not args.read_existing:
        raise ValueError("--repair-existing requires --read-existing")
    sys.path.insert(0, str(source / "src"))
    from ai_accounting.kernel.business_queries import BusinessQueries
    from ai_accounting.kernel.contracts import BalanceEffect, Fact, Line, Outcome, Read, Registry
    from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
    from ai_accounting.kernel.dashboard_funds import FundsRead
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.integrity import verify_close_integrity, verify_integrity
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.storage import Store
    from ai_accounting.kernel.types import PositiveFen, YearMonth, canonical

    spec = importlib.util.spec_from_file_location(
        "stage3_schema_fixture", source / "tests/kernel/schema_fixture.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    class Movement(Fact):
        kind: ClassVar[str] = "stage3_benchmark_movement"
        account_id: str
        amount_fen: PositiveFen
        cumulative: bool

        def reads(self):
            return (
                (Read("calculation", self.kind, "*", before_period=self.period),)
                if self.cumulative
                else ()
            )

    def calculate(version, context):
        fact = version.fact
        parents = context.select(fact.reads()[0]) if fact.cumulative else ()
        obligation = {
            "key": "payable:" + version.subject_id,
            "name": "primary",
            "amount_fen": fact.amount_fen,
            "account": "2202",
            "normal": "credit",
            "category": "payable",
            "counterparty_id": "synthetic-supplier",
            "cashflow": "operating",
        }
        return Outcome(
            (Line("1001", debit=fact.amount_fen), Line("2202", credit=fact.amount_fen)),
            {
                "cash_account_id": fact.account_id,
                "parent_count": len(parents),
                "obligations": [obligation],
            },
            (
                BalanceEffect(fact.account_id, fact.amount_fen, "cash"),
                BalanceEffect(obligation["key"], fact.amount_fen, "payable"),
            ),
        )

    registry = Registry()
    registry.register(Movement, calculate)
    bundle = fixture.test_bundle(registry)

    class CountingStore(Store):
        def __init__(self, *values, **kwargs):
            super().__init__(*values, **kwargs)
            self.counts = Counter()

        @contextmanager
        def connection(self, *, read_only=False):
            with super().connection(read_only=read_only) as connection:
                yield Connection(connection, self.counts)

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    folder = (
        args.read_existing.resolve()
        if args.read_existing
        else output.parent / (output.stem + "-companies")
    )
    if not args.read_existing:
        folder.mkdir(exist_ok=True)
    report = {
        "source": str(source),
        "python": sys.executable,
        "sizes": sizes,
        "timing_context": args.timing_context,
        "companies": str(folder),
        "read_existing": bool(args.read_existing),
        "measurement": "instrumented wall time; application SQL and Python-returned rows/bytes",
        "dataset": (
            "2 new typed movements/month, target cash account + distinct unrelated cash account; "
            "100/300 fen"
        ),
        "notes": [
            "Cumulative facts explicitly depend on every prior month's calculations.",
            "Counters include source verification, not only the final query.",
            "Account projection seals are validated at period/category scope in stage 3.",
            "SQL counters exclude Store connection setup; integer scalar bytes use width 8.",
            "DB file bytes include retained WAL; logical page bytes exclude its duplication.",
            "Target account uses old FundsRead summary or new scoped balance_totals; "
            "funds_history compares the same production summary API in both sources.",
        ],
        "workloads": {},
    }

    def save_report():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for workload in ("independent", "cumulative") if args.workload == "both" else (args.workload,):
        engine = Engine(
            CountingStore(
                folder / (workload + ".sqlite"), bundle, "synthetic-stage3", "stage3-" + workload
            )
            if args.read_existing
            else CountingStore.create(
                folder / (workload + ".sqlite"),
                bundle,
                "synthetic-stage3",
                "91310000123456789A",
                "stage3-" + workload,
            )
        )
        periods = Periods(engine)
        if args.repair_existing:

            def source_fingerprint():
                with engine.store.connection(read_only=True) as connection:
                    connection.execute("BEGIN")
                    tables = {
                        "evidence",
                        "calculation",
                        "calculation_current",
                        "calculation_publication",
                        "calculation_seal",
                        "dependency_fact",
                        "dependency_calculation",
                        "dependency_scope",
                        "voucher",
                        "voucher_version",
                        "voucher_current",
                        "voucher_line",
                        "period_close",
                        "material_revision",
                        "material_item",
                    }
                    tables.update(
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_schema WHERE type='table' "
                            "AND name GLOB 'fact_*'"
                        )
                    )
                    signature = hashlib.sha256()
                    for table in sorted(tables):
                        rows = sorted(
                            canonical(
                                [
                                    value.hex() if isinstance(value, bytes) else value
                                    for value in row
                                ]
                            )
                            for row in connection.execute(f'SELECT * FROM "{table}"')
                        )
                        signature.update(canonical([table, rows]).encode("utf-8"))
                    return signature.hexdigest()

            before = source_fingerprint()
            repaired = engine.rebuild_projections(request_id="benchmark-repair:" + output.stem)
            assert source_fingerprint() == before, "repair changed an authoritative business source"
            report.setdefault("repairs", {})[workload] = repaired | {
                "sources_and_closes_unchanged": True,
                "preserved_source_digest": before,
            }
        proof = (
            None
            if args.read_existing
            else engine.register_evidence(
                b"synthetic benchmark evidence", "text/plain", "synthetic", request_id="evidence"
            )["digest"]
        )
        checkpoints, close_samples, publish_samples = [], [], []
        report["workloads"][workload] = checkpoints

        def measure(operation):
            engine.store.counts.clear()
            started = time.perf_counter()
            with instrumentation(engine.store.counts, QueryReads):
                value = operation()
            return value, {"seconds": time.perf_counter() - started, **dict(engine.store.counts)}

        def repeat(operation):
            samples = [measure(operation)[1] for _ in range(args.repeats)]
            return {
                "median_seconds": statistics.median(item["seconds"] for item in samples),
                "samples": samples,
            }

        if args.read_existing:
            with engine.store.connection(read_only=True) as connection:
                database_months = connection.execute(
                    "SELECT count(*) FROM period_close"
                ).fetchone()[0]
                assert database_months == sizes[-1]
        for index in sizes if args.read_existing else range(1, sizes[-1] + 1):
            month = str(YearMonth.from_ordinal(YearMonth("2010-01").ordinal + index - 1))
            if not args.read_existing:
                subjects = []
                for account, amount in (("target", 100), ("other-" + month, 300)):
                    subject = account + ":" + month
                    subjects.append(subject)
                    engine.save_fact(
                        Movement.kind,
                        subject,
                        {
                            "period": month,
                            "account_id": account,
                            "amount_fen": amount,
                            "cumulative": workload == "cumulative",
                        },
                        evidence=(proof,),
                        expected_revision=0,
                        request_id="save:" + subject,
                    )

                def publish():
                    preview = engine.preview(subjects)
                    return engine.confirm(
                        subjects,
                        preview_digest=preview["digest"],
                        epochs=preview["epochs"],
                        request_id="publish:" + month,
                    )

                _, sample = measure(publish)
                publish_samples.append(sample)
                for category in MATERIAL_CATEGORIES:
                    periods.inventory(
                        month,
                        category,
                        evidence=[],
                        expected=0,
                        no_business=True,
                        confirmation_evidence=proof,
                        request_id=month + ":" + category,
                    )

                def close():
                    preview = periods.preview_close(month, owner_confirmation=proof)
                    return periods.close(
                        month,
                        owner_confirmation=proof,
                        preview_digest=preview["digest"],
                        epochs=preview["epochs"],
                        request_id="close:" + month,
                    )

                _, sample = measure(close)
                close_samples.append(sample)
            if index not in sizes:
                continue

            def verify():
                with engine.store.connection(read_only=True) as connection:
                    connection.execute("BEGIN")
                    result = verify_integrity(engine, connection)
                    assert result["status"] == "verified", result
                    return result

            def verify_close():
                with engine.store.connection(read_only=True) as connection:
                    connection.execute("BEGIN")
                    result = verify_close_integrity(engine, connection, month)
                    assert result["status"] == "verified", result
                    return result

            def funds():
                with QueryReads.snapshot(engine) as reads:
                    snapshot = _Snapshot(engine, reads.connection, month, reads)
                    view = FundsRead(snapshot)
                    view.account_summary()
                    assert view.account_rows["cash", "target"]["closing_fen"] == index * 100
                    assert len(view.account_rows) == index + 1
                    return view.account_rows

            def summary():
                with QueryReads.snapshot(engine) as reads:
                    result = BusinessQueries(engine, reads=reads).settlement_summary(
                        reads.connection, month
                    )
                    assert len(result["obligations"]) == index * 2, result
                    assert (
                        sum(item["remaining_fen"] for item in result["obligations"]) == index * 400
                    ), result
                    return result

            def target_balance():
                # The old production path exposes only account_summary; stage 3
                # gives consumers a scoped, sealed contribution read.
                with QueryReads.snapshot(engine) as reads:
                    if (source / "src/ai_accounting/kernel/period_balances.py").exists():
                        from ai_accounting.kernel.period_balances import balance_totals

                        rows = balance_totals(
                            reads.connection,
                            YearMonth(month).ordinal,
                            "cash",
                            ("target",),
                            reads=reads,
                        )
                        assert len(rows) == 1 and rows[0]["amount"] == index * 100, rows
                    else:
                        view = FundsRead(_Snapshot(engine, reads.connection, month, reads))
                        view.account_summary()
                        assert view.account_rows["cash", "target"]["closing_fen"] == index * 100

            def brief():
                result = Dashboard(engine).brief(month, limit=5, preparation="deferred")
                assert result["data"]["voucher_count"] == 2, result
                assert result["data"]["total_debit_fen"] == 400, result
                assert result["data"]["total_credit_fen"] == 400, result
                assert result["data"]["funds_overview"]["cash_fen"] == index * 400, result
                return result

            stats = {
                "months": index,
                "database_months": database_months if args.read_existing else index,
                "publish_last": publish_samples[-1] if publish_samples else None,
                "publish_cumulative_seconds": sum(item["seconds"] for item in publish_samples),
                "close_last": close_samples[-1] if close_samples else None,
                "close_cumulative_seconds": sum(item["seconds"] for item in close_samples),
                "full_verify": repeat(verify),
                "close_verify": repeat(verify_close),
                "funds_history": repeat(funds),
                "target_account_balance": repeat(target_balance),
                "settlement_summary": repeat(summary),
                "brief": repeat(brief),
            }
            with engine.store.connection(read_only=True) as connection:
                manifests = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT manifest FROM period_close ORDER BY period"
                    )
                ]
                stats["manifest_bytes"] = sum(
                    len(canonical(item).encode("utf-8")) for item in manifests
                )
                stats["stored_close_result_references"] = sum(
                    len(item.get("adopted_results", item.get("calculations", ())))
                    for item in manifests
                )
                stats["last_close_result_references"] = len(
                    manifests[-1].get("adopted_results", manifests[-1].get("calculations", ()))
                )
                stats["database_logical_bytes"] = (
                    connection.execute("PRAGMA page_count").fetchone()[0]
                    * connection.execute("PRAGMA page_size").fetchone()[0]
                )
                stats["dependency_edges"] = connection.execute(
                    "SELECT count(*) FROM dependency_calculation"
                ).fetchone()[0]
                stats["calculation_count"] = connection.execute(
                    "SELECT count(*) FROM calculation"
                ).fetchone()[0]
                stats["close_reference_rows"] = connection.execute(
                    "SELECT count(*) FROM close_reference"
                ).fetchone()[0]
            stats["database_file_bytes"] = sum(
                path.stat().st_size for path in folder.glob(workload + ".sqlite*")
            )
            checkpoints.append(stats)
            save_report()
            print(
                json.dumps(
                    {
                        "workload": workload,
                        "months": index,
                        "close_seconds": close_samples[-1]["seconds"] if close_samples else None,
                        "funds_seconds": stats["funds_history"]["median_seconds"],
                        "output": str(output),
                    }
                ),
                flush=True,
            )
    save_report()


if __name__ == "__main__":
    main()
