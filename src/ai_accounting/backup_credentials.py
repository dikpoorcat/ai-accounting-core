"""Windows Credential Manager and protected pgpass leases for DEC-029 A."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import psycopg
from pydantic import SecretStr

from .backup_integration import (
    BackupIntegrationError,
    PgPassFileAccessVerifier,
    PostgresEndpoint,
    VerifiedArchiveCopyProvider,
)

WINDOWS_BACKUP_CREDENTIAL_TARGET = "ai-accounting-core/finance-backup-password/v1"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_SDDL_REVISION_1 = 1
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_BEGIN = 0
_FILE_ATTRIBUTE_TEMPORARY = 0x00000100
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ARCHIVE_COPY_CHUNK_BYTES = 1024 * 1024


class BackupCredentialError(BackupIntegrationError):
    """Stable failure without exposing a password or native exception text."""


class FinanceBackupPasswordStore(Protocol):
    def save_password(self, password: SecretStr) -> None: ...

    def load_password(self) -> SecretStr | None: ...

    def delete_password(self) -> None: ...


class WindowsFinanceBackupCredentialStore:
    """Store the dedicated finance_backup password for the current Windows user."""

    def __init__(self, *, target: str = WINDOWS_BACKUP_CREDENTIAL_TARGET) -> None:
        if sys.platform != "win32":
            raise BackupCredentialError("BACKUP_CREDENTIAL_STORE_UNAVAILABLE")
        _assert_backup_credential_layout()
        self._target = target
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        _configure_credential_api(self._advapi32)

    def save_password(self, password: SecretStr) -> None:
        raw = bytearray(password.get_secret_value().encode("utf-8"))
        try:
            if not raw or len(raw) > 2_560 or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
                raise BackupCredentialError("BACKUP_CREDENTIAL_INVALID")
            blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            credential = _CredentialW()
            credential.Type = _CRED_TYPE_GENERIC
            credential.TargetName = self._target
            credential.CredentialBlobSize = len(raw)
            credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
            credential.UserName = "finance_backup"
            if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
                raise BackupCredentialError("BACKUP_CREDENTIAL_WRITE_FAILED")
        finally:
            for index in range(len(raw)):
                raw[index] = 0

    def load_password(self) -> SecretStr | None:
        pointer = ctypes.POINTER(_CredentialW)()
        if not self._advapi32.CredReadW(
            self._target,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise BackupCredentialError("BACKUP_CREDENTIAL_READ_FAILED")
        try:
            size = int(pointer.contents.CredentialBlobSize)
            if size <= 0 or size > 2_560:
                raise BackupCredentialError("BACKUP_CREDENTIAL_READ_FAILED")
            raw = bytearray(ctypes.string_at(pointer.contents.CredentialBlob, size))
            try:
                password = raw.decode("utf-8")
                if not password or any(character in password for character in "\x00\r\n"):
                    raise BackupCredentialError("BACKUP_CREDENTIAL_READ_FAILED")
                return SecretStr(password)
            except UnicodeDecodeError as exc:
                raise BackupCredentialError("BACKUP_CREDENTIAL_READ_FAILED") from exc
            finally:
                for index in range(len(raw)):
                    raw[index] = 0
        finally:
            self._advapi32.CredFree(pointer)

    def delete_password(self) -> None:
        if not self._advapi32.CredDeleteW(self._target, _CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() != 1168:
                raise BackupCredentialError("BACKUP_CREDENTIAL_DELETE_FAILED")


class WindowsProtectedPgPassProvider:
    """Materialize one pgpass inside a current-user-only temporary directory."""

    def __init__(
        self,
        credential_store: FinanceBackupPasswordStore,
        lease_root: Path,
        access_verifier: PgPassFileAccessVerifier,
    ) -> None:
        self._credential_store = credential_store
        self._lease_root = lease_root
        self._access_verifier = access_verifier

    @contextmanager
    def lease_pgpass(self, endpoint: PostgresEndpoint) -> Iterator[Path]:
        password = self._credential_store.load_password()
        if password is None:
            raise BackupCredentialError("BACKUP_CREDENTIAL_REQUIRED")
        content = _pgpass_content(endpoint, password)
        directory, pgpass = _create_protected_pgpass(
            self._lease_root,
            content,
            self._access_verifier,
        )
        try:
            yield pgpass
        finally:
            _delete_protected_pgpass(directory, pgpass)


class WindowsProtectedArchiveCopyProvider(VerifiedArchiveCopyProvider):
    """Lease a current-user-only copy pinned against replacement until restore ends."""

    def __init__(
        self,
        lease_root: Path,
        access_verifier: PgPassFileAccessVerifier,
    ) -> None:
        self._lease_root = lease_root
        self._access_verifier = access_verifier

    @contextmanager
    def lease_verified_archive(
        self,
        source: Path,
        source_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> Iterator[Path]:
        directory, archive, handle, kernel32 = _create_protected_archive_copy(
            source,
            source_root,
            self._lease_root,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
            verifier=self._access_verifier,
        )
        try:
            yield archive
        finally:
            if not kernel32.CloseHandle(handle):
                raise BackupCredentialError("BACKUP_ARCHIVE_COPY_CLEANUP_FAILED")
            if not kernel32.DeleteFileW(str(archive)):
                raise BackupCredentialError("BACKUP_ARCHIVE_COPY_CLEANUP_FAILED")
            if not kernel32.RemoveDirectoryW(str(directory)):
                raise BackupCredentialError("BACKUP_ARCHIVE_COPY_CLEANUP_FAILED")


class CredentialManagerConnectionProvider:
    """Open the snapshot connection without a password URL or process environment."""

    def __init__(self, credential_store: FinanceBackupPasswordStore) -> None:
        self._credential_store = credential_store

    @contextmanager
    def connect(self, endpoint: PostgresEndpoint):  # type: ignore[no-untyped-def]
        password = self._credential_store.load_password()
        if password is None:
            raise BackupCredentialError("BACKUP_CREDENTIAL_REQUIRED")
        try:
            with psycopg.connect(
                host=endpoint.host,
                port=endpoint.port,
                dbname=endpoint.database,
                user=endpoint.username,
                password=password.get_secret_value(),
                application_name=endpoint.application_name,
                connect_timeout=15,
            ) as connection:
                yield connection
        except BackupCredentialError:
            raise
        except Exception as exc:
            raise BackupCredentialError("BACKUP_DATABASE_CONNECTION_FAILED") from exc


def _pgpass_content(endpoint: PostgresEndpoint, password: SecretStr) -> bytearray:
    fields = (
        endpoint.host,
        str(endpoint.port),
        endpoint.database,
        endpoint.username,
        password.get_secret_value(),
    )
    if any(any(character in field for character in "\x00\r\n") for field in fields):
        raise BackupCredentialError("BACKUP_CREDENTIAL_INVALID")
    escaped = tuple(field.replace("\\", "\\\\").replace(":", "\\:") for field in fields)
    return bytearray((":".join(escaped) + "\n").encode("utf-8"))


def _create_protected_pgpass(
    lease_root: Path,
    content: bytearray,
    verifier: PgPassFileAccessVerifier,
) -> tuple[Path, Path]:
    if sys.platform != "win32":
        _zero(content)
        raise BackupCredentialError("BACKUP_PGPASS_UNAVAILABLE")
    root = _validated_lease_root(lease_root)
    sid = _current_windows_sid()
    security_descriptor = _security_descriptor_for_sid(sid)
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), security_descriptor, 0
    )
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    _configure_protected_file_api(kernel32)
    directory = root / f".finance-pgpass-{uuid.uuid4().hex}"
    pgpass = directory / "pgpass.conf"
    handle: int | None = None
    try:
        if not kernel32.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
            raise BackupCredentialError("BACKUP_PGPASS_CREATE_FAILED")
        verifier.assert_current_windows_user_only(directory)
        handle = kernel32.CreateFileW(
            str(pgpass),
            _GENERIC_WRITE,
            0,
            ctypes.byref(attributes),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_TEMPORARY,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise BackupCredentialError("BACKUP_PGPASS_CREATE_FAILED")
        buffer = (ctypes.c_ubyte * len(content)).from_buffer(content)
        written = ctypes.c_uint32()
        if not kernel32.WriteFile(
            handle,
            ctypes.byref(buffer),
            len(content),
            ctypes.byref(written),
            None,
        ) or written.value != len(content):
            raise BackupCredentialError("BACKUP_PGPASS_WRITE_FAILED")
        if not kernel32.FlushFileBuffers(handle):
            raise BackupCredentialError("BACKUP_PGPASS_WRITE_FAILED")
        if not kernel32.CloseHandle(handle):
            raise BackupCredentialError("BACKUP_PGPASS_WRITE_FAILED")
        handle = None
        verifier.assert_current_windows_user_only(pgpass)
        verifier.assert_current_windows_user_only(directory)
        return directory, pgpass
    except Exception:
        if handle not in (None, _INVALID_HANDLE_VALUE):
            kernel32.CloseHandle(handle)
        if pgpass.exists() and not pgpass.is_symlink():
            kernel32.DeleteFileW(str(pgpass))
        if directory.exists() and not directory.is_symlink():
            kernel32.RemoveDirectoryW(str(directory))
        raise
    finally:
        kernel32.LocalFree(security_descriptor)
        _zero(content)


def _delete_protected_pgpass(directory: Path, pgpass: Path) -> None:
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    _configure_protected_file_api(kernel32)
    if not kernel32.DeleteFileW(str(pgpass)):
        raise BackupCredentialError("BACKUP_PGPASS_CLEANUP_FAILED")
    if not kernel32.RemoveDirectoryW(str(directory)):
        raise BackupCredentialError("BACKUP_PGPASS_CLEANUP_FAILED")


def _create_protected_archive_copy(
    source: Path,
    source_root: Path,
    lease_root: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    verifier: PgPassFileAccessVerifier,
) -> tuple[Path, Path, int, object]:
    if (
        sys.platform != "win32"
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or expected_size_bytes < 0
    ):
        raise BackupCredentialError("BACKUP_ARCHIVE_COPY_UNAVAILABLE")
    source_path, source_stat = _validated_archive_source(source, source_root)
    if source_stat.st_size != expected_size_bytes:
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_CHANGED")
    root = _validated_lease_root(lease_root)
    sid = _current_windows_sid()
    security_descriptor = _security_descriptor_for_sid(sid)
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), security_descriptor, 0
    )
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    _configure_protected_file_api(kernel32)
    directory = root / f".finance-restore-{uuid.uuid4().hex}"
    archive = directory / "database.dump"
    source_handle: int | None = None
    destination_handle: int | None = None
    try:
        if not kernel32.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
            raise BackupCredentialError("BACKUP_ARCHIVE_COPY_CREATE_FAILED")
        verifier.assert_current_windows_user_only(directory)
        source_handle = kernel32.CreateFileW(
            str(source_path),
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if source_handle == _INVALID_HANDLE_VALUE:
            raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
        source_info = _file_information(kernel32, source_handle)
        if (
            source_info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or _file_size(source_info) != expected_size_bytes
            or _file_index(source_info) != source_stat.st_ino
        ):
            raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_CHANGED")
        _assert_archive_path_still_same(source_path, source_stat)
        destination_handle = kernel32.CreateFileW(
            str(archive),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ,
            ctypes.byref(attributes),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_TEMPORARY | _FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if destination_handle == _INVALID_HANDLE_VALUE:
            raise BackupCredentialError("BACKUP_ARCHIVE_COPY_CREATE_FAILED")
        copied_sha256, copied_size = _copy_windows_handles(
            kernel32, source_handle, destination_handle
        )
        if not kernel32.FlushFileBuffers(destination_handle):
            raise BackupCredentialError("BACKUP_ARCHIVE_COPY_WRITE_FAILED")
        _assert_archive_path_still_same(source_path, source_stat)
        source_after = _file_information(kernel32, source_handle)
        if (
            _file_index(source_after) != _file_index(source_info)
            or _file_size(source_after) != _file_size(source_info)
            or copied_sha256 != expected_sha256
            or copied_size != expected_size_bytes
        ):
            raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_CHANGED")
        copied_again_sha256, copied_again_size = _hash_windows_handle(
            kernel32, destination_handle
        )
        if (
            copied_again_sha256 != expected_sha256
            or copied_again_size != expected_size_bytes
        ):
            raise BackupCredentialError("BACKUP_ARCHIVE_COPY_MISMATCH")
        verifier.assert_current_windows_user_only(archive)
        verifier.assert_current_windows_user_only(directory)
        if not kernel32.CloseHandle(source_handle):
            raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
        source_handle = None
        return directory, archive, destination_handle, kernel32
    except Exception:
        if source_handle not in (None, _INVALID_HANDLE_VALUE):
            kernel32.CloseHandle(source_handle)
        if destination_handle not in (None, _INVALID_HANDLE_VALUE):
            kernel32.CloseHandle(destination_handle)
        if archive.exists() and not archive.is_symlink():
            kernel32.DeleteFileW(str(archive))
        if directory.exists() and not directory.is_symlink():
            kernel32.RemoveDirectoryW(str(directory))
        raise
    finally:
        kernel32.LocalFree(security_descriptor)


def _validated_archive_source(source: Path, source_root: Path) -> tuple[Path, os.stat_result]:
    root = Path(os.path.abspath(source_root))
    candidate = Path(os.path.abspath(source))
    try:
        resolved_root = root.resolve(strict=True)
        resolved_source = candidate.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE") from exc
    if (
        resolved_root != root
        or resolved_source != candidate
        or candidate.parent != root
        or candidate.name != "database.dump"
    ):
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
    _reject_reparse_points(root)
    _reject_reparse_points(candidate)
    try:
        source_stat = candidate.lstat()
    except OSError as exc:
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
    return candidate, source_stat


def _assert_archive_path_still_same(
    source: Path, expected: os.stat_result
) -> None:
    _reject_reparse_points(source)
    try:
        current = source.lstat()
    except OSError as exc:
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_CHANGED") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
        or current.st_size != expected.st_size
    ):
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_CHANGED")


def _reject_reparse_points(path: Path) -> None:
    current = path
    while True:
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (is_junction is not None and is_junction()):
            raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
        if current == current.parent:
            return
        current = current.parent


def _copy_windows_handles(
    kernel32: object, source_handle: int, destination_handle: int
) -> tuple[str, int]:
    _rewind_windows_handle(kernel32, source_handle)
    digest = hashlib.sha256()
    size = 0
    buffer = (ctypes.c_ubyte * _ARCHIVE_COPY_CHUNK_BYTES)()
    while True:
        read = ctypes.c_uint32()
        if not kernel32.ReadFile(  # type: ignore[attr-defined]
            source_handle,
            ctypes.byref(buffer),
            len(buffer),
            ctypes.byref(read),
            None,
        ):
            raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
        if read.value == 0:
            return digest.hexdigest(), size
        content = bytes(buffer[: read.value])
        digest.update(content)
        size += read.value
        written = ctypes.c_uint32()
        if not kernel32.WriteFile(  # type: ignore[attr-defined]
            destination_handle,
            ctypes.byref(buffer),
            read.value,
            ctypes.byref(written),
            None,
        ) or written.value != read.value:
            raise BackupCredentialError("BACKUP_ARCHIVE_COPY_WRITE_FAILED")


def _hash_windows_handle(kernel32: object, handle: int) -> tuple[str, int]:
    _rewind_windows_handle(kernel32, handle)
    digest = hashlib.sha256()
    size = 0
    buffer = (ctypes.c_ubyte * _ARCHIVE_COPY_CHUNK_BYTES)()
    while True:
        read = ctypes.c_uint32()
        if not kernel32.ReadFile(  # type: ignore[attr-defined]
            handle,
            ctypes.byref(buffer),
            len(buffer),
            ctypes.byref(read),
            None,
        ):
            raise BackupCredentialError("BACKUP_ARCHIVE_COPY_MISMATCH")
        if read.value == 0:
            return digest.hexdigest(), size
        digest.update(bytes(buffer[: read.value]))
        size += read.value


def _rewind_windows_handle(kernel32: object, handle: int) -> None:
    if not kernel32.SetFilePointerEx(  # type: ignore[attr-defined]
        handle, ctypes.c_longlong(0), None, _FILE_BEGIN
    ):
        raise BackupCredentialError("BACKUP_ARCHIVE_COPY_MISMATCH")


def _file_information(kernel32: object, handle: int) -> _ByHandleFileInformation:
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(  # type: ignore[attr-defined]
        handle, ctypes.byref(information)
    ):
        raise BackupCredentialError("BACKUP_ARCHIVE_SOURCE_UNAVAILABLE")
    return information


def _file_size(information: _ByHandleFileInformation) -> int:
    return (information.nFileSizeHigh << 32) | information.nFileSizeLow


def _file_index(information: _ByHandleFileInformation) -> int:
    return (information.nFileIndexHigh << 32) | information.nFileIndexLow


def _validated_lease_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise BackupCredentialError("BACKUP_PGPASS_ROOT_UNAVAILABLE") from exc
    if resolved != absolute or not resolved.is_dir() or resolved.is_symlink():
        raise BackupCredentialError("BACKUP_PGPASS_ROOT_UNAVAILABLE")
    current = resolved
    while True:
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (is_junction is not None and is_junction()):
            raise BackupCredentialError("BACKUP_PGPASS_ROOT_UNAVAILABLE")
        if current == current.parent:
            return resolved
        current = current.parent


def _current_windows_sid() -> str:
    script = "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    result = subprocess.run(
        (
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ),
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        env=_minimal_windows_environment(),
    )
    sid = result.stdout.strip()
    if result.returncode != 0 or not sid.startswith("S-1-"):
        raise BackupCredentialError("BACKUP_WINDOWS_IDENTITY_UNAVAILABLE")
    return sid


def _security_descriptor_for_sid(sid: str) -> int:
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise BackupCredentialError("BACKUP_PGPASS_ACL_CREATE_FAILED")
    if descriptor.value is None:
        raise BackupCredentialError("BACKUP_PGPASS_ACL_CREATE_FAILED")
    return descriptor.value


def _minimal_windows_environment() -> dict[str, str]:
    allowed = ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "PATHEXT")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _configure_credential_api(advapi32: object) -> None:
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), ctypes.c_uint32]  # type: ignore[attr-defined]
    advapi32.CredWriteW.restype = ctypes.c_int  # type: ignore[attr-defined]
    advapi32.CredReadW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    advapi32.CredReadW.restype = ctypes.c_int  # type: ignore[attr-defined]
    advapi32.CredDeleteW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    advapi32.CredDeleteW.restype = ctypes.c_int  # type: ignore[attr-defined]
    advapi32.CredFree.argtypes = [ctypes.c_void_p]  # type: ignore[attr-defined]
    advapi32.CredFree.restype = None  # type: ignore[attr-defined]


def _configure_protected_file_api(kernel32: object) -> None:
    kernel32.CreateDirectoryW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.POINTER(_SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_SecurityAttributes),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p  # type: ignore[attr-defined]
    kernel32.WriteFile.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.ReadFile.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]  # type: ignore[attr-defined]
    kernel32.FlushFileBuffers.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.SetFilePointerEx.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        ctypes.c_uint32,
    ]
    kernel32.SetFilePointerEx.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.GetFileInformationByHandle.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]  # type: ignore[attr-defined]
    kernel32.CloseHandle.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.DeleteFileW.argtypes = [ctypes.c_wchar_p]  # type: ignore[attr-defined]
    kernel32.DeleteFileW.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.RemoveDirectoryW.argtypes = [ctypes.c_wchar_p]  # type: ignore[attr-defined]
    kernel32.RemoveDirectoryW.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]  # type: ignore[attr-defined]
    kernel32.LocalFree.restype = ctypes.c_void_p  # type: ignore[attr-defined]


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


def _assert_backup_credential_layout() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    expected_size = 80 if pointer_size == 8 else 52 if pointer_size == 4 else None
    if expected_size is None or ctypes.sizeof(_CredentialW) != expected_size:
        raise BackupCredentialError("BACKUP_CREDENTIAL_STORE_UNAVAILABLE")
