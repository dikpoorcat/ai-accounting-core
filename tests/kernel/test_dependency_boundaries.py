from __future__ import annotations

from typing import ClassVar

import pytest
from schema_fixture import test_bundle

from ai_accounting.kernel.contracts import (
    Claim,
    Context,
    Fact,
    FactVersion,
    KernelError,
    Outcome,
    Read,
    Registry,
)
from ai_accounting.kernel.dependencies import (
    calculation_matches,
    checked_lanes,
    fact_matches,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


class ClaimSource(Fact):
    kind: ClassVar[str] = "dependency_claim_source"
    amount: int
    bucket: str

    def scopes(self):
        return ("custom:" + self.bucket,)

    def claims(self):
        return (Claim("capacity:" + self.bucket, self.amount),)


class PendingReader(Fact):
    kind: ClassVar[str] = "dependency_reader"
    source: str
    source_kind: str
    source_key: str
    before_period: YearMonth | None = None

    def reads(self):
        return (Read(self.source, self.source_kind, self.source_key, self.before_period),)


class MaterialNote(Fact):
    kind: ClassVar[str] = "dependency_material"
    lane: ClassVar[str] = "material"
    amount: int


class ManagementNote(Fact):
    kind: ClassVar[str] = "dependency_management"
    lane: ClassVar[str] = "management"
    amount: int


class MaterialCalculation(Fact):
    kind: ClassVar[str] = "dependency_material_calculation"
    lane: ClassVar[str] = "material"
    amount: int


def calculate_source(version, _context):
    return Outcome((), {"amount": version.fact.amount})


def calculate_reader(version, context):
    selected = context.select(version.fact.reads()[0])
    return Outcome((), {"selected": [item.id for item in selected]})


@pytest.fixture
def dependency_engine(tmp_path):
    registry = Registry()
    registry.register(ClaimSource, calculate_source)
    registry.register(PendingReader, calculate_reader)
    registry.register(MaterialNote)
    registry.register(ManagementNote)
    registry.register(MaterialCalculation, calculate_source)
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            test_bundle(registry),
            "dependency-company",
            "911100000000000001",
            "dependency-db",
        )
    )
    proof = engine.register_evidence(
        b"synthetic dependency evidence",
        "text/plain",
        "proof",
        request_id="proof",
    )["digest"]
    return engine, proof


def save(engine, proof, kind, subject, data, *, revision=0, request=None, amend=False):
    arguments = dict(
        evidence=(proof,),
        expected_revision=revision,
        request_id=request or "save-" + subject,
    )
    if amend:
        return engine.amend_fact(
            kind,
            subject,
            data,
            recording_error_confirmed=True,
            **arguments,
        )
    return engine.save_fact(kind, subject, data, **arguments)


def publish(engine, *subjects):
    preview = engine.preview(list(subjects))
    return engine.confirm(
        list(subjects),
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish-" + "-".join(subjects),
    )


def source_data(amount=100, bucket="one"):
    return {"period": "2026-01", "amount": amount, "bucket": bucket}


def test_claim_scopes_select_facts_but_never_calculation_outputs(dependency_engine):
    engine, proof = dependency_engine
    saved = save(engine, proof, ClaimSource.kind, "source", source_data())
    published = publish(engine, "source")
    with engine.store.connection(read_only=True) as connection:
        fact = engine.store.fact(connection, saved["fact_id"])
        calculation = engine.store.select(
            connection, Read("calculation", ClaimSource.kind, "@source")
        )[0]
        fact_claim = Read("fact", ClaimSource.kind, "capacity:one")
        calculation_claim = Read("calculation", ClaimSource.kind, "capacity:one")
        assert engine.store.select(connection, fact_claim) == (fact,)
        assert engine.store.select(connection, calculation_claim) == ()
    assert fact_matches(fact_claim, fact)
    assert not calculation_matches(calculation_claim, calculation, fact.fact)
    assert calculation.id == published["results"][0]["calculation_id"]


