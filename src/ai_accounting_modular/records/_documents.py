"""Current final facts are published through one atomic manifest switch."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

from ai_accounting_modular.contracts.public import (
    ContractError,
    analyze_fact,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from ._changes import _missing_sources, note_current
from ._sources import atomic_write, controlled_path, issue_source_ids
from ._types import DocumentSnapshot, PreparedDocuments, ShardTarget

FORMAT = "ai-accounting-standard/1"
FIXED_KEYS = frozenset({"sources", "objects", "work", "notes"})
_FACT_KEY = re.compile(
    r"facts:(operations|people|assets|financing|investment|money|taxes|opening|reports):[0-9]{4}-(0[1-9]|1[0-2])"
)
_PART_FILE = re.compile(r"[a-z]+(?:--[0-9]{4}-[0-9]{2})?--[0-9a-f]{64}\.json")


def validate_key(key):
    if key == "company":
        return key
    if not isinstance(key, str) or (key not in FIXED_KEYS and _FACT_KEY.fullmatch(key) is None):
        raise ContractError("invalid_input", "标准文档范围必须是固定片或明确的业务领域与月份。")
    if key.startswith("facts:"):
        validate_contract("core.schema.json", "Month", key.rsplit(":", 1)[1])
    return key


def mark_targets(keys, uow):
    if uow.next_head <= uow.head:
        raise ContractError("invalid_operation", "文档目标只能随正式业务事务推进。")
    keys = set(validate_key(key) for key in keys)
    repository = uow.repository("records")
    if repository.document_set() is None:
        keys.update(FIXED_KEYS)
    if keys:
        repository.mark_targets(sorted(keys), uow.next_head)


def is_pending(target):
    return target["published_head"] is None or target["wanted_head"] > target["published_head"]


def ensure_targets(uow, root):
    """Initialize or repair derived documents at the saved business head."""
    status = document_status(uow, root)
    if status["state"] == "ready" or status["pending_scopes"]:
        return
    request_repair(uow)


def request_repair(uow):
    """Only derived publication state is reset; saved accounting stays unchanged."""
    repository = uow.repository("records")
    targets = repository.targets()
    if any(row["wanted_head"] > uow.head for row in targets):
        raise ContractError("storage_corrupt", "标准文档目标不能超过已保存的业务版本。")
    keys = FIXED_KEYS | {"company"} | {row["shard_key"] for row in targets}
    repository.request_documents(sorted(keys), uow.head)


def _standard_object(obj):
    obj = loads_json(canonical_json(obj))
    if "seq" in obj or "content_digest" in obj:
        validate_contract("core.schema.json", "CurrentObject", obj)
        del obj["seq"]
        del obj["content_digest"]
    validate_contract("core.schema.json", "DocumentObject", obj)
    if "evidence_ids" in obj:
        obj["evidence_ids"] = sorted(set(obj["evidence_ids"]))
    return obj


def part_bytes(part):
    return (canonical_json(part) + "\n").encode("utf-8")


def capture_documents(company_identity, facts, objects, read, root, scopes=None):
    validate_contract("core.schema.json", "CompanyIdentity", company_identity)
    repository = read.repository("records")
    targets = tuple(
        ShardTarget(
            row["shard_key"],
            row["wanted_head"],
            row["published_head"],
            row["current_path"],
            row["current_digest"],
        )
        for row in repository.targets()
    )
    indexed = {target.key: target for target in targets}
    selected = (
        set(
            target.key
            for target in targets
            if target.published_head is None or target.wanted_head > target.published_head
        )
        if scopes is None
        else set(scopes)
    )
    for key in selected:
        validate_key(key)
        if key not in indexed:
            raise ContractError("invalid_operation", "文档范围尚未在正式业务事务中建立目标。")
    current = repository.document_set()
    base = None
    if current is not None:
        try:
            base = loads_json(controlled_path(root, current["manifest_path"]).read_bytes())
            verify_manifest(root, base, parts=False)
            # A process may have switched the manifest before confirming it in SQLite.
            # The same target capture below regenerates the pending parts and confirms once.
        except (OSError, ContractError):
            if selected != set(indexed):
                raise ContractError(
                    "documents_corrupt", "旧清单不可复用，需显式重建全部标准文档。"
                ) from None
            base = None
    if base is None and not FIXED_KEYS <= selected:
        raise ContractError("invalid_operation", "首次发布必须包含全部固定文档片。")
    parts, used_sources = {}, set()
    groups = defaultdict(list)
    seen = set()
    for value in facts:
        analysis = analyze_fact(value)
        fact = analysis.fact
        if fact["business_id"] in seen:
            raise ContractError("invalid_input", "同一最终业务不能出现在多个标准分片。")
        seen.add(fact["business_id"])
        fact["evidence_ids"] = sorted(set(fact["evidence_ids"]))
        used_sources.update(analysis.source_ids)
        key = validate_key("facts:" + fact["kind"].split(".")[0] + ":" + fact["period"])
        if key not in selected:
            raise ContractError("invalid_input", "事实导出必须只覆盖本次选定的完整业务分片。")
        groups[key].append(fact)
    for key, values in groups.items():
        parts[key] = {
            "format": FORMAT,
            "key": key,
            "facts": sorted(values, key=lambda fact: fact["business_id"]),
        }
    if "objects" in selected and objects is None:
        raise ContractError("invalid_input", "对象片待更新时必须导出完整当前对象集合。")
    if "objects" not in selected and objects:
        raise ContractError("invalid_input", "未变化的对象片应复用，不重复导出对象。")
    document_objects = sorted(
        (_standard_object(obj) for obj in (objects or ())), key=lambda obj: obj["object_id"]
    )
    if len({obj["object_id"] for obj in document_objects}) != len(document_objects):
        raise ContractError("invalid_input", "标准文档中的对象身份不能重复。")
    for obj in document_objects:
        used_sources.update(obj.get("evidence_ids", ()))
    if "objects" in selected:
        parts["objects"] = {"format": FORMAT, "key": "objects", "objects": document_objects}
    issues = repository.issues() if "work" in selected else []
    for issue in issues:
        validate_contract("core.schema.json", "CurrentIssue", issue)
        used_sources.update(issue_source_ids(issue))
    if "work" in selected:
        parts["work"] = {"format": FORMAT, "key": "work", "issues": issues}
    notes = []
    note_rows = repository.notes() if "notes" in selected else {}
    note_uses = repository.uses_many("note", tuple(note_rows))
    for scope, row in note_rows.items():
        current_note = note_current(row, repository, note_uses[scope])
        if current_note["state"] == "current":
            note = {key: current_note[key] for key in ("scope", "text", "evidence_ids")}
            notes.append(note)
            used_sources.update(note["evidence_ids"])
    if "notes" in selected:
        parts["notes"] = {"format": FORMAT, "key": "notes", "notes": notes}
    if "sources" in selected:
        used_sources.update(repository.used_source_ids())
    sources = repository.sources(tuple(sorted(used_sources)))
    _missing_sources(used_sources, sources)
    for source in sources.values():
        validate_contract("core.schema.json", "Source", source)
    if "sources" in selected:
        parts["sources"] = {
            "format": FORMAT,
            "key": "sources",
            "sources": [sources[source_id] for source_id in sorted(sources)],
        }
    for key, part in parts.items():
        validate_contract("core.schema.json", "StandardPart", part)
        target = indexed.get(key)
        digest = hashlib.sha256(part_bytes(part)).hexdigest()
        if target is None or (
            target.wanted_head == target.published_head and target.current_digest != digest
        ):
            raise ContractError("invalid_operation", "标准文档内容变化却没有同步推进文档目标。")
    return DocumentSnapshot(
        read.company_id,
        read.database_id,
        read.head,
        Path(root).absolute(),
        canonical_json(company_identity),
        targets,
        tuple(sorted(selected)),
        current["published_set_id"] if current else None,
        canonical_json(base) if base is not None else None,
        tuple(canonical_json(parts[key]) for key in sorted(parts)),
    )


def _part_name(key, digest):
    return (key.removeprefix("facts:").replace(":", "--")) + "--" + digest + ".json"


def _markdown(manifest):
    company = manifest["company_identity"]
    lines = [
        "# " + company["legal_name"] + " · 标准业务文档",
        "",
        "这里保存当前正确事实，用于查阅及简化重建。机器读取入口是 current.json。"
        "本说明由同一套 JSON 生成。",
        "",
        "统一社会信用代码：" + company["credit_code"],
        "建账月份：" + company["start_period"],
        "已进入的工作末月：" + company.get("working_period_to", company["start_period"]),
        "当前集合：`" + manifest["set_id"] + "`",
        "",
        "## 当前内容",
        "",
    ]
    for shard in manifest["shards"]:
        lines.append("- [" + shard["key"] + "](" + shard["path"].removeprefix("standard/") + ")")
    lines.extend(
        [
            "",
            "已替换的开放期错误、旧编号及操作过程不在这里保存；真实收付款、退款和损失仍是业务事实。",
            "",
        ]
    )
    return "\n".join(lines)


def prepare_documents(snapshot):
    base = (
        loads_json(snapshot.base_manifest_json) if snapshot.base_manifest_json is not None else None
    )
    shards = (
        [shard for shard in base["shards"] if shard["key"] not in snapshot.scopes] if base else []
    )
    for raw in snapshot.parts_json:
        part = loads_json(raw)
        validate_contract("core.schema.json", "StandardPart", part)
        data = part_bytes(part)
        digest = hashlib.sha256(data).hexdigest()
        relative = "standard/parts/" + _part_name(part["key"], digest)
        path = controlled_path(snapshot.company_root, relative, parents=True)
        if not path.exists() or path.read_bytes() != data:
            atomic_write(snapshot.company_root, relative, data)
        shards.append(
            {"key": part["key"], "path": relative, "sha256": digest, "byte_size": len(data)}
        )
    shards.sort(key=lambda shard: shard["key"])
    manifest = {
        "format": FORMAT,
        "company_identity": loads_json(snapshot.company_identity_json),
        "shards": shards,
    }
    manifest["set_id"] = content_digest(manifest)
    validate_contract("core.schema.json", "DocumentManifest", manifest)
    return PreparedDocuments(snapshot, canonical_json(manifest), _markdown(manifest))


def verify_manifest(root, manifest, *, parts=True):
    validate_contract("core.schema.json", "DocumentManifest", manifest)
    content = {key: value for key, value in manifest.items() if key != "set_id"}
    if content_digest(content) != manifest["set_id"]:
        raise ContractError("documents_corrupt", "标准文档清单内容与集合标识不一致。")
    seen = set()
    for shard in manifest["shards"]:
        if shard["key"] in seen:
            raise ContractError("documents_corrupt", "标准文档清单包含重复分片。")
        seen.add(shard["key"])
        expected_path = "standard/parts/" + _part_name(validate_key(shard["key"]), shard["sha256"])
        if shard["path"] != expected_path:
            raise ContractError("documents_corrupt", "标准文档分片位置不符合当前格式。")
        path = controlled_path(root, shard["path"])
        if parts:
            try:
                raw = path.read_bytes()
            except FileNotFoundError as exc:
                raise ContractError("documents_missing", "标准文档分片缺失。") from exc
            if len(raw) != shard["byte_size"] or hashlib.sha256(raw).hexdigest() != shard["sha256"]:
                raise ContractError("documents_corrupt", "标准文档分片内容与清单不一致。")
            part = loads_json(raw)
            validate_contract("core.schema.json", "StandardPart", part)
            if part["key"] != shard["key"]:
                raise ContractError("documents_corrupt", "标准文档分片归属与清单不一致。")


def publish_documents(prepared, uow, root):
    if not isinstance(prepared, PreparedDocuments):
        raise ContractError("invalid_input", "发布必须使用已经生成的严格标准文档。")
    snapshot = prepared.snapshot
    if (uow.company_id, uow.database_id, Path(root).absolute()) != (
        snapshot.company_id,
        snapshot.database_id,
        snapshot.company_root,
    ):
        raise ContractError("conflict", "标准文档不属于当前公司或资料根。")
    repository = uow.repository("records")
    expected = {target.key: target.wanted_head for target in snapshot.targets}
    actual = {row["shard_key"]: row["wanted_head"] for row in repository.targets()}
    if actual != expected:
        raise ContractError("conflict", "生成期间当前事实已经变化，旧文档不能覆盖新目标。")
    manifest = prepared.manifest
    current = repository.document_set()
    actual_set_id = current["published_set_id"] if current else None
    base_set_id = snapshot.confirmed_set_id
    if actual_set_id != base_set_id:
        if actual_set_id == manifest["set_id"]:
            return manifest
        raise ContractError("conflict", "已有更新的完整清单发布，请基于当前集合重新生成。")
    verify_manifest(root, manifest, parts=False)
    changed = set(snapshot.scopes)
    for shard in manifest["shards"]:
        if shard["key"] in changed:
            raw = controlled_path(root, shard["path"]).read_bytes()
            if len(raw) != shard["byte_size"] or hashlib.sha256(raw).hexdigest() != shard["sha256"]:
                raise ContractError("documents_corrupt", "待发布分片与已生成的内容不一致。")
    atomic_write(root, "standard/README.md", prepared.markdown.encode("utf-8"))
    atomic_write(root, "standard/current.json", part_bytes(manifest))
    repository.confirm_documents(
        tuple(target for target in snapshot.targets if target.key in snapshot.scopes), manifest
    )
    return manifest


def cleanup_parts(root, manifest, protected=()):
    current = {shard["path"] for shard in manifest["shards"]} | set(protected)
    directory = controlled_path(root, "standard/parts")
    for path in directory.iterdir():
        relative = "standard/parts/" + path.name
        if _PART_FILE.fullmatch(path.name) and relative not in current:
            controlled_path(root, relative).unlink()


def document_status(read, root):
    repository = read.repository("records")
    current = repository.document_set()
    targets = repository.targets()
    pending = [row["shard_key"] for row in targets if is_pending(row)]
    errors = [
        {
            "scope": row["shard_key"],
            "code": row["last_error_code"],
            "message": row["last_error_message"],
        }
        for row in targets
        if row["last_error_code"] is not None
    ]
    result = {
        "company_id": read.company_id,
        "state": "pending",
        "set_id": current["published_set_id"] if current else None,
        "pending_scopes": pending,
        "errors": errors,
    }
    if current and not pending:
        try:
            path = controlled_path(root, current["manifest_path"])
            manifest = loads_json(path.read_bytes())
            verify_manifest(root, manifest, parts=False)
            if manifest["set_id"] != current["published_set_id"]:
                raise ContractError("documents_corrupt", "标准文档清单尚未与当前数据库确认一致。")
        except (OSError, ContractError) as exc:
            result["errors"] = [
                {
                    "scope": "documents",
                    "code": getattr(exc, "code", "documents_missing"),
                    "message": "当前标准文档不可用，需要按当前事实重新归档。",
                }
            ]
        else:
            result["state"] = "ready"
            result["manifest_path"] = str(path)
    return validate_contract("core.schema.json", "DocumentsResult", result)
