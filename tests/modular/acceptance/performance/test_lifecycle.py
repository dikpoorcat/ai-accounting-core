"""Reduced true resumption and portable fixture chain; no timing budget assertion."""

from .._support import COMPANY
from .dataset import build, open_dataset
from .lifecycle import whole_month
from .portable_cases import package, restore
from .workload import plan_month


def test_real_backup_restore_and_deferred_full_month_resume(tmp_path):
    baseline = tmp_path / "prefix"
    manifest = build(baseline, 2, probe_business_count=240, pending_last_month=True)
    backup = package(baseline, tmp_path / "baseline.zip", "original")
    target = tmp_path / "restored"
    target.mkdir()
    restore(backup, target, manifest, "aggregate_join")
    with open_dataset(target, "aggregate_join", clock_period="2026-02") as (kernel, _):
        planned = plan_month(1, business_count=240, opening_bank=manifest["ending_bank_fen"])
        result = whole_month(kernel, planned)
        assert result["evidence"]["expected_business_count"] == 240
        assert result["evidence"]["archived_business_count"] == 240
        assert result["evidence"]["all_current_month_facts_verified"] is True
        assert result["saved"]["company_id"] == COMPANY
        assert result["saved"]["documents"] == "ready"
        assert result["elapsed_ms"] >= 0
