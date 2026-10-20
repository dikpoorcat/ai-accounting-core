"""Prove published final documents and all registered originals against one saved view."""

import hashlib

from ai_accounting_modular.contracts.public import (
    ContractError,
    fact_source_ids,
    loads_json,
)

from . import _documents, _sources
from ._changes import note_current
from ._standard import open_standard_bundle
from ._types import RecordsSnapshot, StandardFile


def _bad(message):
    raise ContractError("storage_corrupt", message)


def _source_uses(facts, objects, issues, notes):
    expected = set()
    for kind, values, key, extract in (
        ("business", facts, "business_id", fact_source_ids),
        ("object", objects, "object_id", lambda value: value.get("evidence_ids", ())),
        ("issue", issues, "issue_id", _sources.issue_source_ids),
        ("note", notes, "scope", lambda value: value["evidence_ids"]),
    ):
        for value in values:
            expected.update((kind, value[key], source_id) for source_id in extract(value))
    return tuple(sorted(expected))


def verify(records, company_identity, facts, objects, read, root, required_source_ids):
    facts, objects = tuple(facts), tuple(objects)
    repository = read.repository("records")
    status = _documents.document_status(read, root)
    if status["state"] != "ready":
        raise ContractError("documents_pending", "保存的账务必须有完整的当前标准业务文档。")
    bundle = open_standard_bundle(root / "standard/current.json")
    if bundle.set_id != status["set_id"] or bundle.company_identity != company_identity:
        _bad("标准文档的公司信息、工作月份或确认集合与数据库不一致。")
    targets = repository.targets()
    if repository.document_set()["manifest_path"] != "standard/current.json" or any(
        row["published_head"] is None
        or not 0 <= row["published_head"] == row["wanted_head"] <= read.head
        for row in targets
    ):
        _bad("标准文档确认入口或范围版本不属于已保存的账务快照。")
    scopes = tuple(row["shard_key"] for row in targets)
    snapshot = _documents.capture_documents(
        company_identity, facts, objects, read, root, scopes=scopes
    )
    expected = {}
    for raw in snapshot.parts_json:
        part = loads_json(raw)
        data = _documents.part_bytes(part)
        expected[part["key"]] = hashlib.sha256(data).hexdigest(), len(data)
    actual = {
        item["key"]: (item["sha256"], item["byte_size"]) for item in bundle.manifest["shards"]
    }
    if actual != expected:
        _bad("标准业务文档没有精确对应完整的当前事实、对象、待办及说明。")
    issues = repository.issues()
    issue_scopes = set(repository.issue_scopes())
    if {issue["scope"] for issue in issues} - issue_scopes:
        _bad("当前待办缺少完整范围身份及集合版本。")
    for scope in sorted(issue_scopes):
        records.current_issues(scope, read)
    notes = tuple(note_current(row, repository) for row in repository.notes().values())
    if repository.all_uses() != _source_uses(facts, objects, issues, notes):
        _bad("当前来源引用索引与业务、对象、待办或说明不一致。")
    sources = repository.all_sources()
    ids = {source["source_id"] for source in sources}
    if set(required_source_ids) - ids:
        _bad("必要的正式采用依据缺少已登记的原始来源。")
    files = {item.relative_path: item for item in bundle.files}
    verified = {source["source_id"]: source for source in bundle.sources}
    for source in sources:
        if verified.get(source["source_id"]) != source:
            _sources.check_source(root, source)
        if source["kind"] == "file":
            item = StandardFile(
                source["relative_path"], source["sha256"], source["byte_size"], "original"
            )
            previous = files.setdefault(item.relative_path, item)
            if previous != item:
                _bad("不同登记来源在同一位置声明了不同内容。")
    if len({path.casefold() for path in files}) != len(files):
        _bad("登记原件与标准文档存在大小写冲突的位置。")
    # Detach from every mutable manifest or repository container.
    return RecordsSnapshot(bundle.set_id, tuple(files[path] for path in sorted(files)))
