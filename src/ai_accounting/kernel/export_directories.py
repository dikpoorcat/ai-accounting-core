"""Resolve export parents and publish complete task directories without replacement."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PureWindowsPath
from typing import Annotated

from pydantic import Field

from .contracts import KernelError

ExportParentDirectory = Annotated[
    str,
    Field(
        description="输出父目录，可使用已有目录；省略时使用当前公司的 exports 目录。"
        "内核会为每个任务创建独立子目录，实际文件位置以成功任务结果为准。",
    ),
]


def resolve_export_parent(directory: str | None, database: Path) -> Path:
    # Keep explicit path normalization identical to previously submitted requests.
    # Cached requests are checked by the caller before validating a new parent.
    if directory is not None and not isinstance(directory, str):
        raise KernelError("export_parent_invalid", "导出父目录必须是目录路径")
    try:
        return (database.parent / "exports" if directory is None else Path(directory)).resolve()
    except (OSError, ValueError) as exc:
        raise KernelError("export_parent_invalid", "导出父目录路径无效") from exc


def validate_export_parent(parent: Path, supplied: str | None) -> None:
    """Reject existing file blockers outside the database write transaction."""
    if supplied is not None and not supplied.strip():
        raise KernelError("export_parent_invalid", "导出父目录不能是空白路径")
    if os.name == "nt":
        # Inspect the original components too: Windows resolution can strip an
        # invalid trailing dot/space. The anchor contains valid drive/UNC syntax.
        _validate_windows_components(str(parent) if supplied is None else supplied)
    try:
        for path in (parent, *parent.parents):
            if path.exists():
                if not path.is_dir():
                    raise KernelError("export_parent_invalid", "导出父目录或上级路径不是目录")
                return
    except OSError as exc:
        raise KernelError("export_parent_invalid", "导出父目录无法访问") from exc


def _validate_windows_components(directory: str) -> None:
    path = PureWindowsPath(directory)
    components = list(path.parts[1:] if path.anchor else path.parts)
    # UNC server/share names belong to the anchor but still must be valid names.
    # Device/extended prefixes are syntax, not server or share components.
    anchor = path.anchor
    unc_components = []
    if anchor.upper().startswith("\\\\?\\UNC\\"):
        unc_components = anchor[8:].rstrip("\\").split("\\")
    elif anchor.upper() == "\\\\.\\UNC\\":
        unc_components, components = components[:2], components[2:]
    elif anchor.startswith("\\\\") and not anchor.startswith(("\\\\?\\", "\\\\.\\")):
        unc_components = anchor[2:].rstrip("\\").split("\\")
    # Shares identify resources, not filesystem objects: DOS reserved file
    # names and terminal dot/space rules do not apply to the UNC volume names.
    for component in unc_components:
        if not component or any(
            character in '\\/[]:;|=,+*?<>"' or ord(character) < 32 for character in component
        ):
            raise KernelError("export_parent_invalid", "导出父目录含有无效的 UNC 名称")
    if len(unc_components) == 2 and len(unc_components[1]) > 80:
        raise KernelError("export_parent_invalid", "导出父目录的 UNC 共享名称超过允许长度")
    for component in components:
        if component in {".", ".."}:
            continue
        if (
            any(character in '?*<>:"|' or ord(character) < 32 for character in component)
            or component.endswith((".", " "))
            or PureWindowsPath(component).is_reserved()
        ):
            raise KernelError("export_parent_invalid", "导出父目录含有 Windows 不允许的名称")


def publish_export_directory(staging: Path, target: Path) -> None:
    """Atomically move a complete directory, preserving even an empty target."""
    if os.name == "nt":
        staging.rename(target)
        return

    # POSIX rename can replace an existing empty directory. Use the native
    # exclusive operation instead; never fall back to replacement semantics.
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        rename = getattr(library, "renameat2", None)
        # AT_FDCWD=-100; RENAME_NOREPLACE=1 (Linux UAPI).
        arguments = (-100, os.fsencode(staging), -100, os.fsencode(target), 1)
        signature = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform == "darwin":
        rename = getattr(library, "renamex_np", None)
        # RENAME_EXCL=4 (Darwin sys/stdio.h).
        arguments = (os.fsencode(staging), os.fsencode(target), 4)
        signature = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    else:
        rename = None
    if rename is None:
        raise OSError(errno.ENOTSUP, "Exclusive export directory publication is unavailable")
    rename.argtypes = signature
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))
