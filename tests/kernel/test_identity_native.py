import json

import pytest
from pydantic import SecretStr
from test_identity import NEW_PASSWORD, PASSWORD, catalogue

from ai_accounting.kernel.security import IdentityError
from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
from ai_accounting.kernel.security.native import NativeSecurityController


def controller(tmp_path):
    service = catalogue(tmp_path / "catalog.sqlite")
    store = InMemoryCredentialStore()
    opened = []
    native = NativeSecurityController(service, credential_store=store, window_opener=opened.append)
    return service, store, native, opened


def call(native, operation, request_id, **data):
    return native.dispatch(operation, {"request_id": request_id, **data}, private=True)


def test_bootstrap_acknowledgement_and_public_secret_boundary(tmp_path):
    service, store, native, opened = controller(tmp_path)
    request = native.request(kind="bootstrap_owner", login_name="owner")
    request_id = request["request_id"]
    assert opened == [request_id]
    assert native.request(kind="bootstrap_owner", login_name="owner") == request
    with pytest.raises(IdentityError, match="PRIVATE_CHANNEL"):
        native.dispatch("native_execute", {"request_id": request_id})
    with pytest.raises(IdentityError, match="OPERATION_INCOMPLETE"):
        call(native, "native_update", request_id, status="succeeded", operation_committed=True)
    result = call(
        native, "native_execute", request_id, new_password=PASSWORD, repeat_password=PASSWORD
    )
    recovery = result["recovery_code"]
    assert isinstance(recovery, str)
    assert store.load_session_token() is None
    assert recovery not in json.dumps(native.status(request_id))
    assert PASSWORD.get_secret_value() not in json.dumps(native.status(request_id))
    with pytest.raises(IdentityError, match="ALREADY_COMMITTED"):
        call(native, "native_execute", request_id, new_password=PASSWORD, repeat_password=PASSWORD)
    with pytest.raises(IdentityError, match="OPERATION_INCOMPLETE"):
        call(native, "native_update", request_id, status="succeeded")
    call(native, "native_finish", request_id, new_password=PASSWORD)
    final = call(native, "native_update", request_id, status="succeeded")
    assert final["login_completed"] and final["recovery_code_acknowledged"]
    service.authorize(store.load_session_token())
    assert native.session_status()["authenticated"]


def test_login_retry_then_password_change_revokes_stored_session(tmp_path):
    service, store, native, _ = controller(tmp_path)
    service.provision("owner", PASSWORD)
    request_id = native.request(kind="login")["request_id"]
    with pytest.raises(IdentityError, match="AUTHENTICATION_FAILED"):
        call(native, "native_execute", request_id, password=SecretStr("wrong-password"))
    assert native.status(request_id)["operation_committed"] is False
    call(native, "native_execute", request_id, password=PASSWORD)
    old = store.load_session_token()
    call(native, "native_update", request_id, status="succeeded")
    request_id = native.request(kind="change_password")["request_id"]
    result = call(
        native,
        "native_execute",
        request_id,
        password=PASSWORD,
        new_password=NEW_PASSWORD,
        repeat_password=NEW_PASSWORD,
    )
    assert result["recovery_code"]
    assert store.load_session_token() is None
    with pytest.raises(IdentityError, match="SESSION_INVALID"):
        service.authorize(old)
    native.cancel(request_id)
    assert native.status(request_id)["operation_committed"]
    assert native.status(request_id)["status"] == "failed"


def test_login_store_failure_revokes_new_token_without_plaintext_fallback(tmp_path):
    service, store, native, _ = controller(tmp_path)
    service.provision("owner", PASSWORD)
    request_id = native.request(kind="login")["request_id"]

    def broken(token):
        raise OSError("synthetic store error")

    store.save_session_token = broken
    with pytest.raises(IdentityError, match="STORE_WRITE_FAILED"):
        call(native, "native_execute", request_id, password=PASSWORD)
    with service._transaction() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM security_session WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 0
        )
    assert store.load_session_token() is None


def test_request_rejects_secrets_and_session_required_operations(tmp_path):
    service, _, native, _ = controller(tmp_path)
    service.provision("owner", PASSWORD)
    with pytest.raises(IdentityError, match="REQUEST_INVALID"):
        native.request(kind="login", password="never a public field")
    with pytest.raises(IdentityError, match="LOCAL_SESSION_REQUIRED"):
        native.request(kind="replace_recovery_code")


def test_close_context_is_displayed_and_revalidated_by_injected_issuer(tmp_path):
    service, store, native, _ = controller(tmp_path)
    service.provision("owner", PASSWORD)
    store.save_session_token(service.login("owner", PASSWORD).session_token)
    seen = []
    native.inspect_close = lambda request: {
        "company_name": "合成测试公司",
        "period_month": request["period"],
    }

    def issuer(request, token, password):
        service.reauthenticate(token, password)
        seen.append(request)
        return {"approval_id": "synthetic", "expires_at": 100}

    native.close_issuer = issuer
    request_id = native.request(
        kind="approve_period_close",
        company_id="company",
        database_id="database",
        period="2026-09",
        calculation_hash="a" * 64,
        epochs={"accounting": 1, "material": 2, "management": 3},
    )["request_id"]
    assert call(native, "native_inspect", request_id)["facts"]["period_month"] == "2026-09"
    result = call(native, "native_execute", request_id, password=PASSWORD)
    assert result["approval_id"] == "synthetic"
    assert seen[0]["calculation_hash"] == "a" * 64
