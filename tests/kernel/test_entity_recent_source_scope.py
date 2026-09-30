"""Recent entity use authenticates the exact current sources it displays."""

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def book(tmp_path):
    engine = Engine(Store.create(
        tmp_path / "entity-recent.sqlite", production_bundle(), "company",
        "911100000000000001", "database",
    ))
    return engine, Entities(engine)


def person(directory, name, request, *, active=True):
    return directory.register_entity(
        "person", {"display_name": name, "active": active},
        source="synthetic identity", request_id=request,
    )["entity_id"]


def expense(engine, entity_id, subject, period):
    proof = engine.register_evidence(
        f"synthetic {subject}".encode(), "text/plain", "invoice", request_id=f"proof-{subject}"
    )["digest"]
    return engine.save_fact(
        "expense", subject,
        {"period": period, "counterparty_id": entity_id, "amount_fen": 100,
         "expense_class": "administration", "creditor_kind": "employee"},
        evidence=(proof,), expected_revision=0, request_id=f"save-{subject}",
    )


def mutate_reference(engine, fact_id, field, value):
    with engine.store.connection() as connection:
        original = connection.execute(
            f"SELECT {field} FROM entity_reference_current "
            "WHERE fact_id=? AND path='counterparty_id'",
            (fact_id,),
        ).fetchone()[0]
        connection.execute(
            f"UPDATE entity_reference_current SET {field}=? "
            "WHERE fact_id=? AND path='counterparty_id'",
            (value, fact_id),
        )
    return original


@pytest.mark.parametrize("field,bad", [
    ("period", YearMonth("2026-12").ordinal),
    ("source_digest", b"\0" * 32),
    ("kind", "payment"),
])
def test_recent_positive_hit_rejects_damaged_winning_source(book, field, bad):
    engine, directory = book
    owner = person(directory, "Alpha", "alpha")
    saved = expense(engine, owner, "january", "2026-01")
    assert directory.find_entities(used_from="2026-12")["items"] == []
    original = mutate_reference(engine, saved["fact_id"], field, bad)
    try:
        with pytest.raises(KernelError) as error:
            directory.find_entities(used_from="2026-01")
        assert error.value.code == "entity_reference_corrupt"
        # A fabricated latest month must be rejected even when used_to would
        # otherwise omit the entity rather than return it.
        if field == "period":
            with pytest.raises(KernelError, match="对象引用目录"):
                directory.find_entities(used_to="2026-01")
    finally:
        mutate_reference(engine, saved["fact_id"], field, original)
    assert directory.find_entities(used_from="2026-01")["items"][0]["recent_period"] == "2026-01"


def test_tied_latest_sources_and_has_more_are_all_authenticated(book):
    engine, directory = book
    first = person(directory, "Alpha", "alpha", active=False)
    second = person(directory, "Alpha Junior", "alpha-junior")
    person(directory, "Unused", "unused")
    directory.register_entity(
        "organization", {"display_name": "Alpha"},
        source="synthetic organization", request_id="alpha-organization",
    )
    first_a = expense(engine, first, "first-a", "2026-02")
    first_b = expense(engine, first, "first-b", "2026-02")
    second_fact = expense(engine, second, "second", "2026-02")
    matching = directory.find_entities(query="Alp", kind="person")["items"]
    assert [item["recent_period"] for item in matching] == [
        "2026-02", "2026-02",
    ]
    assert directory.find_entities(query="Unused")["items"][0]["recent_period"] is None
    assert directory.find_entities(query="Unused", used_from="2026-01")["items"] == []
    assert directory.find_entities(query="Alpha", kind="person", limit=1)["has_more"] is True
    assert len(directory.find_entities(query="Alpha", kind="organization")["items"]) == 1
    for fact_id in (first_a["fact_id"], first_b["fact_id"], second_fact["fact_id"]):
        original = mutate_reference(engine, fact_id, "source_digest", b"\0" * 32)
        try:
            with pytest.raises(KernelError) as error:
                directory.find_entities(query="Alp", kind="person", limit=1)
            assert error.value.code == "entity_reference_corrupt"
        finally:
            mutate_reference(engine, fact_id, "source_digest", original)
    items = directory.find_entities(query="Alpha", kind="person", limit=1)["items"]
    assert len(items) == 1 and items[0]["entity_id"] == first
    assert items[0]["profile"]["active"] is False


