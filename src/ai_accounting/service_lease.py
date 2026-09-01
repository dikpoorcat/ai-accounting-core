"""Cross-process Windows lease shared by production services and exclusive backups."""

from __future__ import annotations

import ctypes
import msvcrt
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Literal, Protocol

_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_LOCK_VIOLATION = 33
_LEASE_MODES = {"service", "backup"}


class ServiceLeaseError(RuntimeError):
    """Stable refusal for an unavailable or already-held service lease."""


class ServiceLeaseAccessVerifier(Protocol):
    def assert_current_windows_user_only(self, path: Path) -> None: ...


class WindowsBackupServiceLease:
    """Adapter implementing ``BackupServiceLease`` without a boolean stop claim."""

    def __init__(
        self,
        lock_file: Path,
        access_verifier: ServiceLeaseAccessVerifier,
    ) -> None:
        self._lock_file = lock_file
        self._access_verifier = access_verifier

    def acquire_backup_lease(self) -> AbstractContextManager[None]:
        return acquire_windows_service_lease(
            self._lock_file,
            mode="backup",
            access_verifier=self._access_verifier,
        )


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


def _configure_kernel32(kernel32: object) -> None:
    kernel32.LockFileEx.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    ]
    kernel32.LockFileEx.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.UnlockFileEx.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    ]
    kernel32.UnlockFileEx.restype = ctypes.c_int  # type: ignore[attr-defined]


@contextmanager
def acquire_windows_service_lease(
    lock_file: Path,
    *,
    mode: Literal["service", "backup"],
    access_verifier: ServiceLeaseAccessVerifier,
) -> Iterator[None]:
    """Hold one shared-service or exclusive-backup byte until the context exits.

    Each Codex task can own a separate STDIO MCP process, so service mode uses a
    shared lock and allows those processes to coexist. Backup mode uses the same
    byte as an exclusive lock: any running service blocks backup, and a backup in
    progress blocks every service start until the verified ``.complete``
    publication has finished. Windows releases the byte-range lock if a process
    exits unexpectedly.
    """
    if mode not in _LEASE_MODES or sys.platform != "win32":
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
    candidate = _validated_lock_file(lock_file)
    before_open = _lstat_regular_file(candidate)
    access_verifier.assert_current_windows_user_only(candidate)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    _configure_kernel32(kernel32)
    descriptor = -1
    locked = False
    yielded = False
    overlapped = _Overlapped()
    try:
        descriptor = os.open(candidate, os.O_RDWR | getattr(os, "O_BINARY", 0))
        opened = os.fstat(descriptor)
        if not _same_file(before_open, opened) or not stat.S_ISREG(opened.st_mode):
            raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))
        lock_flags = _LOCKFILE_FAIL_IMMEDIATELY
        if mode == "backup":
            lock_flags |= _LOCKFILE_EXCLUSIVE_LOCK
        locked = bool(
            kernel32.LockFileEx(
                handle,
                lock_flags,
                0,
                1,
                0,
                ctypes.byref(overlapped),
            )
        )
        if not locked:
            error = ctypes.get_last_error()
            if error == _ERROR_LOCK_VIOLATION:
                raise ServiceLeaseError("SERVICE_LEASE_HELD")
            raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
        # Re-check the named file after locking its handle. The deployment ACL
        # verifier must reject inherited or non-current-user read grants.
        if candidate.resolve(strict=True) != Path(os.path.abspath(lock_file)):
            raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
        _assert_named_file_matches_handle(candidate, opened)
        access_verifier.assert_current_windows_user_only(candidate)
        yielded = True
        yield
    except ServiceLeaseError:
        raise
    except Exception as exc:
        if yielded:
            raise
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE") from exc
    finally:
        release_error: ServiceLeaseError | None = None
        if descriptor >= 0:
            if locked:
                try:
                    _assert_named_file_matches_handle(candidate, opened)
                except ServiceLeaseError as exc:
                    release_error = exc
                handle = ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))
                if not kernel32.UnlockFileEx(  # type: ignore[possibly-undefined]
                    handle, 0, 1, 0, ctypes.byref(overlapped)
                ):
                    release_error = ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
            os.close(descriptor)
        if release_error is not None:
            raise release_error


def _validated_lock_file(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        candidate = absolute.resolve(strict=True)
    except OSError as exc:
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE") from exc
    if candidate != absolute or not candidate.is_file() or candidate.is_symlink():
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
    return candidate


def _lstat_regular_file(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE") from exc
    if not stat.S_ISREG(value.st_mode):
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")
    return value


def _assert_named_file_matches_handle(path: Path, opened: os.stat_result) -> None:
    current = _lstat_regular_file(path)
    if not _same_file(current, opened):
        raise ServiceLeaseError("SERVICE_LEASE_UNAVAILABLE")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino
