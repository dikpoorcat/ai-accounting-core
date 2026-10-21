"""The owner can log in, change/recover access and rotate recovery without business approvals."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.host.runtime import _native
from ai_accounting_modular.storage.public import Storage

from .conftest import PASSWORD


def test_password_change_recovery_and_code_rotation_keep_identity_and_invalidate_sessions(local):
    runtime, _, old_code = local
    security = runtime.security
    original = runtime.kernel.storage.owner_profile()

    def execute(kind, **values):
        window = security.request({"kind": kind})
        result = security.native("execute", {"request_id": window["request_id"], **values})
        validate_contract(
            "dashboard-response-v2.json",
            "browser_security_status",
            {key: value for key, value in result.items() if key != "recovery_code"},
        )
        if result.get("recovery_code"):
            security.native("acknowledge", {"request_id": window["request_id"]})
        return result

    execute("login", password=PASSWORD)
    old_token = security.token()
    security.authorize(old_token)
    changed = execute(
        "change_password",
        password=PASSWORD,
        new_password="another-synthetic-password",
        repeat_password="another-synthetic-password",
    )
    with pytest.raises(ContractError, match="本机窗口登录"):
        security.authorize(old_token)
    assert changed["recovery_code"] != old_code.get_secret_value()
    execute(
        "recover",
        recovery_code=changed["recovery_code"],
        new_password=PASSWORD,
        repeat_password=PASSWORD,
    )
    execute("login", password=PASSWORD)
    token = security.token()
    execute("replace_recovery_code")
    security.authorize(token)
    assert runtime.kernel.storage.owner_profile().owner_id == original.owner_id
    assert runtime.kernel.storage.owner_profile().display_name == original.display_name


def test_wrong_password_has_no_login_or_credential_change_and_no_gui_fallback(local):
    runtime, _, _ = local
    security = runtime.security
    original = runtime.kernel.storage.owner_profile()
    window = security.request({"kind": "login"})
    with pytest.raises(ContractError, match="密码或恢复码"):
        security.native("execute", {"request_id": window["request_id"], "password": "wrong"})
    assert not security.status(local=True)["authenticated"]
    assert runtime.kernel.storage.owner_profile() == original
    security.public("cancel", {"request_id": window["request_id"]})
    security.window_opener = None
    with pytest.raises(ContractError, match="本机密码窗口"):
        security.request({"kind": "login"})


def test_cancelling_after_rotation_does_not_claim_nothing_was_changed(local):
    runtime, _, code = local
    security = runtime.security
    window = security.request({"kind": "recover"})
    security.native(
        "execute",
        {
            "request_id": window["request_id"],
            "recovery_code": code.get_secret_value(),
            "new_password": PASSWORD,
            "repeat_password": PASSWORD,
        },
    )
    result = security.public("cancel", {"request_id": window["request_id"]})
    assert result["status"] == "failed"
    assert result["operation_committed"]
    assert result["error_code"] == "OWNER_SECURITY_RECOVERY_CODE_NOT_ACKNOWLEDGED"


def test_first_owner_setup_uses_native_form_and_does_not_erase_created_identity_on_cancel(
    tmp_path, monkeypatch
):
    catalog = tmp_path / "new-catalog.sqlite3"
    monkeypatch.setattr(_native, "_desktop_available", lambda: True)

    def form(kind, _, submit, cancel):
        assert kind == "bootstrap_owner"
        submit(
            {"display_name": "合成新负责人", "new_password": PASSWORD, "repeat_password": PASSWORD}
        )
        cancel()

    monkeypatch.setattr(_native, "_form", form)
    assert _native.initialize(catalog)["status"] == "recovery_pending"
    assert Storage(catalog).owner_profile().display_name == "合成新负责人"
    with pytest.raises(ContractError, match="已经存在"):
        _native.initialize(catalog)
