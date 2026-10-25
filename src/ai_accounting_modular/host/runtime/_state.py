"""A catalog-bound, same-OS-user service address; no accounting or session database."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError

PROTOCOL = "ai-accounting-modular-service/1"
MAX_REQUEST_BYTES = 64 * 1024 * 1024


def state_path(catalog: Path) -> Path:
    return catalog.with_name(catalog.name + ".modular-service")


def _checked(path: Path) -> Path:
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if (
            part.is_symlink()
            or part.exists()
            and getattr(part.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ContractError("invalid_path", "运行路径不能经过链接或重解析点。")
    return path.resolve()


def catalog_path(path: Path) -> Path:
    path = _checked(path)
    if path.is_file() and path.stat().st_nlink > 1:
        raise ContractError("invalid_path", "目录库不能使用硬链接别名，请使用独立的目录库文件。")
    return path


def _protect(raw: bytes, catalog: Path, *, decrypt=False) -> bytes:
    if os.name != "nt":
        return raw

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return Blob(len(value), buffer), buffer

    source, source_buffer = blob(raw)
    entropy, entropy_buffer = blob(hashlib.sha256(str(catalog).encode()).digest())
    output = Blob()
    crypt = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(Blob),
    ]
    operation.restype = ctypes.c_int
    # UI_FORBIDDEN: an inaccessible state must fail without opening an OS prompt.
    if not operation(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)
    ):
        raise ContractError("service_state_invalid", "本机服务连接资料无法验证。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def publish(catalog: Path, value: dict) -> None:
    target = _checked(state_path(catalog))
    temporary = target.with_name(target.name + "." + uuid4().hex)
    raw = _protect(json.dumps(value, ensure_ascii=False).encode(), catalog)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read_bytes(catalog: Path) -> bytes:
    path = _checked(state_path(catalog))
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or os.name != "nt"
                and (metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid())
            ):
                raise ContractError("service_state_invalid", "本机服务连接文件不属于私有访问范围。")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        return raw
    except FileNotFoundError:
        raise
    except (ValueError, OSError) as error:
        raise ContractError("service_state_invalid", "本机服务连接资料无法验证。") from error


def read(catalog: Path) -> dict:
    raw = _read_bytes(catalog)
    try:
        value = json.loads(_protect(raw, catalog, decrypt=True))
        if (
            not isinstance(value, dict)
            or value.get("protocol") != PROTOCOL
            or value.get("catalog_path") != str(catalog)
            or type(value.get("port")) is not int
            or not 1 <= value["port"] <= 65535
            or not isinstance(value.get("capability"), str)
            or len(value["capability"]) < 32
            or not isinstance(value.get("catalog_id"), str)
        ):
            raise ValueError
        return value
    except (ValueError, OSError) as error:
        raise ContractError("service_state_invalid", "本机服务连接资料无法验证。") from error


@contextmanager
def instance_lock(catalog: Path):
    """The OS releases this catalog-specific lock even after abrupt process termination."""
    catalog = catalog_path(catalog)
    path = _checked(catalog.with_name(catalog.name + ".modular-lock"))
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b")
    acquired = False
    try:
        if not path.stat().st_size:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise ContractError("busy", "此目录已经有一个新体系后台运行。") from None
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
