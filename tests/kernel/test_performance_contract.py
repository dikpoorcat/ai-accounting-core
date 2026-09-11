"""Structural performance contracts; wall-clock values belong in the benchmark."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

from ai_accounting.kernel.backup import verify_file

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark-local-kernel.py"
SPEC = importlib.util.spec_from_file_location("local_kernel_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def test_foreground_sql_counts_do_not_grow_with_accounting_history(tmp_path):
    engine = benchmark.make_engine(tmp_path / "history.sqlite")
    small = benchmark.seed_to(engine, 100)
    small_metrics, _ = benchmark.measure(
        engine,
        lambda n: benchmark.foreground_probe(engine, small["proof"], f"small:{n}"),
        repetitions=2,
    )
    large = benchmark.seed_to(engine, 2000, existing=100)
    large_metrics, _ = benchmark.measure(
        engine,
        lambda n: benchmark.foreground_probe(engine, large["proof"], f"large:{n}"),
        repetitions=2,
    )
    assert small_metrics["read_calls"] == large_metrics["read_calls"]
    assert small_metrics["driver_calls"] == large_metrics["driver_calls"]
    assert verify_file(engine.store.path, _registry=engine.store.registry)["evidence_count"] == 1


def test_overview_cursor_and_rebuild_work_without_per_voucher_sql(tmp_path):
    engine = benchmark.make_engine(tmp_path / "history.sqlite")
    benchmark.seed_to(engine, 240)
    overview_small, before = benchmark.measure(
        engine, lambda _: engine.overview("2025-12"), repetitions=1
    )
    rebuild_small, _ = benchmark.measure(
        engine, lambda _: engine.rebuild_projections(request_id="small-rebuild"), repetitions=1
    )
    benchmark.seed_to(engine, 2400, existing=240)
    overview_large, _ = benchmark.measure(
        engine, lambda _: engine.overview("2025-12"), repetitions=1
    )
    rebuild_large, _ = benchmark.measure(
        engine, lambda _: engine.rebuild_projections(request_id="large-rebuild"), repetitions=1
    )
    assert overview_small["read_calls"] == overview_large["read_calls"]
    assert rebuild_small["driver_calls"] == rebuild_large["driver_calls"]
    assert len(before["accounts"]) == 2
    first = engine.ledger("2025-12", limit=3)
    second = engine.ledger("2025-12", after_number=first[-1]["number"], limit=3)
    assert len(first) == len(second) == 3
    assert set(row["id"] for row in first).isdisjoint(row["id"] for row in second)
    assert first[-1]["number"] < second[0]["number"]


def test_empty_pending_checks_do_not_scan_accounting_history(tmp_path, monkeypatch):
    def prepare_and_measure(size):
        engine = benchmark.make_engine(tmp_path / f"pending-{size}.sqlite")
        fixture = benchmark.seed_to(engine, size)
        benchmark.prepare_history_closes(engine, fixture["proof"], before="2026-01")
        periods = benchmark.Periods(engine)
        for category in benchmark.MATERIAL_CATEGORIES:
            periods.inventory(
                "2026-01",
                category,
                evidence=[],
                expected=0,
                no_business=True,
                confirmation_evidence=fixture["proof"],
                request_id=f"empty-period-{category}",
            )
        real_connection = engine.store.connection
        steps = 0

        def progress():
            nonlocal steps
            steps += 10
            return 0

        @contextmanager
        def counted_connection(*, read_only=False):
            with real_connection(read_only=read_only) as connection:
                connection.set_progress_handler(progress, 10)
                yield connection

        monkeypatch.setattr(engine.store, "connection", counted_connection)

        def count_steps(operation):
            nonlocal steps
            steps = 0
            operation()
            return steps

        return (
            count_steps(lambda: engine.overview("2025-12")),
            count_steps(
                lambda: periods.preview_close("2026-01", owner_confirmation=fixture["proof"])
            ),
        )

    small = prepare_and_measure(240)
    large = prepare_and_measure(2400)
    # Counting SQL calls alone misses a planner that probes the empty pending
    # table once for every historical fact. SQLite VM work catches that regression.
    assert large[0] <= small[0] + 200
    assert large[1] <= small[1] + 200
