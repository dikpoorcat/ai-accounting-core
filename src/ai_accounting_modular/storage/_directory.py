"""Publish one verified company directory without ever replacing a target."""

import ctypes
import errno
import os
import sys
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError


def _linux_rename(source: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise ContractError("unsupported", "当前系统没有无覆盖的原子目录发布能力")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # AT_FDCWD resolves absolute paths; RENAME_NOREPLACE is the atomic target guard.
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(target))


def _rename_no_replace(source: Path, target: Path) -> None:
    if sys.platform == "win32":
        os.rename(source, target)
    elif sys.platform.startswith("linux"):
        _linux_rename(source, target)
    else:
        raise ContractError("unsupported", "当前系统没有无覆盖的原子目录发布能力")


def publish_company_directory(staging: Path | str, target: Path | str) -> None:
    """Move an owned, fully built directory; failure leaves staging in place.

    The target parent must already exist on the staging filesystem. The final
    OS operation itself refuses an existing target, including an empty directory
    created after these checks. No fallback uses POSIX's replacing rename.
    """
    source = Path(os.path.abspath(staging))
    destination = Path(os.path.abspath(target))
    if source.is_symlink() or source.is_junction() or not source.is_dir():
        raise ContractError("invalid_path", "待发布位置必须是实际目录，不能是链接")
    source = source.resolve()
    destination = destination.parent.resolve() / destination.name
    if source == destination or source in destination.parents or destination in source.parents:
        raise ContractError("invalid_path", "待发布目录与目标必须不同且不能相互包含")
    if os.path.lexists(destination):
        raise ContractError("target_exists", "目标已存在，不能覆盖已有目录、文件或链接")
    if not destination.parent.is_dir():
        raise ContractError("invalid_path", "发布目标的上级目录必须已经存在")
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise ContractError("invalid_path", "目录原子发布必须位于同一文件系统")
    try:
        _rename_no_replace(source, destination)
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ContractError(
                "target_exists", "目标已存在，不能覆盖已有目录、文件或链接"
            ) from exc
        if exc.errno == errno.EXDEV:
            raise ContractError("invalid_path", "目录原子发布必须位于同一文件系统") from exc
        if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise ContractError("unsupported", "当前系统不支持无覆盖的原子目录发布") from exc
        raise
