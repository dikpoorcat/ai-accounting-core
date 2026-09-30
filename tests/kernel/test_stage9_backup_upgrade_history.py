"""A released old ZIP upgrade must retain verified historical source identities."""

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from monthly_close_fixture import close_months, ready
from schema_fixture import TEST_FAMILY, full_contract, write_contract
from test_engine import Charge, Source, calculate, evidence, publish, save

from ai_accounting.kernel import backup
from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.contracts import KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle, verify_current_company


def _released_bundles(tmp_path):
    registry = Registry()
    registry.register(Source)
    registry.register(Charge, calculate)
    directory = tmp_path / "contracts"
    steps = []
    for kind, script, added in (
        (
            "company",
            schema_sql(registry),
            "CREATE INDEX stage9_company_backup_upgrade ON fact_revision(subject_id)",
        ),
        ("catalog", catalog_sql(), "CREATE INDEX stage9_catalog_backup_upgrade ON company(name)"),
    ):
        source = full_contract(script, kind=kind, status="released", version=1)
        target = full_contract(script + added + ";", kind=kind, status="released", version=2)
        write_contract(directory, source)
        write_contract(
            directory,
            {key: value for key, value in target.items() if key != "objects"}
            | {
                "base_version": 1,
                "base_sha256": source["sha256"],
                "add": [item for item in target["objects"] if item not in source["objects"]],
                "remove": [],
                "replace": [],
            },
        )
        steps.append(
            MigrationStep(
                TEST_FAMILY,
                kind,
                1,
                source["sha256"],
                2,
                target["sha256"],
                lambda connection, sql=added: connection.execute(sql),
                lambda _connection: None,
            )
        )

    def bundle(version):
        return load_bundle(
            registry,
            directory,
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
            status="released",
            current_versions={"company": version, "catalog": version},
            steps=tuple(steps),
            company_verifiers={1: verify_current_company, 2: verify_current_company},
        )

    return bundle(1), bundle(2)


def _released_zip(tmp_path, *, with_completed_job=False):
    old, target = _released_bundles(tmp_path)
    root = tmp_path / "root"
    catalog = Catalog(root, old)
    company = catalog.create_company("91310000123456789A", "合成恢复历史企业")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
    ready(engine, proof, first="2026-01", last="2026-01")
    save(engine)
    publish(engine)
    close_months(Periods(engine), proof, first="2026-01", last="2026-01")
    Periods(engine).management(
        "charge", note="unclosed management history", payment_period=None,
        payment_category=None, expected_revision=0, request_id="open-management",
    )
    with engine.store.connection() as connection:
        content = b"unreferenced synthetic history evidence"
        connection.execute(
            "INSERT INTO evidence(digest,content,media_type,name) VALUES(?,?,?,?)",
            (hashlib.sha256(content).digest(), content, "application/pdf", "original.pdf"),
        )
    if with_completed_job:
        engine.queue_backup(str(tmp_path / "job-archives"), request_id="historical-job")
        assert backup.run_backup_jobs(engine.store.path, _bundle=old)[0]["status"] == "succeeded"
    source = engine.store.path
    archive = Path(backup.create_portable(source, tmp_path / "archives", _bundle=old)["path"])
    return source, archive, target, company


def _history(connection):
    return {
        table: tuple(tuple(row) for row in connection.execute(sql))
        for table, sql in {
            "facts": "SELECT id,digest FROM fact_revision ORDER BY id",
            "calculations": "SELECT id,digest FROM calculation ORDER BY id",
            "closes": "SELECT period,digest FROM period_close ORDER BY period",
            "evidence": "SELECT digest,name FROM evidence ORDER BY digest",
            "management": "SELECT id,subject_id,revision,note FROM management_revision ORDER BY id",
        }.items()
    }


def test_released_v1_zip_restores_to_v2_with_nonempty_history(tmp_path):
    source, archive, target, company = _released_zip(tmp_path)
    archive_sha = backup._digest(archive)
    destination = tmp_path / "upgraded.sqlite"
    result = backup.restore_portable(
        archive,
        destination,
        _bundle=target,
        expected_company_id=company["id"],
    )
    assert result["source_database_format"]["version"] == 1
    assert result["database_format"]["version"] == 2
    assert result["verification"]["status"] == "verified"
    assert result["latest_closed_period"] == "2026-01"
    assert backup._digest(archive) == archive_sha
    assert backup.verify_file(destination, _bundle=target)["verification"]["status"] == "verified"
    with sqlite3.connect(source) as old, sqlite3.connect(destination) as upgraded:
        assert _history(old) == _history(upgraded)


