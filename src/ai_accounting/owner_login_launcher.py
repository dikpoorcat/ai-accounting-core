"""Launch visible local-owner password prompts in dedicated Windows consoles.

The launchers are intentionally Windows-only.  They pass only non-secret command
facts to the existing identity CLI; passwords remain no-echo interactive input.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

_SW_SHOWNORMAL = 1


class _ChildProcess(Protocol):
    def poll(self) -> int | None: ...


class OwnerLoginWindowLauncher:
    """Start at most one visible owner-login window for this MCP process."""

    def __init__(
        self,
        *,
        working_directory: Path | None = None,
        python_executable: Path | None = None,
        retry_cooldown_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., _ChildProcess] = subprocess.Popen,
    ) -> None:
        self._working_directory = (working_directory or Path.cwd()).resolve()
        self._python_executable = (python_executable or Path(sys.executable)).resolve()
        self._retry_cooldown_seconds = retry_cooldown_seconds
        self._monotonic = monotonic
        self._which = which
        self._popen = popen
        self._lock = threading.Lock()
        self._process: _ChildProcess | None = None
        self._retry_after = 0.0

    def request(self, *, login_name: str) -> bool:
        """Ensure a visible login prompt is running; return whether it was requested."""

        if sys.platform != "win32":
            return False
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            now = self._monotonic()
            if now < self._retry_after:
                return True
            shell = self._powershell_executable()
            if shell is None:
                self._retry_after = now + self._retry_cooldown_seconds
                return False
            encoded_command = _encoded_login_script(
                working_directory=self._working_directory,
                python_executable=self._python_executable,
                login_name=login_name,
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = _SW_SHOWNORMAL
            try:
                self._process = self._popen(
                    [
                        shell,
                        "-NoLogo",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-EncodedCommand",
                        encoded_command,
                    ],
                    cwd=self._working_directory,
                    close_fds=True,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    startupinfo=startupinfo,
                )
            except OSError:
                self._process = None
                self._retry_after = now + self._retry_cooldown_seconds
                return False
            return True

    def _powershell_executable(self) -> str | None:
        return _powershell_executable(self._which)


class OwnerCloseApprovalWindowLauncher:
    """Start at most one visible close-approval window for this caller."""

    def __init__(
        self,
        *,
        working_directory: Path | None = None,
        python_executable: Path | None = None,
        retry_cooldown_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., _ChildProcess] = subprocess.Popen,
    ) -> None:
        self._working_directory = (working_directory or Path.cwd()).resolve()
        self._python_executable = (python_executable or Path(sys.executable)).resolve()
        self._retry_cooldown_seconds = retry_cooldown_seconds
        self._monotonic = monotonic
        self._which = which
        self._popen = popen
        self._lock = threading.Lock()
        self._process: _ChildProcess | None = None
        self._retry_after = 0.0

    def request(
        self,
        *,
        org_id: str,
        period_id: str,
        calculation_hash: str,
        login_name: str,
    ) -> bool:
        """Ensure a visible close-approval prompt is running."""

        if sys.platform != "win32":
            return False
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            now = self._monotonic()
            if now < self._retry_after:
                return True
            shell = _powershell_executable(self._which)
            if shell is None:
                self._retry_after = now + self._retry_cooldown_seconds
                return False
            encoded_command = _encoded_close_approval_script(
                working_directory=self._working_directory,
                python_executable=self._python_executable,
                org_id=org_id,
                period_id=period_id,
                calculation_hash=calculation_hash,
                login_name=login_name,
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = _SW_SHOWNORMAL
            try:
                self._process = self._popen(
                    [
                        shell,
                        "-NoLogo",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-EncodedCommand",
                        encoded_command,
                    ],
                    cwd=self._working_directory,
                    close_fds=True,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    startupinfo=startupinfo,
                )
            except OSError:
                self._process = None
                self._retry_after = now + self._retry_cooldown_seconds
                return False
            return True


def _encoded_login_script(
    *,
    working_directory: Path,
    python_executable: Path,
    login_name: str,
) -> str:
    working_directory_literal = _powershell_literal(str(working_directory))
    python_literal = _powershell_literal(str(python_executable))
    login_name_literal = _powershell_literal(login_name)
    script = f"""
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'AI 记账内核 - 负责人登录'
try {{
    Set-Location -LiteralPath {working_directory_literal}
    & {python_literal} -m ai_accounting.identity_cli login --login-name {login_name_literal}
    if ($LASTEXITCODE -ne 0) {{
        throw "LOGIN_COMMAND_FAILED_$LASTEXITCODE"
    }}
    Write-Host ''
    Write-Host 'LOGIN_STATUS=SUCCESS' -ForegroundColor Green
    Write-Host '登录成功，本窗口将在 2 秒后自动关闭。'
    Start-Sleep -Seconds 2
    exit 0
}} catch {{
    Write-Host ''
    Write-Host 'LOGIN_STATUS=FAILED' -ForegroundColor Red
    Write-Host '负责人登录失败。请保留本窗口并回到 Codex。'
    Read-Host '按 Enter 关闭窗口'
    exit 1
}}
""".strip()
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _encoded_close_approval_script(
    *,
    working_directory: Path,
    python_executable: Path,
    org_id: str,
    period_id: str,
    calculation_hash: str,
    login_name: str,
) -> str:
    working_directory_literal = _powershell_literal(str(working_directory))
    python_literal = _powershell_literal(str(python_executable))
    org_id_literal = _powershell_literal(org_id)
    period_id_literal = _powershell_literal(period_id)
    calculation_hash_literal = _powershell_literal(calculation_hash)
    login_name_literal = _powershell_literal(login_name)
    approval_command = (
        f"& {python_literal} -m ai_accounting.identity_cli approve-close "
        f"--org-id {org_id_literal} --period-id {period_id_literal} "
        f"--calculation-hash {calculation_hash_literal} --login-name {login_name_literal}"
    )
    script = f"""
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'AI 记账内核 - 关账密码确认'
try {{
    Set-Location -LiteralPath {working_directory_literal}
    {approval_command}
    if ($LASTEXITCODE -ne 0) {{
        throw "CLOSE_APPROVAL_COMMAND_FAILED_$LASTEXITCODE"
    }}
    Write-Host ''
    Write-Host 'CLOSE_APPROVAL_STATUS=SUCCESS' -ForegroundColor Green
    Write-Host '关账授权已完成，本窗口将在 2 秒后自动关闭。'
    Start-Sleep -Seconds 2
    exit 0
}} catch {{
    Write-Host ''
    Write-Host 'CLOSE_APPROVAL_STATUS=FAILED' -ForegroundColor Red
    Write-Host '关账授权失败。请保留本窗口并回到 Codex。'
    Read-Host '按 Enter 关闭窗口'
    exit 1
}}
""".strip()
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell_executable(which: Callable[[str], str | None]) -> str | None:
    for candidate in ("pwsh.exe", "powershell.exe"):
        resolved = which(candidate)
        if resolved:
            return resolved
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    fallback = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(fallback) if fallback.is_file() else None


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
