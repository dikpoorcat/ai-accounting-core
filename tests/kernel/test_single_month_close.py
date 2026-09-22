"""Sequential setup keeps normal historical business behavior after batch retirement."""

import json

import pytest
from monthly_close_fixture import close_months, ready
from test_new_company_reports import profile
from test_opening_continuation import book as book  # noqa: F401

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import Periods


def test_formation_months_freeze_exact_sources_and_independent_backups(book, tmp_path):
    engine, save, _, package, proof = book
    package([])
    profile(save, "2026-01")
    for quarter in (1, 2):
        save(
            "report_income_tax_confirmation",
            f"tax-{quarter}",
            {
                "period": f"2026-{quarter * 3:02d}",
                "treatment": "zero",
                "cumulative_assessed_fen": 0,
                "explanation": "Explicit synthetic zero tax",
            },
        )
    ready(engine, proof, last="2026-07")
    periods = Periods(engine)
    results = close_months(
        periods, proof, last="2026-07", backup_directory=str(tmp_path / "backups")
    )
    previous_digest = None
    for preview, result in results:
        frozen = periods.closed_report(result["period"])
        assert frozen["previous_close_digest"] == previous_digest
        for field in (
            "vouchers",
            "adopted_results",
            "inventories",
            "management_snapshot",
            "material_coverage",
            "readiness",
            "trial_balance",
            "owner_review",
        ):
            assert frozen[field] == preview["manifest"][field]
        with engine.store.connection(read_only=True) as connection:
            job = connection.execute(
                "SELECT kind,status,payload FROM jobs WHERE id=?", (result["backup_job"],)
            ).fetchone()
        payload = json.loads(job["payload"])
        assert job["status"] == "pending" and job["kind"] == "portable_backup"
        assert payload["close_period"] == result["period"]
        assert payload["close_digest"] == result["digest"]
        previous_digest = result["digest"]
    assert results[0][0]["manifest"]["adopted_results"]
    assert len({result["backup_job"] for _, result in results}) == 7
    with pytest.raises(KernelError) as unavailable:
        periods.closed_report("2026-08")
    assert unavailable.value.code == "frozen_snapshot_unavailable"


def test_unready_next_month_does_not_rewrite_previous_close(book):
    engine, _, _, _, proof = book
    ready(engine, proof, omit={("2026-02", "tax")})
    periods = Periods(engine)
    close_months(periods, proof, last="2026-01")
    frozen = periods.closed_report("2026-01")
    with pytest.raises(KernelError) as rejected:
        periods.preview_close("2026-02", owner_confirmation=proof)
    assert rejected.value.code == "period_not_ready"
    assert periods.closed_report("2026-01") == frozen
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1


def test_unpublished_earlier_business_still_blocks_single_month_close(book):
    engine, save, _, _, proof = book
    save(
        "expense",
        "unhandled",
        {
            "period": "2025-12",
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
            "counterparty_id": "supplier",
        },
    )
    ready(engine, proof)
    with pytest.raises(KernelError) as rejected:
        Periods(engine).preview_close("2026-01", owner_confirmation=proof)
    assert rejected.value.code == "earlier_period_open"
    assert rejected.value.details["period"] == "2025-12"
