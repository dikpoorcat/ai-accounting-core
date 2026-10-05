"""Actual owned terminal rows prove only their normal reverse relation pairs."""

import pytest
from stage9_metrics import measure_work
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save
from test_integrity_content import damage
from test_open_correction_source_heads import prepare
from test_publication_periods import company as company  # noqa: F401
from test_publication_periods import publish as position_publish
from test_publication_periods import save as position_save

from ai_accounting.kernel import query_reads
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications
from ai_accounting.kernel.types import YearMonth

JAN = YearMonth("2026-01").ordinal


def read_pairs(engine, period=JAN):
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(period)
        versions = {row[0] for row in reads.connection.execute(
            "SELECT version_id FROM voucher_current",
        )}
        pairs = verify_current_voucher_publications(reads.connection, versions)
        assert not reads._verified_source_contents
        assert not reads._verified_saved_input_identities
        return {key: dict(row) for key, row in pairs.items()}


def test_normal_actual_rows_replace_reverse_reread_and_preserve_receipt_shape(engine, monkeypatch):
    for subject in ("one", "two", "three"):
        save(engine, subject=subject, request=subject)
    publish(engine, ["one", "two", "three"])
    owned_work, owned = measure_work(engine, lambda: read_pairs(engine))
    with monkeypatch.context() as patch:
        patch.setattr(query_reads, "_owns_current_selector_snapshot", lambda *args: False)
        independent_work, independent = measure_work(engine, lambda: read_pairs(engine))
    assert owned == independent and len(owned) == 3
    assert all(row["has_successor"] == 0 and "version_id" not in row for row in owned.values())
    statements = [row["statement"] for row in owned_work["sql"]]
    assert not any(sql.startswith("SELECT a.subject_id,a.calculation_id") for sql in statements)
    assert not any(sql.startswith("SELECT p.*,EXISTS(") for sql in statements)
    assert any(sql.startswith("SELECT v.id FROM voucher_version v INDEXED") for sql in statements)
    for counter in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        assert owned_work["counters"][counter] < independent_work["counters"][counter]
    assert owned_work["counters"]["calculation_result_rows_loaded"] == 0


@pytest.mark.parametrize("mode", ["review", "replace", "clear", "withdraw"])
def test_legal_noninitial_receipts_match_independent_checker(company, monkeypatch, mode):
    engine = company[0]
    position_save(company, 100)
    position_publish(company, "initial")
    if mode == "withdraw":
        preview = engine.preview_delete("position")
        engine.delete("position", preview_digest=preview["digest"], epochs=preview["epochs"],
                      request_id="delete")
    else:
        position_save(company, 100 if mode == "review" else 150 if mode == "replace" else 0, 1)
        position_publish(company, "second")
    owned = read_pairs(engine)
    with monkeypatch.context() as patch:
        patch.setattr(query_reads, "_owns_current_selector_snapshot", lambda *args: False)
        independent = read_pairs(engine)
    assert owned == independent


@pytest.mark.parametrize("mode", ["same_month_replace", "move_review", "zero_correction"])
def test_reversal_and_review_keep_independent_pairs(company, monkeypatch, mode):
    prepare(company, mode)
    engine = company[0]
    owned_work, owned = measure_work(engine, lambda: read_pairs(engine, JAN + 3))
    with monkeypatch.context() as patch:
        patch.setattr(query_reads, "_owns_current_selector_snapshot", lambda *args: False)
        independent = read_pairs(engine, JAN + 3)
    assert owned == independent
    assert any(row["statement"].startswith("SELECT p.*,EXISTS(") for row in owned_work["sql"])
    assert any(row["statement"].startswith("SELECT ids.value requested_id,v.id")
               for row in owned_work["sql"])


@pytest.mark.parametrize("target", ["current", "publication", "subject"])
def test_missing_or_conflicting_source_never_publishes_normal_relations(engine, target):
    save(engine)
    publish(engine)
    if target == "current":
        damage(engine, "calculation_current", "DELETE FROM calculation_current")
    elif target == "publication":
        damage(engine, "calculation_publication", "DELETE FROM calculation_publication",
               foreign_keys=False)
    else:
        damage(engine, "calculation", "UPDATE calculation SET subject_id='other'",
               foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError):
                reads.verify_open_voucher_scope(JAN)
            assert not reads._verified_current_voucher_publications
            assert not reads._verified_publication_ids
            assert not reads._verified_open_voucher_scopes


def test_independent_failure_discards_staged_normals_and_keeps_prior_success(
    engine, monkeypatch,
):
    save(engine, subject="prior", request="prior")
    publish(engine, ["prior"], request="prior-publish")
    save(engine, subject="normal", period="2026-02", request="normal")
    save(engine, subject="review", period="2026-02", request="review")
    publish(engine, ["normal", "review"], request="feb-publish")
    save(engine, subject="review", revision=1, period="2026-02", request="review-again")
    publish(engine, ["review"], request="review-publish")
    original = query_reads._current_voucher_signature

    def fail_independent(row):
        if row["subject_id"] == "review":
            raise KernelError("content_integrity_failed", "independent reverse fault")
        return original(row)

    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(JAN)
        previous_relations = dict(reads._verified_current_voucher_publications)
        previous_records = dict(reads._verified_publication_ids)
        with monkeypatch.context() as patch:
            patch.setattr(query_reads, "_current_voucher_signature", fail_independent)
            for _ in range(2):
                with pytest.raises(KernelError, match="independent reverse fault"):
                    reads.verify_open_voucher_scope(JAN + 1)
                assert reads._verified_current_voucher_publications == previous_relations
                assert reads._verified_publication_ids == previous_records
                assert reads._verified_open_voucher_scopes == {None: JAN}
        reads.verify_open_voucher_scope(JAN + 1)
        assert len(reads._verified_current_voucher_publications) == 3


def test_existing_signature_conflict_fails_before_independent_can_publish(engine, monkeypatch):
    save(engine)
    publish(engine)
    with QueryReads.snapshot(engine) as reads:
        version = reads.connection.execute("SELECT version_id FROM voucher_current").fetchone()[0]
        existing = (None, {})
        reads._verified_current_voucher_publications[version] = existing

        def forbidden(*args):
            pytest.fail("independent reverse check must follow normal signature validation")

        monkeypatch.setattr(query_reads, "_verify_current_voucher_publication_rows", forbidden)
        with pytest.raises(KernelError, match="凭证头与本次已核内容不一致"):
            reads.verify_open_voucher_scope(JAN)
        assert reads._verified_current_voucher_publications == {version: existing}
        assert not reads._verified_publication_ids
