"""Compiled facts require their source-checking command at every public save boundary."""

import json
from typing import ClassVar

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.command_schema import command_models
from ai_accounting.kernel.contracts import Fact, KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen, canonical


class OrdinaryFact(Fact):
    kind: ClassVar[str] = "test_ordinary"
    amount_fen: PositiveFen


class CompiledFact(OrdinaryFact):
    kind: ClassVar[str] = "test_compiled"
    registration_command: ClassVar[str] = "compile_test_fact"
    lane: ClassVar[str] = "material"


@pytest.fixture
def book(tmp_path):
    registry = Registry()
    registry.register(OrdinaryFact)
    registry.register(CompiledFact)
    engine = Engine(
        Store.create(tmp_path / "company.sqlite", registry, "company", "taxpayer", "database")
    )
    evidence = engine.register_evidence(
        b"synthetic compiler source", "text/plain", "source", request_id="evidence"
    )["digest"]
    return engine, evidence


def record(evidence, kind=CompiledFact.kind, subject="compiled", amount=100, revision=0):
    return {
        "kind": kind,
        "subject_id": subject,
        "data": {"period": "2026-01", "amount_fen": amount},
        "evidence": [evidence],
        "expected_revision": revision,
    }


def write_compiled(engine, evidence, *, request_id="compile"):
    # A dedicated command may persist its checked output through the common writer;
    # no particular domain compiler or Materials implementation is required here.
    request_hash, operation = engine._registration(False, **record(evidence))
    return engine._write(
        request_id,
        request_hash,
        None,
        (CompiledFact.lane,),
        CompiledFact.registration_command,
        operation,
    )


def persisted_state(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            "epochs": engine.store.epochs(connection),
            **{
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "subject",
                    "fact_revision",
                    "fact_current",
                    "pending",
                    "audit",
                    "request",
                )
            },
        }


@pytest.mark.parametrize("command", ["save_fact", "amend_fact"])
def test_direct_single_save_and_recording_amendment_reject_compiled_facts(book, command):
    engine, evidence = book
    if command == "amend_fact":
        original = write_compiled(engine, evidence)
        arguments = record(evidence, amount=200, revision=1)
        arguments["recording_error_confirmed"] = True
    else:
        arguments = record(evidence)
    before = persisted_state(engine)
    with pytest.raises(KernelError) as failure:
        getattr(engine, command)(**arguments, request_id="direct-save")
    assert failure.value.code == "registration_command_required"
    assert failure.value.details["command"] == CompiledFact.registration_command
    assert persisted_state(engine) == before
    if command == "amend_fact":
        with engine.store.connection(read_only=True) as connection:
            current = engine.store.current_fact(connection, "compiled")
            assert current.id == original["fact_id"]
            assert current.fact.amount_fen == 100


@pytest.mark.parametrize("compiled_first", [True, False])
def test_mixed_batch_rejects_compiled_kind_without_saving_other_records(book, compiled_first):
    engine, evidence = book
    ordinary = record(evidence, OrdinaryFact.kind, "ordinary")
    batch = [record(evidence), ordinary] if compiled_first else [ordinary, record(evidence)]
    before = persisted_state(engine)
    with pytest.raises(KernelError) as failure:
        engine.save_facts(batch, request_id="mixed")
    assert failure.value.code == "registration_command_required"
    assert failure.value.details["command"] == CompiledFact.registration_command
    assert persisted_state(engine) == before
    # A rejected batch does not reserve its idempotency key or impede ordinary input.
    accepted = engine.save_facts([ordinary], request_id="mixed")
    assert accepted["results"][0]["subject_id"] == "ordinary"
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "ordinary").fact.amount_fen == 100
        assert connection.execute("SELECT 1 FROM subject WHERE id='compiled'").fetchone() is None


def kind_literals(value):
    if isinstance(value, dict):
        field = value.get("properties", {}).get("kind", {})
        found = {field["const"]} if "const" in field else set()
        return found.union(*(kind_literals(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(kind_literals(item) for item in value))
    return set()


@pytest.mark.parametrize("command", ["save_fact", "amend_fact", "save_facts"])
def test_public_save_schemas_and_validation_exclude_compiled_kind(book, command):
    engine, evidence = book
    adapter = command_models(engine.store.registry)[command]
    assert kind_literals(adapter.json_schema()) == {OrdinaryFact.kind}

    def payload(kind):
        item = record(evidence, kind)
        shared = {"company_id": "company", "request_id": "wire-save"}
        if command == "save_facts":
            return {**shared, "facts": [item]}
        if command == "amend_fact":
            shared["recording_error_confirmed"] = True
        return {**shared, **item}

    ordinary = payload(OrdinaryFact.kind)
    assert adapter.validate_json(canonical(ordinary)).model_dump(mode="json") == ordinary
    with pytest.raises(ValidationError):
        adapter.validate_json(canonical(payload(CompiledFact.kind)))


def test_fact_discovery_preserves_compiled_schema_and_declares_required_command(book):
    engine, _ = book
    schemas = engine.store.registry.schemas()
    assert set(schemas) == {OrdinaryFact.kind, CompiledFact.kind}
    assert schemas[CompiledFact.kind]["x-registration-command"] == CompiledFact.registration_command
    assert "amount_fen" in schemas[CompiledFact.kind]["properties"]
    assert "registration_command" not in schemas[CompiledFact.kind]["properties"]
    assert "x-registration-command" not in schemas[OrdinaryFact.kind]


def test_internal_registration_uses_shared_atomic_audited_idempotent_writer(book):
    engine, evidence = book
    before = persisted_state(engine)
    result = write_compiled(engine, evidence)
    assert result["status"] == "confirmed"
    after = persisted_state(engine)
    assert after["epochs"] == {**before["epochs"], "material": before["epochs"]["material"] + 1}
    assert after["fact_revision"] == before["fact_revision"] + 1
    assert write_compiled(engine, evidence) == result
    assert persisted_state(engine) == after
    with engine.store.connection(read_only=True) as connection:
        current = engine.store.current_fact(connection, "compiled")
        assert isinstance(current.fact, CompiledFact)
        assert current.id == result["fact_id"]
        assert current.evidence == (evidence,)
        action, payload = connection.execute(
            "SELECT action,payload FROM audit WHERE request_id='compile'"
        ).fetchone()
        assert action == CompiledFact.registration_command
        assert json.loads(payload) == result


def test_internal_registration_failure_rolls_back_and_can_retry(book):
    engine, evidence = book
    before = persisted_state(engine)

    def interrupted(stage, connection):
        if stage == "published":
            raise OSError("synthetic compiler write interruption")

    engine.fault = interrupted
    with pytest.raises(OSError, match="synthetic compiler write interruption"):
        write_compiled(engine, evidence)
    assert persisted_state(engine) == before
    engine.fault = lambda stage, connection: None
    assert write_compiled(engine, evidence)["revision"] == 1
