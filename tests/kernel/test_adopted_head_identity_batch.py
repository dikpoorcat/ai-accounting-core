"""Adopted identity batches keep exact source/seal proof without body authority."""

import pytest
from test_employee_head_adoption_reads import heads
from test_integrity_content import damage
from test_payroll import payroll
from test_settlement_late_reviews import prepared

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import verified_payroll_heads


@pytest.mark.parametrize("field", ["subject_id", "kind", "period"])
def test_adopted_head_rejects_damaged_fact_identity_before_publication_batch(tmp_path, field):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    with company.engine.store.connection(read_only=True) as connection:
        selected = company.engine.store.current_fact(connection, "january")
        second = company.engine.store.current_fact(connection, "february-unpublished")
    if field == "kind":
        damage(company.engine, "subject", "UPDATE subject SET kind='annual_bonus' WHERE id=?",
               (selected.subject_id,))
    else:
        value = second.subject_id if field == "subject_id" else second.fact.period.ordinal
        change = f"{field}=?" + (",revision=revision+1000" if field == "subject_id" else "")
        damage(company.engine, "fact_revision", f"UPDATE fact_revision SET {change} WHERE id=?",
               (value, selected.id))
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        with pytest.raises(KernelError) as failure:
            verified_payroll_heads(snap, heads(snap))
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("field", ["subject_id", "fact_id", "kind", "period", "posting_period"])
def test_all_head_fields_still_bind_to_verified_source_and_adoption(tmp_path, field):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        selected = heads(snap)
        old = next(row for row in selected if row["subject_id"] == "january")
        old[field] = old[field] + 1 if field in {"period", "posting_period"} else "other"
        with pytest.raises(KernelError) as failure:
            verified_payroll_heads(snap, selected)
        assert failure.value.code == "content_integrity_failed"
        assert not snap.reads._verified_source_contents
        assert old["id"] not in snap.reads._verified_sql_outcomes


def test_identity_headers_success_keeps_bodies_and_metadata_unproven(tmp_path):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        selected = heads(snap)
        identifiers = {head["id"] for head in selected}
        before = dict(snap.reads._metadata)
        result = snap.reads.calculation_identity_headers(identifiers)
        assert result.keys() == identifiers
        assert all(row["source_id"] == ident and row["id"] is not None
                   for ident, row in result.items())
        proven = verified_payroll_heads(snap, selected)
        assert {row["id"] for row in proven.values()} == identifiers
        assert snap.reads._metadata == before
        assert not snap.reads._verified_source_contents
        assert not snap.reads._verified_sql_outcomes
        assert not snap.reads._anchored_source_bytes


def test_identity_headers_missing_requested_source_does_not_publish_partial_proof(tmp_path):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        selected = heads(snap)
        identifiers = {head["id"] for head in selected} | {"missing-calculation"}
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                snap.reads.calculation_identity_headers(identifiers)
            assert failure.value.code == "content_integrity_failed"
            assert not snap.reads._metadata
            assert not snap.reads._verified_source_contents
            assert not snap.reads._verified_sql_outcomes


def test_identity_headers_missing_publication_rejects_complete_batch(tmp_path):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        selected = heads(snap)
        missing = next(head["id"] for head in selected if head["subject_id"] == "january")
    damage(company.engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE calculation_id=?", (missing,),
           foreign_keys=False)
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        with pytest.raises(KernelError) as failure:
            snap.reads.calculation_identity_headers({head["id"] for head in selected})
        assert failure.value.code == "content_integrity_failed"
        assert not snap.reads._metadata
        assert not snap.reads._verified_source_contents
        assert not snap.reads._verified_sql_outcomes
