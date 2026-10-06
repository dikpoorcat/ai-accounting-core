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
        native,
        "native_execute",
        request_id,
        login_name="owner",
        new_password=PASSWORD,
        repeat_password=PASSWORD,
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
        preview_digest="a" * 64,
        epochs={"accounting": 1, "material": 2, "management": 3},
    )["request_id"]
    assert call(native, "native_inspect", request_id)["facts"]["period_month"] == "2026-09"
    result = call(native, "native_execute", request_id, password=PASSWORD)
    assert result["approval_id"] == "synthetic"
    assert seen[0]["preview_digest"] == "a" * 64


@pytest.mark.parametrize("prefill", [None, "  suggested.owner  "])
def test_bootstrap_accepts_native_login_name_and_finish_uses_chosen_name(tmp_path, prefill):
    service, store, native, opened = controller(tmp_path)
    fields = {"kind": "bootstrap_owner"}
    if prefill is not None:
        fields["login_name"] = prefill
    request_id = native.request(**fields)["request_id"]
    assert opened == [request_id]
    inspect = call(native, "native_inspect", request_id)
    assert inspect["request"]["login_name"] == ("suggested.owner" if prefill else None)
    assert not service.status()["provisioned"]
    chosen = "  New.Owner_2026  "
    result = call(
        native,
        "native_execute",
        request_id,
        login_name=chosen,
        new_password=PASSWORD,
        repeat_password=PASSWORD,
    )
    assert result["operation_committed"] and result["recovery_code"]
    assert service.status()["login_name"] == "New.Owner_2026"
    assert native._records[request_id].request.login_name == "New.Owner_2026"
    assert not result["login_completed"] and store.load_session_token() is None
    call(native, "native_finish", request_id, new_password=PASSWORD)
    authority = service.authorize(store.load_session_token())
    assert authority.owner_id == native.session_status()["owner_id"]
    assert native.status(request_id)["login_completed"]


@pytest.mark.parametrize("name", [None, "", "  ", "ab", "bad name", "中文名称", 42])
def test_invalid_or_missing_native_bootstrap_name_preserves_record_and_owner(tmp_path, name):
    service, store, native, _ = controller(tmp_path)
    request_id = native.request(kind="bootstrap_owner", login_name="prefilled.owner")["request_id"]
    before = vars(native._records[request_id]).copy()
    data = {"new_password": PASSWORD, "repeat_password": PASSWORD}
    if name is not None:
        data["login_name"] = name
    with pytest.raises(IdentityError, match="LOGIN_NAME_(REQUIRED|INVALID)"):
        call(native, "native_execute", request_id, **data)
    assert vars(native._records[request_id]) == before
    assert not service.status()["provisioned"]
    assert store.load_session_token() is None


def test_bootstrap_password_failure_does_not_adopt_edited_name(tmp_path):
    service, _, native, _ = controller(tmp_path)
    request_id = native.request(kind="bootstrap_owner", login_name="suggested.owner")["request_id"]
    before = vars(native._records[request_id]).copy()
    with pytest.raises(IdentityError, match="PASSWORD_CONFIRMATION_MISMATCH"):
        call(
            native,
            "native_execute",
            request_id,
            login_name="chosen.owner",
            new_password=PASSWORD,
            repeat_password=NEW_PASSWORD,
        )
    assert vars(native._records[request_id]) == before
    assert not service.status()["provisioned"]


def test_native_bootstrap_name_is_private_and_existing_owner_still_rejects_bootstrap(tmp_path):
    service, _, native, _ = controller(tmp_path)
    request_id = native.request(kind="bootstrap_owner")["request_id"]
    before = vars(native._records[request_id]).copy()
    with pytest.raises(IdentityError, match="PRIVATE_CHANNEL_REQUIRED"):
        native.dispatch(
            "native_execute",
            {
                "request_id": request_id,
                "login_name": "chosen.owner",
                "new_password": PASSWORD,
                "repeat_password": PASSWORD,
            },
        )
    assert vars(native._records[request_id]) == before
    assert not service.status()["provisioned"]
    service.provision("existing.owner", PASSWORD)
    with pytest.raises(IdentityError, match="OWNER_ALREADY_PROVISIONED"):
        native.request(kind="bootstrap_owner")
    with pytest.raises(IdentityError, match="OWNER_ALREADY_PROVISIONED"):
        call(
            native,
            "native_execute",
            request_id,
            login_name="chosen.owner",
            new_password=PASSWORD,
            repeat_password=PASSWORD,
        )
    assert vars(native._records[request_id]) == before
    assert service.status()["login_name"] == "existing.owner"


@pytest.mark.parametrize(
    "kind", ["login", "change_password", "recover", "replace_recovery_code", "approve_period_close"]
)
def test_nonbootstrap_native_execute_cannot_override_login_name(tmp_path, kind):
    service, store, native, _ = controller(tmp_path)
    service.provision("existing.owner", PASSWORD)
    store.save_session_token(service.login("existing.owner", PASSWORD).session_token)
    native.inspect_close = lambda request: {}
    native.close_issuer = lambda *args: pytest.fail("issuer must not be called")
    fields = {"kind": kind}
    if kind == "approve_period_close":
        fields.update(
            company_id="company",
            database_id="database",
            period="2026-09",
            preview_digest="a" * 64,
            epochs={"accounting": 1, "material": 1, "management": 1},
        )
    request_id = native.request(**fields)["request_id"]
    before = vars(native._records[request_id]).copy()
    with pytest.raises(IdentityError, match="REQUEST_INVALID"):
        call(native, "native_execute", request_id, login_name="another.owner", password=PASSWORD)
    assert vars(native._records[request_id]) == before
    assert service.status()["login_name"] == "existing.owner"
