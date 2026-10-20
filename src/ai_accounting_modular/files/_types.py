"""Process-local file production contracts, independent of accounting owners."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from ai_accounting_modular.contracts.public import ContractError, FilePlan
from ai_accounting_modular.storage.public import CompanyMetadata


@dataclass(frozen=True)
class FileScope:
    key: str
    head: str
    file_name: str


@dataclass(frozen=True)
class CaptureContext:
    company: CompanyMetadata
    capture_id: str
    capture_root: Path


@dataclass(frozen=True)
class FileTaskContext:
    company: CompanyMetadata
    task_id: str
    task_root: Path
    capture_root: Path | None


@dataclass(frozen=True)
class FileArtifact:
    """A candidate beneath this task's controlled root."""

    relative_path: str


@dataclass(frozen=True)
class RunClaim:
    company_id: str
    task_id: str
    scope_head: str
    attempt: int


@dataclass(frozen=True)
class FileFailure:
    code: str
    message: str
    retryable: bool = True


@dataclass(frozen=True)
class FileCompletion:
    claim: RunClaim
    artifact: FileArtifact | None = None
    failure: FileFailure | None = None

    def __post_init__(self):
        if (self.artifact is None) == (self.failure is None):
            raise ContractError("invalid_input", "文件执行结果须为候选文件或明确失败。")


@dataclass(frozen=True)
class ExpireTask:
    code: str = "plan_expired"
    message: str = "文件计划已经失效，请按当前结果重新生成。"


@dataclass(frozen=True)
class VerifiedDownload:
    stream: BinaryIO
    file_name: str
    sha256: str
    byte_size: int


class ProductionError(ContractError):
    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(code, message)
        self.retryable = retryable


class FileProducer(Protocol):
    """Injected by host; content, semantic validity and backup rolling belong here.

    produce writes only task_root; publish is called under the scope lock after
    fencing, and may atomically roll a backup into the configured backup root.
    verify reads the exact candidate/download bytes, at position zero. cleanup
    removes only capture temporaries and must honor keep_capture. Files removes
    the separate delivery task_root itself. External valid backups survive it.
    is_current also runs inside the final maintenance writer transaction: it
    must only compare lightweight public semantic heads, without writes, file
    verification, report generation or original-material scans.
    Context company metadata supplies captured identity/configuration, not a
    substitute for reading current semantic heads. Whole-company backup uses
    FileScope.key='company', retaining current/previous packages rather than
    one task scope per historical capture.
    publish can run again after the package was installed but before its task
    confirmation survived: repeating the same verified semantic package must
    preserve the genuine previous package. The delivered bytes must equal the
    verified candidate. If an already installed same-plan package has different
    container bytes, produce reuses those exact bytes as its candidate rather
    than publishing another representation and rolling previous again.
    recover_lease and files check pending/running physical inputs. A succeeded
    task retains its strict plan after capture cleanup: backup is_current checks
    saved semantics/structure/identity without requiring those temporary inputs;
    verify checks the published package bytes, never rereads deleted captures.
    cleanup is idempotent when temporary files have already been removed.
    discard_capture handles an unaccepted capture with no task identity. Files
    first rules out a current backup task reference and a live lease under the
    company backup scope lock. Remove only capture_root, never task outputs or
    published packages; repeat calls are harmless and cleanup failure is raised.
    """

    def describe(self, plan: FilePlan) -> FileScope: ...

    def recover_lease(
        self, plan: FilePlan, context: FileTaskContext
    ) -> AbstractContextManager[None]: ...

    def is_current(self, plan: FilePlan, context: FileTaskContext) -> bool: ...

    def produce(self, plan: FilePlan, context: FileTaskContext) -> FileArtifact: ...

    def verify(self, plan: FilePlan, stream: BinaryIO, context: FileTaskContext) -> None: ...

    def publish(self, plan: FilePlan, artifact: FileArtifact, context: FileTaskContext) -> Path: ...

    def cleanup(self, context: FileTaskContext, *, keep_capture: bool) -> None: ...

    def discard_capture(self, context: CaptureContext) -> None: ...
