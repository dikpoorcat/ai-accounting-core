"""Capacity and accounting invariants; measured latency is reported separately."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from ai_accounting.kernel.backup import verify_file

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark-takeover.py"
SPEC = importlib.util.spec_from_file_location("takeover_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows controlled-runtime RSS probe")


def test_twenty_mib_evidence_limit_preserves_atomic_storage(tmp_path):
    _, engine = benchmark.make_engine(tmp_path / "evidence")
    metrics, digest = benchmark.evidence_boundary(engine, 20 * benchmark.MIB)
    assert metrics["bytes"] == 20 * benchmark.MIB
    assert metrics["over_limit_code"] == "evidence_too_large"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
        row = connection.execute("SELECT digest, length(content) FROM evidence").fetchone()
        assert bytes(row[0]).hex() == digest
        assert row[1] == metrics["bytes"]
    assert verify_file(engine.store.path, _registry=engine.store.registry)["evidence_count"] == 1


def test_material_limit_counts_data_rows_and_preserves_exact_integer_totals():
    metrics = benchmark.material_boundary(100_000)
    assert metrics["normalized_items"] == 100_000
    assert metrics["total_fen"] == 12_300_000
    assert metrics["columns"] == 8
    assert metrics["nonempty_cells"] == 800_016
    assert metrics["over_limit_rows"]["status"] == "material_too_large"
    assert metrics["over_limit_rows"]["data_rows"] == 100_001
    assert metrics["over_limit_width"] == "material_too_large"


def test_batch_limit_rejects_before_mutation_and_publication_is_balanced(tmp_path):
    _, engine = benchmark.make_engine(tmp_path / "batch")
    _, digest = benchmark.evidence_boundary(engine, 4096)
    result = benchmark.batch_boundary(engine, digest, 12)
    assert result["save"]["over_limit_rejected"] is True
    assert result["save"]["facts"] == result["publish"]["vouchers"] == 12
    assert result["save"]["sql_driver_calls"] > 0
    assert result["publish"]["sql_driver_calls"] > 0
    assert 0 < result["save"]["write_lock_seconds"] <= result["save"]["seconds"]
    assert 0 < result["publish"]["write_lock_seconds"] <= result["publish"]["seconds"]
    ledger = engine.ledger("2026-03", limit=100)
    assert len(ledger) == 12
    assert all(item["total"] == 100 for item in ledger)
    assert verify_file(engine.store.path, _registry=engine.store.registry)["evidence_count"] == 1


def test_background_backup_preserves_overview_and_ready_quarterly_report(tmp_path):
    catalog, engine = benchmark.make_engine(tmp_path / "background")
    _, digest = benchmark.evidence_boundary(engine, 2 * benchmark.MIB)
    benchmark.batch_boundary(engine, digest, 12)
    benchmark.report_facts(engine, digest)
    result = benchmark.foreground_during_backup(
        catalog, engine, tmp_path / "backups", repetitions=1
    )
    assert result["foreground_results_unchanged"] is True
    assert result["backup_attempts"] == 1
    assert result["baseline"]["overview"][0]["seconds"] > 0
    assert result["baseline"]["quarter_report"][0]["seconds"] > 0
    # Fast hardware may finish the backup before a query starts; overlap is
    # reported explicitly by the benchmark instead of asserted by a timing race.
    assert all(
        "backup_active_at_start" in sample
        for samples in result["during_backup"].values()
        for sample in samples
    )
