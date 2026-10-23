"""Small native password/recovery form; secret bootstrap travels only through stdin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog

from ._client import request
from ._security import new_credentials
from ._state import _checked


def _desktop_available():
    if os.name != "nt":
        return bool(os.environ.get("DISPLAY") or sys.platform == "darwin")
    import ctypes

    class Flags(ctypes.Structure):
        _fields_ = [
            ("inherit", ctypes.c_int),
            ("reserved", ctypes.c_int),
            ("flags", ctypes.c_uint32),
        ]

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetProcessWindowStation.restype = ctypes.c_void_p
    user.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    flags, needed = Flags(), ctypes.c_uint32()
    return bool(
        user.GetUserObjectInformationW(
            user.GetProcessWindowStation(),
            1,
            ctypes.byref(flags),
            ctypes.sizeof(flags),
            ctypes.byref(needed),
        )
        and flags.flags & 1
    )


def launch(request_id, *, port, capability, catalog_id):
    if not _desktop_available():
        raise ContractError("OWNER_SECURITY_WINDOW_UNAVAILABLE", "当前会话没有可用的本机桌面。")
    executable = Path(sys.executable)
    if os.name == "nt":
        executable = executable.with_name("pythonw.exe")
    process = subprocess.Popen(
        [str(executable), "-I", "-X", "utf8", "-m", "ai_accounting_modular.host.runtime._native"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process.stdin.write(
            json.dumps(
                {
                    "port": port,
                    "capability": capability,
                    "catalog_id": catalog_id,
                    "request_id": request_id,
                }
            ).encode()
        )
        process.stdin.close()
    except Exception:
        process.terminate()
        raise ContractError("OWNER_SECURITY_WINDOW_UNAVAILABLE", "本机密码窗口未能打开。") from None
    return process


def _form(kind, display_name, submit, cancel, *, status=None):
    import tkinter as tk

    from ._native_form import SecurityForm

    window = tk.Tk()
    SecurityForm(window, kind, display_name, submit, cancel, status=status)
    window.mainloop()


def initialize(catalog):
    """Explicit first-use native setup; never replace an existing catalog."""
    catalog = _checked(Path(catalog))
    if catalog.exists():
        raise ContractError("target_exists", "目录已经存在，不能重新建立负责人。")
    if not _desktop_available():
        raise ContractError("OWNER_SECURITY_WINDOW_UNAVAILABLE", "当前会话没有可用的本机桌面。")
    completed, created = False, False

    def submit(values):
        nonlocal completed, created
        if values is None:
            completed = True
            return {}
        if values["new_password"] != values["repeat_password"]:
            raise ContractError("invalid_input", "两次输入的新密码不一致。")
        password, recovery, code = new_credentials(values["new_password"])
        create_catalog(
            catalog, OwnerProfile(str(uuid4()), values["display_name"], password, recovery)
        )
        created = True
        return {"recovery_code": code}

    _form("bootstrap_owner", "", submit, lambda: None)
    if created and not completed:
        return {
            "status": "recovery_pending",
            "message": "负责人已建立；恢复码尚未确认保存。请登录后更换恢复码。",
        }
    return {"status": "ready" if completed else "cancelled"}


def main():
    metadata = json.loads(sys.stdin.buffer.read(65537))
    if not isinstance(metadata, dict) or set(metadata) != {
        "port",
        "capability",
        "catalog_id",
        "request_id",
    }:
        return
    request_id = metadata.pop("request_id")

    def call(operation, payload=None):
        return request(
            metadata,
            "/private/native",
            {"operation": operation, "payload": {"request_id": request_id, **(payload or {})}},
        )

    try:
        state = call("inspect")
        _form(
            state["kind"],
            state["display_name"],
            lambda values: call("acknowledge" if values is None else "execute", values),
            lambda: request(
                metadata,
                "/private/security",
                {"operation": "cancel", "payload": {"request_id": request_id}},
                timeout=2,
            ),
            status=lambda: request(
                metadata,
                "/private/security",
                {"operation": "status", "payload": {"request_id": request_id}},
                timeout=2,
            ),
        )
    except Exception:
        # Passwords and capabilities must never reach a Python traceback or event log.
        try:
            call("unavailable")
        except Exception:
            pass
        return


if __name__ == "__main__":
    main()
