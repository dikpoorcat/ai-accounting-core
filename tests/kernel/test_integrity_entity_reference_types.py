"""Raw fact authority rejects object-type drift independently of repairable indexes."""

import sqlite3
from types import SimpleNamespace

import pytest
import test_banking as banking
from test_integrity_content import damage

from ai_accounting.kernel import content_v1_semantics
from ai_accounting.kernel import entity_references as references
from ai_accounting.kernel.backup import BackupError, backup_to_file
from ai_accounting.kernel.content_v1 import registry_descriptor
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.service import default_registry

bank_book = banking.book


@pytest.fixture
def objects():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE entity(id TEXT PRIMARY KEY,kind TEXT,account_type TEXT);"
        "INSERT INTO entity VALUES('account','fund_account','bank');"
        "INSERT INTO entity VALUES('party','organization',NULL);"
    )
    try:
        yield connection
    finally:
        connection.close()


def declared(kind="fund_account", account_type="bank", *, path="bank_account_id"):
    return {"path": path, "role": "bank_account", "reference_type": "entity",
            "kinds": (kind,), "account_type": account_type}


def registry(declarations):
    return SimpleNamespace(content_version=2, reference_declarations={"source": declarations})


@pytest.mark.parametrize("fault", ["missing", "kind", "account_type"])
def test_type_failures_locate_exact_fact_path_and_object_without_source_values(objects, fault):
    if fault == "missing":
        objects.execute("DELETE FROM entity WHERE id='account'")
    elif fault == "kind":
        objects.execute(
            "UPDATE entity SET kind='organization',account_type=NULL WHERE id='account'"
        )
    else:
        objects.execute("UPDATE entity SET account_type='cash' WHERE id='account'")
    facts = {"original-fact": {"kind": "source", "data": {
        "members": [{"bank_account_id": "account"}], "private_note": "not-for-errors",
    }}}
    with pytest.raises(KernelError) as failure:
        references.verify_fact_entity_types(
            objects, facts, registry=registry([declared(path="members.*.bank_account_id")]),
        )
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details == {
        "component": "entity_reference", "record_id": "original-fact",
        "path": "members.0.bank_account_id", "entity_id": "account",
        "reason": {"missing": "referenced_entity_missing", "kind": "entity_kind_mismatch",
                   "account_type": "entity_account_type_mismatch"}[fault],
    }
    assert "not-for-errors" not in str(failure.value.response())


def test_repeated_references_and_distinct_constraints_share_one_exact_entity_query(objects):
    statements = []
    objects.set_trace_callback(statements.append)
    facts = {f"fact-{index}": {"kind": "source", "data": {
        "bank_account_id": "account", "counterparty_id": "party",
    }} for index in range(64)}
    party = {"path": "counterparty_id", "role": "counterparty", "reference_type": "entity",
             "kinds": ("person", "organization"), "account_type": None}
    declarations = [declared(), party]
    def observed(source):
        statements.clear()
        steps = [0]

        def progress():
            steps[0] += 1
            return 0

        objects.set_progress_handler(progress, 1)
        try:
            references.verify_fact_entity_types(objects, source, registry=registry(declarations))
        finally:
            objects.set_progress_handler(None, 0)
        assert len(statements) == 1
        return steps[0]

    before = observed(facts)
    objects.executemany(
        "INSERT INTO entity VALUES(?,'person',NULL)",
        [(f"unrelated-{index}",) for index in range(4096)],
    )
    history = {f"historical-fact-{index}": facts["fact-0"] for index in range(2048)}
    after = observed(history)
    # Both source scopes require these same two identities. Unrelated objects
    # and older repeated references must not widen the actual SQLite scan.
    assert after == before
    # The same ID under another explicit constraint must still be checked.
    statements.clear()
    with pytest.raises(KernelError) as failure:
        references.verify_fact_entity_types(
            objects, facts,
            registry=registry(declarations + [{**party, "kinds": ("asset",)}]),
        )
    assert failure.value.details["entity_id"] == "party"
    assert failure.value.details["reason"] == "entity_kind_mismatch"
    assert len(statements) == 1


