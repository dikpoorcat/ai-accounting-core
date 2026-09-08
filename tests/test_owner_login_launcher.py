from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ai_accounting import owner_login_launcher as windows
from ai_accounting.config import Settings
from ai_accounting.identity import IdentityError
from ai_accounting.owner_security import OwnerSecurityWindowRequest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows security-window IPC")


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    live = {os.getpid(): 123}
    monkeypatch.setattr(windows, "_process_stamp", lambda pid: live.get(pid, 0))
    calls = []

    class Operations:
        settings = Settings(_env_file=None, database_url="sqlite://")
        target = "a" * 64

        def scope(self):
            return self.target

        def inspect(self, request, target):
            return {"company_name": "test", "login_name": request.login_name or "owner"}

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=os.getpid())

    manager = windows.OwnerSecurityWindowLauncher(operations=Operations(), popen=popen)
    return manager, calls, live


def test_native_launcher_uses_fixed_module_private_files_and_frozen_config(launcher):
    manager, calls, _ = launcher
    result = manager.request(OwnerSecurityWindowRequest(kind="login"))
    assert result["status"] == "starting"
    args, options = calls[0]
    assert args[:3] == [
        str(windows.Path(sys.executable).with_name("pythonw.exe")),
        "-m",
        "ai_accounting.owner_security_window",
    ]
    assert args[3:] == ["--request-id", result["request_id"]]
    assert options["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["env"]["FINANCE_SECURITY_FROZEN_CONFIG"] == "1"
    assert options["env"]["DATABASE_URL"] == "sqlite://"
    root = windows._state_root()
    windows._verify_path(root)
    for path in root.iterdir():
        windows._verify_path(path)
    assert set(json.loads((root / (result["request_id"] + ".json")).read_text())) == {
        "request_id",
        "target_id",
        "request",
        "status",
        "pid",
        "process_stamp",
        "updated_at",
        "operation_committed",
        "login_completed",
        "recovery_code_acknowledged",
        "error_code",
    }


def test_parallel_requests_deduplicate_and_do_not_replace_other_operation(launcher):
    manager, calls, _ = launcher
    request = OwnerSecurityWindowRequest(kind="login")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: manager.request(request), range(4)))
    assert len({result["request_id"] for result in results}) == 1
    assert len(calls) == 1
    assert (
        manager.request(OwnerSecurityWindowRequest(kind="login", login_name="owner"))["request_id"]
        == results[0]["request_id"]
    )
    with pytest.raises(IdentityError, match="OWNER_SECURITY_WINDOW_BUSY"):
        manager.request(OwnerSecurityWindowRequest(kind="recover"))
    manager.operations.target = "b" * 64
    with pytest.raises(IdentityError, match="OWNER_SECURITY_WINDOW_BUSY"):
        manager.request(request)
    with pytest.raises(IdentityError, match="OWNER_SECURITY_TARGET_MISMATCH"):
        manager.status(results[0]["request_id"])


def test_failed_start_and_crash_are_never_success_and_are_recoverable(launcher):
    manager, calls, live = launcher

    def fail(*args, **kwargs):
        raise OSError("SECRET-ERROR-DETAIL")

    original = manager.popen
    manager.popen = fail
    request = OwnerSecurityWindowRequest(kind="login")
    failed = manager.request(request)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "OWNER_SECURITY_WINDOW_UNAVAILABLE"
    assert "SECRET" not in json.dumps(failed)
    assert manager.request(request) == failed
    manager.popen = original
    # A different operation can start after a failed child launch.
    started = manager.request(OwnerSecurityWindowRequest(kind="recover"))
    live.clear()
    crashed = manager.status(started["request_id"])
    assert crashed["status"] == "failed"
    assert crashed["error_code"] == "OWNER_SECURITY_WINDOW_INTERRUPTED"


def test_partial_or_unknown_commit_is_not_automatically_repeated(launcher, monkeypatch):
    manager, calls, live = launcher
    request = OwnerSecurityWindowRequest(kind="replace_recovery_code")
    started = manager.request(request)
    manager.update(started["request_id"], status="running", operation_committed=None)
    live.clear()
    crashed = manager.status(started["request_id"])
    assert crashed["next_action"] == "check_identity_before_retry"
    now = time.time()
    monkeypatch.setattr(windows.time, "time", lambda: now + 60)
    assert manager.request(request)["request_id"] == started["request_id"]
    assert len(calls) == 1


def test_state_rejects_unknown_fields_and_unsafe_error_details(launcher):
    manager, _, _ = launcher
    started = manager.request(OwnerSecurityWindowRequest(kind="login"))
    with pytest.raises(IdentityError, match="OWNER_SECURITY_STATE_INVALID"):
        manager.update(started["request_id"], password="test-secret")
    with pytest.raises(ValueError):
        manager.update(started["request_id"], error_code="test-secret")
    assert (
        "test-secret" not in (windows._state_root() / (started["request_id"] + ".json")).read_text()
    )


def test_old_launchers_are_thin_unified_adapters(monkeypatch):
    calls = []

    class Launcher:
        def request(self, request):
            calls.append(request)
            return {"status": "starting"}

    monkeypatch.setattr(windows, "OwnerSecurityWindowLauncher", Launcher)
    windows.OwnerLoginWindowLauncher().request(login_name="owner")
    windows.OwnerCloseApprovalWindowLauncher().request(
        login_name="owner",
        org_id=str(uuid.uuid4()),
        period_id=str(uuid.uuid4()),
        calculation_hash="a" * 64,
    )
    assert [request.kind for request in calls] == ["login", "approve_period_close"]


def test_separate_cli_processes_share_one_request(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    script = """
import json, sys
from types import SimpleNamespace
from ai_accounting.config import Settings
from ai_accounting.owner_login_launcher import OwnerSecurityWindowLauncher
from ai_accounting.owner_security import OwnerSecurityWindowRequest
class Operations:
    settings = Settings(_env_file=None, database_url="sqlite://")
    def scope(self): return "a" * 64
    def inspect(self, *args): return {}
manager = OwnerSecurityWindowLauncher(
    operations=Operations(), popen=lambda *a, **k: SimpleNamespace(pid=int(sys.argv[1]))
)
print(json.dumps(manager.request(OwnerSecurityWindowRequest(kind="login"))))
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(os.getpid())],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(3)
    ]
    try:
        outputs = [child.communicate(timeout=60) for child in children]
        assert all(child.returncode == 0 for child in children), outputs
        results = [json.loads(out) for out, _ in outputs]
        assert len({result["request_id"] for result in results}) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
