"""Calibration for the Stage 9 diagnostic work counters."""

import json

from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.materials import Specification, _CompletenessInspectionCache
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import read_open_contributions

engine = engine_fixture


def test_typed_and_result_decodes_count_actual_snapshot_work(engine):
    fact_id = save(engine)["fact_id"]
    _, published = publish(engine)
    calculation_id = published["results"][0]["calculation_id"]

    def read_twice():
        with QueryReads.snapshot(engine) as reads:
            assert reads.fact_version(fact_id) == reads.fact_version(fact_id)
            assert reads.calculation(calculation_id) == reads.calculation(calculation_id)

    report, _ = measure_work(engine, read_twice)
    counters = report["counters"]
    assert counters["typed_fact_json_decodes"] == 1
    assert counters["calculation_result_json_decodes"] == 1
    assert counters["typed_fact_json_input_bytes"] > 0
    assert counters["calculation_result_input_bytes"] > 0
    assert counters["stdlib_json_loads"] >= counters["calculation_result_json_decodes"]
    assert "Pydantic" in report["decode_counter_definitions"]["stdlib_json_loads"]

    # Store.fact and Store.facts are two real typed parses, never four counts
    # from also counting their wrapper calls.
    def direct_loads():
        with engine.store.connection(read_only=True) as connection:
            engine.store.fact(connection, fact_id)
            engine.store.facts(connection, (fact_id,))

    direct, _ = measure_work(engine, direct_loads)
    assert direct["counters"]["typed_fact_json_decodes"] == 2


def test_adoption_and_original_parser_counters_skip_successful_cache_hits(engine):
    save(engine)
    publish(engine)
    close(engine)
    raw = b"name,amount\nexample,1\n"
    specification = Specification(format="csv")

    def read_twice():
        with QueryReads.snapshot(engine) as reads:
            row = reads.connection.execute("SELECT * FROM period_close").fetchone()
            first = reads.close_accounting(row, subjects={"charge"})
            assert first == reads.close_accounting(row, subjects={"charge"})
            inspections = _CompletenessInspectionCache(reads.connection)
            parsed = inspections.inspect(reads.connection, "source", "evidence", specification, raw)
            assert parsed == inspections.inspect(
                reads.connection, "source", "evidence", specification, raw
            )

    report, _ = measure_work(engine, read_twice)
    counters = report["counters"]
    assert counters["adoption_accounting_slice_reads"] == 1
    assert counters["adoption_accounting_rows"] == 1
    assert counters["material_original_parses"] == 1
    assert counters["material_original_input_bytes"] == len(raw)
    assert (
        json.loads(json.dumps(report["decode_counter_definitions"]))
        == report["decode_counter_definitions"]
    )


def test_raw_fact_proof_counts_pydantic_core_decode_separately(engine):
    fact_id = save(engine)["fact_id"]

    def load_and_prove():
        with QueryReads.snapshot(engine) as reads:
            reads.fact_version(fact_id)
            verify_sources(engine, reads.connection, fact_ids={fact_id})

    report, _ = measure_work(engine, load_and_prove)
    assert report["counters"]["typed_fact_json_decodes"] == 1
    assert report["counters"]["raw_fact_json_decodes"] == 1


def test_contribution_counter_survives_parser_change_and_skips_cached_reads(engine):
    save(engine)
    _, published = publish(engine)
    calculation_ids = {published["results"][0]["calculation_id"]}

    def read_twice():
        with QueryReads.snapshot(engine) as reads:
            first = read_open_contributions(
                engine, reads.connection, calculation_ids, reads=reads
            )
            assert first == read_open_contributions(
                engine, reads.connection, calculation_ids, reads=reads
            )
            assert set(first) == calculation_ids

    report, _ = measure_work(engine, read_twice)
    assert report["counters"]["report_contribution_json_decodes"] == 1
    assert report["counters"]["report_contribution_json_input_bytes"] > 0