@pytest.mark.parametrize("case", ["unknown-kind", "business-only", "nullable"])
def test_no_explicit_object_reference_performs_no_sql(objects, case):
    statements = []
    objects.set_trace_callback(statements.append)
    declarations = {
        "unknown-kind": [],
        "business-only": [{"path": "bank_account_id", "role": "business_source",
                           "reference_type": "business"}],
        "nullable": [declared()],
    }[case]
    references.verify_fact_entity_types(
        objects, {"fact": {"kind": "source", "data": {
            "bank_account_id": None if case == "nullable" else "unregistered-business-source",
        }}}, registry=registry(declarations),
    )
    assert not statements


def test_source_version_uses_released_declaration_instead_of_current_rule(objects, monkeypatch):
    # Draft development has no released content-v1.json. Capture a versioned
    # descriptor before changing today's rule, as other retained-v1 tests do.
    descriptor = registry_descriptor(default_registry())
    historical = SimpleNamespace(content_version=1,
                                 reference_declarations=descriptor["references"])
    assert historical.content_version == 1
    assert references.declarations_for("bank_opening", registry=historical)[0][
        "account_type"
    ] == "bank"
    monkeypatch.setitem(references.DECLARATIONS, "bank_opening", [declared("person", None)])
    current = SimpleNamespace(content_version=2, reference_declarations=references.DECLARATIONS)
    facts = {"fact": {"kind": "bank_opening", "data": {"bank_account_id": "account"}}}
    calls = []
    original = content_v1_semantics.references_from_data

    def historical_paths(kind, data, declarations):
        calls.append(kind)
        return original(kind, data, declarations)

    monkeypatch.setattr(content_v1_semantics, "references_from_data", historical_paths)
    references.verify_fact_entity_types(objects, facts, registry=historical)
    assert calls == ["bank_opening"]
    with pytest.raises(KernelError) as failure:
        references.verify_fact_entity_types(objects, facts, registry=current)
    assert failure.value.details["reason"] == "entity_kind_mismatch"
    assert calls == ["bank_opening"]


@pytest.mark.parametrize("fault", ["kind", "account_type"])
def test_core_and_repair_preflight_and_backup_reject_original_source_type_drift(
    bank_book, tmp_path, fault,
):
    engine, save, publish, _proof = bank_book
    banking.opening(save, publish)
    with engine.store.connection(read_only=True) as connection:
        fact_id = engine.store.current_fact(connection, "opening-bank-a").id
        assert verify_integrity(engine, connection)["status"] == "verified"
        before = {table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                  for table in ("entity_reference_recorded", "entity_reference_current",
                                "monthly_account", "audit")}
    sql = (
        "UPDATE entity SET kind='organization',account_type=NULL WHERE id='bank-a'"
        if fault == "kind" else
        "UPDATE entity SET account_type='cash' WHERE id='bank-a'"
    )
    damage(engine, "entity", sql)
    for include_indexes in (True, False):
        with (
            engine.store.connection(read_only=True) as connection,
            pytest.raises(KernelError) as bad,
        ):
            verify_integrity(engine, connection, include_indexes=include_indexes,
                             include_projections=include_indexes)
        assert bad.value.code == "content_integrity_failed"
        assert bad.value.details["record_id"] == fact_id
        assert bad.value.details["path"] == "bank_account_id"
        assert bad.value.details["entity_id"] == "bank-a"
    for operation in (engine.rebuild_projections, Maintenance(engine).repair_read_indexes):
        with pytest.raises(KernelError) as bad:
            operation(request_id="reject-source-type-" + operation.__name__)
        assert bad.value.code == "content_integrity_failed"
        assert bad.value.details["record_id"] == fact_id
    target = tmp_path / "type-damaged.finance-company.zip"
    with pytest.raises(BackupError) as bad_backup:
        backup_to_file(engine.store.path, target)
    assert bad_backup.value.code == "backup_content_invalid"
    assert not target.exists()
    with engine.store.connection(read_only=True) as connection:
        assert {table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                for table in before} == before
