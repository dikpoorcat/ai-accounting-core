"""Public current-task factory and host-injected file production protocols."""

from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from ._repository import FilesRepository
from ._service import Files
from ._types import (
    CaptureContext,
    ExpireTask,
    FileArtifact,
    FileCompletion,
    FileFailure,
    FileProducer,
    FileScope,
    FileTaskContext,
    ProductionError,
    RunClaim,
    VerifiedDownload,
)


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("files"), FilesRepository, FilesRepository)


def create_files(storage, producers) -> Files:
    return Files(storage, producers)


__all__ = [
    "CaptureContext",
    "ExpireTask",
    "FileArtifact",
    "FileCompletion",
    "FileFailure",
    "FileProducer",
    "FileScope",
    "FileTaskContext",
    "Files",
    "ProductionError",
    "RunClaim",
    "VerifiedDownload",
    "create_files",
    "repository_factory",
]
