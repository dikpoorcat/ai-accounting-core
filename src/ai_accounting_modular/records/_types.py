"""Immutable records plans and detached public document captures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ai_accounting_modular.contracts.public import (
    CurrentIssue,
    NoteChange,
    ReadContext,
    Source,
    loads_json,
)


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
    published_head: int
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
