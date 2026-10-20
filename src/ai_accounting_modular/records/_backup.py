"""Exact material capture and controlled temporary roots; no packing or ledger reads."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    UnitOfWork,
    canonical_json,
    loads_json,
    validate_contract,
)

from . import _documents, _sources
from ._types import BackupMaterials

_FORMAT = "ai-accounting-backup-capture/1"
_MARKER = "capture.json"


@dataclass(frozen=True)
class PreparedMaterials:
    materials: BackupMaterials
    paths: tuple[str, ...]
    mutable_files: tuple[tuple[str, bytes], ...]
    manifest_json: str


def _capture_path(root, capture_id):
    validate_contract("core.schema.json", "Id", capture_id)
    return _sources.controlled_path(root, "runtime/tasks/" + capture_id)


def _identity(read, capture_id):
    return {
        "format": _FORMAT,
        "company_id": read.company_id,
        "database_id": read.database_id,
        "capture_id": capture_id,
    }


def _marker(root, read, capture_id):
    path = _capture_path(root, capture_id)
    try:
        with _sources.controlled_path(path, _MARKER).open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096 or loads_json(raw) != _identity(read, capture_id):
            raise ValueError("capture_identity")
    except (OSError, ValueError, ContractError) as exc:
        raise ContractError("capture_invalid", "临时捕获标识与当前公司数据库不一致。") from exc
    return path


def _file_input(source_root, relative, archive_path, digest, size):
    return validate_contract(
        "core.schema.json",
        "CapturedFile",
        {
            "source_root": source_root,
            "relative_path": relative,
            "archive_path": archive_path,
            "sha256": digest,
            "byte_size": size,
        },
    )


def prepare(root, read, capture_id):
    if isinstance(read, UnitOfWork) or read.view != "current" or read.as_of is not None:
        raise ContractError("invalid_operation", "备份资料捕获必须使用当前只读一致点。")
    capture_root = _capture_path(root, capture_id)
    if capture_root.exists():
        raise ContractError("target_exists", "捕获位置已经存在，不能覆盖已有文件。")
    repository = read.repository("records")
    current = repository.document_set()
    targets = repository.targets()
    if current is None or any(row["wanted_head"] != row["published_head"] for row in targets):
        raise ContractError("documents_pending", "标准业务文档尚未完整归档，不能开始备份。")
    try:
        if current["manifest_path"] != "standard/current.json":
            raise ValueError("manifest_path")
        raw_manifest = _sources.controlled_path(root, current["manifest_path"]).read_bytes()
        manifest = loads_json(raw_manifest)
        _documents.verify_manifest(root, manifest, parts=False)
        if manifest["set_id"] != current["published_set_id"]:
            raise ValueError("unconfirmed_manifest")
        shards = {item["key"]: item for item in manifest["shards"]}
        if set(shards) - {row["shard_key"] for row in targets}:
            raise ValueError("untracked_shard")
        for row in targets:
            item = shards.get(row["shard_key"], {})
            if (row["current_path"], row["current_digest"]) != (
                item.get("path"),
                item.get("sha256"),
            ):
                raise ValueError("target_manifest_mismatch")
        raw_readme = _sources.controlled_path(root, "standard/README.md").read_bytes()
        if raw_readme != _documents._markdown(manifest).encode("utf-8"):
            raise ValueError("readme_manifest_mismatch")
    except (OSError, ValueError, ContractError) as exc:
        raise ContractError(
            "documents_corrupt", "标准文档、当前目标和数据库确认集合不一致，不能混合备份。"
        ) from exc
    inputs = {}
    for item in manifest["shards"]:
        inputs[item["path"]] = _file_input(
            "company", item["path"], item["path"], item["sha256"], item["byte_size"]
        )
    for source in repository.all_sources():
        validate_contract("core.schema.json", "Source", source)
        if source["kind"] == "owner_statement":
            if (
                _sources.source_text(source["text"]) != source["text"]
                or _sources.text_digest(source["text"]) != source["sha256"]
            ):
                raise ContractError("storage_corrupt", "已登记来源说明的正文与摘要不一致。")
            continue
        path = source["relative_path"]
        if not path.startswith("originals/"):
            raise ContractError("storage_corrupt", "已登记原件不在公司原件目录。")
        _sources.controlled_path(root, path)
        item = _file_input("company", path, path, source["sha256"], source["byte_size"])
        if path in inputs and inputs[path] != item:
            raise ContractError("storage_corrupt", "相同备份位置对应了不同原字节。")
        inputs[path] = item
    mutable = (("standard/current.json", raw_manifest), ("standard/README.md", raw_readme))
    for path, raw in mutable:
        inputs[path] = _file_input("capture", path, path, hashlib.sha256(raw).hexdigest(), len(raw))
    materials = BackupMaterials(
        read.company_id,
        read.database_id,
        read.head,
        capture_id,
        capture_root,
        manifest["set_id"],
        canonical_json(manifest["company_identity"]),
        canonical_json([inputs[key] for key in sorted(inputs)]),
    )
    return PreparedMaterials(
        materials,
        tuple(
            sorted(
                item["relative_path"]
                for item in inputs.values()
                if item["source_root"] == "company"
            )
        ),
        mutable,
        canonical_json(manifest),
    )


def check_inputs(root, capture_root, inputs):
    """Files owns full byte hashing; restore exact locations before its validation."""
    for item in inputs:
        base = root if item["source_root"] == "company" else capture_root
        path = _sources.controlled_path(base, item["relative_path"])
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise ContractError("capture_missing", "保存计划所需的精确输入已经缺失。") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ContractError("invalid_path", "备份输入必须是受控根内的普通文件。")
        if info.st_size != item["byte_size"]:
            raise ContractError("capture_invalid", "保存计划所需输入的字节数已经变化。")


def create(root, read, prepared):
    materials = prepared.materials
    path = _sources.controlled_path(root, "runtime/tasks/" + materials.capture_id, parents=True)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise ContractError("target_exists", "捕获位置已经存在，不能覆盖已有文件。") from exc
    _sources.atomic_write(
        path, _MARKER, (canonical_json(_identity(read, materials.capture_id)) + "\n").encode()
    )
    for relative, raw in prepared.mutable_files:
        _sources.atomic_write(path, relative, raw)


def restore(root, read, plan):
    plan = validate_contract("core.schema.json", "BackupPlan", loads_json(canonical_json(plan)))
    if (plan["company_id"], plan["database_id"]) != (read.company_id, read.database_id):
        raise ContractError("conflict", "备份输入计划不属于当前公司数据库。")
    if (plan["manifest"]["company_id"], plan["manifest"]["database_id"]) != (
        read.company_id,
        read.database_id,
    ):
        raise ContractError("conflict", "备份清单不属于当前公司数据库。")
    capture_root = _marker(root, read, plan["capture_id"])
    paths = set()
    for item in plan["inputs"]:
        if item["source_root"] == "company":
            path = item["relative_path"]
            if not (path.startswith("originals/") or path.startswith("standard/parts/")):
                raise ContractError("invalid_path", "公司备份输入只允许固定原件和标准分片。")
            _sources.controlled_path(root, path)
            paths.add(path)
    return capture_root, tuple(sorted(paths)), tuple(plan["inputs"])


def capture_ids(root, read):
    parent = _sources.controlled_path(root, "runtime/tasks")
    if not parent.exists():
        return ()
    result = []
    for child in parent.iterdir():
        try:
            validate_contract("core.schema.json", "Id", child.name)
            _marker(root, read, child.name)
        except ContractError:
            continue
        result.append(child.name)
    return tuple(sorted(result))


def _remove_tree(path, *, capture_root=False):
    for child in path.iterdir():
        if capture_root and child.name == _MARKER:
            continue
        _sources.controlled_path(path, child.name)
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    if not capture_root:
        path.rmdir()


def discard(root, read, capture_id, *, keep_capture):
    if type(keep_capture) is not bool:
        raise ContractError("invalid_input", "捕获清理须明确是否仍有输入租约。")
    path = _capture_path(root, capture_id)
    if not path.exists():
        return True
    _marker(root, read, capture_id)
    if keep_capture:
        return False
    # Keep the identity until every payload has gone. A locked/bad child must
    # not turn a partially cleaned capture into an unidentifiable directory.
    _remove_tree(path, capture_root=True)
    marker = _sources.controlled_path(path, _MARKER)
    marker.unlink()
    try:
        path.rmdir()
    except OSError:
        if path.is_dir():
            _sources.atomic_write(
                path, _MARKER, (canonical_json(_identity(read, capture_id)) + "\n").encode()
            )
        raise
    return True
