"""Live journal consumers retain the exact frozen voucher header authority."""

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import metric_rows

engine = engine_fixture


@pytest.fixture
def frozen_journal(engine):
    save(engine, subject="first", request="first")
    save(engine, subject="second", request="second", amount=200)
    publish(engine, ["first", "second"])
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        vouchers = [dict(row) for row in connection.execute(
            "SELECT v.*,n.number FROM voucher_version v JOIN voucher n ON n.id=v.voucher_id "
            "ORDER BY n.number",
        )]
    return engine, vouchers


def journal_read(engine, consumer):
    with Dashboard(engine)._snapshot("2026-01") as snap:
        journal = snap.month_journal
        if consumer == "page":
            return journal.page(0, 20)
        if consumer == "metrics":
            return metric_rows(journal, ("amount",))
        if consumer == "summary":
            return journal.prime_summary()
        if consumer == "kinds":
            return journal.kind_counts()
        if consumer == "totals":
            return journal.totals()
        return len(journal)


@pytest.mark.parametrize("consumer", ["page", "metrics", "summary", "kinds", "totals", "count"])
@pytest.mark.parametrize("field", ["reverses_id", "total", "number"])
def test_frozen_live_header_changes_fail_all_journal_consumers(frozen_journal, consumer, field):
    engine, vouchers = frozen_journal
    assert journal_read(engine, consumer)
    target = vouchers[-1]
    table = "voucher" if field == "number" else "voucher_version"
    ident = target["voucher_id"] if field == "number" else target["id"]
    changed = vouchers[0]["id"] if field == "reverses_id" else target[field] + 1000
    damage(engine, table, f"UPDATE {table} SET {field}=? WHERE id=?", (changed, ident))
    for _ in range(2):
        with pytest.raises(KernelError) as failure:
            journal_read(engine, consumer)
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("subjects", [None, {"second"}])
@pytest.mark.parametrize("consumer", ["summary", "kinds", "totals", "count", "page", "metrics"])
def test_frozen_month_moved_header_cannot_disappear_from_requested_scope(
    frozen_journal, subjects, consumer,
):
    engine, vouchers = frozen_journal
    damage(engine, "voucher_version", "UPDATE voucher_version SET period=period+1 WHERE id=?",
           (vouchers[-1]["id"],))
    for _ in range(2):
        with Dashboard(engine)._snapshot("2026-01") as snap:
            journal = snap.month_journal.select(subjects=subjects)
            with pytest.raises(KernelError) as failure:
                if consumer == "summary":
                    journal.prime_summary()
                elif consumer == "kinds":
                    journal.kind_counts()
                elif consumer == "totals":
                    journal.totals()
                elif consumer == "count":
                    len(journal)
                elif consumer == "metrics":
                    metric_rows(journal, ("amount",))
                else:
                    journal.page(0, 20)
            assert failure.value.code == "content_integrity_failed"


def test_header_only_summary_reuse_does_not_claim_result_body_proof(frozen_journal):
    engine, vouchers = frozen_journal
    with Dashboard(engine)._snapshot("2026-01") as snap:
        journal = snap.month_journal
        summary = journal.prime_summary()
        assert summary["count"] == len(vouchers) == 2
        assert summary["totals"] == {"line_count": 4, "debit": 300, "credit": 300}
        assert journal.kind_counts() == [{"kind": "test_charge", "reversal": 0, "count": 2}]
        assert len(journal) == 2
        assert journal.totals() == summary["totals"]
        assert journal.verified_rows() is None
        assert not snap.reads._verified_source_contents
        assert not snap.reads._verified_sql_outcomes
