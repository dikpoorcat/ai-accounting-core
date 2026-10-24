"""Check exact archive bytes, physical database structure and each owner's saved facts."""

from __future__ import annotations

import hashlib
import stat
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.storage.public import (
    company_structure_contract,
    open_database_unit,
    restore_database_copy,
    upgrade_company_database,
    verify_database,
)

from . import _documents
from ._backup_scope import CONTRACT_PATH, DATABASE_PATH, MANIFEST_PATH, backup_scope_head
from ._context import DomainRegistry
from ._snapshot import SnapshotVerifier
from ._target_paths import beneath

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ExtractedBackup:
    """Strict archive structure and exact extracted bytes, before owner verification."""

    company_root: Path
    _manifest_json: str
    sha256: str

    @property
    def manifest(self):
        return loads_json(self._manifest_json)


@dataclass(frozen=True)
class VerifiedBackup(ExtractedBackup):
    """The extracted company also passed complete physical and owner verification."""


def _bad(message):
    raise ContractError("backup_corrupt", message)


def _copy_archive(stream, destination):
    stream.seek(0)
    digest = hashlib.sha256()
    with destination.open("xb") as copied:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
            copied.write(chunk)
    return digest.hexdigest()


def _entries(package, root):
    result, folded = {}, set()
    for entry in package.infolist():
        name = entry.filename
        beneath(root, name)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            entry.is_dir()
            or entry.orig_filename != name
            or mode not in (0, stat.S_IFREG)
            or name.casefold() in folded
            or entry.flag_bits & 1
        ):
            _bad("备份包含重复位置、目录、链接或加密条目。")
        folded.add(name.casefold())
        result[name] = entry
    if MANIFEST_PATH not in result:
        _bad("备份缺少完整清单。")
    return result


def _manifest(package, entries):
    manifest = loads_json(package.read(entries[MANIFEST_PATH]))
    validate_contract("core.schema.json", "BackupManifest", manifest)
    backup_scope_head(manifest)
    listed = {item["path"]: item for item in manifest["files"]}
    if (
        not {DATABASE_PATH, CONTRACT_PATH, "standard/current.json", "standard/README.md"}
        <= listed.keys()
    ):
        _bad("完整备份缺少数据库、结构合同、标准清单或标准说明。")
    if set(entries) != set(listed) | {MANIFEST_PATH} or MANIFEST_PATH in listed:
        _bad("备份内文件与清单不完全对应。")
    if {item["path"] for item in listed.values() if item["kind"] == "contract"} != {CONTRACT_PATH}:
        _bad("备份必须包含唯一固定位置的公司结构合同。")
    for path, item in listed.items():
        if (
            (item["kind"] == "original" and not path.startswith("originals/"))
            or (item["kind"] == "standard" and not path.startswith("standard/"))
            or entries[path].file_size != item["byte_size"]
        ):
            _bad("备份文件的种类、位置或大小与清单不一致。")
    return manifest


def _extract(package, entries, manifest, root):
    for item in manifest["files"]:
        path = beneath(root, item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with package.open(entries[item["path"]]) as source, path.open("xb") as output:
            while chunk := source.read(CHUNK_SIZE):
                size += len(chunk)
                if size > item["byte_size"]:
                    _bad("备份解出的资料超出清单大小。")
                digest.update(chunk)
                output.write(chunk)
        if (digest.hexdigest(), size) != (item["sha256"], item["byte_size"]):
            _bad("备份文件原字节与清单摘要不一致。")


def structure_version(root):
    """Accept only the exact packaged current or explicitly archived company contract."""
    saved = canonical_json(loads_json(beneath(root, CONTRACT_PATH).read_bytes()))
    for version in (None, 1):
        contract = company_structure_contract(version=version)
        if saved == canonical_json(contract):
            return contract["version"]
    _bad("备份结构合同不属于当前内核或明确封存的上一公司结构。")


@contextmanager
def _current_verification_database(database, version, fingerprint):
    if version == company_structure_contract()["version"]:
        yield database
        return
    # The restored source stays exact; only a disposable independent copy is upgraded.
    with TemporaryDirectory(prefix="accounting-backup-upgrade-check-") as temporary:
        candidate = Path(temporary) / "company.sqlite3"
        restore_database_copy(database, candidate, structure_version=version)
        upgrade_company_database(candidate, expected_structure_fingerprint=fingerprint)
        yield candidate


class BackupVerifier:
    def __init__(self, *, identity, ledger, records, domains, repositories):
        self._snapshot = SnapshotVerifier(identity, ledger, records, DomainRegistry(domains))
        self._repositories = tuple(repositories)

    def verify_snapshot(self, read_context):
        return self._snapshot.verify(read_context)

    def __call__(self, stream):
        with self.open(stream) as verified:
            return verified.manifest

    @contextmanager
    def open(self, stream):
        with self.extract(stream) as extracted:
            self.verify_extracted(extracted.company_root, extracted.manifest)
            yield VerifiedBackup(extracted.company_root, extracted._manifest_json, extracted.sha256)

    @contextmanager
    def extract(self, stream):
        """Check every archive byte and keep the independent extraction alive."""
        try:
            with TemporaryDirectory(prefix="accounting-backup-check-") as temporary:
                scratch = Path(temporary)
                archive, root = scratch / "package.zip", scratch / "company"
                root.mkdir()
                raw_digest = _copy_archive(stream, archive)
                with zipfile.ZipFile(archive) as package:
                    entries = _entries(package, root)
                    manifest = _manifest(package, entries)
                    _extract(package, entries, manifest, root)
                yield ExtractedBackup(root, canonical_json(manifest), raw_digest)
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            NotImplementedError,
            EOFError,
            zlib.error,
        ) as error:
            raise ContractError("backup_corrupt", "备份不是可完整读取的有效ZIP文件。") from error

    def verify_extracted(self, root, manifest):
        """Fully verify the actual extracted or copied target against its manifest."""
        version = structure_version(root)
        database = beneath(root, DATABASE_PATH)
        physical = (
            verify_database(database)
            if version == company_structure_contract()["version"]
            else verify_database(database, structure_version=version)
        )
        if (
            physical.company_id,
            physical.database_id,
            physical.head,
            physical.structure_fingerprint,
        ) != (
            manifest["company_id"],
            manifest["database_id"],
            manifest["database_head"],
            manifest["structure_fingerprint"],
        ):
            _bad("备份数据库的公司身份、保存版本或结构与清单不一致。")
        with _current_verification_database(
            database, version, physical.structure_fingerprint
        ) as current:
            with open_database_unit(
                current, repositories=self._repositories, company_root=root
            ) as read:
                if _documents.company_identity(read) != manifest["company_identity"]:
                    _bad("备份数据库的公司信息或工作末月与清单不一致。")
                records = self.verify_snapshot(read)
        if records.document_set_id != manifest["document_set_id"]:
            _bad("备份标准业务文档集合与数据库或清单不一致。")
        expected = {
            item.relative_path: (item.kind, item.sha256, item.byte_size) for item in records.files
        }
        actual = {
            item["path"]: (item["kind"], item["sha256"], item["byte_size"])
            for item in manifest["files"]
            if item["kind"] not in {"database", "contract"}
        }
        if actual != expected:
            _bad("备份没有精确保全当前标准文档和所有已登记原件。")
        return physical


def create_backup_verifier(*, identity, ledger, records, domains, repositories):
    return BackupVerifier(
        identity=identity,
        ledger=ledger,
        records=records,
        domains=domains,
        repositories=repositories,
    )
