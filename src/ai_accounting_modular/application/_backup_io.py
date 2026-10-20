"""Bounded exact-byte copies and local controlled paths for backup publication."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files.public import ProductionError

from ._target_paths import beneath

CHUNK_SIZE = 1024 * 1024


def controlled(root: Path, relative: str, *, parents=False) -> Path:
    try:
        path = beneath(root, relative)
        if parents:
            path.parent.mkdir(parents=True, exist_ok=True)
            path = beneath(root, relative)
        return path
    except ContractError as error:
        raise ProductionError(
            "unsafe_path", "备份位置须位于明确的受控根内且不能经过链接。", retryable=False
        ) from error


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def atomic_output(root: Path, relative: str, *, temporary_prefix=".backup-write-"):
    destination = controlled(root, relative, parents=True)
    temporary = None
    try:
        temporary_relative = (
            destination.parent.relative_to(root.resolve())
            / (temporary_prefix + uuid4().hex + ".tmp")
        ).as_posix()
        temporary_path = controlled(root, temporary_relative)
        with temporary_path.open("xb") as out:
            temporary = temporary_path
            yield out
            out.flush()
            os.fsync(out.fileno())
        controlled(root, relative)
        os.replace(temporary, destination)
        temporary = None
        sync_directory(destination.parent)
    finally:
        if temporary is not None:
            controlled(root, temporary.relative_to(root.resolve()).as_posix()).unlink(
                missing_ok=True
            )


@dataclass(frozen=True)
class FileBytes:
    stream: BinaryIO
    sha256: str
    byte_size: int


@contextmanager
def snapshot(root: Path, relative: str):
    path = controlled(root, relative)
    with SpooledTemporaryFile(max_size=8 * CHUNK_SIZE, mode="w+b") as copied:
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ProductionError("unsafe_path", "备份输入必须是普通文件。", retryable=False)
            while chunk := source.read(CHUNK_SIZE):
                copied.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        controlled(root, relative)
        copied.seek(0)
        yield FileBytes(copied, digest.hexdigest(), size)


def copy_bytes(
    value: FileBytes, root: Path, relative: str, *, temporary_prefix=".backup-write-"
) -> Path:
    value.stream.seek(0)
    digest, size = hashlib.sha256(), 0
    with atomic_output(root, relative, temporary_prefix=temporary_prefix) as output:
        while chunk := value.stream.read(CHUNK_SIZE):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        if (digest.hexdigest(), size) != (value.sha256, value.byte_size):
            raise ProductionError("backup_changed", "备份原字节在复制中发生变化。", retryable=False)
    return controlled(root, relative)
