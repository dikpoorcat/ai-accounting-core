"""Owner-private filesystem objects for local accounting state."""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class PrivatePathError(PermissionError):
    code = "private_path_permission_required"

    def __init__(self, path: str | Path):
        super().__init__(f"Private local accounting path is unavailable: {path}")


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_links(path: Path) -> None:
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise PrivatePathError(path)


def reject_reparse_path(path: str | Path) -> Path:
    """Return a lexical absolute path only after rejecting linked components."""
    target = _absolute(path)
    _reject_links(target)
    return target


def _posix_owner(info: os.stat_result, path: Path) -> None:
    if info.st_uid != os.geteuid():
        raise PrivatePathError(path)


def _posix_assert(path: Path, *, directory: bool) -> None:
    _reject_links(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise PrivatePathError(path) from exc
    _posix_owner(info, path)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    expected_mode = PRIVATE_DIRECTORY_MODE if directory else PRIVATE_FILE_MODE
    if not expected_type(info.st_mode) or stat.S_IMODE(info.st_mode) != expected_mode:
        raise PrivatePathError(path)


def _translate(path: Path, operation) -> None:
    try:
        operation()
    except FileExistsError:
        raise
    except (OSError, ValueError) as exc:
        raise PrivatePathError(path) from exc


def assert_private_directory(path: str | Path) -> Path:
    target = _absolute(path)
    if os.name == "nt":
        from .security.windows import assert_private_path

        _translate(target, lambda: assert_private_path(target, directory=True))
    else:
        _posix_assert(target, directory=True)
    return target


def assert_private_file(path: str | Path) -> Path:
    target = _absolute(path)
    if os.name == "nt":
        from .security.windows import assert_private_path

        _translate(target, lambda: assert_private_path(target))
    else:
        _posix_assert(target, directory=False)
    return target


def _create_directory(path: Path) -> None:
    if os.name == "nt":
        from .security.windows import create_private_directory

        _translate(path, lambda: create_private_directory(path))
        return
    _reject_links(path.parent)
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PrivatePathError(path) from exc
    _posix_assert(path, directory=True)


def ensure_private_directory(path: str | Path, *, parents: bool = False) -> Path:
    target = _absolute(path)
    if target.exists():
        _reject_links(target)
        if os.name == "nt":
            from .security.windows import ensure_private_path

            _translate(target, lambda: ensure_private_path(target, directory=True))
        else:
            try:
                info = target.lstat()
                _posix_owner(info, target)
                if not stat.S_ISDIR(info.st_mode):
                    raise PrivatePathError(target)
                os.chmod(target, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
            except OSError as exc:
                raise PrivatePathError(target) from exc
        return assert_private_directory(target)
    if not parents:
        _create_directory(target)
        return target
    missing: list[Path] = []
    candidate = target
    while not candidate.exists():
        missing.append(candidate)
        if candidate == candidate.parent:
            raise PrivatePathError(target)
        candidate = candidate.parent
    _reject_links(candidate)
    if not candidate.is_dir():
        raise PrivatePathError(target)
    for directory in reversed(missing):
        try:
            _create_directory(directory)
        except FileExistsError:
            ensure_private_directory(directory)
    return target


def create_private_file(path: str | Path) -> Path:
    target = _absolute(path)
    _reject_links(target.parent)
    if not target.parent.is_dir():
        raise PrivatePathError(target)
    if os.name == "nt":
        from .security.windows import create_private_empty_file

        try:
            create_private_empty_file(target)
        except FileExistsError:
            raise
        except (OSError, ValueError) as exc:
            raise PrivatePathError(target) from exc
    else:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
        except FileExistsError:
            raise
        except OSError as exc:
            raise PrivatePathError(target) from exc
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
    return assert_private_file(target)


def ensure_private_file(path: str | Path, *, create: bool = False) -> Path:
    target = _absolute(path)
    if not target.exists():
        if create:
            try:
                return create_private_file(target)
            except FileExistsError:
                pass
        else:
            raise PrivatePathError(target)
    _reject_links(target)
    if os.name == "nt":
        from .security.windows import ensure_private_path

        _translate(target, lambda: ensure_private_path(target))
    else:
        try:
            info = target.lstat()
            _posix_owner(info, target)
            if not stat.S_ISREG(info.st_mode):
                raise PrivatePathError(target)
            os.chmod(target, PRIVATE_FILE_MODE, follow_symlinks=False)
        except OSError as exc:
            raise PrivatePathError(target) from exc
    return assert_private_file(target)


def adopt_private_file(path: str | Path) -> Path:
    target = _absolute(path)
    _reject_links(target)
    if os.name == "nt":
        from .security.windows import adopt_private_path

        _translate(target, lambda: adopt_private_path(target))
    else:
        ensure_private_file(target)
    return assert_private_file(target)


def ensure_sqlite_sidecars(
    database: str | Path, *, existing: set[Path] | frozenset[Path] = frozenset()
) -> None:
    target = _absolute(database)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            if sidecar in existing:
                ensure_private_file(sidecar)
            else:
                adopt_private_file(sidecar)


@contextmanager
def private_temporary_directory(parent: str | Path, *, prefix: str):
    parent_path = _absolute(parent)
    if not parent_path.is_dir():
        raise PrivatePathError(parent_path)
    directory = parent_path / f"{prefix}{uuid.uuid4().hex}"
    ensure_private_directory(directory)
    try:
        yield directory
    finally:
        if directory.exists():
            shutil.rmtree(directory)
