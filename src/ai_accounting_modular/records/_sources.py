"""Controlled company paths, fixed source bytes and short-lived read leases."""

from __future__ import annotations

import errno
import hashlib
import io
import mimetypes
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO
from uuid import UUID, uuid5

from ai_accounting_modular.contracts.public import (
    ContractError,
    partial_fact_source_ids,
    validate_contract,
)

from ._types import SourceStatus

CHUNK_SIZE = 1024 * 1024


def issue_source_ids(issue):
    required = set(issue["evidence_ids"])
    if "known_fact" in issue:
        required.update(partial_fact_source_ids(issue["known_fact"]))
    return tuple(sorted(required))


def source_status(root: Path, source) -> SourceStatus:
    """Inspect registered file metadata without reading or claiming to verify bytes."""
    source_id = source["source_id"]
    if source["kind"] == "owner_statement":
        return SourceStatus(source_id, "present", len(source["text"].encode("utf-8")))
    try:
        path = controlled_path(root, source["relative_path"])
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ContractError("source_unavailable", "原件位置不是可读取的普通文件。")
    except FileNotFoundError:
        return SourceStatus(source_id, "missing_file", None)
    except OSError as exc:
        raise ContractError("source_unavailable", "暂时无法检查原件位置。") from exc
    except ContractError as exc:
        raise ContractError("source_unavailable", "原件路径不可安全读取。") from exc
    state = "present" if info.st_size == source["byte_size"] else "content_mismatch"
    return SourceStatus(source_id, state, info.st_size)


def source_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_digest(text: str) -> str:
    return hashlib.sha256(source_text(text).encode("utf-8")).hexdigest()


