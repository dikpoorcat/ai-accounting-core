"""A whole-month money proof already contains the complete journal summary."""

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard


@pytest.mark.parametrize("closed", [False, True])
def test_complete_proof_summary_equals_independent_full_range_and_needs_no_second_scan(
    engine, closed
):
    save(engine, subject="first", amount=100, request="first")
    save(engine, subject="second", amount=200, request="second")
    publish(engine, ["first", "second"])
    if closed:
        close(engine)
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-01") as snap:
        expected = snap.month_journal.prime_summary()
        assert snap.month_journal.verified_rows() is None
    with dashboard._snapshot("2026-01") as snap:
        assert snap.month_journal.account_amounts() == {"5602": [300, 0], "2202": [0, 300]}
        assert len(snap.month_journal.verified_rows()) == 2
        vm_steps = [0]

        def observe_vm():
            vm_steps[0] += 1
            return 0

        snap.connection.set_progress_handler(observe_vm, 1)
        try:
            result = snap.month_journal.prime_summary()
        finally:
            snap.connection.set_progress_handler(None, 0)
        assert result == expected
        assert vm_steps == [0]
        assert snap.month_journal.select(subjects={"first"}).verified_rows() is None


def test_failed_full_range_money_proof_cannot_publish_headers_or_summary(engine):
    save(engine, subject="first", amount=100)
    publish(engine, ["first"])
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute("SELECT id FROM voucher_version").fetchone()[0]
    damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=? AND line_no=1",
           (ident,))
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
        assert snap.month_journal.verified_rows() is None
        assert not any(key[0] == "journal_summary" for key in snap.reads._report_snapshot_cache)