def test_batch_selection_cost_and_results_follow_matches_not_unrelated_history(
    dependency_engine,
):
    engine, proof = dependency_engine
    saved = engine.save_facts(
        [
            {
                "kind": ClaimSource.kind,
                "subject_id": f"source-{index}",
                "data": source_data(index + 1, str(index)),
                "evidence": (proof,),
                "expected_revision": 0,
            }
            for index in range(20)
        ],
        request_id="save-source-batch",
    )["results"]
    old_id = saved[0]["fact_id"]
    replacement = save(
        engine,
        proof,
        ClaimSource.kind,
        "source-0",
        source_data(100, "0"),
        revision=1,
        request="replace-source-0",
        amend=True,
    )["fact_id"]
    reads = (
        Read("fact", "*", "#" + old_id),
        Read("fact", ClaimSource.kind, "@source-0"),
        Read("fact", ClaimSource.kind, "capacity:0"),
        Read("fact", "*", "2026-01"),
        Read("fact", ClaimSource.kind, "*"),
    )
    with engine.store.connection(read_only=True) as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        selected = engine.store.select_many(connection, reads)
        connection.set_trace_callback(None)
    current = {replacement, *(item["fact_id"] for item in saved[1:])}
    assert {item.id for item in selected[reads[0]]} == {old_id}
    assert {item.id for item in selected[reads[1]]} == {replacement}
    assert {item.id for item in selected[reads[2]]} == {replacement}
    assert {item.id for item in selected[reads[3]]} == current
    assert {item.id for item in selected[reads[4]]} == current
    # One bounded query excludes explicitly superseded current subjects.
    assert len(statements) == 5
    assert sum("identity_correction_item" in sql for sql in statements) == 1


def test_context_copies_inputs_and_traces_empty_reads_without_exposing_selections():
    read = Read("fact", MaterialNote.kind, "missing")
    supplied = {read: ()}
    context = Context(supplied)
    supplied[read] = ("later",)
    assert context.select(read) == ()
    assert not hasattr(context, "selections")
    assert context.used == frozenset({read})
    assert context.versions == frozenset()
    assert context.trace().selections == ((read, ()),)


def test_empty_reads_check_their_kind_lane_and_cross_kind_checks_all_lanes():
    registry = Registry()
    registry.register(ClaimSource)
    registry.register(MaterialNote)
    registry.register(ManagementNote)
    owner = FactVersion(
        "owner-version",
        "owner",
        1,
        ClaimSource.model_validate(source_data()),
    )
    material_read = Read("fact", MaterialNote.kind, "missing")
    material_context = Context({material_read: ()})
    material_context.select(material_read)
    assert checked_lanes(registry, ((owner, material_context.trace()),)) == (
        "accounting",
        "material",
    )
    cross_read = Read("fact", "*", "missing")
    cross_context = Context({cross_read: ()})
    cross_context.select(cross_read)
    assert checked_lanes(registry, ((owner, cross_context.trace()),)) == (
        "accounting",
        "management",
        "material",
    )


@pytest.mark.parametrize(
    ("source", "key", "before_period", "blocked"),
    [
        ("fact", "2026-01", None, True),
        ("fact", "*", None, True),
        ("fact", "custom:one", None, True),
        ("fact", "capacity:one", None, True),
        ("fact", "custom:one", "2026-02", True),
        ("fact", "custom:one", "2026-01", False),
        ("calculation", "capacity:one", None, False),
    ],
)
def test_delete_matches_every_pending_read_form(
    dependency_engine, source, key, before_period, blocked
):
    engine, proof = dependency_engine
    save(engine, proof, ClaimSource.kind, "source", source_data())
    publish(engine, "source")
    save(
        engine,
        proof,
        PendingReader.kind,
        "reader",
        {
            "period": "2026-01",
            "source": source,
            "source_kind": ClaimSource.kind,
            "source_key": key,
            "before_period": before_period,
        },
    )
    if blocked:
        with pytest.raises(KernelError) as failure:
            engine.preview_delete("source")
        assert failure.value.code == "has_dependents"
        assert failure.value.details["subjects"] == ["reader"]
    else:
        assert engine.preview_delete("source")["status"] == "preview"


