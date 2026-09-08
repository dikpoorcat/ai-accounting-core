from __future__ import annotations

import json
import sys
import uuid

import pytest
from pydantic import SecretStr

from ai_accounting import identity_cli
from ai_accounting.credential_store import WindowsCredentialStore, _assert_windows_credential_layout


@pytest.mark.parametrize("command,kind", list(identity_cli._ALIASES.items()))
def test_legacy_commands_only_launch_the_unified_form(monkeypatch, capsys, command, kind):
    org, period = uuid.uuid4(), uuid.uuid4()
    calls = []

    class Launcher:
        def __init__(self, **kwargs):
            pass

        def request(self, request):
            calls.append(request)
            return {"status": "starting", "request_id": str(uuid.uuid4())}

    monkeypatch.setattr(identity_cli, "OwnerSecurityWindowLauncher", Launcher)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("terminal input forbidden"))
    argv = ["finance-login", command]
    if kind in {"bootstrap_owner", "approve_period_close"}:
        argv += ["--org-id", str(org)]
    if kind == "approve_period_close":
        argv += ["--period-id", str(period), "--calculation-hash", "a" * 64]
    monkeypatch.setattr(sys, "argv", argv)
    identity_cli.main()
    assert len(calls) == 1 and calls[0].kind == kind
    assert json.loads(capsys.readouterr().out)["status"] == "starting"


@pytest.mark.parametrize(
    "arguments",
    [
        ["login", "--password", "SENTINEL-SECRET"],
        ["security-window", "--kind", "SENTINEL-SECRET"],
        ["setup", "--org-id", "SENTINEL-SECRET"],
    ],
)
def test_cli_rejects_secret_arguments_without_echo(monkeypatch, capsys, arguments):
    monkeypatch.setattr(sys, "argv", ["finance-login", *arguments])
    with pytest.raises(SystemExit) as error:
        identity_cli.main()
    output = capsys.readouterr()
    assert error.value.code == 2
    assert "SENTINEL-SECRET" not in output.out + output.err
    assert "IDENTITY_LOCAL_COMMAND_INVALID" in output.err


def test_cli_status_and_logout_do_not_open_a_window(monkeypatch, capsys):
    calls = []

    class Operations:
        def __init__(self, **kwargs):
            pass

        def logout(self):
            calls.append("logout")

    class Launcher:
        def __init__(self, **kwargs):
            pass

        def status(self, request_id):
            calls.append(request_id)
            return {"status": "cancelled"}

    monkeypatch.setattr(identity_cli, "OwnerSecurityOperations", Operations)
    monkeypatch.setattr(identity_cli, "OwnerSecurityWindowLauncher", Launcher)
    request_id = uuid.uuid4()
    monkeypatch.setattr(
        sys, "argv", ["finance-login", "security-window-status", "--request-id", str(request_id)]
    )
    identity_cli.main()
    monkeypatch.setattr(sys, "argv", ["finance-login", "logout"])
    identity_cli.main()
    assert calls == [request_id, "logout"]
    assert "LOGOUT_SUCCEEDED" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_credential_store_abi_and_unique_target_round_trip() -> None:
    _assert_windows_credential_layout()
    target = f"ai-accounting-core/test-session/{uuid.uuid4()}"
    store = WindowsCredentialStore(target_name=target)
    previous = store.load_session_token()
    try:
        expected = SecretStr("test-only-opaque-session-token")
        store.save_session_token(expected)
        loaded = store.load_session_token()
        assert loaded is not None
        assert loaded.get_secret_value() == expected.get_secret_value()
        store.delete_session_token()
        assert store.load_session_token() is None
    finally:
        if previous is None:
            store.delete_session_token()
        else:
            store.save_session_token(previous)
