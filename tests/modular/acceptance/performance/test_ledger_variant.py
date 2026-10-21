"""A small real C14 replacement check; this is not an official performance run."""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from ai_accounting_modular.storage.public import RepositorySession

from .._support import COMPANY, SPEC, FixedDateTime, http, snapshot
from .._worker import Worker
from .ledger_variant import install_ledger_implementation
from .projections import capture


@contextmanager
def grouped_sql_reads():
    """Observe successful real reads; never replace a query or its returned rows."""
    executed = []
    original = RepositorySession.fetchall

    def observed(session, sql, parameters=()):
        rows = original(session, sql, parameters)
        # This is the non-None cutoff branch of the actual alternate selector.
        # A helper call alone, a current-head JOIN or a ready cache cannot count.
        if (
            "GROUP BY business_id,result_slot" in sql
            and "JOIN (" in sql
            and "selected.business_id=r.business_id" in sql
        ):
            executed.append((sql, parameters, len(rows)))
        return rows

    with patch.object(RepositorySession, "fetchall", observed):
        yield executed


def test_real_grouped_sql_keeps_complete_public_projections_and_ready_models(tmp_path):
    with ExitStack() as stack:
        for name in (
            "ai_accounting_modular.reading._brief.datetime",
            "ai_accounting_modular.reading._business_status.datetime",
        ):
            stack.enter_context(patch(name, FixedDateTime))
        root = tmp_path / "synthetic"
        root.mkdir()
        with install_ledger_implementation("original"):
            worker = Worker(root)
            try:
                for name in (
                    "common",
                    "opening_bank_2000",
                    "employment",
                    "wage_1000",
                    "pay_wage_1000",
                    "expense_a_1000",
                    "page_25_seed",
                ):
                    result = worker.apply(SPEC["requests"][name])
                    assert result["completion"] == "complete" and result["documents"] == "ready", (
                        result
                    )
                baseline = {
                    month: capture(worker.kernel, COMPANY, month)
                    for month in ("2026-01", "2026-02")
                }
                business = snapshot(worker.kernel)
            finally:
                worker.kernel.close()
        with install_ledger_implementation("aggregate_join"):
            worker = Worker(root)
            try:
                assert worker.kernel.reading.run_pending(COMPANY) == ()
                with grouped_sql_reads() as initial_sql:
                    actual = {
                        month: capture(worker.kernel, COMPANY, month)
                        for month in ("2026-01", "2026-02")
                    }
                assert any(count for _, _, count in initial_sql), initial_sql
                assert actual == baseline
                assert snapshot(worker.kernel) == business
                assert worker.kernel.reading.run_pending(COMPANY) == ()
                saved = worker.apply(SPEC["requests"]["expense_a_900"])
                assert saved["completion"] == "complete", saved
                for page in ("funds", "employees", "assets"):
                    response = http(
                        worker.kernel, page, {"company_id": COMPANY, "period": "2026-01"}
                    )
                    assert response["body"]["state"] == "ready", response
                    assert response["body"]["response"] == baseline["2026-01"]["defaults"][page]
                with grouped_sql_reads() as changed_sql:
                    changed = capture(worker.kernel, COMPANY, "2026-01")
                assert any(count for _, _, count in changed_sql), changed_sql
                assert (
                    changed["defaults"]["brief"]["snapshot_version"]
                    != baseline["2026-01"]["defaults"]["brief"]["snapshot_version"]
                )
                assert (
                    changed["defaults"]["quarterly-report"]
                    != baseline["2026-01"]["defaults"]["quarterly-report"]
                )
            finally:
                worker.kernel.close()
        with install_ledger_implementation("original"):
            worker = Worker(root)
            try:
                assert capture(worker.kernel, COMPANY, "2026-01") == changed
                assert worker.kernel.reading.run_pending(COMPANY) == ()
            finally:
                worker.kernel.close()
