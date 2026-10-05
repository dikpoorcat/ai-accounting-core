"""Detail loaders consume an already authenticated result without reading it again."""

import pytest
from stage9_metrics import measure_work
from test_published_source_bindings import sources as sources  # noqa: F401
from test_reports import book as book  # noqa: F401

from ai_accounting.kernel.query_reads import QueryReads


@pytest.mark.parametrize("owned", [True, False])
def test_complete_detail_loaders_reuse_only_the_owned_snapshot(sources, owned):
    engine, rows = sources
    ident = rows["cost"]["id"]

    def read_with(reads):
        expected = reads.verify_selected_content({ident})[ident]
        typed = reads.calculations({ident})[ident]
        historical = reads.raw_calculations({ident})[ident]
        assert typed["outcome"] == historical["outcome"] == expected
        assert typed["fact_id"] == historical["fact_id"]
        assert typed["fact_data"]["amount_fen"] == historical["fact_data"]["amount_fen"]
        return expected

    def operation():
        if owned:
            with QueryReads.snapshot(engine) as reads:
                return read_with(reads)
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return read_with(QueryReads(engine, connection))

    work, result = measure_work(engine, operation)
    assert result["values"]["creditor_kind"] == "supplier"
    if owned:
        assert work["counters"]["calculation_result_rows_loaded"] == 1
    else:
        # Unmanaged loaders also retain their independent SQL syntax proof.
        assert work["counters"]["calculation_result_rows_loaded"] >= 3
    if owned:
        assert work["counters"]["calculation_result_json_decodes"] == 1
    else:
        assert work["counters"]["calculation_result_json_decodes"] >= 3


def test_typed_detail_failure_does_not_publish_a_partial_loader_cache(sources, monkeypatch):
    engine, rows = sources
    ident = rows["cost"]["id"]
    with QueryReads.snapshot(engine) as reads:
        expected = reads.verify_selected_content({ident})[ident]
        original = reads.facts

        def fail(_):
            raise ValueError("injected typed fact loading failure")

        monkeypatch.setattr(reads, "facts", fail)
        with pytest.raises(ValueError, match="injected"):
            reads.calculations({ident})
        assert not reads._calculations
        monkeypatch.setattr(reads, "facts", original)
        assert reads.calculations({ident})[ident]["outcome"] == expected
