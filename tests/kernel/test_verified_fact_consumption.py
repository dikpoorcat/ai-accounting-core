"""Authenticate stored facts once before typed consumption in the same read."""

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_published_source_bindings import sources as sources  # noqa: F401
from test_reports import book as book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import _verify_report_fact_sources


def classification_ids(engine):
    with engine.store.connection(read_only=True) as connection:
        return {row[0] for row in connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='report_classification'"
        )}


def test_raw_authentication_serves_typed_report_without_second_source_transfer(book):
    engine = book[0]
    scenario(book)
    identifiers = classification_ids(engine)
    assert identifiers

    def read(combined):
        with QueryReads.snapshot(engine) as reads:
            if combined:
                _verify_report_fact_sources(reads.connection, reads, identifiers)
            else:
                verify_sources(engine, reads.connection, fact_ids=identifiers)
            return {
                ident: (version.fact.model_dump(mode="json"), version.evidence)
                for ident, version in reads.fact_versions(identifiers).items()
            }

    together, actual = measure_work(engine, lambda: read(True))
    separate, expected = measure_work(engine, lambda: read(False))
    assert actual == expected
    assert any(row[0]["profit_details"] for row in actual.values())
    assert together["counters"]["returned_value_bytes"] < (
        separate["counters"]["returned_value_bytes"]
    )


def test_damaged_stored_report_is_not_authenticated_by_a_typed_model(book):
    engine = book[0]
    scenario(book)
    identifiers = classification_ids(engine)
    ident = next(iter(identifiers))
    damage(
        engine, "fact_report_classification_profit_details",
        "UPDATE fact_report_classification_profit_details SET amount_fen=amount_fen+1 "
        "WHERE revision_id=?", (ident,),
    )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError, match="核验失败|内容"):
            _verify_report_fact_sources(reads.connection, reads, identifiers)
        assert not identifiers & reads._fact_versions.keys()
        assert not identifiers & reads._report_snapshot_cache["verified_report_fact_sources"]


def test_failed_typed_batch_does_not_publish_half_of_verified_facts(book, monkeypatch):
    import ai_accounting.kernel.storage as storage

    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        identifiers = {row[0] for row in connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind IN ('report_classification','report_profile')"
        )}
    assert len(identifiers) >= 2
    original = storage._validation_json
    calls = 0

    def fail_second(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic typed batch failure")
        return original(data)

    with QueryReads.snapshot(engine) as reads:
        with monkeypatch.context() as scope:
            scope.setattr(storage, "_validation_json", fail_second)
            with pytest.raises(ValueError, match="synthetic"):
                _verify_report_fact_sources(reads.connection, reads, identifiers)
        assert calls == 2
        assert not identifiers & reads._fact_versions.keys()
        assert not identifiers & reads._report_snapshot_cache["verified_report_fact_sources"]
        _verify_report_fact_sources(reads.connection, reads, identifiers)
        assert reads.fact_versions(identifiers).keys() == identifiers


def test_fresh_raw_detail_combines_checked_state_and_body_without_changing_result(sources):
    engine, rows = sources
    ident = rows["cost"]["id"]

    def read(owned):
        if owned:
            with QueryReads.snapshot(engine) as reads:
                return reads.raw_calculations({ident})[ident]
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return QueryReads(engine, connection).raw_calculations({ident})[ident]

    together, actual = measure_work(engine, lambda: read(True))
    separate, expected = measure_work(engine, lambda: read(False))
    assert actual == expected
    assert actual["outcome"]["values"]["creditor_kind"] == "supplier"
    assert together["counters"]["calculation_result_json_decodes"] < (
        separate["counters"]["calculation_result_json_decodes"]
    )