def controlled_path(root: Path, relative: str, *, parents: bool = False) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or ":" in relative
        or "\x00" in relative
    ):
        raise ContractError("invalid_path", "资料位置必须是公司根内的规范相对路径。")
    parts = PurePosixPath(relative).parts
    if (
        PurePosixPath(relative).is_absolute()
        or any(part in {".", ".."} for part in parts)
        or "/".join(parts) != relative
    ):
        raise ContractError("invalid_path", "资料位置不能越过公司目录。")
    root = Path(root).absolute()
    current = root
    for part in (None, *parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if parents and current != root / relative:
                current.mkdir(exist_ok=True)
                info = current.lstat()
            else:
                continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ContractError("invalid_path", "资料路径不能经过符号链接或目录联接。")
    if not (root / relative).resolve().is_relative_to(root.resolve()):
        raise ContractError("invalid_path", "资料位置不能越过公司目录。")
    return root / relative


def atomic_write(root: Path, relative: str, data: bytes | BinaryIO, *, replace=True) -> None:
    path = controlled_path(root, relative, parents=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".records-", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            if isinstance(data, bytes):
                stream.write(data)
            else:
                while chunk := data.read(CHUNK_SIZE):
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        controlled_path(root, relative)
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # The caller verifies the existing immutable target against its source.
            temporary.unlink()
        temporary = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_file(root, source, output=None):
    digest, size = hashlib.sha256(), 0
    try:
        path = controlled_path(root, source["relative_path"])
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ContractError("invalid_path", "原件必须是普通文件。")
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > source["byte_size"]:
                    raise ContractError("source_corrupt", "原件字节数与登记内容不一致。")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
        controlled_path(root, source["relative_path"])
    except FileNotFoundError as exc:
        raise ContractError("source_missing", "已登记原件不存在，需要补回正确资料。") from exc
    except OSError as exc:
        raise ContractError("source_unavailable", "暂时无法读取已登记原件。") from exc
    if size != source["byte_size"] or digest.hexdigest() != source["sha256"]:
        raise ContractError("source_corrupt", "原件内容与登记摘要不一致。")


def checked_stream(root: Path, source):
    """Expose a verified fixed copy; later external edits cannot alter the lease."""
    if source["kind"] == "owner_statement":
        raw = source["text"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ContractError("source_corrupt", "老板说明与登记摘要不一致。")
        return io.BytesIO(raw)
    result = tempfile.SpooledTemporaryFile(max_size=8 * CHUNK_SIZE, mode="w+b")
    try:
        _check_file(root, source, result)
        result.seek(0)
        return result
    except BaseException:
        result.close()
        raise


class SourceLease(io.BufferedIOBase):
    """Read-only bytes detached from mutable host paths; usable only while open."""

    def __init__(self, source, stream):
        super().__init__()
        self.source = source
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def read1(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def seek(self, offset, whence=io.SEEK_SET):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def fileno(self):
        return self._stream.fileno()

    def readable(self):
        return True

    def seekable(self):
        return True

    def close(self):
        if not self.closed:
            self._stream.close()
            super().close()

    def iter_chunks(self, chunk_size: int = CHUNK_SIZE):
        if type(chunk_size) is not int or not 1 <= chunk_size <= 8 * CHUNK_SIZE:
            raise ContractError("invalid_input", "读取块大小必须在1字节至8 MiB之间。")
        while chunk := self._stream.read(chunk_size):
            yield chunk

    def copy_to(self, stream) -> int:
        count = 0
        for chunk in self.iter_chunks():
            stream.write(chunk)
            count += len(chunk)
        return count


@contextmanager
def lease_source(root, source):
    with checked_stream(root, source) as stream:
        with SourceLease(source, stream) as lease:
            yield lease


def _file_name(name):
    if not name or len(name) > 255:
        raise ContractError("invalid_input", "原件名称超过合同长度。")
    if (
        name in {".", ".."}
        or any(character in name for character in "/\\:\x00")
        or (
            os.name == "nt"
            and (
                PureWindowsPath(name).is_reserved()
                or name.endswith((".", " "))
                or any(ord(character) < 32 or character in '<>"|?*' for character in name)
            )
        )
    ):
        raise ContractError(
            "invalid_path", f"原件名称 {name!r} 必须是当前文件系统支持的单一文件名。"
        )


def _file_path(source):
    name = source["original_name"]
    _file_name(name)
    return (
        "originals/"
        + source.get("archive_month", "unassigned")
        + "/"
        + source["source_id"]
        + "/"
        + name
    )


def validate_source(source):
    validate_contract("core.schema.json", "Source", source)
    if source["kind"] == "owner_statement" and source_text(source["text"]) != source["text"]:
        raise ContractError("invalid_input", "老板说明使用规范LF换行，内容改变须使用新来源身份。")
    if source["kind"] == "file":
        expected = _file_path(source)
        if source["relative_path"] != expected:
            raise ContractError("invalid_path", "原件必须采用公司归档月份下的稳定来源文件名。")


def check_source(root, source):
    validate_source(source)
    if source["kind"] == "file":
        _check_file(root, source)
    else:
        with checked_stream(root, source):
            pass


def prepare_source(source_import, root, existing=None, find_existing=None, *, company_id):
    validate_contract("core.schema.json", "SourceImport", source_import)
    try:
        return _prepare_source(source_import, root, existing, find_existing, company_id)
    except OSError as error:
        code = (
            "invalid_path"
            if error.errno in {errno.EINVAL, errno.ENAMETOOLONG}
            or getattr(error, "winerror", None) in {123, 206}
            else "source_unavailable"
        )
        relative = source_import.get("relative_path", "老板说明")
        raise ContractError(
            code,
            f"无法整理原件 {relative!r}；请检查文件名、资料位置与文件系统是否支持读取和归档。",
        ) from error


def _prepare_source(source_import, root, existing, find_existing, company_id):
    if existing is not None and existing["kind"] != source_import["kind"]:
        raise ContractError("conflict", "来源身份不能换成另一种内容。")
    source_id = source_import.get("source_id")
    if source_import["kind"] == "owner_statement":
        text = source_text(source_import["text"])
        source = {
            "source_id": source_id,
            "kind": "owner_statement",
            "text": text,
            "sha256": text_digest(text),
        }
    else:
        relative = source_import["relative_path"]
        if not relative.startswith("originals/"):
            raise ContractError("invalid_path", "待整理原件须先放入当前公司originals目录。")
        _file_name(PurePosixPath(relative).name)
        path = controlled_path(root, relative)
        if not path.exists() and existing is not None:
            check_source(root, existing)
            source = dict(existing)
        else:
            if not path.is_file():
                raise ContractError("source_missing", "待登记原件不存在。")
            size = path.stat().st_size
            if size > 2147483647:
                raise ContractError("invalid_input", "单份原件超过2 GiB，不能导入。")
            name = existing.get("original_name") if existing else path.name
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(CHUNK_SIZE):
                    digest.update(chunk)
            source = {
                "source_id": source_id,
                "kind": "file",
                "relative_path": relative,
                "original_name": name,
                "media_type": existing["media_type"]
                if existing is not None
                else mimetypes.guess_type(name)[0] or "application/octet-stream",
                "byte_size": size,
                "sha256": digest.hexdigest(),
            }
        if (
            "expected_sha256" in source_import
            and source_import["expected_sha256"] != source["sha256"]
        ):
            raise ContractError("source_corrupt", "原件与指定的内容摘要不一致。")
    if existing is None and "source_id" not in source_import and find_existing is not None:
        existing = find_existing(source["kind"], source["sha256"])
        if existing is not None:
            source_id = existing["source_id"]
            source["source_id"] = source_id
            if source["kind"] == "file":
                source["original_name"] = existing["original_name"]
                source["media_type"] = existing["media_type"]
    if source_id is None:
        source_id = str(uuid5(UUID(company_id), source["kind"] + ":" + source["sha256"]))
        source["source_id"] = source_id
    if existing is not None and (existing["kind"], existing["sha256"]) != (
        source["kind"],
        source["sha256"],
    ):
        raise ContractError("conflict", "来源身份已绑定固定内容；改变内容须新建来源。")
    if "archive_month" in source_import:
        source["archive_month"] = source_import["archive_month"]
    elif existing is not None and "archive_month" in existing:
        source["archive_month"] = existing["archive_month"]
    if source["kind"] == "file":
        target = _file_path(source)
        if target != source["relative_path"]:
            target_path = controlled_path(root, target, parents=True)
            if target_path.exists():
                check_source(root, source | {"relative_path": target})
                source["relative_path"] = target
                return source
            with checked_stream(root, source) as stream:
                atomic_write(root, target, stream, replace=False)
            source["relative_path"] = target
    check_source(root, source)
    return source
