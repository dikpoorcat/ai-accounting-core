"""Bind a database snapshot to the exact published materials, then hand off its lease."""

import os
from contextlib import contextmanager
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError, canonical_json, validate_contract
from ai_accounting_modular.storage.public import company_structure_contract

from . import _documents
from ._backup_scope import CONTRACT_PATH, DATABASE_PATH, backup_scope_head
from ._target_paths import beneath, file_digest


@contextmanager
def capture(application, company_id, expected_database_id):
    for identity in (company_id, expected_database_id):
        validate_contract("core.schema.json", "Id", identity)
    with application.storage.read_unit(company_id) as read:
        if read.database_id != expected_database_id:
            raise ContractError("conflict", "公司已切换数据库，不能按旧身份建立备份。")
        status = application.records.document_status(read)
        pending = status["state"] != "ready"
    if pending:
        status = _documents.finish(application, company_id)
        if status["state"] != "ready":
            raise ContractError("documents_pending", "标准业务文档尚未归档完整，备份未开始。")
    capture_id = str(uuid4())
    try:
        with application.storage.read_unit(company_id) as read:
            if read.database_id != expected_database_id:
                raise ContractError("conflict", "公司数据库在捕获前已经变化。")
            with application.records.capture_backup_materials(read, capture_id) as materials:
                snapshot = application.storage.capture_database(
                    read, beneath(materials.capture_root, DATABASE_PATH)
                )
                contract_path = beneath(materials.capture_root, CONTRACT_PATH)
                contract_path.parent.mkdir(parents=True, exist_ok=True)
                raw = (canonical_json(company_structure_contract()) + "\n").encode("utf-8")
                with contract_path.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                inputs = list(materials.inputs)
                for relative in (DATABASE_PATH, CONTRACT_PATH):
                    digest, size = file_digest(beneath(materials.capture_root, relative))
                    inputs.append(
                        {
                            "source_root": "capture",
                            "relative_path": relative,
                            "archive_path": relative,
                            "sha256": digest,
                            "byte_size": size,
                        }
                    )
                files = []
                for item in sorted(inputs, key=lambda value: value["archive_path"]):
                    path = item["archive_path"]
                    kind = (
                        "database"
                        if path == DATABASE_PATH
                        else "contract"
                        if path == CONTRACT_PATH
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
                    "company_id": read.company_id,
                    "database_id": read.database_id,
                    "database_head": snapshot.head,
                    "structure_fingerprint": snapshot.structure_fingerprint,
                    "document_set_id": materials.document_set_id,
                    "files": files,
                }
                plan = validate_contract(
                    "core.schema.json",
                    "BackupPlan",
                    {
                        "kind": "backup",
                        "company_id": read.company_id,
                        "database_id": read.database_id,
                        "capture_id": capture_id,
                        "scope_head": backup_scope_head(manifest),
                        "manifest": manifest,
                        "inputs": sorted(inputs, key=lambda item: item["archive_path"]),
                    },
                )
                yield plan
    finally:
        if application.files is not None:
            application.files.discard_unaccepted_capture(company_id, capture_id)
        else:
            with application.storage.read_unit(company_id) as read:
                application.records.discard_backup_capture(capture_id, read)


def start(application, request):
    validate_contract("core.schema.json", "BackupRequest", request)
    if application.files is None:
        raise ContractError("unsupported", "当前实例未装配备份文件任务。")
    with capture(application, request["company_id"], request["expected_database_id"]) as plan:
        return application.files.ensure_current(plan, plan["scope_head"], str(uuid4()))
