"""New pass-through registrations reuse real names and reject anonymous objects."""

from types import SimpleNamespace

import pytest
from historical_pass_through_fixture import prior_pass_through_registration
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.entity_references import _named_entities
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(Store.create(
        tmp_path / "company.sqlite", production_bundle(), "company",
        "911100000000000001", "database",
    ))
    evidence = engine.register_evidence(
        b"Synthetic beneficiary identity evidence", "text/plain", "fixture",
        request_id="evidence",
    )["digest"]
    entities = Entities(engine)
    payer = entities.register_entity(
        "organization", {}, source="synthetic payer", request_id="payer",
    )["entity_id"]
    beneficiary = entities.register_entity(
        "person", {}, source="synthetic beneficiary", request_id="beneficiary",
    )["entity_id"]
    return engine, evidence, payer, beneficiary


def register(book, *, beneficiary=None, subject="pass-through", request="save"):
    engine, evidence, payer, original = book
    return engine.save_fact(
        "pass_through", subject,
        {"period": "2026-09", "payer_id": payer,
         "beneficiary_id": original if beneficiary is None else beneficiary,
         "amount_fen": 10000, "rights_and_obligation_confirmed": True},
        evidence=(evidence,), expected_revision=0, request_id=request,
    )


@pytest.mark.parametrize("display_name", [None, "   "])
def test_nonempty_id_with_anonymous_profile_cannot_register_or_write(book, display_name):
    engine, _, _, beneficiary = book
    if display_name is not None:
        Entities(engine).update_entity_profile(
            beneficiary, {"display_name": display_name}, source="synthetic blank name",
            expected_revision=1, request_id="blank-name",
        )
    with engine.store.connection(read_only=True) as connection:
        before = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("subject", "fact_revision", "request", "audit")}
    with pytest.raises(NeedsInformation) as failure:
        register(book)
    issue = failure.value.response()["fact_issues"][0]
    assert issue["field"] == "beneficiary_id"
    assert issue["semantics"] in ("accounting", "management")
    assert issue["reusable_sources"] == [beneficiary, "original_document", "owner_confirmation"]
    with engine.store.connection(read_only=True) as connection:
        after = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                 for table in before}
    assert after == before


@pytest.mark.parametrize("source", ["profile", "payee", "tax_identity"])
def test_named_beneficiary_reuses_existing_sources_without_payer_name(book, source):
    engine, evidence, _, beneficiary = book
    if source == "profile":
        Entities(engine).update_entity_profile(
            beneficiary, {"display_name": "合成最终权利人"}, source="synthetic identity",
            expected_revision=1, request_id="name",
        )
    elif source == "payee":
        Exports(engine).save_payee(
            beneficiary, name="合成最终权利人", account="000123456789",
            evidence_digest=evidence, expected_revision=0, request_id="name",
        )
    else:
        engine.save_fact(
            "tax_import_identity_v2", "identity",
            {"period": "2026-09", "employee_id": beneficiary, "employee_code": "synthetic",
             "name": "合成最终权利人", "document_type": "居民身份证",
             "document_number": "synthetic-document"},
            evidence=(evidence,), expected_revision=0, request_id="name",
        )
    result = register(book)
    assert result["status"] == "confirmed"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT beneficiary_id FROM fact_pass_through WHERE revision_id=?",
            (result["fact_id"],),
        ).fetchone()[0] == beneficiary


def test_name_lookup_does_not_authenticate_unrelated_profiles(book):
    engine, _, _, beneficiary = book
    entities = Entities(engine)
    entities.update_entity_profile(
        beneficiary, {"display_name": "合成权利人"}, source="synthetic identity",
        expected_revision=1, request_id="name",
    )
    unrelated = entities.register_entity(
        "person", {"display_name": "无关对象"}, source="synthetic unrelated",
        request_id="unrelated",
    )["entity_id"]
    # Deliberately corrupt only a synthetic unrelated profile to expose broad reads.
    damage(
        engine, "entity_profile_revision",
        "UPDATE entity_profile_revision SET digest=zeroblob(32) WHERE entity_id=?",
        (unrelated,),
    )
    assert register(book)["status"] == "confirmed"


def test_prior_anonymous_id_cannot_be_newly_calculated(book):
    engine, _, _, _ = book
    with prior_pass_through_registration():
        register(book)
    with pytest.raises(NeedsInformation) as failure:
        engine.preview(["pass-through"])
    assert failure.value.response()["fact_issues"][0]["field"] == "beneficiary_id"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == 0


def test_reused_tax_name_must_pass_saved_source_integrity(book):
    engine, evidence, _, beneficiary = book
    result = engine.save_fact(
        "tax_import_identity_v2", "identity",
        {"period": "2026-09", "employee_id": beneficiary, "employee_code": "synthetic",
         "name": "合成最终权利人", "document_type": "居民身份证",
         "document_number": "synthetic-document"},
        evidence=(evidence,), expected_revision=0, request_id="name",
    )
    damage(
        engine, "fact_tax_import_identity_v2",
        "UPDATE fact_tax_import_identity_v2 SET name='damaged' WHERE revision_id=?",
        (result["fact_id"],),
    )
    with pytest.raises(KernelError) as failure:
        register(book)
    assert failure.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM subject WHERE id='pass-through'"
        ).fetchone() is None


def test_conflicting_current_tax_names_require_source_review(book):
    engine, evidence, _, beneficiary = book
    identities = []
    for index, name in enumerate(("合成权利人甲", "合成权利人乙")):
        result = engine.save_fact(
            "tax_import_identity_v2", f"identity-{index}",
            {"period": "2026-09", "employee_id": beneficiary, "employee_code": "synthetic",
             "name": name, "document_type": "居民身份证", "document_number": "synthetic-document"},
            evidence=(evidence,), expected_revision=0, request_id=f"name-{index}",
        )
        identities.append(result["fact_id"])
    with pytest.raises(NeedsInformation) as failure:
        register(book)
    issue = failure.value.response()["fact_issues"][0]
    assert issue["field"] == "beneficiary_id"
    assert "冲突" in issue["message"]
    assert set(identities) <= set(issue["reusable_sources"])
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM subject WHERE id='pass-through'"
        ).fetchone() is None


def test_registry_without_tax_identity_never_reads_tax_tables(book):
    engine, _, _, beneficiary = book
    statements = []
    store = SimpleNamespace(registry=SimpleNamespace(models={}))
    with engine.store.connection(read_only=True) as connection:
        connection.set_trace_callback(statements.append)
        assert _named_entities(connection, {beneficiary}, store=store) == (set(), {})
    assert not any("tax_import_identity" in statement for statement in statements)
