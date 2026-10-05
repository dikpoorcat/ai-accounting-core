"""A source body is transferred once even when two required proofs consume it."""

from stage9_metrics import measure_work
from test_published_source_bindings import sources as sources  # noqa: F401
from test_reports import book as book  # noqa: F401

from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import verify_published_source_bindings


def test_anchor_links_existing_content_proof_without_second_result_transfer(sources):
    engine, rows = sources
    ident, original = rows["cost"]["id"], rows["cost"]["outcome"]

    def read():
        with QueryReads.snapshot(engine) as reads:
            result = reads.verify_selected_content({ident})[ident]
            assert not verify_published_source_bindings(reads, {ident})
            reads.verify_sql_outcomes({ident})
            return result

    work, result = measure_work(engine, read)
    assert result["values"]["creditor_kind"] == "supplier"
    assert work["counters"]["calculation_result_rows_loaded"] == 1
    assert work["counters"]["calculation_result_bytes_loaded"] == len(original.encode("utf-8"))
    assert work["counters"]["calculation_result_json_decodes"] == 1


def test_anchor_result_lookup_does_not_walk_unrelated_content_cache(sources):
    engine, rows = sources

    class ExactLookups(dict):
        def keys(self):
            raise AssertionError("source lookup scanned unrelated proven results")

        def __iter__(self):
            raise AssertionError("source lookup scanned unrelated proven results")

    with QueryReads.snapshot(engine) as reads:
        ident = rows["cost"]["id"]
        expected = reads.verify_selected_content({ident})[ident]
        reads._verified_source_contents = ExactLookups(reads._verified_source_contents)
        reads._verified_source_contents.update((f"unrelated-{i}", {}) for i in range(5000))
        assert not verify_published_source_bindings(reads, {ident})
        reads.verify_sql_outcomes({ident})
        assert reads._verified_source_contents[ident] == expected
