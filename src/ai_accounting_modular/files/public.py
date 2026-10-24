"""Public current-task factory and host-injected file production protocols."""

from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from ._repository import FilesRepository
from ._service import Files
from ._types import (
    CaptureContext,
    CurrentFile,
    ExpireTask,
    FileArtifact,
    FileCompletion,
    FileFailure,
    FileProducer,
    FileScope,
    FileTaskContext,
    ProductionError,
    RunClaim,
    VerifiedArtifact,
    VerifiedDownload,
)


def repository_factory(*, structure_version=None) -> RepositoryFactory:
    return RepositoryFactory(
        module_schema("files", structure_version=structure_version),
        FilesRepository,
        FilesRepository,
    )


def create_files(storage, producers) -> Files:
    return Files(storage, producers)


def reset_for_restore(uow) -> None:
    """A relocated database has no live tasks or downloadable local outputs."""
    uow.repository("files").reset()


__all__ = [
    "CaptureContext",
    "CurrentFile",
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
    "VerifiedArtifact",
    "VerifiedDownload",
    "create_files",
    "repository_factory",
    "reset_for_restore",
]
