"""Selected source checks preserve facts without hydrating unused scalar fields."""

import pytest
from stage9_metrics import measure_work
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.schema import table_name


def selected(engine):
    save(engine)
    publish(engine)
    with engine.store.connection(read_only=True) as connection:
        return connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='charge'"
        ).fetchone()[0]


def test_scalar_source_result_is_exact_without_full_fact_hydration(engine, monkeypatch):
    ident = selected(engine)
    decoded = []
    original = engine.store.fact_data_many

    def counted(connection, identifiers):
        decoded.extend(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "fact_data_many", counted)

    def read():
        with QueryReads.snapshot(engine) as reads:
            return reads.verify_selected_content({ident})

    before, result = measure_work(engine, read)
    assert result[ident]["values"]["amount"] == 100
    assert decoded == []
    for index in range(32):
        save(
            engine,
            subject=f"unrelated-{index}",
            kind="test_source",
            amount=50,
            period="2025-01",
            request=f"unrelated-{index}",
        )
    after, repeated = measure_work(engine, read)
    assert repeated == result
    assert decoded == []
    for counter in ("returned_rows", "returned_value_bytes", "calculation_result_json_decodes"):
        assert after["counters"][counter] == before["counters"][counter]
    # The progress sampler only records complete batches of VM instructions.
    assert (
        after["counters"].get("sqlite_vm_steps", 0)
        <= before["counters"].get("sqlite_vm_steps", 0) + 1000
    )


@pytest.mark.parametrize("field,value", [("amount", 101), ("period", 24288)])
def test_selected_source_rejects_damaged_scalar_body_and_physical_period(engine, field, value):
    ident = selected(engine)
    damage(
        engine,
        table_name("test_charge"),
        f'UPDATE {table_name("test_charge")} SET "{field}"=?',
        (value,),
    )
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.verify_selected_content({ident})
            assert failure.value.code == "content_integrity_failed"
            assert reads._verified_source_contents == {}


def test_scalar_encoding_mismatch_uses_original_fact_decoder(engine, monkeypatch):
    from ai_accounting.kernel import storage

    ident = selected(engine)
    original = engine.store.fact_data_many
    decoded = []

    def counted(connection, identifiers):
        identifiers = set(identifiers)
        decoded.extend(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "fact_data_many", counted)
    # An unsupported/nonmatching byte representation is not a failure and
    # cannot become an unchecked success. The complete original path decides.
    monkeypatch.setattr(storage, "_scalar_fact_hashes", lambda *_: {})
    with QueryReads.snapshot(engine) as reads:
        assert reads.verify_selected_content({ident})[ident]["values"]["amount"] == 100
        assert len(decoded) == 1