def test_released_zip_target_verifier_fails_inside_transaction_without_publishing(tmp_path):
    _source, archive, target, company = _released_zip(tmp_path)
    archive_sha = backup._digest(archive)
    checked = []

    def reject_target(connection, _bundle):
        checked.append(connection.in_transaction)
        raise KernelError("synthetic_target_rejected", "synthetic target rejected")

    bad = replace(
        target,
        company_verifiers=MappingProxyType({1: target.company_verifiers[1], 2: reject_target}),
    )
    destination = tmp_path / "rejected.sqlite"
    with pytest.raises(backup.BackupError, match="synthetic_target_rejected"):
        backup.restore_portable(
            archive,
            destination,
            _bundle=bad,
            expected_company_id=company["id"],
        )
    assert checked == [True]
    assert not destination.exists()
    assert backup._digest(archive) == archive_sha


def test_released_zip_rejects_same_count_historical_replacement(tmp_path):
    _source, archive, target, company = _released_zip(tmp_path)
    archive_sha = backup._digest(archive)
    step = next(item for item in target.steps if item.kind == "company")

    def replace_history(connection):
        step.apply(connection)
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='immutable_evidence_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_evidence_UPDATE")
        connection.execute("UPDATE evidence SET name='replacement.pdf' WHERE name='original.pdf'")
        connection.execute(trigger)

    altered = replace(
        target,
        steps=tuple(
            replace(item, apply=replace_history) if item is step else item for item in target.steps
        ),
    )
    destination = tmp_path / "altered.sqlite"
    with pytest.raises(backup.BackupError, match="changed retained history"):
        backup.restore_portable(
            archive,
            destination,
            _bundle=altered,
            expected_company_id=company["id"],
        )
    assert not destination.exists()
    assert backup._digest(archive) == archive_sha


@pytest.mark.parametrize("changed", ("request", "open_management", "fact_scope"))
def test_released_zip_rejects_authoritative_source_rewrite(tmp_path, changed):
    _source, archive, target, company = _released_zip(tmp_path)
    archive_sha = backup._digest(archive)
    step = next(item for item in target.steps if item.kind == "company")

    trigger_name, update = {
        "request": (
            "immutable_request_UPDATE", "UPDATE request SET result='{}' WHERE id='evidence'"
        ),
        "open_management": (
            "immutable_management_revision_UPDATE",
            "UPDATE management_revision SET note='rewritten' WHERE subject_id='charge'",
        ),
        "fact_scope": (
            "immutable_fact_scope_UPDATE",
            "UPDATE fact_scope SET scope_key='@rewritten' "
            "WHERE fact_id=(SELECT id FROM fact_revision WHERE subject_id='charge') "
            "AND scope_key='@charge'",
        ),
    }[changed]

    def replace_source(connection):
        step.apply(connection)
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()[0]
        connection.execute(f"DROP TRIGGER {trigger_name}")
        changed_count = connection.execute(update).rowcount
        assert changed_count == 1
        connection.execute(trigger)

    altered = replace(
        target,
        steps=tuple(
            replace(item, apply=replace_source) if item is step else item
            for item in target.steps
        ),
    )
    destination = tmp_path / f"{changed}-altered.sqlite"
    with pytest.raises(backup.BackupError, match="changed retained history"):
        backup.restore_portable(
            archive,
            destination,
            _bundle=altered,
            expected_company_id=company["id"],
        )
    assert not destination.exists()
    assert backup._digest(archive) == archive_sha


def test_released_zip_rejects_completed_job_receipt_rewrite(tmp_path):
    _source, archive, target, company = _released_zip(tmp_path, with_completed_job=True)
    archive_sha = backup._digest(archive)
    step = next(item for item in target.steps if item.kind == "company")

    def replace_receipt(connection):
        step.apply(connection)
        assert connection.execute(
            "UPDATE jobs SET result='{}' WHERE status='succeeded' AND kind='portable_backup'"
        ).rowcount == 1

    altered = replace(
        target,
        steps=tuple(
            replace(item, apply=replace_receipt) if item is step else item
            for item in target.steps
        ),
    )
    destination = tmp_path / "job-receipt-altered.sqlite"
    with pytest.raises(backup.BackupError, match="changed retained history"):
        backup.restore_portable(
            archive, destination, _bundle=altered,
            expected_company_id=company["id"],
        )
    assert not destination.exists()
    assert backup._digest(archive) == archive_sha
