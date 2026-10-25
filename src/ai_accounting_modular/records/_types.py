"""Immutable records plans and detached public document captures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ai_accounting_modular.contracts.public import (
    CapturedFile,
    ContractError,
    CurrentIssue,
    CurrentNote,
    FactDocumentPart,
    NoteChange,
    ReadContext,
    Source,
    canonical_json,
    loads_json,
    validate_contract,
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
    retained_source_ids: tuple[str, ...] = ()


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
    source_uses_changed: bool = False


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
            result.append("notes")
        if any(item.source_uses_changed for item in self._notes):
            result.append("sources")
        if self._issue_scope is not None and self._issue_scope.changed:
            result.extend(("work", "sources"))
        return tuple(sorted(set(result)))


@dataclass(frozen=True, slots=True)
class ShardTarget:
    key: str
    wanted_head: int
    published_head: int | None
    current_path: str | None
    current_digest: str | None

    def __post_init__(self):
        if (
            type(self.key) is not str
            or type(self.wanted_head) is not int
            or (self.published_head is not None and type(self.published_head) is not int)
            or any(
                value is not None and type(value) is not str
                for value in (self.current_path, self.current_digest)
            )
        ):
            raise ContractError("invalid_input", "标准文档目标必须是不可变的身份、版本和位置。")


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    """Strict canonical JSON, detached once at construction and never exposed as containers."""

    company_id: str
    database_id: str
    head: int
    company_root: Path
    company_identity_json: str
    targets: tuple[ShardTarget, ...]
    scopes: tuple[str, ...]
    confirmed_set_id: str | None
    base_manifest_json: str | None
    parts_json: tuple[str | FactDocumentPart, ...]
    _part_keys: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self):
        def document(raw, definition):
            value = loads_json(raw)
            validate_contract("core.schema.json", definition, value)
            return value, canonical_json(value)

        try:
            targets = tuple(self.targets)
            scopes = tuple(self.scopes)
            raw_parts = tuple(self.parts_json)
            root = Path(self.company_root)
        except (TypeError, ValueError) as error:
            raise ContractError(
                "invalid_input", "标准文档快照必须包含明确的目标、范围和 JSON 原文。"
            ) from error
        if (
            type(self.company_id) is not str
            or type(self.database_id) is not str
            or type(self.head) is not int
            or any(type(target) is not ShardTarget for target in targets)
            or any(type(scope) is not str for scope in scopes)
            or (self.confirmed_set_id is not None and type(self.confirmed_set_id) is not str)
        ):
            raise ContractError("invalid_input", "标准文档快照身份和目标必须是不可变值。")
        identity = document(self.company_identity_json, "CompanyIdentity")[1]
        base = (
            document(self.base_manifest_json, "DocumentManifest")[1]
            if self.base_manifest_json is not None
            else None
        )
        parts, keys = [], []
        for raw in raw_parts:
            if type(raw) is FactDocumentPart:
                body, key = raw.canonical_json, raw.key
            else:
                if isinstance(raw, FactDocumentPart):
                    raise ContractError("invalid_input", "事实文档片必须使用正常构造的精确类型。")
                part, body = document(raw, "StandardPart")
                from ._documents import validate_part_key

                validate_part_key(part)
                key = part["key"]
            parts.append(body)
            keys.append(key)
        object.__setattr__(self, "company_root", root)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "company_identity_json", identity)
        object.__setattr__(self, "base_manifest_json", base)
        object.__setattr__(self, "parts_json", tuple(parts))
        object.__setattr__(self, "_part_keys", tuple(keys))


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
