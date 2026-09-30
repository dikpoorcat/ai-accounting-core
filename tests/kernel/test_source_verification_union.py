"""One source verification loads overlapping facts and originals only once."""

from collections import Counter

import pytest
from stage9_metrics import measure_work
from test_engine import engine as engine_fixture
from test_engine import publish, save
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_sources

engine = engine_fixture


def source_book(engine):
    dependency = save(engine, "source", "test_source", 25, request="source")["fact_id"]
    own = save(engine)["fact_id"]
    _, published = publish(engine)
    extra = save(engine, "extra", "test_source", 12, request="extra", period="2026-02")[
        "fact_id"
    ]
    return dependency, own, extra, published["results"][0]["calculation_id"]


def test_overlapping_input_union_has_one_load_and_no_unrelated_history_growth(engine, monkeypatch):
    dependency, own, extra, calculation = source_book(engine)
    loaded = Counter()
    original = engine.store.fact_data_many

    def counted(connection, identifiers):
        identifiers = tuple(identifiers)
        loaded.update(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "fact_data_many", counted)

    def inspect():
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return verify_sources(
                engine, connection, calculation_ids=(calculation,),
                fact_ids=(own, extra, own), _return_facts=True,
            )

    first, facts = measure_work(engine, inspect)
    assert set(facts) == {own, extra}
    assert facts[own]["data"]["amount"] == 100
    assert facts[extra]["data"]["amount"] == 12
    assert loaded == Counter({dependency: 1, own: 1, extra: 1})
    originals = [row for row in first["sql"] if "SELECT e.digest,e.content" in row["statement"]]
    assert sum(row["returned_rows"] for row in originals) == 1

    # More unrelated months and revisions may grow B-tree depth, not the
    # selected fact/evidence population delivered to Python.
    for index in range(24):
        for revision in range(2):
            save(
                engine, f"unrelated-{index}", "test_source", 100 + revision,
                request=f"unrelated-{index}-{revision}", revision=revision,
                period=f"{2027 + index // 12}-{index % 12 + 1:02}",
            )
    loaded.clear()
    grown, same = measure_work(engine, inspect)
    assert same == facts
    assert loaded == Counter({dependency: 1, own: 1, extra: 1})
    assert grown["counters"]["returned_rows"] == first["counters"]["returned_rows"]
    assert grown["counters"]["returned_value_bytes"] == first["counters"]["returned_value_bytes"]
    assert grown["counters"]["sqlite_vm_steps"] <= first["counters"]["sqlite_vm_steps"] + 200


@pytest.mark.parametrize("target", ["dependency", "own", "extra", "original", "publication"])
def test_union_rechecks_all_sources_on_a_new_call_and_rejects_damage(engine, target):
    dependency, own, extra, calculation = source_book(engine)

    def inspect():
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return verify_sources(
                engine, connection, calculation_ids=(calculation,), fact_ids=(own, extra)
            )

    assert inspect() == {"status": "verified", "calculations": 1, "vouchers": 1, "facts": 2}
    if target in {"dependency", "extra"}:
        damage(
            engine, "fact_test_source",
            "UPDATE fact_test_source SET amount=amount+1 WHERE revision_id=?",
            (dependency if target == "dependency" else extra,),
        )
    elif target == "own":
        damage(engine, "fact_test_charge", "UPDATE fact_test_charge SET amount=amount+1")
    elif target == "original":
        damage(engine, "evidence", "UPDATE evidence SET content=x'00'")
    else:
        damage(
            engine, "calculation_publication", "UPDATE calculation_publication SET voucher_id=NULL"
        )
    with pytest.raises(KernelError) as failure:
        inspect()
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("calculations", [(), None])
def test_union_still_rejects_an_explicit_missing_fact(engine, calculations):
    source_book(engine)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            verify_sources(engine, connection, calculation_ids=calculations, fact_ids=("missing",))
    assert failure.value.details["reason"] == "referenced_fact_missing"


def test_unexpected_decode_failure_is_a_source_error_not_missing_business_info(engine, monkeypatch):
    _, own, _, calculation = source_book(engine)

    def broken_decode(*args):
        raise ValueError("private damaged source value")

    monkeypatch.setattr(engine.store, "fact_data_many", broken_decode)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            verify_sources(engine, connection, calculation_ids=(calculation,), fact_ids=(own,))
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["component"] == "source"
    assert "private damaged" not in str(failure.value)
    assert "fact_issues" not in failure.value.details
