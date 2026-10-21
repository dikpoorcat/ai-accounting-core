"""Immutable records plans and detached public document captures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ai_accounting_modular.contracts.public import (
    CapturedFile,
    CurrentIssue,
    CurrentNote,
    NoteChange,
    ReadContext,
    Source,
    loads_json,
)

SOURCE_BLOCK_CHARS = 20000


@dataclass(frozen=True)
class SourceStatus:
    """Cheap availability observation; full byte verification belongs to the lease."""

    source_id: str
    state: Literal["present", "missing_file", "content_mismatch", "not_found"]
    observed_size: int | None


@dataclass(frozen=True)
class SourceDiscards:
    discarded_source_ids: tuple[str, ...]
    missing_source_ids: tuple[str, ...]
    marker_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceDiscardError:
    marker_id: str
    code: str
    message: str


@dataclass(frozen=True)
class SourceDiscardCleanup:
    completed_marker_ids: tuple[str, ...]
    pending_marker_ids: tuple[str, ...]
    pending_source_ids: tuple[str, ...] = ()
    errors: tuple[SourceDiscardError, ...] = ()
    next_marker_id: str | None = None


@dataclass(frozen=True, eq=False)
class PreparedSources:
    """One live, company-bound verification; paths stay pinned until it closes."""

    company_id: str
    database_id: str
    root: Path
    _sources_json: str
    _file_identities: tuple[tuple[str, tuple[int, ...]], ...]
    _active: bool = field(default=False, init=False)

    def check(self, sources, read, root):
        from ._prepared_sources import check

        check(self, sources, read, root)


@dataclass(frozen=True)
class RecordsChange:
    sources: tuple[Source, ...] = ()
    notes: tuple[NoteChange, ...] = ()
    unresolved: tuple[CurrentIssue, ...] | None = None
    unresolved_scope: str | None = None
    expected_scope_head: int | None = None


@dataclass(frozen=True)
class RecordsContext:
    read_context: ReadContext
    command_id: str
    note_basis_heads: Mapping[str, str] = field(default_factory=dict)
    prepared_sources: PreparedSources | None = None


@dataclass(frozen=True)
class PlannedRecord:
    key: str
    before_digest: str | None
    target_json: str
    changed: bool


@dataclass(frozen=True)
class RecordsPlan:
    company_id: str
    database_id: str
    head: int
    command_id: str
    _sources: tuple[PlannedRecord, ...]
    _notes: tuple[PlannedRecord, ...]
    _issue_scope: PlannedRecord | None
    _prepared_sources: PreparedSources | None = field(default=None, repr=False)

    @property
    def sources(self) -> tuple[Source, ...]:
        return tuple(loads_json(item.target_json) for item in self._sources)

    @property
    def notes(self) -> tuple[dict, ...]:
        return tuple(loads_json(item.target_json)["current"] for item in self._notes)

    @property
    def issues(self) -> tuple[CurrentIssue, ...]:
        if self._issue_scope is None:
            return ()
        return tuple(loads_json(self._issue_scope.target_json)["issues"])

    @property
    def issue_scope(self) -> dict | None:
        if self._issue_scope is None:
            return None
        value = loads_json(self._issue_scope.target_json)
        return {key: value[key] for key in ("scope", "head", "issues")}

    @property
    def changed_source_ids(self) -> tuple[str, ...]:
        return tuple(item.key for item in self._sources if item.changed)

    @property
    def changed_scopes(self) -> tuple[str, ...]:
        result = ["sources"] if self.changed_source_ids else []
        if any(item.changed for item in self._notes):
            result.extend(("notes", "sources"))
        if self._issue_scope is not None and self._issue_scope.changed:
            result.extend(("work", "sources"))
        return tuple(sorted(set(result)))


@dataclass(frozen=True)
class ShardTarget:
    key: str
    wanted_head: int
    published_head: int | None
    current_path: str | None
    current_digest: str | None


@dataclass(frozen=True)
class DocumentSnapshot:
    company_id: str
    database_id: str
    head: int
    company_root: Path
    company_identity_json: str
    targets: tuple[ShardTarget, ...]
    scopes: tuple[str, ...]
    confirmed_set_id: str | None
    base_manifest_json: str | None
    parts_json: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class PreparedDocuments:
    """A live preparation has its own lifetime even when another has identical content."""

    snapshot: DocumentSnapshot
    manifest_json: str
    markdown: str

    @property
    def manifest(self) -> dict:
        return loads_json(self.manifest_json)


@dataclass(frozen=True)
class BackupMaterials:
    """One read snapshot's material inputs; releasing it does not delete a capture."""

    company_id: str
    database_id: str
    head: int
    capture_id: str
    capture_root: Path
    document_set_id: str
    _company_identity_json: str
    _inputs_json: str

    @property
    def company_identity(self) -> dict:
        return loads_json(self._company_identity_json)

    @property
    def inputs(self) -> tuple[CapturedFile, ...]:
        return tuple(loads_json(self._inputs_json))


@dataclass(frozen=True)
class StandardFile:
    relative_path: str
    sha256: str
    byte_size: int
    kind: Literal["standard", "original"]


@dataclass(frozen=True)
class RecordsSnapshot:
    document_set_id: str
    files: tuple[StandardFile, ...]


@dataclass(frozen=True)
class StandardBundle:
    """Verified final documents, detached from any catalog or company database."""

    manifest_path: Path
    company_root: Path
    manifest_digest: str
    files: tuple[StandardFile, ...]
    _manifest_json: str
    _parts_json: tuple[str, ...]

    @property
    def manifest(self) -> dict:
        return loads_json(self._manifest_json)

    @property
    def set_id(self) -> str:
        return self.manifest["set_id"]

    @property
    def company_identity(self) -> dict:
        return self.manifest["company_identity"]

    def _items(self, field) -> tuple[dict, ...]:
        return tuple(item for raw in self._parts_json for item in loads_json(raw).get(field, ()))

    @property
    def facts(self) -> tuple[dict, ...]:
        return self._items("facts")

    @property
    def objects(self) -> tuple[dict, ...]:
        return self._items("objects")

    @property
    def sources(self) -> tuple[dict, ...]:
        return self._items("sources")

    @property
    def notes(self) -> tuple[dict, ...]:
        return self._items("notes")

    @property
    def issues(self) -> tuple[dict, ...]:
        return self._items("issues")

    @property
    def working_period_to(self) -> str:
        identity = self.company_identity
        if "working_period_to" in identity:
            return identity["working_period_to"]
        latest = max((fact["period"] for fact in self.facts), default=identity["start_period"])
        return max(identity["start_period"], latest)


@dataclass(frozen=True)
class PeriodNotesPage:
    items: tuple[CurrentNote, ...]
    total: int
    next_cursor: str | None


@dataclass(frozen=True)
class PeriodIssuesPage:
    """A filtered view, never an IssueScope suitable for complete replacement."""

    items: tuple[CurrentIssue, ...]
    scope_heads: Mapping[str, int]
    total: int
    next_cursor: str | None
