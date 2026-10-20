"""Current/previous package publication with recoverable, owned temporary ZIPs."""

from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from zipfile import BadZipFile, ZipFile

from ai_accounting_modular.contracts.public import ContractError, loads_json, validate_contract
from ai_accounting_modular.files.public import ProductionError

from ._backup_io import FileBytes, controlled, copy_bytes, snapshot, sync_directory
from ._backup_scope import MANIFEST_PATH, backup_scope_head


@dataclass(frozen=True)
class Package:
    bytes: FileBytes
    manifest: dict | None
    valid: bool


def _identity(manifest):
    return manifest["company_id"], manifest["database_id"]


def _plain_destination(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ProductionError(
            "backup_collision", "将被覆盖的备份位置不是普通文件。", retryable=False
        )


def _identified(stream):
    """Ownership alone never certifies that a damaged package is usable."""
    try:
        stream.seek(0)
        with ZipFile(stream) as archive:
            candidates = [
                item for item in archive.infolist() if item.filename.casefold() == MANIFEST_PATH
            ]
            if len(candidates) != 1 or candidates[0].filename != MANIFEST_PATH:
                return None
            manifest = loads_json(archive.read(candidates[0]))
            return validate_contract("core.schema.json", "BackupManifest", manifest)
    except (BadZipFile, ContractError, OSError, ValueError, RuntimeError):
        return None


class Rotation:
    def __init__(self, context, verify_package):
        self.root = context.company.backup_directory
        self.identity = context.company.company_id, context.company.database_id
        self.verify_package = verify_package
        self.prefix = ".backup--" + self.identity[0] + "--"
        self.temporary_prefix = self.prefix + "write--"
        self.temporary_pattern = re.compile(re.escape(self.temporary_prefix) + r"[a-f0-9]{32}\.tmp")
        self.pattern = re.compile(
            re.escape(self.prefix) + r"([A-Z0-9]{18})--([a-f0-9]{64})--([a-f0-9]{64})\.zip"
        )

    @staticmethod
    def names(credit_code):
        return credit_code + ".finance-company.zip", credit_code + ".previous.finance-company.zip"

    @contextmanager
    def inspect(self, relative):
        with snapshot(self.root, relative) as raw:
            try:
                manifest = self.verify_package(raw.stream)
                backup_scope_head(manifest)
                value = Package(raw, manifest, True)
            except ContractError:
                value = Package(raw, _identified(raw.stream), False)
            raw.stream.seek(0)
            yield value

    def is_owned(self, package):
        return package.manifest is not None and _identity(package.manifest) == self.identity

    def owned(self, package):
        if not self.is_owned(package):
            raise ProductionError(
                "backup_collision",
                "将被覆盖的位置含有归属不明或其他公司数据库的文件。",
                retryable=False,
            )

    def check_destination(self, relative):
        path = controlled(self.root, relative)
        if path.exists():
            _plain_destination(path)
            with self.inspect(relative) as existing:
                self.owned(existing)

    @staticmethod
    def reject_older(plan, existing):
        if (
            existing is not None
            and existing.valid
            and (existing.manifest["database_head"] > plan["manifest"]["database_head"])
        ):
            raise ProductionError(
                "backup_superseded", "已有更新的一致点备份，较早捕获不能覆盖它。", retryable=False
            )

    @contextmanager
    def current(self, relative):
        path = controlled(self.root, relative)
        if not path.exists():
            yield None
        else:
            _plain_destination(path)
            with self.inspect(relative) as package:
                self.owned(package)
                yield package

    def finish(self, pending, previous):
        os.replace(controlled(self.root, pending), controlled(self.root, previous))
        sync_directory(self.root)

    def cleanup_staging(self):
        """Only Files' company scope lock may remove interrupted publication copies."""
        if not self.root.exists():
            return
        controlled(self.root, "probe")
        removed = False
        for path in self.root.iterdir():
            if self.temporary_pattern.fullmatch(path.name) and stat.S_ISREG(path.lstat().st_mode):
                controlled(self.root, path.name).unlink()
                removed = True
        if removed:
            sync_directory(self.root)

    def copy(self, value, relative):
        return copy_bytes(value, self.root, relative, temporary_prefix=self.temporary_prefix)

    def recover(self):
        if not self.root.exists():
            return
        controlled(self.root, "probe")
        for path in sorted(self.root.iterdir()):
            match = self.pattern.fullmatch(path.name)
            if match is None or not stat.S_ISREG(path.lstat().st_mode):
                continue
            code, wanted, old_digest = match.groups()
            current, previous = self.names(code)
            with self.inspect(path.name) as old:
                if not self.is_owned(old):
                    # A foreign/unknown file is not ours to recover or remove.
                    # Check ownership only if publication must overwrite it.
                    continue
                if not old.valid:
                    controlled(self.root, path.name).unlink()
                    sync_directory(self.root)
                    continue
                if old.bytes.sha256 != old_digest:
                    raise ProductionError(
                        "backup_transition_invalid",
                        "待滚动旧包的完整内容或摘要已经变化。",
                        retryable=False,
                    )
            if not controlled(self.root, current).exists():
                raise ProductionError(
                    "backup_transition_invalid", "待滚动旧包缺少对应当前包。", retryable=False
                )
            with self.inspect(current) as present:
                self.owned(present)
                if present.bytes.sha256 == old_digest:
                    # Installation never happened; the original still exists.
                    controlled(self.root, path.name).unlink()
                    sync_directory(self.root)
                    continue
                # A strict owned header can locate an interrupted installation
                # even when its payload was later damaged. Only the independently
                # verified old package is promoted; present stays untrusted.
                if backup_scope_head(present.manifest) != wanted:
                    raise ProductionError(
                        "backup_transition_invalid",
                        "待滚动旧包与当前包的交接身份不一致。",
                        retryable=False,
                    )
            self.check_destination(previous)
            self.finish(path.name, previous)

    def publish(self, plan, candidate):
        """Called only after Files fences the task under its company scope lock."""
        code = plan["manifest"]["company_identity"]["credit_code"]
        current, previous = self.names(code)
        with self.current(current) as existing:
            self.reject_older(plan, existing)
            # Recovery only completes previous; the protected current snapshot
            # stays exact. A superseded point makes no publication changes.
            self.cleanup_staging()
            self.recover()
            pending = None
            if existing is not None and existing.valid:
                if backup_scope_head(existing.manifest) == plan["scope_head"]:
                    if (existing.bytes.sha256, existing.bytes.byte_size) != (
                        candidate.sha256,
                        candidate.byte_size,
                    ):
                        raise ProductionError(
                            "backup_representation_changed",
                            "同语义备份已安装；请重试并复用其确切原字节。",
                        )
                    return controlled(self.root, current)
                self.check_destination(previous)
                pending = (
                    self.prefix
                    + code
                    + "--"
                    + plan["scope_head"]
                    + "--"
                    + existing.bytes.sha256
                    + ".zip"
                )
                self.check_destination(pending)
                self.copy(existing.bytes, pending)
            # A damaged but strictly identified current is replaced without rolling
            # it into previous. An untouched previous is never a publication gate.
            result = self.copy(candidate, current)
            if pending is not None:
                self.finish(pending, previous)
            return result
