"""Exact historical references remain frozen inside a batch with newer results."""

from typing import ClassVar

from test_engine import Charge, Source, calculate, evidence, publish, save

from ai_accounting.kernel.contracts import Fact, Outcome, Read, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


class Comparison(Fact):
    kind: ClassVar[str] = "historical_comparison"
    accepted_id: str

    def reads(self):
        return (
            Read("calculation", "test_charge", "@charge"),
            Read("calculation", "test_charge", "#" + self.accepted_id),
        )


def compare(version, context):
    current = context.calculations("test_charge", "@charge")[0]
    accepted = context.calculations("test_charge", "#" + version.fact.accepted_id)[0]
    return Outcome(
        (),
        {
            "accepted": accepted.values["amount"],
            "current": current.values["amount"],
            "unchanged": accepted.result_digest == current.result_digest,
        },
    )


def test_exact_versions_are_not_replaced_by_current_batch_overlay(tmp_path):
    registry = Registry()
    registry.register(Source)
    registry.register(Charge, calculate)
    registry.register(Comparison, compare)
    engine = Engine(Store.create(tmp_path / "history.sqlite", registry, "a", "tax-a", "db-a"))
    original_fact = save(engine)
    _, published = publish(engine)
    original_id = published["results"][0]["calculation_id"]
    engine.save_fact(
        Comparison.kind,
        "comparison",
        {"period": "2026-01", "accepted_id": original_id},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="comparison",
    )
    publish(engine, ["comparison"], request="accept")
    save(engine, amount=150, revision=1, request="change")
    preview, _ = publish(engine, request="change-published")
    compared = next(item for item in preview["results"] if item["subject_id"] == "comparison")
    assert compared["values"] == {"accepted": 100, "current": 150, "unchanged": False}
    with engine.store.connection(read_only=True) as connection:
        historical = engine.store.select(connection, Read("calculation", "*", "#" + original_id))
        assert historical[0].values["amount"] == 100
        assert len(historical[0].result_digest) == 64
        assert engine.store.select(
            connection, Read("calculation", "wrong", "#" + original_id)
        ) == ()
        assert engine.store.select(
            connection, Read("calculation", "*", "#" + original_id, YearMonth("2026-01"))
        ) == ()
        assert engine.store.select(
            connection, Read("fact", "test_charge", "#" + original_fact["fact_id"])
        )[0].fact.amount == 100
