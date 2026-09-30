"""Page readiness checks the same issues without decoding frozen coverage rows."""

import pytest
from stage9_book import MixedBook

from ai_accounting.kernel import close_storage, frozen_material, materials
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth


def test_read_summary_has_full_issue_parity_and_does_not_load_closed_rows(tmp_path, monkeypatch):
    book = MixedBook(tmp_path / "stage9-summary", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    month = YearMonth("2016-02").ordinal
    with QueryReads.snapshot(book.engine) as reads:
        full = materials.check_completeness(reads.connection, month, book.engine.store.registry)
        reused = frozen_material.verified_frozen_material_summary(
            reads.connection,
            month,
            month - 1,
            book.engine.store.registry,
            _query_reads=reads,
        )
        assert reused.source_ids

        def no_full_rows(*_args, **_kwargs):
            raise AssertionError("page summary must not decode frozen coverage bodies")

        monkeypatch.setattr(close_storage, "decode_close", no_full_rows)
        monkeypatch.setattr(close_storage, "read_material_sources", no_full_rows)
        original = close_storage._bucket_rows

        def bounded(connection, header, subroot, field, bucket, **options):
            assert field != "material_coverage.coverage"
            return original(connection, header, subroot, field, bucket, **options)

        monkeypatch.setattr(close_storage, "_bucket_rows", bounded)
        summary = materials.read_completeness_summary(
            reads.connection,
            month,
            book.engine.store.registry,
            _query_reads=reads,
        )
        assert isinstance(summary, materials.MaterialReadSummary)
        assert list(summary.issues) == full["issues"]
        assert not hasattr(summary, "coverage")
        assert not hasattr(summary, "fact_ids")
        again = materials.read_completeness_summary(
            reads.connection,
            month,
            book.engine.store.registry,
            _query_reads=reads,
        )
        assert again == summary

    # A closed page asks about today's state of that exact older month. A
    # verified complete source can also establish absence of issues there.
    with QueryReads.snapshot(book.engine) as reads:
        monkeypatch.undo()
        historical = materials.check_completeness(
            reads.connection,
            month - 1,
            book.engine.store.registry,
        )
        historical_summary = materials.read_completeness_summary(
            reads.connection,
            month - 1,
            book.engine.store.registry,
            _query_reads=reads,
        )
        assert list(historical_summary.issues) == historical["issues"]


def test_changed_closed_source_is_rechecked_and_summary_blocks_are_verified(tmp_path):
    from test_integrity_content import damage

    book = MixedBook(tmp_path / "stage9-summary-change", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    month = YearMonth("2016-02").ordinal
    # A changed linked accounting head invalidates reuse; neither path can hide it.
    with book.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        subject = connection.execute(
            "SELECT l.subject_id FROM fact_material_resolution_v2 r "
            "JOIN fact_current h ON h.fact_id=r.revision_id "
            "JOIN fact_material_resolution_v2_links l ON l.revision_id=r.revision_id "
            "WHERE r.period=? LIMIT 1",
            (month - 1,),
        ).fetchone()[0]
        connection.execute("DELETE FROM calculation_current WHERE subject_id=?", (subject,))
        full = materials.check_completeness(connection, month, book.engine.store.registry)
        summary = materials.read_completeness_summary(connection, month, book.engine.store.registry)
        assert list(summary.issues) == full["issues"]
        assert summary.issues
        connection.rollback()
    damage(
        book.engine,
        "close_storage_block",
        "UPDATE close_storage_block SET content='[]' WHERE field='material_source_summaries'",
    )
    with book.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError, match="关账"):
            materials.read_completeness_summary(connection, month, book.engine.store.registry)