@pytest.mark.parametrize(
    ("kind", "subject", "lane"),
    [
        (ClaimSource.kind, "accounting-source", "accounting"),
        (MaterialNote.kind, "material-source", "material"),
        (ManagementNote.kind, "management-source", "management"),
    ],
)
def test_delete_returns_complete_epochs_and_advances_the_owning_lane(
    dependency_engine, kind, subject, lane
):
    engine, proof = dependency_engine
    data = source_data() if kind == ClaimSource.kind else {"period": "2026-01", "amount": 1}
    save(engine, proof, kind, subject, data)
    with engine.store.connection(read_only=True) as connection:
        before = engine.store.epochs(connection)
    preview = engine.preview_delete(subject)
    assert preview["epochs"] == before
    assert preview["checked_lanes"] == ["accounting", "management", "material"]
    result = engine.delete(
        subject,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="delete-" + subject,
    )
    assert result["status"] == "withdrawn"
    with engine.store.connection(read_only=True) as connection:
        after = engine.store.epochs(connection)
        cause = connection.execute(
            "SELECT cause_id FROM disposition WHERE subject_id=?", (subject,)
        ).fetchone()[0]
    assert cause == preview["fact_id"]
    assert after == {name: value + int(name == lane) for name, value in before.items()}


def test_deleting_a_material_calculation_advances_material_and_accounting(dependency_engine):
    engine, proof = dependency_engine
    save(
        engine,
        proof,
        MaterialCalculation.kind,
        "material-calculation",
        {"period": "2026-01", "amount": 1},
    )
    publish(engine, "material-calculation")
    preview = engine.preview_delete("material-calculation")
    before = preview["epochs"]
    engine.delete(
        "material-calculation",
        preview_digest=preview["digest"],
        epochs=before,
        request_id="delete-material-calculation",
    )
    with engine.store.connection(read_only=True) as connection:
        after = engine.store.epochs(connection)
    assert after == {
        **before,
        "accounting": before["accounting"] + 1,
        "material": before["material"] + 1,
    }


def test_material_change_between_delete_preview_and_lock_expires_the_plan(
    dependency_engine, monkeypatch
):
    engine, proof = dependency_engine
    save(
        engine,
        proof,
        MaterialNote.kind,
        "material-source",
        {"period": "2026-01", "amount": 1},
    )
    preview = engine.preview_delete("material-source")
    original_write = engine._write
    injected = False

    def raced_write(key, request_hash, expected, lanes, action, operation, *, checked_lanes=None):
        nonlocal injected
        if action == "withdraw":
            assert not injected
            injected = True
            save(
                engine,
                proof,
                MaterialNote.kind,
                "material-source",
                {"period": "2026-01", "amount": 2},
                revision=1,
                request="late-material-change",
                amend=True,
            )
        return original_write(
            key,
            request_hash,
            expected,
            lanes,
            action,
            operation,
            checked_lanes=checked_lanes,
        )

    monkeypatch.setattr(engine, "_write", raced_write)
    with pytest.raises(KernelError) as failure:
        engine.delete(
            "material-source",
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="stale-delete",
        )
    assert failure.value.code == "preview_expired"
    with engine.store.connection(read_only=True) as connection:
        current = engine.store.current_fact(connection, "material-source")
        assert (current.revision, current.fact.amount) == (2, 2)


def test_amendment_invalidates_consumers_from_the_persisted_old_scope(
    dependency_engine, monkeypatch
):
    engine, proof = dependency_engine
    save(engine, proof, ClaimSource.kind, "source", source_data())
    save(
        engine,
        proof,
        PendingReader.kind,
        "reader",
        {
            "period": "2026-01",
            "source": "fact",
            "source_kind": ClaimSource.kind,
            "source_key": "custom:one",
        },
    )
    publish(engine, "reader")

    def changed_scope(self):
        return ("replacement:" + self.bucket,)

    monkeypatch.setattr(ClaimSource, "scopes", changed_scope)
    result = save(
        engine,
        proof,
        ClaimSource.kind,
        "source",
        source_data(125),
        revision=1,
        request="amend-source",
        amend=True,
    )
    assert result["pending"] == ["reader", "source"]
