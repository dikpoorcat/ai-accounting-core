"""Production host, full owner verifier and real ZIP publication share one contract."""

import hashlib
import sqlite3
from uuid import uuid4
from zipfile import ZipFile

import pytest

from ai_accounting_modular.application.public import create_backup_producer
from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.host.public import create_offline_application

from ._working_case import case as case
from .test_backup_producer import BackupCase


@pytest.fixture
def real_case(case):
    case.seed()
    return BackupCase(
        case.app,
        case.company,
        create_backup_producer(
            storage=case.kernel.storage,
            records=case.kernel.records,
            verify_package=case.app.backup_verifier,
        ),
    )


def test_captured_point_survives_new_business_and_is_downloadable_after_capture_cleanup(case):
    case.seed()
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
    with case.app.capture_backup_snapshot(case.company, metadata.database_id) as plan:
        capture = metadata.company_root / "runtime/tasks" / plan["capture_id"]
        pending = case.kernel.files.ensure_current(plan, plan["scope_head"], str(uuid4()))
        changed = case.app.accounting_apply(case.request(case.expense("200000")))
        assert changed["completion"] == "complete", changed
        with case.kernel.storage.read_unit(case.company) as read:
            assert read.head > plan["manifest"]["database_head"]
        delivered = case.kernel.files.run(case.company, pending["task_id"])
        assert delivered["state"] == "succeeded", delivered
        assert not capture.exists()
    with case.kernel.files.verified_download(case.company, delivered["task_id"]) as download:
        manifest = create_offline_application().backup_verifier(download.stream)
        assert manifest["database_head"] == plan["manifest"]["database_head"]
        assert manifest["document_set_id"] == plan["manifest"]["document_set_id"]
        assert download.sha256 == delivered["sha256"]


def test_full_verifier_reuses_exact_existing_zip_despite_maintenance_database_bytes(real_case):
    case = real_case
    plan, context = case.capture()
    installed = case.install(plan, context)
    original = installed.read_bytes()
    task = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": context.company.database_id}
    )
    completed = case.app.files.run(case.company, task["task_id"])
    assert completed["state"] == "succeeded", completed
    assert task["scope_head"] == plan["scope_head"]
    assert installed.read_bytes() == original
    fresh, following = case.capture()
    before = next(item for item in plan["manifest"]["files"] if item["kind"] == "database")
    after = next(item for item in fresh["manifest"]["files"] if item["kind"] == "database")
    assert before["sha256"] != after["sha256"]
    assert fresh["scope_head"] == plan["scope_head"]
    assert case.install(fresh, following).read_bytes() == original
    assert not installed.with_name(
        context.company.credit_code + ".previous.finance-company.zip"
    ).exists()


def test_full_verifier_never_reuses_or_rolls_checksum_consistent_but_bad_business_current(
    real_case, tmp_path
):
    case = real_case
    first, first_context = case.capture()
    current = case.install(first, first_context)
    first_bytes = current.read_bytes()
    case.advance("200000")
    plan, context = case.capture()
    case.install(plan, context)
    previous = current.with_name(context.company.credit_code + ".previous.finance-company.zip")
    assert previous.read_bytes() == first_bytes
    with ZipFile(current) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    database = tmp_path / "corrupted-business.sqlite3"
    database.write_bytes(entries["runtime/company.sqlite3"])
    with sqlite3.connect(database) as connection:
        business_id, raw = connection.execute(
            "SELECT business_id,data_json FROM op_fact LIMIT 1"
        ).fetchone()
        fact = loads_json(raw)
        fact["data"]["amount_fen"] = str(int(fact["data"]["amount_fen"]) + 1)
        connection.execute(
            "UPDATE op_fact SET data_json=? WHERE business_id=?",
            (canonical_json(fact), business_id),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = database.read_bytes()
    entries["runtime/company.sqlite3"] = raw
    manifest = loads_json(entries["manifest.json"])
    descriptor = next(item for item in manifest["files"] if item["kind"] == "database")
    descriptor.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    entries["manifest.json"] = canonical_json(manifest).encode()
    with ZipFile(current, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    corrupt = current.read_bytes()
    with current.open("rb") as stream, pytest.raises(ContractError):
        case.app.backup_verifier(stream)
    fresh, following = case.capture()
    assert fresh["scope_head"] == plan["scope_head"]
    repaired = case.install(fresh, following)
    assert repaired.read_bytes() != corrupt
    assert previous.read_bytes() == first_bytes
    with repaired.open("rb") as stream:
        assert (
            case.app.backup_verifier(stream)["database_head"] == plan["manifest"]["database_head"]
        )
