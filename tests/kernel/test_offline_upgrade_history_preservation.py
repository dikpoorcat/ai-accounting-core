"""Offline migration must not rewrite an old company's preserved source rows."""

from dataclasses import replace
from types import MappingProxyType

import pytest
from schema_fixture import TEST_FAMILY, full_contract, write_contract
from test_offline_upgrade import _fixture

from ai_accounting.kernel import offline_upgrade
from ai_accounting.kernel.backup import _retained_history_digest, run_backup_jobs
from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.contracts import KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle, verify_current_company
from ai_accounting.kernel.versions import verify_schema


def test_migration_rewriting_evidence_metadata_rolls_back_and_retries(tmp_path):
    root, companies, target = _fixture(tmp_path)
    source = replace(
        target, current_versions=MappingProxyType({"company": 1, "catalog": 1})
    )
    company = companies[0]
    engine = Engine(Catalog(root, source).bind(company["id"]))
    evidence_digest = engine.register_evidence(
        b"synthetic historical evidence", "text/plain", "original-name.txt",
        request_id="historic-evidence",
    )["digest"]
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT name FROM evidence WHERE digest=?", (bytes.fromhex(evidence_digest),)
        ).fetchone()[0]
    assert original == "original-name.txt"

    def rewrite_history(connection, original_apply):
        original_apply(connection)
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='immutable_evidence_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_evidence_UPDATE")
        connection.execute(
            "UPDATE evidence SET name='renamed-by-migration.txt' WHERE digest=?",
            (bytes.fromhex(evidence_digest),),
        )
        connection.execute(trigger)

    bad_steps = tuple(
        replace(
            step,
            apply=lambda connection, original_apply=step.apply: rewrite_history(
                connection, original_apply
            ),
        ) if step.kind == "company" else step
        for step in target.steps
    )
    bad_target = replace(target, steps=bad_steps)
    with pytest.raises(KernelError, match="保留的历史来源") as failed:
        offline_upgrade.upgrade_root(root, bundle=bad_target)
    assert failed.value.code == "migration_integrity_failed"

    with engine.store.connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=target, kind="company", allow_previous=True) == 1
        assert connection.execute(
            "SELECT name FROM evidence WHERE digest=?", (bytes.fromhex(evidence_digest),)
        ).fetchone()[0] == original
    result = offline_upgrade.upgrade_root(root, bundle=target)
    assert result["status"] == "upgraded"
    upgraded = Engine(Catalog(root, target).bind(company["id"]))
    with upgraded.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT name FROM evidence WHERE digest=?", (bytes.fromhex(evidence_digest),)
        ).fetchone()[0] == original


