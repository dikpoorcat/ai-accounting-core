"""Frozen results adapt only when their accounting meaning is compared."""

import json
from dataclasses import replace

import pytest
from test_engine import engine as engine_fixture
from test_engine import publish, save

from ai_accounting.kernel import engine as engine_module
from ai_accounting.kernel.accounting import AccountingBook, compatibility
from ai_accounting.kernel.contracts import KernelError, Read, freeze
from ai_accounting.kernel.types import digest

engine = engine_fixture


def _database_dump(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


def test_unsigned_history_uses_frozen_inputs_without_evaluating_or_rewriting(engine, monkeypatch):
    save(engine)
    old_preview, old_result = publish(engine)
    old_id = old_result["results"][0]["calculation_id"]
    with monkeypatch.context() as deployment:
        deployment.setattr(engine_module, "PROGRAM_VERSION", "next-controlled-build")
        next_build = engine.preview(["charge"])["results"][0]
        assert next_build["calculation_id"] != old_id
        assert next_build["result_digest"] == old_preview["results"][0]["result_digest"]
        assert next_build["accounting"] == old_preview["results"][0]["accounting"]
        assert next_build["impact"] == "review_no_impact"
    save(engine, amount=200, revision=1, request="changed-fact")
    _, current_result = publish(engine, request="changed-calculation")
    current_id = current_result["results"][0]["calculation_id"]
    before = _database_dump(engine)

    def evaluator_must_not_run(*args):
        pytest.fail("historical comparison must not evaluate current business rules")

    monkeypatch.setitem(engine.store.registry.evaluators, "test_charge", evaluator_must_not_run)
    book = AccountingBook(engine.store.registry)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM calculation WHERE id=?", (old_id,)).fetchone()
        original_json, original_digest = row["outcome"], row["digest"]
        assert "accounting" not in json.loads(original_json)
        book.load(engine.store, connection, [old_id, current_id])
        historical = engine.store.select(
            connection, Read("calculation", "test_charge", "#" + old_id)
        )
        assert historical[0].id == old_id
        assert historical[0].values["amount"] == 100
        assert historical[0].result_digest == original_digest.hex()
    assert book.signature(old_id) == old_preview["results"][0]["accounting"]
    assert book.signature(old_id) != book.signature(current_id)
    assert book.records[old_id].version.fact.amount == 100
    with pytest.raises(KernelError) as failure:
        book.signature(old_id, contract="accounting-future")
    assert failure.value.code == "accounting_compatibility_required"
    assert failure.value.details["reason"] == "unsupported_comparison_contract"
    trace = engine.trace(old_id)["calculation"]
    assert trace["outcome"] == json.loads(original_json)
    assert digest(trace["outcome"]) == original_digest
    assert trace["digest"] == original_digest.hex()
    assert _database_dump(engine) == before


@pytest.mark.parametrize(
    "reason", ["required_outcome_structure_missing", "unsupported_comparison_contract"]
)
def test_comparison_failure_is_atomic_isolated_and_recoverable(engine, monkeypatch, reason):
    save(engine)
    _, original = publish(engine)
    old_id = original["results"][0]["calculation_id"]
    save(engine, revision=1, request="review-fact")
    save(engine, subject="a-new", request="new-fact")
    before = _database_dump(engine)
    original_load = AccountingBook.load

    def unsupported_adapter(book, store, connection, calculation_ids):
        original_load(book, store, connection, calculation_ids)
        if old_id not in book.records:
            return
        if reason == "required_outcome_structure_missing":
            record = book.records[old_id]
            outcome = dict(record.outcome)
            del outcome["balances"]
            book.records[old_id] = replace(record, outcome=freeze(outcome))
        else:
            book.errors[old_id] = compatibility(old_id, reason)

    monkeypatch.setattr(AccountingBook, "load", unsupported_adapter)
    subjects = ["a-new", "charge"]
    preview = engine.preview(subjects)
    results = {item["subject_id"]: item for item in preview["results"]}
    assert results["a-new"]["impact"] == "initial"
    assert results["charge"]["impact"] == "compatibility_required"
    assert results["charge"]["compatibility_issue"]["reason"] == reason
    with pytest.raises(KernelError) as failure:
        engine.confirm(
            subjects, preview_digest=preview["digest"], epochs=preview["epochs"],
            request_id="blocked-review",
        )
    assert failure.value.code == "accounting_compatibility_required"
    assert failure.value.details["reason"] == reason
    assert failure.value.details["calculation_id"] == old_id
    assert _database_dump(engine) == before

    def signature_must_not_run(*args, **kwargs):
        pytest.fail("ordinary exact reads and trace must not require accounting adaptation")

    with monkeypatch.context() as ordinary_reads:
        ordinary_reads.setattr(AccountingBook, "signature", signature_must_not_run)
        with engine.store.connection(read_only=True) as connection:
            historical = engine.store.select(
                connection, Read("calculation", "test_charge", "#" + old_id)
            )
            assert historical[0].id == old_id
        assert engine.trace(old_id)["calculation"]["outcome"]["values"]["amount"] == 100

    # The failed historical adapter is still installed, but an independent request works.
    _, separate = publish(engine, ["a-new"], request="independent-publish")
    assert separate["results"][0]["impact"] == "initial"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='charge'"
        ).fetchone()[0] == old_id

    # A controlled adapter repair only requires re-previewing; no historical write is needed.
    monkeypatch.setattr(AccountingBook, "load", original_load)
    recovered_preview, recovered = publish(engine, subjects, request="blocked-review")
    assert all(item["impact"] == "review_no_impact" for item in recovered_preview["results"])
    assert all(item["impact"] == "review_no_impact" for item in recovered["results"])
    assert engine.overview("2026-01")["pending"] == []
    assert engine.trace(old_id)["calculation"]["outcome"]["values"]["amount"] == 100


@pytest.mark.parametrize("stage", ["calculation", "commit"])
def test_failed_no_impact_review_keeps_old_publication_and_can_retry(engine, stage):
    save(engine)
    publish(engine)
    save(engine, revision=1, request="new-proof")
    preview = engine.preview(["charge"])
    assert preview["results"][0]["impact"] == "review_no_impact"
    before = _database_dump(engine)

    def fail(current, connection):
        if current == stage:
            raise RuntimeError("review fault")

    engine.fault = fail
    kwargs = dict(
        preview_digest=preview["digest"], epochs=preview["epochs"], request_id="review-retry"
    )
    with pytest.raises(RuntimeError, match="review fault"):
        engine.confirm(["charge"], **kwargs)
    assert _database_dump(engine) == before
    engine.fault = lambda *args: None
    result = engine.confirm(["charge"], **kwargs)
    assert result["results"][0]["impact"] == "review_no_impact"
    published = _database_dump(engine)
    assert engine.confirm(["charge"], **kwargs) == result
    assert _database_dump(engine) == published
