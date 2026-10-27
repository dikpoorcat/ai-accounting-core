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
    Source,
    SourceImport,
    SourcesRequest,
    SourcesResult,
    UnitOfWork,
    canonical_json,
    loads_json,
    validate_contract,
    validate_contract_items,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import (
    _backup,
    _changes,
    _documents,
    _period_issues,
    _periods,
    _prepared_sources,
    _snapshot,
    _source_discards,
    _source_imports,
    _source_queries,
    _sources,
)
from ._repository import RecordsRepository
from ._standard import copy_standard_bundle, open_standard_bundle
from ._types import (
    BackupMaterials,
    DocumentSnapshot,
    PeriodIssuesPage,
    PeriodNotesPage,
    PreparedDocuments,
    PreparedSources,
    RecordsChange,
    RecordsContext,
    RecordsPlan,
    RecordsSnapshot,
    ShardTarget,
    SourceDiscardCleanup,
    SourceDiscardError,
    SourceDiscards,
    SourceStatus,
    StandardBundle,
    StandardFile,
)

__all__ = [
    "BackupMaterials",
    "DocumentSnapshot",
    "PeriodIssuesPage",
    "PeriodNotesPage",
    "PreparedDocuments",
    "PreparedSources",
    "Records",
    "RecordsChange",
    "RecordsContext",
    "RecordsPlan",
    "RecordsSnapshot",
    "ShardTarget",
    "SourceDiscardCleanup",
    "SourceDiscardError",
    "SourceDiscards",
    "SourceStatus",
    "StandardBundle",
    "StandardFile",
    "copy_standard_bundle",
    "create_records",
    "open_standard_bundle",
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
            source_import,
            self._root(read_context),
            existing,
            repository.find_source_content,
            company_id=read_context.company_id,
        )

    def prepare_source_imports(
        self, imports: Sequence[SourceImport], read_context: ReadContext
    ) -> tuple[Source, ...]:
        return _source_imports.prepare(self, imports, read_context)

    def sources(self, request: SourcesRequest, read_context: ReadContext) -> SourcesResult:
        return _source_queries.query(self, request, read_context)

    def period_sources(self, period: str, read_context: ReadContext) -> tuple[Source, ...]:
        """Return this archive month's complete registrations without opening originals."""
        validate_contract("core.schema.json", "Month", period)
        sources = read_context.repository("records").period_sources(period)
        for source in sources:
            _sources.validate_source(source)
        return sources

    def source_discard_protection(
        self,
        source_ids: Sequence[str],
        read_context: ReadContext,
        *,
        retained_source_ids: Sequence[str],
        protected_paths: Sequence[str],
    ) -> tuple[str, ...]:
        """Observe current references, supplied protections and active source leases together."""
        root = self._root(read_context)
        publication = _publication(root)
        with publication.lock:
            return _source_discards.protection(
                source_ids,
                read_context,
                root,
                publication,
                retained_source_ids=retained_source_ids,
                protected_paths=protected_paths,
            )

    def clear_period_issues(
        self, period: str, command_id: str, uow: UnitOfWork
    ) -> tuple[str | None, ...]:
        """Clear selected issues; return actual changed periods, retaining None for no period."""
        return _period_issues.clear(self, period, command_id, uow)

    def discard_sources(
        self,
        source_ids: Sequence[str],
        uow: UnitOfWork,
        *,
        retained_source_ids: Sequence[str],
        protected_paths: Sequence[str],
        retain_protected: bool = False,
    ) -> SourceDiscards:
        """Retire only explicitly selected unused sources; never unlink before commit."""
        root = self._root(uow)
        publication = _publication(root)
        with publication.lock:
            return _source_discards.discard(
                source_ids,
                uow,
                root,
                publication,
                retained_source_ids=retained_source_ids,
                protected_paths=protected_paths,
                retain_protected=retain_protected,
            )

    def has_source_discards(self, read_context: ReadContext) -> bool:
        """A cheap scheduling hint; cleanup itself rechecks in a dedicated writer."""
        read_context.repository("records")  # Require this company's live storage context.
        root = self._root(read_context)
        with _publication(root).lock:
            return _source_discards.has_pending(root)

    def pending_source_discard_markers(
        self, archive_month: str, read_context: ReadContext
    ) -> tuple[str, ...]:
        """Select this month's durable cleanup markers without deleting any files."""
        root = self._root(read_context)
        with _publication(root).lock:
            return _source_discards.pending_for_period(root, archive_month, read_context)

    def finish_source_discards(
        self,
        uow: UnitOfWork,
        *,
        protected_paths: Sequence[str],
        marker_ids: Sequence[str] | None = None,
        limit: int = 500,
        after_marker_id: str | None = None,
    ) -> SourceDiscardCleanup:
        """Use a dedicated new writer, before other writes; imports cancel old markers."""
        root = self._root(uow)
        publication = _publication(root)
        with publication.lock:
            return _source_discards.finish(
                uow,
                root,
                publication,
                protected_paths=protected_paths,
                marker_ids=marker_ids,
                limit=limit,
                after_marker_id=after_marker_id,
            )

    def plan_change(self, change: RecordsChange, context: RecordsContext) -> RecordsPlan:
        """Plan current records and the actual periods whose issue bodies change."""
        return _changes.plan_change(change, context, self._root(context.read_context))

    @contextmanager
    def prepare_sources(self, sources, read_context: ReadContext):
        if isinstance(read_context, UnitOfWork):
            raise ContractError("invalid_operation", "来源字节须在业务写事务以前预核验。")
        root = self._root(read_context)
        sources = _prepared_sources.values(sources)
        paths = tuple(source["relative_path"] for source in sources if source["kind"] == "file")
        publication = _publication(root)
        with publication.lock:
            publication.leases.update(paths)
        prepared = None
        try:
            prepared = _prepared_sources.prepare(sources, read_context, root)
            yield prepared
        finally:
            if prepared is not None:
                object.__setattr__(prepared, "_active", False)
            self._release_paths(root, publication, paths)

    def apply_current(self, plan: RecordsPlan, uow: UnitOfWork) -> None:
        keys = _changes.apply_current(plan, uow, self._root(uow))
        if keys:
            self.mark_document_targets(keys, uow)

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

    def source_status(
        self, ids: Sequence[str], read_context: ReadContext
    ) -> tuple[SourceStatus, ...]:
        wanted = _changes.source_ids(ids)
        sources = {
            source["source_id"]: source for source in self.resolve_sources(wanted, read_context)
        }
        root = self._root(read_context)
        return tuple(
            _sources.source_status(root, sources[source_id])
            if source_id in sources
            else SourceStatus(source_id, "not_found", None)
            for source_id in wanted
        )

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
        ids = _changes.source_ids(evidence_ids, limit=None if owner_kind == "business" else 1000)
        repository = uow.repository("records")
        _changes._missing_sources(ids, repository.sources(ids))
        old_ids = repository.uses(owner_kind, owner_key)
        if old_ids != ids:
            repository.replace_uses(owner_kind, owner_key, ids)
            self.mark_document_targets(
                tuple("sources:" + source_id for source_id in set(old_ids) ^ set(ids)), uow
            )

    def current_notes(self, scopes, read_context: ReadContext) -> tuple[dict, ...]:
        repository = read_context.repository("records")
        rows = repository.notes(scopes)
        uses = repository.uses_many("note", tuple(rows))
        return tuple(
            _changes.note_current(row, repository, uses[scope]) for scope, row in rows.items()
        )

    def clear_notes(self, scopes, command_id: str, uow: UnitOfWork) -> tuple[str, ...]:
        """Clear selected business descriptions; preserve versions and all owner issues."""
        changed, source_ids = _changes.clear_notes(scopes, command_id, uow)
        if changed:
            self.mark_document_targets(
                tuple("notes:" + scope for scope in changed)
                + tuple("sources:" + source_id for source_id in source_ids),
                uow,
            )
        return changed

    def note_written_by(self, scope: str, command_id: str, read_context: ReadContext) -> bool:
        """Prove this command still owns the current note, without returning private metadata."""
        validate_contract_items(
            "core.schema.json", "NoteChange", [{"scope": scope}], fields=("scope",)
        )
        validate_contract("core.schema.json", "Id", command_id)
        repository = read_context.repository("records")
        row = repository.notes((scope,)).get(scope)
        if row is None:
            return False
        current = _changes.note_current(row, repository)
        return current["state"] == "current" and row["latest_command_id"] == command_id

    def current_issues(self, scope: str, read_context: ReadContext) -> dict:
        repository = read_context.repository("records")
        row = repository.issue_scope(scope)
        return _changes.issues_current(scope, row, repository)

    def period_notes(
        self, period_from, period_to, read_context: ReadContext, *, limit=500, cursor=None
    ) -> PeriodNotesPage:
        return _periods.notes(period_from, period_to, read_context, limit=limit, cursor=cursor)

    def period_issues(
        self, period_from, period_to, read_context: ReadContext, *, limit=500, cursor=None
    ) -> PeriodIssuesPage:
        """All scopes' range/no-period issues, with full scope versions, paged."""
        return _periods.issues(period_from, period_to, read_context, limit=limit, cursor=cursor)

    def invalidate_notes(
        self, note_basis_heads, command_id: str, uow: UnitOfWork
    ) -> tuple[str, ...]:
        validate_contract("core.schema.json", "Id", command_id)
        repository = uow.repository("records")
        changed = []
        source_ids = set()
        for scope, row in repository.notes(tuple(note_basis_heads)).items():
            if (
                not scope.startswith("period:")
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
            source_ids.update(repository.uses("note", scope))
            repository.save_note(current, row["seq"], command_id, _changes.note_digest(current))
            repository.replace_uses("note", scope, ())
            changed.append(scope)
        if changed:
            self.mark_document_targets(
                tuple("notes:" + scope for scope in changed)
                + tuple("sources:" + source_id for source_id in source_ids),
                uow,
            )
        return tuple(changed)

    def mark_document_targets(self, keys, uow: UnitOfWork) -> None:
        _documents.mark_targets(keys, uow)

    def ensure_document_targets(self, uow: UnitOfWork) -> None:
        """Prepare a fresh or unavailable collection without changing formal business."""
        root = self._root(uow)
        with _publication(root).lock:
            _documents.ensure_targets(uow, root)

    def request_document_repair(self, uow: UnitOfWork) -> None:
        """Republish the saved final facts after a detected document failure."""
        root = self._root(uow)
        with _publication(root).lock:
            _documents.request_repair(uow)

    def pending_document_scopes(self, read_context: ReadContext) -> tuple[str, ...]:
        return tuple(
            row["shard_key"]
            for row in read_context.repository("records").targets()
            if _documents.is_pending(row)
        )

    def document_target_heads(self, read_context: ReadContext) -> dict[str, int]:
        return {
            row["shard_key"]: row["wanted_head"]
            for row in read_context.repository("records").targets()
        }

    def capture_documents(
        self, company_identity, facts, objects, read_context: ReadContext
    ) -> DocumentSnapshot:
        root = self._root(read_context)
        with _publication(root).lock:
            return _documents.capture_documents(
                company_identity, facts, objects, read_context, root
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

    def _release_paths(self, root, publication, paths):
        with publication.lock:
            for path in paths:
                publication.leases[path] -= 1
                if not publication.leases[path]:
                    del publication.leases[path]
            self._cleanup_current_parts(root, publication)

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

    def verify_snapshot(
        self, company_identity, facts, objects, read_context: ReadContext, *, required_source_ids=()
    ) -> RecordsSnapshot:
        """Prove saved documents; FactAnalysis inputs reuse their exact strict analysis."""
        root = self._root(read_context)
        with _publication(root).lock:
            return _snapshot.verify(
                self, company_identity, facts, objects, read_context, root, required_source_ids
            )

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
            self._release_paths(root, publication, paths)

    @contextmanager
    def capture_backup_materials(self, read_context: ReadContext, capture_id: str):
        """Pin this snapshot's originals/parts and copy its two mutable documents."""
        root = self._root(read_context)
        publication = _publication(root)
        with publication.lock:
            prepared = _backup.prepare(root, read_context, capture_id)
            publication.leases.update(prepared.paths)
        try:
            _documents.verify_manifest(root, loads_json(prepared.manifest_json))
            _backup.check_inputs(
                root,
                prepared.materials.capture_root,
                tuple(
                    item for item in prepared.materials.inputs if item["source_root"] == "company"
                ),
            )
            _backup.create(root, read_context, prepared)
            yield prepared.materials
        finally:
            self._release_paths(root, publication, prepared.paths)

    @contextmanager
    def lease_backup_inputs(self, plan, read_context: ReadContext):
        """Restore exact saved physical pins, without selecting today's document set."""
        root = self._root(read_context)
        publication = _publication(root)
        with publication.lock:
            capture_root, paths, inputs = _backup.restore(root, read_context, plan)
            publication.leases.update(paths)
        try:
            _backup.check_inputs(root, capture_root, inputs)
            yield
        finally:
            self._release_paths(root, publication, paths)

    def backup_capture_ids(self, read_context: ReadContext) -> tuple[str, ...]:
        """List only strictly identified temporary captures; files decides acceptance."""
        return _backup.capture_ids(self._root(read_context), read_context)

    def discard_backup_capture(
        self, capture_id: str, read_context: ReadContext, *, keep_capture: bool = False
    ) -> bool:
        """Called after files confirms nonacceptance or releases a completed capture."""
        return _backup.discard(
            self._root(read_context), read_context, capture_id, keep_capture=keep_capture
        )


def create_records(root_for: Callable[[ReadContext], Path]) -> Records:
    return Records(root_for)
