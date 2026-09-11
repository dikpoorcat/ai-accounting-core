"""Small Windows ACL/desktop primitives with no database or legacy imports.

Metadata containing a service capability is created with its final private DACL
before any bytes are written. Reads verify the opened handle's owner and DACL,
so checking a pathname and then following a replacement is not sufficient.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import sys
import uuid
from functools import lru_cache
from pathlib import Path

from .primitives import IdentityError

MAX_METADATA_BYTES = 65_536
FILE_ALL_ACCESS = 0x001F01FF
SYSTEM_SID = "S-1-5-18"


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _ACL(ctypes.Structure):
    _fields_ = [
        ("revision", ctypes.c_ubyte),
        ("sbz1", ctypes.c_ubyte),
        ("size", ctypes.c_uint16),
        ("count", ctypes.c_uint16),
        ("sbz2", ctypes.c_uint16),
    ]


def _apis():
    if sys.platform != "win32":
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel.ReadFile.restype = ctypes.c_int
    kernel.WriteFile.argtypes = kernel.ReadFile.argtypes
    kernel.WriteFile.restype = ctypes.c_int
    kernel.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    kernel.FlushFileBuffers.restype = ctypes.c_int
    kernel.GetFileSizeEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)]
    kernel.GetFileSizeEx.restype = ctypes.c_int
    advapi.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi.GetTokenInformation.restype = ctypes.c_int
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    advapi.GetSecurityInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetSecurityInfo.restype = ctypes.c_uint32
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi.GetSecurityDescriptorControl.restype = ctypes.c_int
    advapi.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = ctypes.c_int
    return kernel, advapi


def _sid_string(kernel, advapi, sid):
    result = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(result)):
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
    try:
        return ctypes.wstring_at(result.value)
    finally:
        kernel.LocalFree(result)


@lru_cache
def current_windows_sid():
    kernel, advapi = _apis()
    token, needed = ctypes.c_void_p(), ctypes.c_uint32()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value < 65_536:
            raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_string(kernel, advapi, sid)
    finally:
        kernel.CloseHandle(token)


def _private_descriptor(advapi):
    sid = current_windows_sid()
    value = ctypes.c_void_p()
    sddl = f"O:{sid}G:{sid}D:P(A;;FA;;;SY)(A;;FA;;;{sid})"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(value), None
    ):
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
    return value


def _assert_private_handle(kernel, advapi, handle):
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if advapi.GetSecurityInfo(
        handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    ):
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
    try:
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        if not advapi.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
        sid = current_windows_sid()
        if (
            not dacl.value
            or not control.value & 0x1000
            or _sid_string(kernel, advapi, owner) != sid
        ):
            raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
        acl = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
        if not 1 <= acl.count <= 2:
            raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
        subjects = set()
        for index in range(acl.count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
            header = ctypes.string_at(ace, 8)
            mask = int.from_bytes(header[4:8], "little")
            subject = _sid_string(kernel, advapi, ctypes.c_void_p(ace.value + 8))
            if (
                header[0] != 0
                or header[1] != 0
                or mask != FILE_ALL_ACCESS
                or subject not in {sid, SYSTEM_SID}
            ):
                raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
            subjects.add(subject)
        if sid not in subjects:
            raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
    finally:
        kernel.LocalFree(descriptor)


def _safe_path(path):
    absolute = Path(os.path.abspath(path))
    for candidate in (*reversed(absolute.parents), absolute):
        if candidate.exists():
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")
    return absolute


def read_protected_bytes(path, *, max_bytes=MAX_METADATA_BYTES):
    path = _safe_path(path)
    kernel, advapi = _apis()
    # No write/delete sharing while checking and reading this very handle.
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x20000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
    try:
        _assert_private_handle(kernel, advapi, handle)
        size = ctypes.c_int64()
        if not kernel.GetFileSizeEx(handle, ctypes.byref(size)) or not 0 <= size.value <= max_bytes:
            raise IdentityError("OWNER_SECURITY_STATE_INVALID")
        buffer, read = ctypes.create_string_buffer(size.value or 1), ctypes.c_uint32()
        if (
            not kernel.ReadFile(handle, buffer, size.value, ctypes.byref(read), None)
            or read.value != size.value
        ):
            raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        return buffer.raw[: read.value]
    finally:
        kernel.CloseHandle(handle)


def assert_private_file(path):
    read_protected_bytes(path)


def write_protected_bytes(path, data: bytes):
    if not isinstance(data, bytes) or len(data) > MAX_METADATA_BYTES:
        raise IdentityError("OWNER_SECURITY_STATE_INVALID")
    path = _safe_path(path)
    if not path.parent.is_dir():
        raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
    if path.exists():
        assert_private_file(path)
    kernel, advapi = _apis()
    descriptor = _private_descriptor(advapi)
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
    temporary = path.with_name(f".{uuid.uuid4().hex}.security-tmp")
    handle = None
    try:
        handle = kernel.CreateFileW(
            str(temporary), 0x40000000 | 0x20000, 0, ctypes.byref(attributes), 1, 0x00200000, None
        )
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        _assert_private_handle(kernel, advapi, handle)
        buffer, written = ctypes.create_string_buffer(data), ctypes.c_uint32()
        if (
            not kernel.WriteFile(handle, buffer, len(data), ctypes.byref(written), None)
            or written.value != len(data)
            or not kernel.FlushFileBuffers(handle)
        ):
            raise IdentityError("OWNER_SECURITY_STATE_UNAVAILABLE")
        kernel.CloseHandle(handle)
        handle = None
        os.replace(temporary, path)
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        kernel.LocalFree(descriptor)
        if temporary.exists():
            temporary.unlink()


def read_protected_json(path):
    raw = read_protected_bytes(path)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise IdentityError("OWNER_SECURITY_STATE_INVALID") from None
    if not isinstance(value, dict):
        raise IdentityError("OWNER_SECURITY_STATE_INVALID")
    return value


def write_protected_json(path, value: dict):
    if not isinstance(value, dict):
        raise IdentityError("OWNER_SECURITY_STATE_INVALID")
    write_protected_bytes(
        path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )


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