@pytest.mark.parametrize(
    "changed",
    (
        "request", "audit", "job_plan", "job_success_receipt", "job_success_status",
        "job_failed_attempts", "open_entity", "open_material", "unused_approval",
    ),
)
def test_migration_retains_unclosed_authoritative_history(tmp_path, changed):
    root, companies, target = _fixture(tmp_path)
    source = replace(target, current_versions=MappingProxyType({"company": 1, "catalog": 1}))
    company = companies[0]
    engine = Engine(Catalog(root, source).bind(company["id"]))
    proof = engine.register_evidence(
        b"synthetic original source", "text/plain", "source.txt", request_id="proof"
    )["digest"]
    replacement_proof = None
    if changed == "job_plan":
        engine.queue_backup(str(tmp_path / "archives"), request_id="backup-plan")
    elif changed in {"job_success_receipt", "job_success_status"}:
        engine.queue_backup(str(tmp_path / "archives"), request_id="delivered-backup")
        assert run_backup_jobs(engine.store.path, _bundle=source)[0]["status"] == "succeeded"
    elif changed == "job_failed_attempts":
        blocker = tmp_path / "blocked-directory"
        blocker.write_text("synthetic blocker", encoding="utf-8")
        engine.queue_backup(str(blocker), request_id="failed-backup")
        assert run_backup_jobs(engine.store.path, _bundle=source)[0]["status"] == "failed"
    elif changed == "open_entity":
        Entities(engine).register_entity(
            "person", {"display_name": "Original Person"},
            source="synthetic owner statement", request_id="entity",
        )
    elif changed == "open_material":
        replacement_proof = engine.register_evidence(
            b"synthetic different source", "text/plain", "other.txt", request_id="other-proof"
        )["digest"]
        Periods(engine).inventory(
            "2026-02", "bank", evidence=[], expected=0, no_business=True,
            confirmation_evidence=proof, request_id="inventory",
        )
    elif changed == "unused_approval":
        with engine.store.connection() as connection:
            connection.execute(
                "INSERT INTO security_close_approval("
                "id,catalog_instance_id,company_id,database_id,period,preview_digest,"
                "accounting_epoch,material_epoch,management_epoch,owner_id,session_id,"
                "credential_version,confirmed_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "synthetic-unconsumed-grant", "synthetic-catalog", company["id"],
                    company["database_id"], 2026 * 12 + 1, bytes(32),
                    0, 0, 0, "synthetic-owner", "synthetic-session", 1, 1, 2,
                ),
            )
    selector, trigger_name, update = {
        "request": (
            "SELECT result FROM request WHERE id='proof'", "immutable_request_UPDATE",
            "UPDATE request SET result='{}' WHERE id='proof'",
        ),
        "audit": (
            "SELECT payload FROM audit WHERE request_id='proof'", "immutable_audit_UPDATE",
            "UPDATE audit SET payload='{}' WHERE request_id='proof'",
        ),
        "job_plan": (
            "SELECT payload FROM jobs WHERE kind='portable_backup'", "frozen_job_payload",
            "UPDATE jobs SET payload='{}' WHERE kind='portable_backup'",
        ),
        "job_success_receipt": (
            "SELECT result FROM jobs WHERE kind='portable_backup'", None,
            "UPDATE jobs SET result='{}' WHERE kind='portable_backup'",
        ),
        "job_success_status": (
            "SELECT status FROM jobs WHERE kind='portable_backup'", None,
            "UPDATE jobs SET status='failed' WHERE kind='portable_backup'",
        ),
        "job_failed_attempts": (
            "SELECT attempts FROM jobs WHERE kind='portable_backup'", None,
            "UPDATE jobs SET attempts=attempts+1 WHERE kind='portable_backup'",
        ),
        "open_entity": (
            "SELECT kind FROM entity LIMIT 1", "immutable_entity_UPDATE",
            "UPDATE entity SET kind='organization'",
        ),
        "open_material": (
            "SELECT evidence_digest FROM material_revision LIMIT 1",
            "immutable_material_revision_UPDATE",
            f"UPDATE material_revision SET evidence_digest=X'{replacement_proof}'",
        ),
        "unused_approval": (
            "SELECT owner_id FROM security_close_approval LIMIT 1", "security_approval_sealed",
            "UPDATE security_close_approval SET owner_id='other-owner'",
        ),
    }[changed]
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(selector).fetchone()[0]

    def rewrite_history(connection, original_apply):
        original_apply(connection)
        if trigger_name is not None:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (trigger_name,)
            ).fetchone()[0]
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(update)
        if trigger_name is not None:
            connection.execute(trigger)

    bad_target = replace(
        target,
        steps=tuple(
            replace(
                step,
                apply=lambda connection, original_apply=step.apply: rewrite_history(
                    connection, original_apply
                ),
            ) if step.kind == "company" else step
            for step in target.steps
        ),
    )
    with pytest.raises(KernelError, match="保留的历史来源") as failed:
        offline_upgrade.upgrade_root(root, bundle=bad_target)
    assert failed.value.code == "migration_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=target, kind="company", allow_previous=True) == 1
        assert connection.execute(selector).fetchone()[0] == original
    result = offline_upgrade.upgrade_root(root, bundle=target)
    assert {item["company_id"]: item["status"] for item in result["companies"]}[company["id"]] == (
        "upgraded"
    )
    upgraded = Engine(Catalog(root, target).bind(company["id"]))
    with upgraded.store.connection(read_only=True) as connection:
        assert connection.execute(selector).fetchone()[0] == original


