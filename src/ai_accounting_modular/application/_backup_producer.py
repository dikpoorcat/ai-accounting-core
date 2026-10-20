"""Real ZIP production, independent of assembled Application and Files instances."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from typing import BinaryIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from ai_accounting_modular.contracts.public import (
    BackupManifest,
    canonical_json,
    validate_contract,
)
from ai_accounting_modular.files.public import (
    FileArtifact,
    FileProducer,
    FileScope,
    ProductionError,
)
from ai_accounting_modular.storage.public import company_structure_contract

from ._backup_io import CHUNK_SIZE, atomic_output, controlled, copy_bytes, snapshot
from ._backup_rotation import Rotation
from ._backup_scope import CONTRACT_PATH, DATABASE_PATH, MANIFEST_PATH, backup_scope_head

_ARTIFACT = "work/company.finance-company.zip"


def _expected_kind(path):
    if path == DATABASE_PATH:
        return "database"
    if path == CONTRACT_PATH:
        return "contract"
    if path in {"standard/current.json", "standard/README.md"} or path.startswith(
        "standard/parts/"
    ):
        return "standard"
    if path.startswith("originals/"):
        return "original"
    return None


def _entry(path):
    value = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    value.compress_type = ZIP_DEFLATED
    value.create_system = 3
    value.external_attr = (stat.S_IFREG | 0o600) << 16
    return value


class BackupProducer:
    def __init__(self, *, storage, records, verify_package):
        if not callable(verify_package):
            raise ProductionError("invalid_input", "备份须装配真实整包核验器。", retryable=False)
        self.storage, self.records, self.verify_package = storage, records, verify_package
        self.structure_fingerprint = company_structure_contract()["fingerprint"]

    def describe(self, plan):
        validate_contract("core.schema.json", "BackupPlan", plan)
        manifest = plan["manifest"]
        expected = backup_scope_head(manifest)
        if expected != plan["scope_head"] or (plan["company_id"], plan["database_id"]) != (
            manifest["company_id"],
            manifest["database_id"],
        ):
            raise ProductionError("backup_invalid", "备份计划语义头不一致。", retryable=False)
        inputs = {
            item["archive_path"]: (item["sha256"], item["byte_size"]) for item in plan["inputs"]
        }
        files = {item["path"]: (item["sha256"], item["byte_size"]) for item in manifest["files"]}
        if len(inputs) != len(plan["inputs"]) or inputs != files:
            raise ProductionError(
                "backup_invalid", "捕获输入与包内完整清单不一致。", retryable=False
            )
        names = {item["path"] for item in manifest["files"]}
        if (
            not {DATABASE_PATH, CONTRACT_PATH, "standard/current.json", "standard/README.md"}
            <= names
        ):
            raise ProductionError(
                "backup_invalid", "备份计划缺少数据库、结构合同或完整标准清单。", retryable=False
            )
        for item in manifest["files"]:
            if _expected_kind(item["path"]) != item["kind"]:
                raise ProductionError(
                    "backup_invalid", "备份计划包含未知文件位置或类型。", retryable=False
                )
        return FileScope(
            "company",
            expected,
            manifest["company_identity"]["credit_code"] + ".finance-company.zip",
        )

    def is_current(self, plan, context):
        manifest = plan["manifest"]
        with self.storage.read_unit(plan["company_id"]) as read:
            return (
                (read.company_id, read.database_id)
                == (plan["company_id"], plan["database_id"])
                == (context.company.company_id, context.company.database_id)
                == (manifest["company_id"], manifest["database_id"])
                and read.head >= manifest["database_head"]
                and manifest["structure_fingerprint"] == self.structure_fingerprint
            )

    @contextmanager
    def recover_lease(self, plan, context):
        with ExitStack() as stack:
            with self.storage.read_unit(plan["company_id"]) as read:
                stack.enter_context(self.records.lease_backup_inputs(plan, read))
            yield

    def produce(self, plan, context):
        self.describe(plan)
        rotation = Rotation(context, self.verify_package)
        current, _ = rotation.names(plan["manifest"]["company_identity"]["credit_code"])
        with rotation.current(current) as existing:
            if existing is not None:
                rotation.reject_older(plan, existing)
                if existing.valid and backup_scope_head(existing.manifest) == plan["scope_head"]:
                    copy_bytes(existing.bytes, context.task_root, _ARTIFACT)
                    return FileArtifact(_ARTIFACT)
        with atomic_output(context.task_root, _ARTIFACT) as output:
            with ZipFile(output, "w") as archive:
                archive.writestr(
                    _entry(MANIFEST_PATH), (canonical_json(plan["manifest"]) + "\n").encode()
                )
                for item in sorted(plan["inputs"], key=lambda value: value["archive_path"]):
                    root = (
                        context.company.company_root
                        if item["source_root"] == "company"
                        else context.capture_root
                    )
                    if root is None:
                        raise ProductionError(
                            "capture_missing", "备份缺少原捕获根。", retryable=False
                        )
                    source = controlled(root, item["relative_path"])
                    digest, size = hashlib.sha256(), 0
                    with (
                        source.open("rb") as origin,
                        archive.open(_entry(item["archive_path"]), "w", force_zip64=True) as target,
                    ):
                        if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode):
                            raise ProductionError(
                                "unsafe_path", "备份输入不是普通文件。", retryable=False
                            )
                        while chunk := origin.read(CHUNK_SIZE):
                            size += len(chunk)
                            if size > item["byte_size"]:
                                raise ProductionError(
                                    "capture_invalid", "捕获输入大小已经变化。", retryable=False
                                )
                            digest.update(chunk)
                            target.write(chunk)
                    controlled(root, item["relative_path"])
                    if (digest.hexdigest(), size) != (item["sha256"], item["byte_size"]):
                        raise ProductionError(
                            "capture_invalid", "捕获输入字节与保存计划不符。", retryable=False
                        )
        return FileArtifact(_ARTIFACT)

    def verify(self, plan, stream, context):
        manifest = self.verify_package(stream)
        if backup_scope_head(manifest) != plan["scope_head"]:
            raise ProductionError(
                "backup_invalid", "交付包与备份计划的语义不一致。", retryable=False
            )

    def publish(self, plan, artifact, context):
        with snapshot(context.task_root, artifact.relative_path) as candidate:
            self.verify(plan, candidate.stream, context)
            return Rotation(context, self.verify_package).publish(plan, candidate)

    def cleanup(self, context, *, keep_capture):
        if context.capture_root is not None:
            with self.storage.read_unit(context.company.company_id) as read:
                self.records.discard_backup_capture(
                    context.capture_root.name, read, keep_capture=keep_capture
                )

    def discard_capture(self, context):
        with self.storage.read_unit(context.company.company_id) as read:
            self.records.discard_backup_capture(context.capture_id, read)


def create_backup_producer(
    *, storage, records, verify_package: Callable[[BinaryIO], BackupManifest]
) -> FileProducer:
    return BackupProducer(storage=storage, records=records, verify_package=verify_package)
