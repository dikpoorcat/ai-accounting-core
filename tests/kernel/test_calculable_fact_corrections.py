"""Fact-range readers follow a changed fact even when that fact also posts a voucher."""

import pytest
from test_engine import Charge, Source, calculate, evidence, publish, save
from test_version_reads import Comparison, compare

from ai_accounting.kernel.contracts import Line, Outcome, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store


def source_calculation(version, context):
    amount = version.fact.amount
    return Outcome((Line("5602", debit=amount), Line("2202", credit=amount)), {"amount": amount})


@pytest.mark.parametrize("initial_source", [False, True], ids=["empty-range", "revised-fact"])
def test_one_source_republishes_fact_consumers_and_descendants_atomically(tmp_path, initial_source):
    registry = Registry()
    registry.register(Source, source_calculation)
    registry.register(Charge, calculate)
    registry.register(Comparison, compare)
    engine = Engine(Store.create(tmp_path / "company.sqlite", registry, "a", "tax", "db"))
    save(engine)
    if initial_source:
        save(engine, "source", Source.kind, 25, request="first-source")
    _, first = publish(engine, ["charge", "source"] if initial_source else ["charge"])
    charge = next(item for item in first["results"] if item["subject_id"] == "charge")
    engine.save_fact(
        Comparison.kind,
        "comparison",
        {"period": "2026-01", "accepted_id": charge["calculation_id"]},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="compare",
    )
    publish(engine, ["comparison"], request="publish-compare")
    save(engine, "unrelated", period="2026-02", request="unrelated")
    _, unrelated = publish(engine, ["unrelated"], request="publish-unrelated")
    before = engine.ledger("2026-01")
    save(
        engine,
        "source",
        Source.kind,
        40,
        revision=int(initial_source),
        request="change-source",
    )

    preview = engine.preview(["source"])
    assert set(preview["subjects"]) == {"source", "charge", "comparison"}
    observed = {item["subject_id"]: item for item in preview["results"]}
    assert observed["charge"]["values"]["amount"] == 140
    assert observed["comparison"]["values"] == {
        "accepted": 125 if initial_source else 100,
        "current": 140,
        "unchanged": False,
    }

    def fail(stage, connection):
        if stage == "commit":
            raise RuntimeError("commit failed")

    engine.fault = fail
    arguments = dict(
        preview_digest=preview["digest"], epochs=preview["epochs"], request_id="replace"
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        engine.confirm(["source"], **arguments)
    assert engine.ledger("2026-01") == before
    assert engine.overview("2026-01")["pending"]

    engine.fault = lambda stage, connection: None
    result = engine.confirm(["source"], **arguments)
    assert engine.confirm(["source"], **arguments) == result
    corrected = next(item for item in result["results"] if item["subject_id"] == "charge")
    assert corrected["voucher_number"] == charge["voucher_number"]
    assert engine.overview("2026-01")["pending"] == []
    with engine.store.connection(read_only=True) as connection:
        current = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='unrelated'"
        ).fetchone()[0]
    assert current == unrelated["results"][0]["calculation_id"]
