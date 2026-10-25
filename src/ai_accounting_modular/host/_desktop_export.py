"""Save verified report bytes to this Windows user's actual desktop.

The desktop copy is user-owned. It is neither a file-task output nor a tracked
backup, and an existing file is never replaced. Path checks follow the local
single-user trust boundary; they are not an adversarial filesystem sandbox.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from uuid import UUID

from ai_accounting_modular.contracts.public import ContractError, validate_contract

_CHUNK = 1024 * 1024
_NAME_SURROGATE = 0x20000000
_MAX_NAMES = 100


def desktop_directory() -> Path:
    """Resolve the current redirected Desktop without creating it or guessing."""
    if sys.platform != "win32":
        raise ContractError("unsupported", "当前桌面报表交付仅支持 Windows。")
    identifier = (ctypes.c_ubyte * 16).from_buffer_copy(
        UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le
    )
    shell = ctypes.WinDLL("Shell32", use_last_error=True)
    ole = ctypes.WinDLL("Ole32", use_last_error=True)
    function = shell.SHGetKnownFolderPath
    function.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    function.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    value = ctypes.c_void_p()
    try:
        result = function(ctypes.byref(identifier), 0, None, ctypes.byref(value))
        if result != 0 or not value.value:
            raise ContractError("desktop_unavailable", "无法取得当前 Windows 用户的桌面目录。")
        return Path(ctypes.wstring_at(value))
    finally:
        if value.value:
            ole.CoTaskMemFree(value)


def _linked(info):
    # Cloud placeholders also have REPARSE_POINT. Only name-surrogate tags
    # represent another named entity; ordinary OneDrive directories stay usable.
    return stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE
    )


def _identity(info):
    return info.st_dev, info.st_ino


def _directory(path, expected=None):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ContractError("unsafe_path", "桌面必须是操作系统提供的明确绝对目录。")
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ContractError("unsafe_path", "桌面路径不能经过符号链接或目录联接。")
    if expected is not None and _identity(info) != expected:
        raise ContractError("desktop_changed", "保存期间桌面目录已变化，请重新交付。")
    return path, _identity(info)


def _name(download):
    value = download.file_name
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError("report_delivery_invalid", "报表交付文件名无效。")
    parts = PureWindowsPath(value)
    if parts.drive or parts.root or len(parts.parts) != 1 or "/" in value or "\\" in value:
        raise ContractError("unsafe_path", "报表交付只能提供文件名，不能提供保存路径。")
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "_", value).strip(" .")
    parts = PureWindowsPath(value)
    try:
        encoded_size = len(value.encode("utf-16-le"))
    except UnicodeError as error:
        raise ContractError(
            "report_delivery_invalid", "报表文件名包含无效的 Windows 字符。"
        ) from error
    if (
        not value
        or parts.suffix.lower() != ".xlsx"
        or encoded_size > 400
        or parts.is_reserved()
    ):
        raise ContractError("report_delivery_invalid", "报表文件名须为长度适当的 Excel 文件名。")
    validate_contract("core.schema.json", "Id", download.task_id)
    if (
        not isinstance(download.sha256, str)
        or re.fullmatch("[0-9a-f]{64}", download.sha256) is None
        or type(download.byte_size) is not int
        or download.byte_size < 0
    ):
        raise ContractError("report_delivery_invalid", "报表交付缺少准确的文件摘要或字节数。")
    return value


def _matches(path, digest, size):
    """Inspect an existing independent file; never follow a linked candidate."""
    try:
        before = path.lstat()
        if _linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return False
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                _identity(opened) != _identity(before)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != size
            ):
                return False
            actual, count = hashlib.sha256(), 0
            while chunk := stream.read(min(_CHUNK, size - count + 1)):
                count += len(chunk)
                if count > size:
                    return False
                actual.update(chunk)
        after = path.lstat()
        return (
            not _linked(after)
            and _identity(before) == _identity(after)
            and before.st_mtime_ns == after.st_mtime_ns
            and after.st_nlink == 1
            and (count, actual.hexdigest()) == (size, digest)
        )
    except OSError:
        # An occupied or unreadable name is preserved; try another explicit name.
        return False


def _copy(download, output):
    digest, size = hashlib.sha256(), 0
    while True:
        try:
            chunk = download.stream.read(_CHUNK)
        except (AttributeError, TypeError, ValueError) as error:
            raise ContractError(
                "report_delivery_invalid", "报表交付流已经关闭或无法读取。"
            ) from error
        if not isinstance(chunk, bytes):
            raise ContractError("report_delivery_invalid", "报表交付流必须提供准确的二进制内容。")
        if not chunk:
            break
        size += len(chunk)
        if size > download.byte_size:
            raise ContractError("report_delivery_invalid", "报表交付字节数与核验结果不符。")
        digest.update(chunk)
        output.write(chunk)
    if (size, digest.hexdigest()) != (download.byte_size, download.sha256):
        raise ContractError("report_delivery_invalid", "报表交付内容与已核验摘要或字节数不符。")
    output.flush()
    os.fsync(output.fileno())


def _candidates(root, name, task_id):
    yield root / name
    stem = name[:-5]
    for number in range(1, _MAX_NAMES):
        suffix = task_id if number == 1 else f"{task_id}-{number}"
        yield root / f"{stem} ({suffix}).xlsx"


def save_report(download, *, desktop_resolver=None):
    """Copy one exact report; retries reuse equal bytes and preserve other files."""
    if sys.platform != "win32":
        raise ContractError("unsupported", "当前桌面报表交付仅支持 Windows。")
    name = _name(download)
    temporary = temporary_identity = None
    root = root_identity = None
    try:
        root, root_identity = _directory((desktop_resolver or desktop_directory)())
        with tempfile.NamedTemporaryFile(
            prefix=".finance-report-", suffix=".tmp", dir=root, delete=False
        ) as output:
            temporary = Path(output.name)
            temporary_identity = _identity(os.fstat(output.fileno()))
            _copy(download, output)
        for target in _candidates(root, name, download.task_id):
            _directory(root, root_identity)
            state = "reused"
            if not _matches(target, download.sha256, download.byte_size):
                try:
                    # Same-directory Windows rename is exclusive, including a
                    # competing writer that appears after the checks above.
                    os.rename(temporary, target)
                except FileExistsError:
                    if not _matches(target, download.sha256, download.byte_size):
                        continue
                else:
                    temporary = None
                    state = "copied"
            _directory(root, root_identity)
            if not _matches(target, download.sha256, download.byte_size):
                raise ContractError(
                    "desktop_file_invalid", "桌面文件未通过最终内容核验，请重新交付。"
                )
            return {
                "state": state,
                "path": str(target),
                "sha256": download.sha256,
                "byte_size": download.byte_size,
            }
        raise ContractError(
            "desktop_export_conflict", "桌面报表文件名已被占用，请整理同名文件后重试。"
        )
    except OSError as error:
        raise ContractError(
            "desktop_export_unavailable", "报表未能完整保存到桌面，请检查桌面目录或稍后重试。"
        ) from error
    finally:
        if temporary is not None:
            try:
                _directory(root, root_identity)
                info = temporary.lstat()
                if not _linked(info) and _identity(info) == temporary_identity:
                    temporary.unlink()
            except (ContractError, OSError):
                # Never follow a changed directory or remove someone else's file.
                pass
