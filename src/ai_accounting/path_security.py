from __future__ import annotations

import os
import stat
from pathlib import Path


class PathSecurityError(ValueError):
    """Stable failure for paths outside the file-import security boundary."""


def resolve_regular_file_in_root(path: Path, root: Path, *, max_bytes: int) -> Path:
    """Return a regular file below ``root`` without accepting reparse points."""
    candidate = _absolute_without_resolving(path)
    allowed_root = _absolute_without_resolving(root)
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc

    if not allowed_root.is_dir():
        raise PathSecurityError("FILE_IMPORT_ROOT_UNAVAILABLE")
    _reject_reparse_points(allowed_root)
    _reject_reparse_points(candidate)
    try:
        resolved_root = allowed_root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc

    try:
        file_stat = resolved_candidate.stat()
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise PathSecurityError("FILE_NOT_REGULAR")
    if file_stat.st_size > max_bytes:
        raise PathSecurityError("FILE_TOO_LARGE")
    return resolved_candidate


def read_regular_file_in_root(
    path: Path,
    root: Path,
    *,
    max_bytes: int,
) -> tuple[Path, bytes]:
    """Read one allowlisted file through a stable regular-file handle."""
    allowed_root, resolved_root, root_stat = _pin_existing_root(root)
    candidate = _absolute_without_resolving(path)
    try:
        candidate.relative_to(allowed_root)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc
    _reject_reparse_points(candidate)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        _assert_open_file_still_in_root(
            candidate,
            allowed_root,
            resolved_root,
            root_stat,
            opened,
        )
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            content = source.read(max_bytes + 1)
            after_read = os.fstat(source.fileno())
        if len(content) > max_bytes:
            raise PathSecurityError("FILE_TOO_LARGE")
        _assert_open_file_still_in_root(
            candidate,
            allowed_root,
            resolved_root,
            root_stat,
            after_read,
        )
        if (
            not _same_file(opened, after_read)
            or opened.st_size != after_read.st_size
        ):
            raise PathSecurityError("FILE_CHANGED_DURING_READ")
        return resolved, content
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_new_regular_file_in_root(
    path: Path,
    root: Path,
    content: bytes,
    *,
    max_bytes: int,
) -> Path:
    """Create one new regular file without writing through a replaced parent."""
    if len(content) > max_bytes:
        raise PathSecurityError("FILE_TOO_LARGE")
    allowed_root = _absolute_without_resolving(root)
    candidate = _absolute_without_resolving(path)
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise PathSecurityError("STORAGE_PATH_NOT_ALLOWED") from exc
    ensure_directory_in_root(candidate.parent, allowed_root)
    allowed_root, resolved_root, root_stat = _pin_existing_root(allowed_root)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(candidate, flags, 0o600)
    except FileExistsError:
        return read_regular_file_in_root(
            candidate,
            allowed_root,
            max_bytes=max_bytes,
        )[0]
    except OSError as exc:
        raise PathSecurityError("STORAGE_FILE_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        _assert_open_file_still_in_root(
            candidate,
            allowed_root,
            resolved_root,
            root_stat,
            opened,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            after_write = os.fstat(output.fileno())
        _assert_open_file_still_in_root(
            candidate,
            allowed_root,
            resolved_root,
            root_stat,
            after_write,
        )
        if not _same_file(opened, after_write) or after_write.st_size != len(content):
            raise PathSecurityError("STORAGE_FILE_CHANGED_DURING_WRITE")
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise PathSecurityError("STORAGE_FILE_UNAVAILABLE") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_directory_in_root(directory: Path, root: Path) -> Path:
    """Create and return a real directory contained by a real storage root."""
    target = _absolute_without_resolving(directory)
    storage_root = _absolute_without_resolving(root)
    try:
        target.relative_to(storage_root)
    except ValueError as exc:
        raise PathSecurityError("STORAGE_PATH_NOT_ALLOWED") from exc

    _reject_reparse_points(storage_root)
    _reject_reparse_points(target)
    try:
        storage_root.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathSecurityError("STORAGE_DIRECTORY_UNAVAILABLE") from exc
    _reject_reparse_points(storage_root)
    _reject_reparse_points(target)
    if not storage_root.is_dir() or not target.is_dir():
        raise PathSecurityError("STORAGE_DIRECTORY_UNAVAILABLE")
    try:
        resolved_root = storage_root.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("STORAGE_DIRECTORY_UNAVAILABLE") from exc
    except ValueError as exc:
        raise PathSecurityError("STORAGE_PATH_NOT_ALLOWED") from exc
    return resolved_target


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _lstat_regular_file(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    if not stat.S_ISREG(value.st_mode):
        raise PathSecurityError("FILE_NOT_REGULAR")
    return value


def _lstat_directory(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise PathSecurityError("FILE_IMPORT_ROOT_UNAVAILABLE") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise PathSecurityError("FILE_IMPORT_ROOT_UNAVAILABLE")
    return value


def _pin_existing_root(root: Path) -> tuple[Path, Path, os.stat_result]:
    allowed_root = _absolute_without_resolving(root)
    _reject_reparse_points(allowed_root)
    try:
        resolved_root = allowed_root.resolve(strict=True)
    except OSError as exc:
        raise PathSecurityError("FILE_IMPORT_ROOT_UNAVAILABLE") from exc
    root_stat = _lstat_directory(resolved_root)
    return allowed_root, resolved_root, root_stat


def _assert_open_file_still_in_root(
    candidate: Path,
    allowed_root: Path,
    resolved_root: Path,
    root_stat: os.stat_result,
    opened_stat: os.stat_result,
) -> None:
    _reject_reparse_points(allowed_root)
    _reject_reparse_points(candidate)
    try:
        current_root = allowed_root.resolve(strict=True)
        current_candidate = candidate.resolve(strict=True)
        current_candidate.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("FILE_CHANGED_DURING_READ") from exc
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc
    current_root_stat = _lstat_directory(current_root)
    current_file_stat = _lstat_regular_file(current_candidate)
    if (
        current_root != resolved_root
        or not _same_file(root_stat, current_root_stat)
        or not _same_file(opened_stat, current_file_stat)
    ):
        raise PathSecurityError("FILE_CHANGED_DURING_READ")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _reject_reparse_points(path: Path) -> None:
    current = path
    while True:
        if _is_reparse_point(current):
            raise PathSecurityError("FILE_REPARSE_POINT_NOT_ALLOWED")
        if current == current.parent:
            return
        current = current.parent


def _is_reparse_point(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())