def test_new_target_column_does_not_change_old_history_digest(tmp_path):
    registry = Registry()
    directory = tmp_path / "contracts"
    company_source = full_contract(
        schema_sql(registry), kind="company", status="released", version=1
    )
    company_added = "ALTER TABLE entity ADD COLUMN future_label TEXT"
    company_target = full_contract(
        schema_sql(registry) + company_added + ";", kind="company", status="released", version=2
    )
    catalog_source = full_contract(catalog_sql(), kind="catalog", status="released", version=1)
    catalog_added = "CREATE INDEX synthetic_catalog_name ON company(name)"
    catalog_target = full_contract(
        catalog_sql() + catalog_added + ";", kind="catalog", status="released", version=2
    )
    for old, new in ((company_source, company_target), (catalog_source, catalog_target)):
        before = {(item["type"], item["name"]): item for item in old["objects"]}
        after = {(item["type"], item["name"]): item for item in new["objects"]}
        write_contract(directory, old)
        write_contract(
            directory,
            {key: value for key, value in new.items() if key != "objects"}
            | {
                "base_version": 1,
                "base_sha256": old["sha256"],
                "add": [after[key] for key in sorted(after.keys() - before.keys())],
                "remove": [
                    {"type": key[0], "name": key[1]}
                    for key in sorted(before.keys() - after.keys())
                ],
                "replace": [
                    after[key] for key in sorted(before.keys() & after.keys())
                    if before[key] != after[key]
                ],
            },
        )
    steps = tuple(
        MigrationStep(
            TEST_FAMILY, old["kind"], 1, old["sha256"], 2, new["sha256"],
            lambda connection, sql=sql: connection.execute(sql), lambda _connection: None,
        )
        for old, new, sql in (
            (company_source, company_target, company_added),
            (catalog_source, catalog_target, catalog_added),
        )
    )

    def bundle(version):
        return load_bundle(
            registry, directory, family=TEST_FAMILY, application_id=APPLICATION_ID,
            status="released", current_versions={"company": version, "catalog": version},
            steps=steps, company_verifiers={1: verify_current_company, 2: verify_current_company},
        )

    root = tmp_path / "root"
    original_bundle, target_bundle = bundle(1), bundle(2)
    catalog = Catalog(root, original_bundle)
    company = catalog.create_company("91310000123456789A", "合成增列企业")
    engine = Engine(catalog.bind(company["id"]))
    Entities(engine).register_entity(
        "person", {"display_name": "Unclosed Historical Person"},
        source="synthetic owner statement", request_id="original-person",
    )
    with engine.store.connection(read_only=True) as connection:
        original_digest = _retained_history_digest(connection)
    result = offline_upgrade.upgrade_root(root, bundle=target_bundle)
    assert result["companies"] == [{"company_id": company["id"], "status": "upgraded"}]
    upgraded = Engine(Catalog(root, target_bundle).bind(company["id"]))
    with upgraded.store.connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=target_bundle, kind="company") == 2
        assert "future_label" in {
            row[1] for row in connection.execute("PRAGMA table_info(entity)")
        }
        assert _retained_history_digest(connection) == original_digest


@pytest.mark.parametrize("changed", ("identity", "operation", "setting", "audit"))
def test_migration_rewriting_catalog_history_rolls_back_and_retries(tmp_path, changed):
    root, companies, target = _fixture(tmp_path)
    source = replace(
        target, current_versions=MappingProxyType({"company": 1, "catalog": 1})
    )
    catalog = Catalog(root, source)
    catalog.configure_backup(
        companies[0]["id"], str(tmp_path / "synthetic-backup"), expected_revision=0
    )
    with catalog.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO security_audit(id,occurred_at,event,outcome,request_id) "
            "VALUES(1,1,'original-event','succeeded','synthetic-audit')"
        )
        connection.commit()
    state_queries = {
        "identity": "SELECT instance_id FROM catalog_identity WHERE id=1",
        "operation": "SELECT attempts FROM company_operation ORDER BY id LIMIT 1",
        "setting": "SELECT backup_directory FROM company_setting ORDER BY company_id LIMIT 1",
        "audit": "SELECT event FROM security_audit WHERE id=1",
    }
    with catalog.connection(read_only=True) as connection:
        original = connection.execute(state_queries[changed]).fetchone()[0]

    def rewrite_history(connection, original_apply):
        original_apply(connection)
        trigger_name, update = {
            "identity": (
                "immutable_catalog_identity_update",
                "UPDATE catalog_identity SET instance_id='different-synthetic-instance' WHERE id=1",
            ),
            "operation": (
                None,
                "UPDATE company_operation SET attempts=attempts+1 "
                "WHERE id=(SELECT min(id) FROM company_operation)",
            ),
            "setting": (
                "immutable_company_setting_update",
                "UPDATE company_setting SET backup_directory='different-synthetic-backup'",
            ),
            "audit": (
                "security_audit_no_update",
                "UPDATE security_audit SET event='different-synthetic-event' WHERE id=1",
            ),
        }[changed]
        if trigger_name is not None:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()[0]
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(update)
        if trigger_name is not None:
            connection.execute(trigger)

    bad_steps = tuple(
        replace(
            step,
            apply=lambda connection, original_apply=step.apply: rewrite_history(
                connection, original_apply
            ),
        ) if step.kind == "catalog" else step
        for step in target.steps
    )
    with pytest.raises(KernelError, match="目录身份或历史") as failed:
        offline_upgrade.upgrade_root(root, bundle=replace(target, steps=bad_steps))
    assert failed.value.code == "migration_integrity_failed"
    with Catalog(root, source).connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=target, kind="catalog", allow_previous=True) == 1
        assert connection.execute(state_queries[changed]).fetchone()[0] == original
    result = offline_upgrade.upgrade_root(root, bundle=target)
    assert result["catalog"] == "upgraded"
    with Catalog(root, target).connection(read_only=True) as connection:
        assert connection.execute(state_queries[changed]).fetchone()[0] == original
