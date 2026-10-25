"""Prove published final documents and all registered originals against one saved view."""

import hashlib

from ai_accounting_modular.contracts.public import (
    ContractError,
    FactAnalysis,
    analyze_fact,
    canonical_json,
)

from . import _documents, _sources
from ._changes import document_note, note_current
from ._standard import _read_standard_bundle
from ._types import RecordsSnapshot, StandardFile


def _bad(message):
    raise ContractError("storage_corrupt", message)


def _known_facts(facts):
    """Reuse only detached strict facts; source identities bind to this exact full body."""
    known = {}
    for value in facts:
        analysis = value if isinstance(value, FactAnalysis) else analyze_fact(value)
        fact = analysis.fact
        if fact["business_id"] in known:
            _bad("当前数据库事实包含重复业务身份。")
        canonical = analysis.canonical_json
        if fact["evidence_ids"] != sorted(fact["evidence_ids"]):
            fact["evidence_ids"] = sorted(fact["evidence_ids"])
            canonical = canonical_json(fact)
        known[fact["business_id"]] = canonical, analysis.source_ids
    return known


def _source_uses(known_facts, objects, issues, notes):
    expected = {
        ("business", business_id, source_id)
        for business_id, (_, source_ids) in known_facts.items()
        for source_id in source_ids
    }
    for kind, values, key, extract in (
        ("object", objects, "object_id", lambda value: value.get("evidence_ids", ())),
        ("issue", issues, "issue_id", _sources.issue_source_ids),
        ("note", notes, "scope", lambda value: value["evidence_ids"]),
    ):
        for value in values:
            expected.update((kind, value[key], source_id) for source_id in extract(value))
    return tuple(sorted(expected))


def verify(records, company_identity, facts, objects, read, root, required_source_ids):
    objects = tuple(objects)
    repository = read.repository("records")
    current, targets = repository.document_set(), repository.targets()
    if current is None or any(_documents.is_pending(row) for row in targets):
        raise ContractError("documents_pending", "保存的账务必须有完整的当前标准业务文档。")
    if current["manifest_path"] != "standard/current.json" or any(
        row["published_head"] is None
        or not 0 <= row["published_head"] == row["wanted_head"] <= read.head
        for row in targets
    ):
        _bad("标准文档确认入口或范围版本不属于已保存的账务快照。")
    known_facts = _known_facts(facts)
    bundle, parts = _read_standard_bundle(root / "standard/current.json", known_facts=known_facts)
    manifest = bundle.manifest
    if bundle.set_id != current["published_set_id"] or bundle.company_identity != company_identity:
        _bad("标准文档的公司信息、工作月份或确认集合与数据库不一致。")
    if {part["key"] for part in parts} - {row["shard_key"] for row in targets}:
        _bad("标准业务文档包含没有已保存目标的分片。")
    actual_shards = {item["key"]: item for item in manifest["shards"]}
    indexed_targets = {row["shard_key"]: row for row in targets}
    for part, raw in zip(parts, bundle._parts_json, strict=True):
        data = (raw + "\n").encode("utf-8")
        shard = actual_shards[part["key"]]
        if (shard["sha256"], shard["byte_size"]) != (
            hashlib.sha256(data).hexdigest(),
            len(data),
        ) or indexed_targets[part["key"]]["current_digest"] != shard["sha256"]:
            _bad("当前标准分片的原字节必须是完整事实的规范内容。")
    issues = repository.issues()
    issue_scopes = set(repository.issue_scopes())
    if {issue["scope"] for issue in issues} - issue_scopes:
        _bad("当前待办缺少完整范围身份及集合版本。")
    for scope in sorted(issue_scopes):
        records.current_issues(scope, read)
    note_rows = repository.notes()
    note_uses = repository.uses_many("note", tuple(note_rows))
    notes = tuple(
        note_current(row, repository, note_uses[scope]) for scope, row in note_rows.items()
    )
    expected_uses = _source_uses(known_facts, objects, issues, notes)
    if repository.all_uses() != expected_uses:
        _bad("当前来源引用索引与业务、对象、待办或说明不一致。")
    used_ids = tuple(sorted({source for _, _, source in expected_uses}))
    sources = repository.all_sources()
    registered = {source["source_id"]: source for source in sources}
    if set(used_ids) - set(registered):
        _bad("当前事实、对象、待办或说明缺少已登记来源。")
    if set(required_source_ids) - set(registered):
        _bad("必要的正式采用依据缺少已登记的原始来源。")
    used_sources = {source_id: registered[source_id] for source_id in used_ids}
    expected = {
        "objects": {
            "format": _documents.FORMAT,
            "key": "objects",
            "objects": sorted(
                (_documents._standard_object(obj) for obj in objects),
                key=lambda obj: obj["object_id"],
            ),
        },
        "work": {"format": _documents.FORMAT, "key": "work", "issues": issues},
    }
    for part in parts:
        if part["key"] in expected and canonical_json(part) != canonical_json(
            expected[part["key"]]
        ):
            _bad("标准业务文档没有精确对应完整的当前对象、待办、说明及来源。")
    for field, identity, values in (
        ("notes", "scope", [document_note(note) for note in notes if note["state"] == "current"]),
        ("sources", "source_id", [used_sources[source_id] for source_id in used_ids]),
    ):
        actual = sorted(
            (item for part in parts for item in part.get(field, ())),
            key=lambda item: item[identity],
        )
        if canonical_json(actual) != canonical_json(
            sorted(values, key=lambda item: item[identity])
        ):
            _bad("标准业务文档没有精确对应完整的当前说明及来源。")
    files = {item.relative_path: item for item in bundle.files}
    for source in sources:
        if source["source_id"] not in used_sources:
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
