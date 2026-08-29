from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting import mcp_server
from ai_accounting.coa import seed_organization
from ai_accounting.credential_store import InMemoryCredentialStore
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutorIdentity, ExecutorKind, IdentityError
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import Evidence, ExecutionAttribution, OwnerAccount, OwnerSession


def _provision(session: Session) -> tuple[uuid.UUID, str]:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="MCP owner attribution",
    )
    service = IdentityService(session)
    service.provision_owner(
        OwnerProvisionRequest(
            org_id=organization.id,
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    login = service.authenticate(
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    session.flush()
    return organization.id, login.session_token.get_secret_value()


def _dummy_arguments(function: object, org_id: uuid.UUID) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in inspect.signature(function).parameters:
        if name == "request":
            result[name] = SimpleNamespace(org_id=org_id)
        elif name == "org_id":
            result[name] = org_id
        else:
            result[name] = uuid.uuid4()
    return result


def test_only_event_schema_is_public_and_all_data_tools_fail_closed(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, token = _provision(session)
    session.commit()
    factory = session.get_bind()
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(  # type: ignore[attr-defined]
            __import__("sqlalchemy.orm").orm.sessionmaker(
                bind=factory, expire_on_commit=False
            )
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="production"),
    )
    store = InMemoryCredentialStore()
    mcp_server._set_mcp_credential_store_for_tests(store)
    tools = mcp_server.mcp._tool_manager.list_tools()
    schema = next(tool for tool in tools if tool.name == "finance_get_event_schema")
    assert schema.fn()["status"] == "ok"

    data_tools = [tool for tool in tools if tool.name != "finance_get_event_schema"]
    for tool in data_tools:
        assert tool.fn(**_dummy_arguments(tool.fn, org_id)) == {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        }

    store.save_session_token(SecretStr("fake-session-token"))
    for tool in data_tools:
        assert tool.fn(**_dummy_arguments(tool.fn, org_id)) == {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        }

    store.save_session_token(SecretStr(token))
    company_tool_names = {
        "finance_list_companies",
        "finance_create_company",
        "finance_get_close_backup_configuration",
        "finance_configure_close_backup",
        "finance_preview_company_profile_change",
        "finance_confirm_company_profile_change",
        "finance_preview_company_status_change",
        "finance_confirm_company_status_change",
    }
    company_tools = [tool for tool in data_tools if tool.name in company_tool_names]
    for tool in company_tools:
        assert tool.fn(**_dummy_arguments(tool.fn, org_id)) == {
            "status": "rejected",
            "errors": ["MULTI_COMPANY_NOT_CONFIGURED"],
        }
    organization_tools = [tool for tool in data_tools if tool.name not in company_tool_names]
    wrong_org = uuid.uuid4()
    with Session(factory) as check:
        before = check.scalar(select(OwnerSession))
        assert before is not None
        session_state = (before.last_seen_at, before.idle_expires_at, before.revoked_at)
    for tool in organization_tools:
        assert tool.fn(**_dummy_arguments(tool.fn, wrong_org)) == {
            "status": "rejected",
            "errors": ["ORGANIZATION_CONTEXT_MISMATCH"],
        }
    with Session(factory) as check:
        after = check.scalar(select(OwnerSession))
        assert after is not None
        assert (after.last_seen_at, after.idle_expires_at, after.revoked_at) == session_state
        assert check.scalar(select(ExecutionAttribution)) is None

    with Session(factory) as revoke_session, revoke_session.begin():
        revoked = revoke_session.scalar(select(OwnerSession))
        assert revoked is not None
        revoked.revoked_at = datetime.now(UTC)
        revoked.revoke_reason = "logout"
    for tool in data_tools:
        assert tool.fn(**_dummy_arguments(tool.fn, org_id)) == {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        }
    mcp_server._set_mcp_credential_store_for_tests(None)


def test_owner_mode_root_requires_current_execution_and_attribution_is_append_only(
    session: Session,
) -> None:
    org_id, token = _provision(session)
    context = IdentityService(session).authorize_execution(
        session_token=token,
        executor=ExecutorIdentity(
            kind=ExecutorKind.AI_AGENT,
            executor_name="ai-accounting-core",
            executor_version="0.1.0",
        ),
        request_correlation_id=uuid.uuid4(),
    )
    with persist_execution_attribution(
        session,
        context=context,
        tool_name="finance_register_evidence",
    ) as attribution:
        evidence = Evidence(
            org_id=org_id,
            sha256="a" * 64,
            original_name="owner.txt",
            source="test",
            size_bytes=1,
            storage_path="test/owner.txt",
        )
        session.add(evidence)
        session.flush()
        assert evidence.execution_attribution_id == attribution.id

    unbound = Evidence(
        org_id=org_id,
        sha256="b" * 64,
        original_name="unbound.txt",
        source="test",
        size_bytes=1,
        storage_path="test/unbound.txt",
    )
    with pytest.raises(ValueError, match="BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED"):
        with session.begin_nested():
            session.add(unbound)
            session.flush()

    attribution = session.scalar(select(ExecutionAttribution))
    assert attribution is not None
    with pytest.raises(ValueError, match="EXECUTION_ATTRIBUTION_APPEND_ONLY"):
        with session.begin_nested():
            attribution.tool_name = "finance_record_event"
            session.flush()


def test_mcp_uses_current_local_session_on_every_enterprise_call(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, first_token = _provision(session)
    first_session = session.scalar(
        select(OwnerSession).order_by(OwnerSession.created_at)
    )
    assert first_session is not None
    second_login = IdentityService(session).authenticate(
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    session.commit()
    factory = session.get_bind()
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(  # type: ignore[attr-defined]
            __import__("sqlalchemy.orm").orm.sessionmaker(
                bind=factory, expire_on_commit=False
            )
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="production"),
    )
    authorized_sessions: list[uuid.UUID] = []
    authorize_execution = IdentityService.authorize_execution

    def track_authorized_session(
        service: IdentityService, **kwargs: object
    ) -> object:
        context = authorize_execution(service, **kwargs)  # type: ignore[arg-type]
        authorized_sessions.append(context.owner_session_id)
        return context

    monkeypatch.setattr(IdentityService, "authorize_execution", track_authorized_session)
    store = InMemoryCredentialStore()
    mcp_server._set_mcp_credential_store_for_tests(store)
    profile = mcp_server.mcp._tool_manager.get_tool("finance_get_profile")
    assert profile is not None
    try:
        assert profile.fn(org_id=str(org_id)) == {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        }

        store.save_session_token(SecretStr(first_token))
        profile_payload = profile.fn(org_id=str(org_id))
        assert profile_payload["status"] == "ok"
        assert profile_payload["organization"]["taxpayer_identification_number"] == (
            "91330106MA1234567T"
        )
        assert authorized_sessions == [first_session.id]

        store.save_session_token(second_login.session_token)
        assert profile.fn(org_id=str(org_id))["status"] == "ok"
        assert authorized_sessions == [first_session.id, second_login.session_id]

        store.delete_session_token()
        assert profile.fn(org_id=str(org_id)) == {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        }
        assert authorized_sessions == [first_session.id, second_login.session_id]
    finally:
        mcp_server._set_mcp_credential_store_for_tests(None)


def test_mcp_authentication_required_requests_the_local_login_launcher(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _token = _provision(session)
    session.commit()
    factory = session.get_bind()
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(  # type: ignore[attr-defined]
            __import__("sqlalchemy.orm").orm.sessionmaker(
                bind=factory, expire_on_commit=False
            )
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="production"),
    )

    class RecordingLauncher:
        def __init__(self) -> None:
            self.login_names: list[str] = []

        def request(self, *, login_name: str) -> bool:
            self.login_names.append(login_name)
            return True

    launcher = RecordingLauncher()
    monkeypatch.setattr(mcp_server, "_OWNER_LOGIN_WINDOW_LAUNCHER", launcher)
    mcp_server._set_owner_login_window_enabled(True)
    mcp_server._set_mcp_credential_store_for_tests(InMemoryCredentialStore())
    profile = mcp_server.mcp._tool_manager.get_tool("finance_get_profile")
    assert profile is not None
    try:
        expected = {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        }
        assert profile.fn(org_id=str(org_id)) == expected
        assert profile.fn(org_id=str(org_id)) == expected
        assert launcher.login_names == ["owner", "owner"]
    finally:
        mcp_server._set_owner_login_window_enabled(False)
        mcp_server._set_mcp_credential_store_for_tests(None)


def test_mcp_credential_store_failure_is_stable_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCredentialStore(InMemoryCredentialStore):
        def load_session_token(self) -> SecretStr | None:
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_READ_FAILED")

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="production"),
    )
    mcp_server._set_mcp_credential_store_for_tests(FailingCredentialStore())
    schema = mcp_server.mcp._tool_manager.get_tool("finance_get_event_schema")
    profile = mcp_server.mcp._tool_manager.get_tool("finance_get_profile")
    assert schema is not None and profile is not None
    try:
        assert schema.fn()["status"] == "ok"
        assert profile.fn(org_id=str(uuid.uuid4())) == {
            "status": "rejected",
            "errors": ["IDENTITY_CREDENTIAL_STORE_READ_FAILED"],
        }
    finally:
        mcp_server._set_mcp_credential_store_for_tests(None)


def test_development_zero_owner_path_does_not_require_a_credential_store(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.commit()
    factory = session.get_bind()
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(  # type: ignore[attr-defined]
            __import__("sqlalchemy.orm").orm.sessionmaker(
                bind=factory, expire_on_commit=False
            )
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="development"),
    )
    mcp_server._set_mcp_credential_store_for_tests(None)
    profile = mcp_server.mcp._tool_manager.get_tool("finance_get_profile")
    assert profile is not None

    assert profile.fn(org_id=str(uuid.uuid4())) == {
        "status": "rejected",
        "errors": ["ORGANIZATION_NOT_FOUND"],
    }


def test_execution_attribution_schema_has_no_secret_or_caller_identity_fields() -> None:
    forbidden = {"password", "token", "secret", "actor", "confirmed_by", "client_id"}
    columns = set(ExecutionAttribution.__table__.columns.keys())
    assert not forbidden.intersection(columns)
    assert {"owner_account_id", "owner_session_id", "executor_name", "executor_version"} <= columns
    assert "password_hash" in set(OwnerAccount.__table__.columns.keys())


def test_evidence_metadata_recursively_rejects_parallel_identity_without_writes(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="Evidence metadata boundary",
    )
    session.flush()
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(  # type: ignore[attr-defined]
            __import__("sqlalchemy.orm").orm.sessionmaker(
                bind=session.get_bind(), expire_on_commit=False
            )
        ),
    )

    async def invoke() -> None:
        with pytest.raises(ToolError, match="VALIDATION_ERROR: request.metadata"):
            await mcp_server.mcp.call_tool(
                "finance_register_evidence",
                {
                    "request": {
                        "org_id": str(organization.id),
                        "source": "test",
                        "content_base64": "eA==",
                        "metadata": {"nested": [{"Session-Token": "must-not-persist"}]},
                    }
                },
            )

    asyncio.run(invoke())
    assert session.scalar(select(ExecutionAttribution)) is None
    assert session.scalar(select(Evidence)) is None
