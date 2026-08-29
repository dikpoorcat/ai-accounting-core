"""Windows backup-storage inspection and durable-publish boundaries."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .backup import BackupError
from .backup_integration import BackupIntegrationError

_DRIVE_REMOVABLE = 2
_MOVEFILE_WRITE_THROUGH = 0x00000008


def _configure_kernel32(kernel32: object) -> None:
    """Freeze Win32 signatures; ctypes' default ``int`` ABI is unsafe on x64."""
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]  # type: ignore[attr-defined]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint32  # type: ignore[attr-defined]
    kernel32.GetVolumePathNameW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    kernel32.GetVolumePathNameW.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.GetVolumeInformationW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    kernel32.GetVolumeInformationW.restype = ctypes.c_int  # type: ignore[attr-defined]
    kernel32.MoveFileExW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    kernel32.MoveFileExW.restype = ctypes.c_int  # type: ignore[attr-defined]


@dataclass(frozen=True)
class WindowsVolumeFacts:
    volume_root: Path
    drive_type: int
    filesystem: str
    volume_status: str
    protection_status: str
    encryption_percentage: int


class WindowsVolumeFactsProvider(Protocol):
    def inspect(self, path: Path) -> WindowsVolumeFacts: ...


class WindowsVolumeInspector:
    """Read drive/filesystem facts and BitLocker state without changing the volume."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise BackupIntegrationError("BACKUP_WINDOWS_VOLUME_CHECK_UNAVAILABLE")
        self._kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        _configure_kernel32(self._kernel32)

    def inspect(self, path: Path) -> WindowsVolumeFacts:
        root = self._volume_root(path)
        drive_type = int(self._kernel32.GetDriveTypeW(str(root)))
        if drive_type == 0:
            raise BackupIntegrationError("BACKUP_WINDOWS_VOLUME_CHECK_FAILED")
        filesystem = self._filesystem(root)
        bitlocker = self._bitlocker(root)
        try:
            return WindowsVolumeFacts(
                volume_root=root,
                drive_type=drive_type,
                filesystem=filesystem,
                volume_status=str(bitlocker["VolumeStatus"]),
                protection_status=str(bitlocker["ProtectionStatus"]),
                encryption_percentage=int(bitlocker["EncryptionPercentage"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupIntegrationError("BACKUP_BITLOCKER_STATUS_INVALID") from exc

    def _volume_root(self, path: Path) -> Path:
        buffer = ctypes.create_unicode_buffer(32_768)
        if not self._kernel32.GetVolumePathNameW(str(path), buffer, len(buffer)):
            raise BackupIntegrationError("BACKUP_WINDOWS_VOLUME_CHECK_FAILED")
        return Path(buffer.value)

    def _filesystem(self, root: Path) -> str:
        filesystem = ctypes.create_unicode_buffer(256)
        if not self._kernel32.GetVolumeInformationW(
            str(root),
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise BackupIntegrationError("BACKUP_WINDOWS_VOLUME_CHECK_FAILED")
        return filesystem.value

    @staticmethod
    def _bitlocker(root: Path) -> dict[str, object]:
        script = (
            "$ErrorActionPreference='Stop';"
            "$v=Get-BitLockerVolume -MountPoint $args[0];"
            "[pscustomobject]@{VolumeStatus=[string]$v.VolumeStatus;"
            "ProtectionStatus=[string]$v.ProtectionStatus;"
            "EncryptionPercentage=[int]$v.EncryptionPercentage}|ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            (
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                str(root),
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
        if result.returncode != 0:
            raise BackupIntegrationError("BACKUP_BITLOCKER_CHECK_FAILED")
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BackupIntegrationError("BACKUP_BITLOCKER_STATUS_INVALID") from exc
        if not isinstance(parsed, dict):
            raise BackupIntegrationError("BACKUP_BITLOCKER_STATUS_INVALID")
        return parsed


class WindowsCurrentUserOnlyAclVerifier:
    """Reject a pgpass lease unless its ACL grants read only to the current SID."""

    def assert_current_windows_user_only(self, path: Path) -> None:
        if sys.platform != "win32":
            raise BackupIntegrationError("BACKUP_PGPASS_ACL_UNAVAILABLE")
        absolute = Path(os.path.abspath(path))
        _reject_reparse_points(absolute)
        candidate = absolute.resolve(strict=True)
        if (
            candidate != absolute
            or not (candidate.is_file() or candidate.is_dir())
            or candidate.is_symlink()
        ):
            raise BackupIntegrationError("BACKUP_PGPASS_ACL_INVALID")
        before = candidate.stat()
        try:
            owner_sid, current_sid, protected, allows = _windows_acl_facts(candidate)
            if (
                protected is not True
                or owner_sid != current_sid
                or allows != (current_sid,)
            ):
                raise BackupIntegrationError("BACKUP_PGPASS_ACL_INVALID")
        except BackupIntegrationError:
            raise
        except Exception as exc:
            raise BackupIntegrationError("BACKUP_PGPASS_ACL_INVALID") from exc
        _reject_reparse_points(candidate)
        after = candidate.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BackupIntegrationError("BACKUP_PGPASS_ACL_INVALID")


class WindowsWriteThroughPublisher:
    """Publish by one write-through directory rename on the already-checked volume."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise BackupIntegrationError("BACKUP_DURABLE_PUBLISH_UNAVAILABLE")
        self._kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        _configure_kernel32(self._kernel32)

    def publish(self, partial: Path, complete: Path, root: Path) -> None:
        root_path = root.resolve(strict=True)
        partial_path = partial.resolve(strict=True)
        if (
            partial_path.parent != root_path
            or complete.parent.resolve(strict=True) != root_path
            or complete.exists()
            or partial_path.name.removesuffix(".partial")
            != complete.name.removesuffix(".complete")
        ):
            raise BackupError("BACKUP_PUBLISH_STATE_INVALID")
        if not self._move_write_through(partial_path, complete):
            if not partial.exists() or complete.exists():
                raise BackupError("BACKUP_PUBLISH_STATE_INVALID")
            raise BackupError("BACKUP_PUBLISH_FAILED")

    def durable_directory_preflight(self, root: Path) -> None:
        """Prove a flushed directory can be renamed write-through and reopened."""
        checked_root = root.resolve(strict=True)
        token = uuid.uuid4().hex
        partial = checked_root / f".backup-durable-probe-{token}.partial"
        complete = checked_root / f".backup-durable-probe-{token}.complete"
        payload = b"finance-backup-durable-probe-v1"
        try:
            partial.mkdir()
            probe = partial / "probe.bin"
            descriptor = os.open(
                probe,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if not self._move_write_through(partial, complete):
                raise BackupIntegrationError("BACKUP_DURABLE_PUBLISH_PREFLIGHT_FAILED")
            if (complete / "probe.bin").read_bytes() != payload:
                raise BackupIntegrationError("BACKUP_DURABLE_PUBLISH_PREFLIGHT_FAILED")
        except BackupIntegrationError:
            raise
        except OSError as exc:
            raise BackupIntegrationError("BACKUP_DURABLE_PUBLISH_PREFLIGHT_FAILED") from exc
        finally:
            for directory in (partial, complete):
                probe = directory / "probe.bin"
                if probe.is_file() and not probe.is_symlink():
                    probe.unlink()
                if directory.is_dir() and not directory.is_symlink():
                    directory.rmdir()

    def _move_write_through(self, source: Path, destination: Path) -> bool:
        return bool(
            self._kernel32.MoveFileExW(
                str(source),
                str(destination),
                _MOVEFILE_WRITE_THROUGH,
            )
        )


def preflight_windows_backup_root(
    backup_root: Path,
    facts_provider: WindowsVolumeFactsProvider,
    publisher: WindowsWriteThroughPublisher,
) -> WindowsVolumeFacts:
    """Optional policy helper requiring encrypted removable media."""
    try:
        root = backup_root.resolve(strict=True)
    except OSError as exc:
        raise BackupIntegrationError("BACKUP_STORAGE_UNAVAILABLE") from exc
    if not root.is_dir() or root.is_symlink():
        raise BackupIntegrationError("BACKUP_STORAGE_UNAVAILABLE")
    _reject_reparse_points(root)
    facts = facts_provider.inspect(root)
    try:
        root.relative_to(facts.volume_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BackupIntegrationError("BACKUP_WINDOWS_VOLUME_MISMATCH") from exc
    if facts.drive_type != _DRIVE_REMOVABLE:
        raise BackupIntegrationError("BACKUP_VOLUME_NOT_REMOVABLE")
    if (
        facts.volume_status != "FullyEncrypted"
        or facts.protection_status != "On"
        or facts.encryption_percentage != 100
    ):
        raise BackupIntegrationError("BACKUP_VOLUME_NOT_ENCRYPTED")
    publisher.durable_directory_preflight(root)
    return facts


def _reject_reparse_points(path: Path) -> None:
    current = path
    while True:
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (is_junction is not None and is_junction()):
            raise BackupIntegrationError("BACKUP_REPARSE_POINT_NOT_ALLOWED")
        if current == current.parent:
            return
        current = current.parent


def _windows_acl_facts(path: Path) -> tuple[str, str, bool, tuple[str, ...]]:
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            current = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )[0]
        finally:
            token.Close()
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        dacl = descriptor.GetSecurityDescriptorDacl()
        control, _ = descriptor.GetSecurityDescriptorControl()
        if owner is None or dacl is None:
            raise BackupIntegrationError("BACKUP_PGPASS_ACL_INVALID")
        allow_types = {
            win32security.ACCESS_ALLOWED_ACE_TYPE,
            getattr(win32security, "ACCESS_ALLOWED_OBJECT_ACE_TYPE", 5),
            getattr(win32security, "ACCESS_ALLOWED_CALLBACK_ACE_TYPE", 9),
            getattr(win32security, "ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE", 11),
        }
        allowed: set[str] = set()
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if ace[0][0] in allow_types:
                allowed.add(win32security.ConvertSidToStringSid(ace[-1]))
        return (
            win32security.ConvertSidToStringSid(owner),
            win32security.ConvertSidToStringSid(current),
            bool(control & win32security.SE_DACL_PROTECTED),
            tuple(sorted(allowed)),
        )
    except BackupIntegrationError:
        raise
    except Exception as exc:
        raise BackupIntegrationError("BACKUP_PGPASS_ACL_CHECK_FAILED") from exc


def _minimal_windows_environment() -> dict[str, str]:
    allowed = ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "PATHEXT")
    return {name: os.environ[name] for name in allowed if name in os.environ}
