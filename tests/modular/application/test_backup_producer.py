"""Backup publication protocols use actual isolated database and record bytes."""

from __future__ import annotations

import hashlib
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import pytest

from ai_accounting_modular.application._backup_producer import create_backup_producer
from ai_accounting_modular.application._backup_scope import backup_scope_head
from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.files.public import FileTaskContext, ProductionError
from ai_accounting_modular.storage.public import company_structure_contract

from .conftest import initial_request


def byte_verifier(stream):
    """Protocol substitute for rotation tests; real full integrity is tested separately."""
    try:
        return _verify_bytes(stream)
    except BadZipFile as exc:
        raise ContractError("backup_invalid", "Protocol malformed ZIP") from exc


def _verify_bytes(stream):
    stream.seek(0)
    with ZipFile(stream) as archive:
        manifest = validate_contract(
            "core.schema.json", "BackupManifest", loads_json(archive.read("manifest.json"))
        )
        expected = {item["path"] for item in manifest["files"]} | {"manifest.json"}
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise ContractError("backup_invalid", "Protocol file set differs")
        for item in manifest["files"]:
            raw = archive.read(item["path"])
            if (hashlib.sha256(raw).hexdigest(), len(raw)) != (item["sha256"], item["byte_size"]):
                raise ContractError("backup_invalid", "Protocol byte mismatch")
    return manifest


@dataclass
class BackupCase:
    app: object
    company: str
    producer: object

    def advance(self, amount="100000"):
        result = self.app.accounting_apply(initial_request(self.company, amount=amount))
        assert result["completion"] == "complete", result

    def capture(self):
        capture_id = str(uuid4())
        with self.app.storage.read_unit(self.company) as read:
            metadata = self.app.storage.company_metadata(read)
            with self.app.records.capture_backup_materials(read, capture_id) as materials:
                root = materials.capture_root
                database = self.app.storage.capture_database(read, root / "runtime/company.sqlite3")
                contract = root / "contracts/company.json"
                contract.parent.mkdir()
                contract.write_bytes((canonical_json(company_structure_contract()) + "\n").encode())
                inputs = list(materials.inputs)
                for path in (database.path, contract):
                    raw = path.read_bytes()
                    relative = path.relative_to(root).as_posix()
                    inputs.append(
                        {
                            "source_root": "capture",
                            "relative_path": relative,
                            "archive_path": relative,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "byte_size": len(raw),
                        }
                    )
                files = []
                for item in inputs:
                    path = item["archive_path"]
                    kind = (
                        "database"
                        if path == "runtime/company.sqlite3"
                        else "contract"
                        if path == "contracts/company.json"
                        else "original"
                        if path.startswith("originals/")
                        else "standard"
                    )
                    files.append(
                        {
                            "path": path,
                            "kind": kind,
                            "sha256": item["sha256"],
                            "byte_size": item["byte_size"],
                        }
                    )
                manifest = {
                    "format": "ai-accounting-company-backup/1",
                    "company_identity": materials.company_identity,
                    "company_id": metadata.company_id,
                    "database_id": metadata.database_id,
                    "database_head": database.head,
                    "structure_fingerprint": database.structure_fingerprint,
                    "document_set_id": materials.document_set_id,
                    "files": files,
                }
                plan = {
                    "kind": "backup",
                    "company_id": metadata.company_id,
                    "database_id": metadata.database_id,
                    "capture_id": capture_id,
                    "scope_head": backup_scope_head(manifest),
                    "manifest": manifest,
                    "inputs": inputs,
                }
        task = str(uuid4())
        task_root = metadata.company_root / "runtime/file-tasks" / task
        (task_root / "work").mkdir(parents=True)
        return plan, FileTaskContext(metadata, task, task_root, root)

    def install(self, plan, context):
        with self.producer.recover_lease(plan, context):
            artifact = self.producer.produce(plan, context)
            candidate = context.task_root / artifact.relative_path
            with candidate.open("rb") as stream:
                self.producer.verify(plan, stream, context)
            delivered = self.producer.publish(plan, artifact, context)
            assert delivered.read_bytes() == candidate.read_bytes()
            return delivered


