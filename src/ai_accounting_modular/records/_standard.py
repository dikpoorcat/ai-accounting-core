"""Offline final-material input and checked copying; no accounting reconstruction."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    fact_source_ids,
    loads_json,
    validate_contract,
)

from . import _documents, _sources
from ._types import StandardBundle, StandardFile


def _bad(message):
    raise ContractError("documents_corrupt", message)


def _bytes(root, relative):
    path = _sources.controlled_path(root, relative)
    try:
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _bad("标准资料必须是普通文件。")
            return stream.read()
    except FileNotFoundError as exc:
        raise ContractError("documents_missing", "标准资料缺失：" + relative) from exc
    except OSError as exc:
        raise ContractError("source_unavailable", "标准资料暂时无法读取：" + relative) from exc


def _file(relative, raw):
    return StandardFile(relative, hashlib.sha256(raw).hexdigest(), len(raw), "standard")


def _unique(items, field, meaning):
    seen = set()
    for item in items:
        key = item[field]
        if key in seen:
            _bad(meaning + "身份重复：" + key)
        seen.add(key)


def _validate_parts(manifest, parts, known_facts=None):
    if not _documents.FIXED_KEYS <= {part["key"] for part in parts}:
        _bad("完整标准文档必须包含对象和待办固定片。")
    collections = {name: [] for name in ("facts", "objects", "sources", "issues", "notes")}
    for part in parts:
        expected_field = (
            part["key"].split(":", 1)[0]
            if part["key"].startswith(("facts:", "notes:", "sources:"))
            else {
                "objects": "objects",
                "sources": "sources",
                "work": "issues",
                "notes": "notes",
            }.get(part["key"])
        )
        if expected_field is None or expected_field not in part:
            _bad("标准分片的内容类型与归属不一致。")
        _documents.validate_part_key(part)
        if part["key"].startswith("facts:"):
            if known_facts is not None and (
                not part["facts"]
                or [fact["business_id"] for fact in part["facts"]]
                != sorted(fact["business_id"] for fact in part["facts"])
            ):
                _bad("当前事实分片必须非空并按完整业务身份顺序保存。")
            for fact in part["facts"]:
                expected = "facts:" + fact["kind"].split(".")[0] + ":" + fact["period"]
                if expected != part["key"]:
                    _bad("最终事实不属于标明的业务领域或月份。")
        for name in collections:
            collections[name].extend(part.get(name, ()))
    for name, field, meaning in (
        ("facts", "business_id", "业务"),
        ("objects", "object_id", "对象"),
        ("sources", "source_id", "来源"),
        ("issues", "issue_id", "待办"),
        ("notes", "scope", "说明范围"),
    ):
        _unique(collections[name], field, meaning)
    required = set()
    for fact in collections["facts"]:
        if known_facts is None:
            required.update(fact_source_ids(fact))
        else:
            proof = known_facts.get(fact["business_id"])
            if proof is None or canonical_json(fact) != proof[0]:
                _bad("标准业务文档没有精确对应完整的当前事实。")
            required.update(proof[1])
    if known_facts is not None and {fact["business_id"] for fact in collections["facts"]} != set(
        known_facts
    ):
        _bad("标准业务文档遗漏了数据库中的当前事实。")
    business_ids = {fact["business_id"] for fact in collections["facts"]}
    if any(
        note["scope"].startswith("business:")
        and note["scope"].removeprefix("business:") not in business_ids
        for note in collections["notes"]
    ):
        _bad("业务说明必须对应同一标准文档集合中的当前业务事实。")
    for name in ("objects", "notes"):
        for item in collections[name]:
            required.update(item.get("evidence_ids", ()))
    for issue in collections["issues"]:
        required.update(_sources.issue_source_ids(issue))
    available = {source["source_id"] for source in collections["sources"]}
    if required - available:
        raise ContractError(
            "documents_corrupt",
            "标准文档缺少显式引用的来源。",
            fact_issues=[
                {
                    "code": "source_not_found",
                    "field": "/sources",
                    "meaning": "最终事实、对象、待办或说明引用的原始来源",
                    "message": "缺少来源：" + source_id,
                    "allowed_precision": ["identity"],
                    "reusable_sources": [],
                    "blocking_scope": "business",
                }
                for source_id in sorted(required - available)
            ],
        )
    identity = manifest["company_identity"]
    latest = max(
        (fact["period"] for fact in collections["facts"]), default=identity["start_period"]
    )
    if identity.get("working_period_to", max(identity["start_period"], latest)) < max(
        identity["start_period"], latest
    ):
        _bad("已进入的工作末月不能早于建账月或最终事实末月。")
    return collections["sources"]


def open_standard_bundle(manifest_path: Path | str) -> StandardBundle:
    return _read_standard_bundle(manifest_path)[0]


def _read_standard_bundle(manifest_path, *, known_facts=None):
    path = Path(manifest_path).absolute()
    if path.name != "current.json" or path.parent.name != "standard":
        raise ContractError("invalid_path", "请明确选择公司standard/current.json完整清单。")
    root = path.parent.parent
    raw = _bytes(root, "standard/current.json")
    manifest = loads_json(raw)
    _documents.verify_manifest(root, manifest, parts=False)
    parts, files = [], [_file("standard/current.json", raw)]
    for shard in manifest["shards"]:
        raw_part = _bytes(root, shard["path"])
        item = _file(shard["path"], raw_part)
        if (item.sha256, item.byte_size) != (shard["sha256"], shard["byte_size"]):
            _bad("标准分片的原字节与清单摘要或大小不一致。")
        part = loads_json(raw_part)
        # A matching database proof saves source analysis, never this strict wire check.
        validate_contract("core.schema.json", "StandardPart", part)
        if part["key"] != shard["key"]:
            _bad("标准分片的归属与清单不一致。")
        parts.append(part)
        files.append(item)
    sources = _validate_parts(manifest, parts, known_facts)
    for source in sources:
        _sources.check_source(root, source)
        if source["kind"] == "file":
            files.append(
                StandardFile(
                    source["relative_path"], source["sha256"], source["byte_size"], "original"
                )
            )
    readme = _sources.controlled_path(root, "standard/README.md")
    if readme.exists():
        readme_raw = _bytes(root, "standard/README.md")
        if readme_raw != _documents._markdown(manifest).encode("utf-8"):
            _bad("标准文档说明与当前清单不一致。")
        files.append(_file("standard/README.md", readme_raw))
    paths = [item.relative_path.casefold() for item in files]
    if len(paths) != len(set(paths)):
        _bad("标准资料含有重复或大小写冲突的文件位置。")
    bundle = StandardBundle(
        path,
        root,
        files[0].sha256,
        tuple(sorted(files, key=lambda item: item.relative_path)),
        canonical_json(manifest),
        tuple(canonical_json(part) for part in sorted(parts, key=lambda item: item["key"])),
    )
    return bundle, tuple(sorted(parts, key=lambda item: item["key"]))


def _copy_file(bundle, target, item):
    source = _sources.controlled_path(bundle.company_root, item.relative_path)
    destination = _sources.controlled_path(target, item.relative_path, parents=True)
    temporary = None
    try:
        with source.open("rb") as origin:
            if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode):
                raise ContractError("invalid_path", "复制来源必须是受控根内的普通文件。")
            with tempfile.NamedTemporaryFile(
                prefix=".records-copy-", dir=destination.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                digest, size = hashlib.sha256(), 0
                while chunk := origin.read(_sources.CHUNK_SIZE):
                    size += len(chunk)
                    if size > item.byte_size:
                        _bad("复制时资料大小已变化：" + item.relative_path)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        _sources.controlled_path(bundle.company_root, item.relative_path)
        if size != item.byte_size or digest.hexdigest() != item.sha256:
            _bad("复制时资料字节已变化：" + item.relative_path)
        _sources.controlled_path(target, item.relative_path)
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        temporary = None
        return destination
    except FileExistsError as exc:
        raise ContractError("target_exists", "目标资料已经存在，不能覆盖。") from exc
    except FileNotFoundError as exc:
        raise ContractError(
            "documents_missing", "复制所需资料已缺失：" + item.relative_path
        ) from exc
    except OSError as exc:
        raise ContractError(
            "source_unavailable", "资料复制尚未完成：" + item.relative_path
        ) from exc
    finally:
        if temporary is not None:
            try:
                _sources.controlled_path(target, temporary.relative_to(target).as_posix()).unlink(
                    missing_ok=True
                )
            except (ContractError, OSError):
                # A changed target is retained for its coordinator, never followed outside it.
                pass


def _discard_copied(target, created):
    for path, item, identity in reversed(created):
        try:
            _sources.controlled_path(target, item.relative_path)
            if path.is_symlink() or path.is_junction() or _copy_identity(path) != identity:
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest == item.sha256 and _copy_identity(path) == identity:
                path.unlink()
        except (ContractError, OSError):
            # The isolated-target coordinator owns directory-level cleanup.
            pass


def _copy_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def copy_standard_bundle(bundle: StandardBundle, target_root: Path | str) -> None:
    if not isinstance(bundle, StandardBundle):
        raise ContractError("invalid_input", "复制必须使用已经核验的完整标准资料。")
    target = Path(target_root).absolute()
    source = bundle.company_root.resolve()
    if target.resolve().is_relative_to(source) or source.is_relative_to(target.resolve()):
        raise ContractError("invalid_path", "新目标与来源公司目录不能重叠。")
    for directory in ("standard", "originals"):
        path = _sources.controlled_path(target, directory)
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ContractError("target_exists", "目标资料目录已有内容，不能覆盖。")
    target.mkdir(parents=True, exist_ok=True)
    for directory in ("standard", "originals"):
        _sources.controlled_path(target, directory).mkdir(exist_ok=True)
    # The manifest becomes visible only after all of its material inputs passed copying.
    created = []
    try:
        for item in sorted(
            bundle.files, key=lambda item: item.relative_path == "standard/current.json"
        ):
            path = _copy_file(bundle, target, item)
            created.append((path, item, _copy_identity(path)))
        copied = open_standard_bundle(target / "standard/current.json")
        if (copied.manifest_digest, copied.files) != (bundle.manifest_digest, bundle.files):
            _bad("目标资料与已核验的源集合不一致。")
    except BaseException:
        _discard_copied(target, created)
        raise