def test_unrelated_and_older_sources_are_outside_the_recent_proof(book):
    engine, directory = book
    selected = person(directory, "Selected", "selected")
    unrelated = person(directory, "Unrelated", "unrelated")
    old = expense(engine, selected, "old", "2026-01")
    expense(engine, selected, "latest", "2026-02")
    other = expense(engine, unrelated, "other", "2026-02")
    old_digest = mutate_reference(engine, old["fact_id"], "source_digest", b"\0" * 32)
    other_digest = mutate_reference(engine, other["fact_id"], "source_digest", b"\0" * 32)
    try:
        items = directory.find_entities(query="Selected")["items"]
        assert len(items) == 1 and items[0]["recent_period"] == "2026-02"
        with pytest.raises(KernelError) as error:
            directory.find_entities()
        assert error.value.code == "entity_reference_corrupt"
    finally:
        mutate_reference(engine, old["fact_id"], "source_digest", old_digest)
        mutate_reference(engine, other["fact_id"], "source_digest", other_digest)


def test_amended_current_source_replaces_old_revision_without_loading_old_index(book):
    engine, directory = book
    owner = person(directory, "Amended", "amended")
    old = expense(engine, owner, "expense", "2026-01")
    new_proof = engine.register_evidence(
        b"synthetic corrected invoice", "text/plain", "invoice", request_id="amend-proof"
    )["digest"]
    engine.amend_fact(
        "expense", "expense",
        {"period": "2026-02", "counterparty_id": owner, "amount_fen": 100,
         "expense_class": "administration", "creditor_kind": "employee"},
        evidence=(new_proof,), expected_revision=1,
        recording_error_confirmed=True, request_id="amend-expense",
    )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM fact_current WHERE fact_id=?", (old["fact_id"],)
        ).fetchone()[0] == 0
    assert directory.find_entities(query="Amended")["items"][0]["recent_period"] == "2026-02"


def test_explicit_identity_correction_keeps_current_recency_at_new_owner(book):
    from ai_accounting.kernel.identity_corrections import IdentityCorrections

    engine, directory = book
    first = person(directory, "Original", "original")
    second = person(directory, "Corrected", "corrected")
    proof = engine.register_evidence(
        b"synthetic identity correction", "text/plain", "confirmation",
        request_id="correction-proof",
    )["digest"]
    original = {
        "period": "2026-01", "counterparty_id": first, "amount_fen": 100,
        "expense_class": "administration", "creditor_kind": "employee",
    }
    engine.save_fact(
        "expense", "corrected-expense", original, evidence=(proof,),
        expected_revision=0, request_id="save-corrected-expense",
    )
    publication = engine.preview(["corrected-expense"])
    engine.confirm(
        ["corrected-expense"], preview_digest=publication["digest"],
        epochs=publication["epochs"], request_id="publish-corrected-expense",
    )
    kwargs = {
        "changes": [
            {"subject_id": "corrected-expense", "expected_revision": 1,
             "action": "reassign", "data": {**original, "counterparty_id": second}},
        ],
        "evidence": [proof], "reason": "synthetic confirmed identity correction",
    }
    corrections = IdentityCorrections(engine)
    preview = corrections.preview_identity_correction(**kwargs)
    corrections.confirm_identity_correction(
        **kwargs, preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="confirm-correction",
    )
    assert directory.find_entities(query="Original")["items"][0]["recent_period"] is None
    assert directory.find_entities(query="Corrected")["items"][0]["recent_period"] == "2026-01"


def test_missing_reference_remains_a_full_integrity_boundary(book):
    from ai_accounting.kernel.maintenance import Maintenance

    engine, directory = book
    owner = person(directory, "Missing", "missing")
    saved = expense(engine, owner, "expense", "2026-01")
    with engine.store.connection() as connection:
        connection.execute(
            "DELETE FROM entity_reference_current WHERE fact_id=?", (saved["fact_id"],)
        )
    assert directory.find_entities(query="Missing")["items"][0]["recent_period"] is None
    assert directory.find_entities(query="Missing", used_from="2026-01")["items"] == []
    with pytest.raises(KernelError) as error:
        Maintenance(engine).verify_integrity()
    assert error.value.code == "entity_reference_corrupt"