@pytest.fixture
def backup_case(assembled):
    app, company = assembled
    case = BackupCase(
        app,
        company,
        create_backup_producer(
            storage=app.storage, records=app.records, verify_package=byte_verifier
        ),
    )
    case.advance()
    return case


def test_real_inputs_produce_exact_complete_zip_and_captureless_verification(backup_case):
    case = backup_case
    plan, context = case.capture()
    delivered = case.install(plan, context)
    with ZipFile(delivered) as archive:
        assert set(archive.namelist()) == {"manifest.json"} | {
            item["path"] for item in plan["manifest"]["files"]
        }
        assert (
            archive.read("runtime/company.sqlite3")
            == (context.capture_root / "runtime/company.sqlite3").read_bytes()
        )
    case.producer.cleanup(context, keep_capture=False)
    assert not context.capture_root.exists()
    assert case.producer.is_current(plan, context)
    with delivered.open("rb") as stream:
        case.producer.verify(plan, stream, context)


def test_new_business_after_capture_does_not_expire_the_historical_package(backup_case):
    case = backup_case
    plan, context = case.capture()
    with case.producer.recover_lease(plan, context):
        case.advance("200000")
        assert case.producer.is_current(plan, context)
        delivered = case.install(plan, context)
    with delivered.open("rb") as stream:
        manifest = byte_verifier(stream)
    assert manifest["database_head"] == plan["manifest"]["database_head"]
    assert manifest["document_set_id"] == plan["manifest"]["document_set_id"]


def test_same_meaning_reuses_installed_database_and_zip_bytes_without_rolling_previous(backup_case):
    case = backup_case
    plan, context = case.capture()
    delivered = case.install(plan, context)
    original = delivered.read_bytes()
    fresh, next_context = case.capture()
    database = next_context.capture_root / "runtime/company.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=7")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = database.read_bytes()
    for items, field in ((fresh["inputs"], "archive_path"), (fresh["manifest"]["files"], "path")):
        item = next(value for value in items if value[field] == "runtime/company.sqlite3")
        item.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    assert fresh["manifest"]["files"] != plan["manifest"]["files"]
    assert backup_scope_head(fresh["manifest"]) == plan["scope_head"]
    assert case.install(fresh, next_context).read_bytes() == original
    assert not delivered.with_name("913100000000000001.previous.finance-company.zip").exists()


def test_same_semantics_but_different_zip_representation_reuses_existing_exact_bytes(backup_case):
    case = backup_case
    plan, context = case.capture()
    delivered = case.install(plan, context)
    with ZipFile(delivered) as archive:
        entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
    with ZipFile(delivered, "w", compression=ZIP_STORED) as archive:
        for name, raw in entries:
            archive.writestr(name, raw)
    alternate = delivered.read_bytes()
    fresh, next_context = case.capture()
    assert case.install(fresh, next_context).read_bytes() == alternate


def test_package_production_rejects_source_paths_outside_the_captured_root(backup_case):
    case = backup_case
    plan, context = case.capture()
    source = next(item for item in plan["inputs"] if item["source_root"] == "capture")
    outside = context.capture_root.parent / "outside"
    outside.write_bytes(b"unrelated material outside this capture")
    source["relative_path"] = "../outside"
    with pytest.raises(ProductionError) as failure:
        case.producer.produce(plan, context)
    assert failure.value.code == "unsafe_path"
    assert outside.read_bytes() == b"unrelated material outside this capture"
    assert not list((context.task_root / "work").iterdir())


@pytest.mark.parametrize("change", ["identity", "document_set", "non_database", "structure"])
def test_scope_covers_every_backup_meaning_except_database_container_bytes(backup_case, change):
    plan, _ = backup_case.capture()
    different = deepcopy(plan["manifest"])
    if change == "identity":
        different["company_identity"]["legal_name"] = "另一个捕获标题"
    elif change == "document_set":
        different["document_set_id"] = "a" * 64
    elif change == "structure":
        different["structure_fingerprint"] = "b" * 64
    else:
        next(item for item in different["files"] if item["kind"] != "database")["sha256"] = "c" * 64
    assert backup_scope_head(different) != plan["scope_head"]
