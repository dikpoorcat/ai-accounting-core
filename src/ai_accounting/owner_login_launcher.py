"""One native security window per Windows user, shared across CLI and MCP processes."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .identity import IdentityError
from .owner_security import WINDOW_TITLES, OwnerSecurityOperations, OwnerSecurityWindowRequest

SecurityState = Literal[
    "starting", "waiting_for_user", "running", "succeeded", "cancelled", "failed"
]
_TERMINAL = {"succeeded", "cancelled", "failed"}


class SecurityWindowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    target_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: OwnerSecurityWindowRequest
    status: SecurityState = "starting"
    pid: int = 0
    process_stamp: int = 0
    updated_at: float = Field(default_factory=time.time)
    operation_committed: bool | None = False
    login_completed: bool = False
    recovery_code_acknowledged: bool = False
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,120}$")

    def public(self) -> dict:
        if self.status not in _TERMINAL:
            action = "wait_for_window" if self.status == "starting" else "complete_local_window"
        elif self.status == "succeeded":
            action = (
                "login"
                if self.request.kind in {"change_password", "recover"}
                else "retry_original_operation"
            )
        elif self.operation_committed is not False:
            action = "check_identity_before_retry"
        else:
            action = "request_window_again"
        return {
            "request_id": str(self.request_id),
            "kind": self.request.kind,
            "status": self.status,
            "window_title": WINDOW_TITLES[self.request.kind],
            "operation_committed": self.operation_committed,
            "login_completed": self.login_completed,
            "recovery_code_acknowledged": self.recovery_code_acknowledged,
            "error_code": self.error_code,
            "next_action": action,
        }


@lru_cache
def _sid() -> str:
    from .backup_credentials import _current_windows_sid

    return _current_windows_sid()


def _kernel32():
    dll = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    dll.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    dll.CreateMutexW.restype = ctypes.c_void_p
    dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    dll.WaitForSingleObject.restype = ctypes.c_uint32
    dll.ReleaseMutex.argtypes = [ctypes.c_void_p]
    dll.ReleaseMutex.restype = ctypes.c_int
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.restype = ctypes.c_int
    dll.LocalFree.argtypes = [ctypes.c_void_p]
    dll.LocalFree.restype = ctypes.c_void_p
    return dll


@contextmanager
def _user_mutex():
    if sys.platform != "win32":
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    from .backup_credentials import _security_descriptor_for_sid, _SecurityAttributes

    dll = _kernel32()
    descriptor = _security_descriptor_for_sid(_sid())
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
    handle = dll.CreateMutexW(
        ctypes.byref(attributes), False, f"Global\\ai-accounting-owner-security-{_sid()}"
    )
    dll.LocalFree(descriptor)
    if not handle:
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    acquired = False
    try:
        acquired = dll.WaitForSingleObject(handle, 5000) in (0, 0x80)
        if not acquired:
            raise IdentityError("OWNER_SECURITY_WINDOW_BUSY")
        yield
    finally:
        if acquired:
            dll.ReleaseMutex(handle)
        dll.CloseHandle(handle)


def _verify_path(path: Path):
    from .windows_backup import WindowsCurrentUserOnlyAclVerifier

    try:
        WindowsCurrentUserOnlyAclVerifier().assert_current_windows_user_only(path)
    except Exception:
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED") from None


def _state_root() -> Path:
    if sys.platform != "win32" or not os.environ.get("LOCALAPPDATA"):
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    from .backup_credentials import _security_descriptor_for_sid, _SecurityAttributes

    parent = Path(os.environ["LOCALAPPDATA"]).resolve(strict=True)
    root = parent / "ai-accounting-owner-security"
    if not root.exists():
        dll = _kernel32()
        dll.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        dll.CreateDirectoryW.restype = ctypes.c_int
        descriptor = _security_descriptor_for_sid(_sid())
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
        try:
            if not dll.CreateDirectoryW(str(root), ctypes.byref(attributes)):
                if ctypes.get_last_error() != 183:
                    raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        finally:
            dll.LocalFree(descriptor)
    _verify_path(root)
    return root


def _protected_write(path: Path, data: bytes):
    """Create with a protected ACL before writing bytes, then atomically replace."""
    from .backup_credentials import (
        _configure_protected_file_api,
        _security_descriptor_for_sid,
        _SecurityAttributes,
    )

    _verify_path(path.parent)
    if path.exists():
        _verify_path(path)
    dll = _kernel32()
    _configure_protected_file_api(dll)
    descriptor = _security_descriptor_for_sid(_sid())
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    handle = None
    try:
        handle = dll.CreateFileW(
            str(temporary), 0x40000000, 0, ctypes.byref(attributes), 1, 0x100, None
        )
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        buffer = ctypes.create_string_buffer(data)
        written = ctypes.c_uint32()
        if not dll.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
            raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        if written.value != len(data) or not dll.FlushFileBuffers(handle):
            raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        dll.CloseHandle(handle)
        handle = None
        _verify_path(temporary)
        os.replace(temporary, path)
    finally:
        if handle is not None:
            dll.CloseHandle(handle)
        dll.LocalFree(descriptor)
        if temporary.exists():
            temporary.unlink()


def _process_stamp(pid: int) -> int:
    if not pid:
        return 0
    dll = _kernel32()
    dll.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    dll.OpenProcess.restype = ctypes.c_void_p
    dll.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    dll.GetProcessTimes.restype = ctypes.c_int
    handle = dll.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return 0
    try:
        if dll.WaitForSingleObject(handle, 0) == 0:
            return 0
        times = [ctypes.c_uint64() for _ in range(4)]
        if not dll.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return 0
        return times[0].value
    finally:
        dll.CloseHandle(handle)


def assert_interactive_desktop():
    if sys.platform != "win32":
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    user32 = ctypes.WinDLL("User32.dll", use_last_error=True)
    user32.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.CloseDesktop.restype = ctypes.c_int
    desktop = user32.OpenInputDesktop(0, False, 1)
    if not desktop:
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    user32.CloseDesktop(desktop)


class OwnerSecurityWindowLauncher:
    def __init__(self, *, operations=None, popen=subprocess.Popen):
        self.operations = operations or OwnerSecurityOperations()
        self.popen = popen

    def _read(self, root, request_id):
        path = root / f"{uuid.UUID(str(request_id))}.json"
        if not path.exists():
            raise IdentityError("OWNER_SECURITY_REQUEST_NOT_FOUND")
        _verify_path(path)
        if path.stat().st_size > 16384:
            raise IdentityError("OWNER_SECURITY_STATE_INVALID")
        try:
            record = SecurityWindowRecord.model_validate_json(path.read_bytes())
        except Exception:
            raise IdentityError("OWNER_SECURITY_STATE_INVALID") from None
        if record.request_id != uuid.UUID(str(request_id)):
            raise IdentityError("OWNER_SECURITY_STATE_INVALID")
        return record

    def _write(self, root, record):
        record.updated_at = time.time()
        record = SecurityWindowRecord.model_validate(record.model_dump())
        _protected_write(root / f"{record.request_id}.json", record.model_dump_json().encode())

    def _refresh(self, root, record):
        if record.status not in _TERMINAL:
            alive = record.process_stamp and _process_stamp(record.pid) == record.process_stamp
            expired_start = record.status == "starting" and time.time() - record.updated_at > 30
            if not alive or expired_start:
                record.status = "failed"
                record.error_code = "OWNER_SECURITY_WINDOW_INTERRUPTED"
                self._write(root, record)
        return record

    @staticmethod
    def _same_request(active, request):
        # An omitted login name means the owner read from this database, not a new operation.
        candidate = request
        if request.login_name is None:
            candidate = request.model_copy(update={"login_name": active.request.login_name})
        return active.request == candidate

    def request(self, request: OwnerSecurityWindowRequest) -> dict:
        target = self.operations.scope()
        with _user_mutex():
            root = _state_root()
            active_path = root / "active.json"
            if active_path.exists():
                _verify_path(active_path)
                try:
                    active_id = uuid.UUID(active_path.read_text(encoding="utf-8"))
                except Exception:
                    raise IdentityError("OWNER_SECURITY_STATE_INVALID") from None
                active = self._refresh(root, self._read(root, active_id))
                alive = active.process_stamp and _process_stamp(active.pid) == active.process_stamp
                if alive or active.status not in _TERMINAL:
                    if active.target_id == target and self._same_request(active, request):
                        return active.public()
                    raise IdentityError("OWNER_SECURITY_WINDOW_BUSY")
                if (
                    active.status == "failed"
                    and active.target_id == target
                    and self._same_request(active, request)
                    and (
                        active.operation_committed is not False
                        or time.time() - active.updated_at < 3
                    )
                ):
                    return active.public()
            facts = self.operations.inspect(request, target)
            if facts.get("login_name") is not None:
                request = request.model_copy(update={"login_name": facts["login_name"]})
            assert_interactive_desktop()
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if not pythonw.is_file():
                raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
            record = SecurityWindowRecord(
                request_id=uuid.uuid4(), target_id=target, request=request
            )
            self._write(root, record)
            _protected_write(active_path, str(record.request_id).encode())
            environment = os.environ.copy()
            for key, value in self.operations.settings.model_dump(mode="json").items():
                if value is None:
                    environment.pop(key.upper(), None)
                else:
                    environment[key.upper()] = str(value)
            environment["FINANCE_SECURITY_FROZEN_CONFIG"] = "1"
            try:
                process = self.popen(
                    [
                        str(pythonw),
                        "-m",
                        "ai_accounting.owner_security_window",
                        "--request-id",
                        str(record.request_id),
                    ],
                    cwd=Path.cwd(),
                    env=environment,
                    shell=False,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                record.pid = process.pid
                record.process_stamp = _process_stamp(process.pid)
                if not record.process_stamp:
                    raise OSError("child exited")
            except Exception:
                record.status = "failed"
                record.error_code = "OWNER_SECURITY_WINDOW_UNAVAILABLE"
            self._write(root, record)
            return record.public()

    def status(self, request_id) -> dict:
        target = self.operations.scope()
        with _user_mutex():
            root = _state_root()
            record = self._read(root, request_id)
            if record.target_id != target:
                raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
            return self._refresh(root, record).public()

    def load_for_window(self, request_id):
        with _user_mutex():
            root = _state_root()
            record = self._read(root, request_id)
            if record.status != "starting":
                raise IdentityError("OWNER_SECURITY_REQUEST_NOT_ACTIVE")
            if record.pid != os.getpid():
                # Windows venv pythonw is a redirector. Adopt only its live direct child.
                if (
                    record.pid != os.getppid()
                    or not record.process_stamp
                    or _process_stamp(record.pid) != record.process_stamp
                ):
                    raise IdentityError("OWNER_SECURITY_REQUEST_NOT_ACTIVE")
                record.pid = os.getpid()
                record.process_stamp = _process_stamp(record.pid)
                if not record.process_stamp:
                    raise IdentityError("OWNER_SECURITY_REQUEST_NOT_ACTIVE")
                self._write(root, record)
            self.operations.assert_target(record.target_id)
            return record

    def update(self, request_id, **changes):
        with _user_mutex():
            root = _state_root()
            record = self._read(root, request_id)
            if record.pid != os.getpid() or record.status in _TERMINAL:
                raise IdentityError("OWNER_SECURITY_REQUEST_NOT_ACTIVE")
            for key, value in changes.items():
                if key not in {
                    "status",
                    "operation_committed",
                    "login_completed",
                    "error_code",
                    "recovery_code_acknowledged",
                }:
                    raise IdentityError("OWNER_SECURITY_STATE_INVALID")
                setattr(record, key, value)
            self._write(root, record)


class OwnerLoginWindowLauncher:
    """Compatibility adapter; all window lifetime management is shared."""

    def request(self, *, login_name):
        return OwnerSecurityWindowLauncher().request(
            OwnerSecurityWindowRequest(kind="login", login_name=login_name)
        )


class OwnerCloseApprovalWindowLauncher:
    def request(self, *, org_id, period_id, calculation_hash, login_name):
        return OwnerSecurityWindowLauncher().request(
            OwnerSecurityWindowRequest(
                kind="approve_period_close",
                org_id=org_id,
                period_id=period_id,
                calculation_hash=calculation_hash,
                login_name=login_name,
            )
        )
