"""Evidence and document services; application owns cross-module coordination."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock
from weakref import WeakSet

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadContext,
    UnitOfWork,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import _changes, _documents, _sources
from ._repository import RecordsRepository
from ._types import (
    DocumentSnapshot,
    PreparedDocuments,
    RecordsChange,
    RecordsContext,
    RecordsPlan,
    ShardTarget,
)

__all__ = [
    "DocumentSnapshot",
    "PreparedDocuments",
    "Records",
    "RecordsChange",
    "RecordsContext",
    "RecordsPlan",
    "ShardTarget",
    "create_records",
    "repository_factory",
]


@dataclass
class _Publication:
    lock: RLock = field(default_factory=RLock)
    leases: Counter[str] = field(default_factory=Counter)
    prepared: WeakSet[PreparedDocuments] = field(default_factory=WeakSet)


_PUBLICATIONS: dict[Path, _Publication] = {}
_REGISTRY_LOCK = Lock()


def _publication(root: Path) -> _Publication:
    root = root.resolve()
    with _REGISTRY_LOCK:
        if root not in _PUBLICATIONS:
            _PUBLICATIONS[root] = _Publication()
        return _PUBLICATIONS[root]


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("records"), RecordsRepository, RecordsRepository)


class Records:
    def __init__(self, root_for: Callable[[ReadContext], Path]):
        self._root_for = root_for

    def _root(self, read):
        root = Path(self._root_for(read))
        if not root.is_absolute():
            raise ContractError("invalid_path", "公司资料根必须是已登记的绝对位置。")
        _sources.controlled_path(root, "runtime")
        return root.resolve()

    def prepare_source(self, source_import, read_context: ReadContext):
        validate_contract("core.schema.json", "SourceImport", source_import)
        repository = read_context.repository("records")
        existing = None
        if "source_id" in source_import:
            existing = repository.sources((source_import["source_id"],)).get(
                source_import["source_id"]
            )
        return _sources.prepare_source(
            source_import, self._root(read_context), existing, repository.find_source_content
        )

    def plan_change(self, change: RecordsChange, context: RecordsContext) -> RecordsPlan:
        return _changes.plan_change(change, context, self._root(context.read_context))

    def apply_current(self, plan: RecordsPlan, uow: UnitOfWork) -> None:
        _changes.apply_current(plan, uow, self._root(uow))
        if plan.changed_scopes:
            self.mark_document_targets(plan.changed_scopes, uow)

    def resolve_sources(self, ids: Sequence[str], read_context: ReadContext) -> tuple[dict, ...]:
        wanted = _changes.source_ids(ids)
        stored = read_context.repository("records").sources(wanted)
        result = tuple(stored[source_id] for source_id in wanted if source_id in stored)
        for source in result:
            validate_contract("core.schema.json", "Source", source)
            if source["kind"] == "owner_statement" and (
                _sources.source_text(source["text"]) != source["text"]
                or _sources.text_digest(source["text"]) != source["sha256"]
            ):
                raise ContractError("storage_corrupt", "来源说明与内容摘要不一致。")
        return result

    def source_locations(self, ids: Sequence[str], read_context: ReadContext) -> list[dict]:
        return [
            {"source_id": source["source_id"]} for source in self.resolve_sources(ids, read_context)
        ]

    @contextmanager
    def lease_source(self, source_id: str, read_context: ReadContext):
        sources = self.resolve_sources((source_id,), read_context)
        if not sources:
            raise ContractError("not_found", "当前公司没有这份来源。")
        with _sources.lease_source(self._root(read_context), sources[0]) as lease:
            yield lease

    def replace_source_uses(
        self, owner_kind: str, owner_key: str, evidence_ids, uow: UnitOfWork
    ) -> None:
        if owner_kind not in {"business", "object", "issue", "note"}:
            raise ContractError("invalid_input", "来源引用只属于业务、对象、当前待办或说明。")
        if owner_kind != "note":
            validate_contract("core.schema.json", "Id", owner_key)
        elif not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 200:
            raise ContractError("invalid_input", "说明范围不能为空。")
        ids = _changes.source_ids(evidence_ids, limit=1000)
        repository = uow.repository("records")
        _changes._missing_sources(ids, repository.sources(ids))
        if repository.uses(owner_kind, owner_key) != ids:
            repository.replace_uses(owner_kind, owner_key, ids)
            self.mark_document_targets(("sources",), uow)

    def current_notes(self, scopes, read_context: ReadContext) -> tuple[dict, ...]:
        repository = read_context.repository("records")
        rows = repository.notes(scopes)
        uses = repository.uses_many("note", tuple(rows))
        return tuple(
            _changes.note_current(row, repository, uses[scope]) for scope, row in rows.items()
        )

    def current_issues(self, scope: str, read_context: ReadContext) -> dict:
        repository = read_context.repository("records")
        row = repository.issue_scope(scope)
        result = {
            "scope": scope,
            "head": row["head"] if row else 0,
            "issues": repository.issues(scope),
        }
        if row is not None and row["content_digest"] != content_digest(result["issues"]):
            raise ContractError("storage_corrupt", "当前待办范围与完整集合的摘要不一致。")
        return validate_contract("core.schema.json", "IssueScope", result)

    def invalidate_notes(
        self, note_basis_heads, command_id: str, uow: UnitOfWork
    ) -> tuple[str, ...]:
        validate_contract("core.schema.json", "Id", command_id)
        repository = uow.repository("records")
        changed = []
        for scope, row in repository.notes(tuple(note_basis_heads)).items():
            if (
                scope == "company"
                or row["state"] != "current"
                or row["basis_head"] == note_basis_heads[scope]
            ):
                continue
            current = {
                "scope": scope,
                "seq": _changes._next(row["seq"]),
                "state": "needs_refresh",
                "text": None,
                "basis_head": note_basis_heads[scope],
                "evidence_ids": [],
            }
            repository.save_note(current, row["seq"], command_id, _changes.note_digest(current))
            repository.replace_uses("note", scope, ())
            changed.append(scope)
        if changed:
            self.mark_document_targets(("notes", "sources"), uow)
        return tuple(changed)

    def mark_document_targets(self, keys, uow: UnitOfWork) -> None:
        _documents.mark_targets(keys, uow)

    def pending_document_scopes(self, read_context: ReadContext) -> tuple[str, ...]:
        return tuple(
            row["shard_key"]
            for row in read_context.repository("records").targets()
            if row["wanted_head"] > row["published_head"]
        )

    def document_target_heads(self, read_context: ReadContext) -> dict[str, int]:
        return {
            row["shard_key"]: row["wanted_head"]
            for row in read_context.repository("records").targets()
        }

    def capture_documents(
        self, company_identity, facts, objects, read_context: ReadContext, *, scopes=None
    ) -> DocumentSnapshot:
        root = self._root(read_context)
        with _publication(root).lock:
            return _documents.capture_documents(
                company_identity, facts, objects, read_context, root, scopes
            )

    def prepare_documents(self, snapshot: DocumentSnapshot) -> PreparedDocuments:
        if not isinstance(snapshot, DocumentSnapshot):
            raise ContractError("invalid_input", "标准文档生成必须使用同一次读快照。")
        publication = _publication(snapshot.company_root)
        with publication.lock:
            prepared = _documents.prepare_documents(snapshot)
            publication.prepared.add(prepared)
            return prepared

    def publish_documents(self, prepared: PreparedDocuments, uow: UnitOfWork) -> dict:
        root = self._root(uow)
        publication = _publication(root)
        with publication.lock:
            try:
                manifest = _documents.publish_documents(prepared, uow, root)
            except ContractError as exc:
                if exc.code == "conflict":
                    publication.prepared.discard(prepared)
                    self._cleanup_current_parts(root, publication)
                raise
            publication.prepared.discard(prepared)
            self._cleanup_parts(root, manifest, publication)
            return _documents.document_status(uow, root)

    def _cleanup_parts(self, root, manifest, publication):
        protected = set(publication.leases)
        for prepared in tuple(publication.prepared):
            protected.update(shard["path"] for shard in prepared.manifest["shards"])
        try:
            _documents.cleanup_parts(root, manifest, protected)
        except (OSError, ContractError):
            # Retry unavailable obsolete files after publication or the next lease release.
            pass

    def _cleanup_current_parts(self, root, publication):
        try:
            current = loads_json(
                _sources.controlled_path(root, "standard/current.json").read_bytes()
            )
            _documents.verify_manifest(root, current, parts=False)
        except (OSError, ContractError):
            # A missing/corrupt current manifest cannot justify deleting any part.
            pass
        else:
            self._cleanup_parts(root, current, publication)

    def record_document_error(
        self, keys, code: str, message: str, uow: UnitOfWork, *, expected_heads: dict[str, int]
    ) -> None:
        keys = tuple(_documents.validate_key(key) for key in keys)
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 200
            or not isinstance(message, str)
            or not 1 <= len(message) <= 2000
        ):
            raise ContractError("invalid_input", "当前归档失败须有明确代码及简明说明。")
        if any(
            key not in expected_heads
            or type(expected_heads[key]) is not int
            or expected_heads[key] < 0
            for key in keys
        ):
            raise ContractError("invalid_input", "归档失败必须绑定失败任务实际捕获的文档目标。")
        uow.repository("records").document_error(keys, code, message, expected_heads)

    def document_status(self, read_context: ReadContext) -> dict:
        root = self._root(read_context)
        with _publication(root).lock:
            return _documents.document_status(read_context, root)

    @contextmanager
    def lease_document_set(self, read_context: ReadContext):
        """Pin the manifest and referenced parts while a reader copies/verifies them."""
        root = self._root(read_context)
        publication = _publication(root)
        with publication.lock:
            status = _documents.document_status(read_context, root)
            if status["state"] != "ready":
                raise ContractError("documents_pending", "标准文档尚未完整归档。")
            manifest = loads_json(
                _sources.controlled_path(root, "standard/current.json").read_bytes()
            )
            _documents.verify_manifest(root, manifest, parts=False)
            paths = tuple({shard["path"] for shard in manifest["shards"]})
            publication.leases.update(paths)
        try:
            # Pin first, then verify bytes without holding up another publication.
            _documents.verify_manifest(root, manifest)
            yield loads_json(canonical_json(manifest))
        finally:
            with publication.lock:
                for path in paths:
                    publication.leases[path] -= 1
                    if not publication.leases[path]:
                        del publication.leases[path]
                self._cleanup_current_parts(root, publication)


def create_records(root_for: Callable[[ReadContext], Path]) -> Records:
    return Records(root_for)
