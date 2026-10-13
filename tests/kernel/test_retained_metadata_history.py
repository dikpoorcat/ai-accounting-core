"""Old metadata is preserved; new events still need the complete current journal."""

from contextlib import closing

import pytest
from test_adjust_stage9_samples import original_file

from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.change_journal import verify_journal
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities, WorkerRegistration
from ai_accounting.kernel.entity_profile_history import PreEventProfile
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import digest
from scripts.stage9_history_adjustment import adjust_connection


@pytest.fixture
def original_metadata(tmp_path, request):
    path = tmp_path / "source.sqlite"
    source = original_file(path, "company")
    engine = Engine(Store(path, source, "synthetic-company", "synthetic-database"))
    proof = engine.register_evidence(
        b"explicit synthetic original hire 2016-01-01, regular employment",
        "text/plain", "original source", request_id="original-evidence",
    )["digest"]
    data = PreEventProfile(
        display_name="Synthetic employee", employment_start="2016-01-01",
        employment_status="active", employment_state="regular",
    ).model_dump(mode="json")
    entities = Entities(engine)

    def insert(connection):
        connection.execute("INSERT INTO entity VALUES('employee','person',NULL)")
        return entities._insert_profile(
            connection, "employee", 1, data, "explicit synthetic original metadata",
            bytes.fromhex(proof),
        )

    saved = engine._write(
        "original-profile", digest(data), None, ("management",), "register_entity", insert,
    )
    for index in range(getattr(request, "param", 0)):
        engine.register_evidence(
            f"unrelated original evidence {index}".encode(), "text/plain", "original",
            request_id=f"unrelated-original-{index}",
        )
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with closing(connect(path)) as connection:
        before = dict(connection.execute("SELECT * FROM entity_profile_revision").fetchone())
        adjust_connection(connection, kind="company")
    current = Engine(Store(path, production_bundle(), "synthetic-company", "synthetic-database"))
    return current, saved, before, proof


def test_exact_original_profile_survives_explicit_current_hire(original_metadata):
    engine, saved, before, proof = original_metadata
    result = Entities(engine).register_worker(
        WorkerRegistration(action="hire", effective_date="2016-01-01"),
        entity_id="employee", expected_revision=1,
        source="explicit synthetic format conversion from original hire date",
        evidence_digest=proof, request_id="convert-explicit-hire",
    )
    assert result["revision"] == 2
    with engine.store.connection(read_only=True) as connection:
        original = dict(connection.execute(
            "SELECT * FROM entity_profile_revision WHERE id=?", (before["id"],),
        ).fetchone())
        assert original == before
        assert verify_journal(connection) >= 1
    assert verify_file(engine.store.path)["verification"]["status"] == "verified"
    timeline = Entities(engine).workforce_relationship_history("employee", period="2016-01")
    assert timeline["monthly"]["period_end_state"] == "regular"
    assert len(timeline["items"]) == 1
    assert timeline["items"][0]["effective_date"] == "2016-01-01"


def test_new_evidence_without_event_is_not_excused_by_old_boundary(original_metadata):
    engine, _saved, _before, _proof = original_metadata
    from hashlib import sha256

    with closing(connect(engine.store.path)) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='change_evidence_insert'",
        ).fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TRIGGER change_evidence_insert")
        raw = b"a new record cannot pretend to have existed before the boundary"
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (sha256(raw).digest(), raw, "text/plain", "missing new event"),
        )
        connection.execute(sql)
        connection.commit()
        with pytest.raises(KernelError) as failure:
            verify_journal(connection)
        assert failure.value.details["reason"] == "journal_source_coverage_mismatch"


def test_retained_record_damage_is_rejected(original_metadata):
    engine, _saved, _before, _proof = original_metadata
    from test_integrity_content import damage

    damage(
        engine, "source_change_retained_record",
        "DELETE FROM source_change_retained_record WHERE source='entity_profile'",
    )
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            verify_journal(connection)
        assert failure.value.details["reason"] == "journal_retained_boundary_mismatch"


def test_unbound_new_profile_cannot_use_the_old_grammar(original_metadata):
    engine, _saved, _before, _proof = original_metadata
    with pytest.raises(ValueError):
        Entities(engine).update_entity_profile(
            "employee", {"employment_start": "2015-01-01"}, source="not an event",
            expected_revision=1, request_id="invalid-old-write",
        )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM entity_profile_revision").fetchone()[0] == 1
