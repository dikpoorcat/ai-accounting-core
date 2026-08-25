from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from ai_accounting.owner_login_launcher import OwnerLoginWindowLauncher


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_launcher_opens_one_visible_console_and_passes_no_secret(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    process = _FakeProcess()

    def popen(args: list[str], **kwargs: object) -> _FakeProcess:
        calls.append((args, kwargs))
        return process

    launcher = OwnerLoginWindowLauncher(
        working_directory=tmp_path,
        python_executable=tmp_path / "python.exe",
        which=lambda name: r"C:\Program Files\PowerShell\7\pwsh.exe"
        if name == "pwsh.exe"
        else None,
        popen=popen,
    )

    assert launcher.request(login_name="owner's account")
    assert launcher.request(login_name="owner's account")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0].endswith("pwsh.exe")
    assert kwargs["creationflags"] == subprocess.CREATE_NEW_CONSOLE
    startupinfo = kwargs["startupinfo"]
    assert isinstance(startupinfo, subprocess.STARTUPINFO)
    assert startupinfo.wShowWindow == 1
    assert "stdin" not in kwargs and "stdout" not in kwargs and "stderr" not in kwargs

    encoded = args[args.index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "ai_accounting.identity_cli login" in script
    assert "--login-name 'owner''s account'" in script
    assert "LOGIN_STATUS=SUCCESS" in script
    assert "Read-Host" in script
    assert "Password:" not in script


def test_launcher_keeps_failed_start_from_spawning_a_window_storm(tmp_path: Path) -> None:
    calls = 0

    def failing_popen(_args: list[str], **_kwargs: object) -> _FakeProcess:
        nonlocal calls
        calls += 1
        raise OSError("not exposed by launcher")

    launcher = OwnerLoginWindowLauncher(
        working_directory=tmp_path,
        python_executable=tmp_path / "python.exe",
        retry_cooldown_seconds=3,
        monotonic=lambda: 10,
        which=lambda _name: r"C:\PowerShell\pwsh.exe",
        popen=failing_popen,
    )

    assert not launcher.request(login_name="owner")
    assert launcher.request(login_name="owner")
    assert calls == 1
