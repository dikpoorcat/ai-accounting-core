from __future__ import annotations

import asyncio
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ai_accounting import mcp_server
from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.company_router import CompanyDatabaseRouter
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.config import Settings
from ai_accounting.credential_store import InMemoryCredentialStore, WindowsCredentialStore
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutorIdentity, ExecutorKind
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import (
    AccountingPeriodAction,
    BusinessEvent,
    Evidence,
    ExecutionAttribution,
    Voucher,
)

pytestmark = pytest.mark.postgres

PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)


def _protect_current_windows_user_only(path: Path) -> None:
    import win32api
    import win32con
    import win32security

    process_token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        current_sid = win32security.GetTokenInformation(
            process_token,
            win32security.TokenUser,
        )[0]
    finally:
        process_token.Close()
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_ALL,
        current_sid,
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        current_sid,
        None,
        dacl,
        None,
    )


def _insert_attribution(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    attribution_id: uuid.UUID,
    catalog_instance_id: uuid.UUID,
) -> None:
    connection.execute(
        sa.text("SELECT set_config('finance.execution_attribution_id', :value, true)"),
        {"value": str(attribution_id)},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_attributions (
                id, org_id, catalog_instance_id, owner_account_id, owner_session_id,
                owner_credential_version, executor_kind, executor_name,
                executor_version, tool_name, request_correlation_id, created_at
            ) VALUES (
                :id, :org, :catalog, :owner, :session, 1, 'ai_agent',
                'ai-accounting-core', '0.1.0', 'finance_register_evidence',
                :correlation, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": attribution_id,
            "org": org_id,
            "owner": owner_id,
            "session": session_id,
            "catalog": catalog_instance_id,
            "correlation": uuid.uuid4(),
        },
    )


def _insert_evidence(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    attribution_id: uuid.UUID | None,
    suffix: str,
) -> uuid.UUID:
    evidence_id = uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO evidence (
                id, org_id, sha256, original_name, media_type, source,
                size_bytes, storage_path, metadata, execution_attribution_id, created_at
            ) VALUES (
                :id, :org, :sha, :name, 'text/plain', 'test', 1,
                :path, '{}'::jsonb, :attribution, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": evidence_id,
            "org": org_id,
            "sha": suffix * 64,
            "name": f"{suffix}.txt",
            "path": f"test/{suffix}.txt",
            "attribution": attribution_id,
        },
    )
    return evidence_id


def _insert_workflow_export(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    attribution_id: uuid.UUID | None,
    suffix: str,
) -> uuid.UUID:
    export_id = uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO payroll_tax_import_exports (
                id, org_id, payroll_period, payroll_source_hash,
                source_snapshot_hash, source_batches, idempotency_key,
                request_payload_hash, relative_storage_path, file_name,
                file_sha256, row_count, execution_attribution_id, created_at
            ) VALUES (
                :id, :org, '2026-08', :payroll_hash, :source_hash,
                '[]'::jsonb, :key, :request_hash, :path, :name,
                :file_hash, 1, :attribution, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": export_id,
            "org": org_id,
            "payroll_hash": suffix * 64,
            "source_hash": suffix * 64,
            "key": f"workflow-export-{suffix}",
            "request_hash": suffix * 64,
            "path": f"exports/{suffix}.xls",
            "name": f"{suffix}.xls",
            "file_hash": suffix * 64,
            "attribution": attribution_id,
        },
    )
    return export_id


def test_postgres_current_transaction_attribution_and_direct_sql_guards() -> None:
    with authenticated_business_database("execution_attribution") as data:
        engine, org_id, _evidence_id, authority = data
        context = authority.context
        with engine.connect() as connection:
            close_validator = connection.scalar(
                sa.text(
                    "SELECT pg_get_functiondef("
                    "'public.finance_assert_accounting_period_close(uuid)'::regprocedure)"
                )
            )
            assert "accounting_period_close_checker_2026.8" in close_validator
            assert "ACCOUNTING_PERIOD_OWNER_WORKFLOW_GATE_INVALID" in close_validator

        attribution_id = uuid.uuid4()
        with engine.begin() as connection:
            _insert_attribution(
                connection,
                org_id=org_id,
                owner_id=context.owner_account_id,
                session_id=context.owner_session_id,
                attribution_id=attribution_id,
                catalog_instance_id=context.catalog_instance_id,
            )
            connection.execute(
                sa.text("SELECT set_config('finance.execution_attribution_id', :value, true)"),
                {"value": str(attribution_id)},
            )
            first = _insert_evidence(
                connection, org_id=org_id, attribution_id=attribution_id, suffix="b"
            )
            _insert_evidence(connection, org_id=org_id, attribution_id=attribution_id, suffix="c")
            workflow_export_id = _insert_workflow_export(
                connection, org_id=org_id, attribution_id=attribution_id, suffix="a"
            )
            with pytest.raises(DBAPIError, match="BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED"):
                with connection.begin_nested():
                    _insert_workflow_export(
                        connection, org_id=org_id, attribution_id=None, suffix="f"
                    )
        with engine.begin() as connection:
            connection.execute(
                sa.text("SELECT set_config('finance.execution_attribution_id', :value, true)"),
                {"value": str(attribution_id)},
            )
            with pytest.raises(DBAPIError, match="BUSINESS_EXECUTION_ATTRIBUTION_NOT_CURRENT"):
                with connection.begin_nested():
                    _insert_evidence(
                        connection, org_id=org_id, attribution_id=attribution_id, suffix="d"
                    )
            with pytest.raises(DBAPIError, match="BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED"):
                with connection.begin_nested():
                    _insert_evidence(connection, org_id=org_id, attribution_id=None, suffix="e")
            with pytest.raises(DBAPIError, match="BUSINESS_EXECUTION_ATTRIBUTION_IMMUTABLE"):
                with connection.begin_nested():
                    connection.execute(
                        sa.text(
                            "UPDATE evidence SET execution_attribution_id = NULL WHERE id = :id"
                        ),
                        {"id": first},
                    )
            with pytest.raises(DBAPIError, match="EXECUTION_ATTRIBUTION_APPEND_ONLY"):
                with connection.begin_nested():
                    connection.execute(
                        sa.text(
                            "UPDATE execution_attributions SET tool_name = "
                            "'finance_record_event' WHERE id = :id"
                        ),
                        {"id": attribution_id},
                    )
            for statement in (
                "UPDATE payroll_tax_import_exports SET row_count = 2 WHERE id = :id",
                "DELETE FROM payroll_tax_import_exports WHERE id = :id",
            ):
                with pytest.raises(DBAPIError, match="FINANCIAL_STATEMENT_FACT_IMMUTABLE"):
                    with connection.begin_nested():
                        connection.execute(sa.text(statement), {"id": workflow_export_id})


@pytest.mark.parametrize("contender", ["logout", "credential_rotation"])
def test_postgres_attribution_and_revocation_share_owner_then_session_lock_order(
    contender: str,
) -> None:
    with authenticated_business_database("execution_revocation") as data:
        business_engine, org_id, _evidence_id, authority = data
        assert authority.catalog_url is not None and authority.session_token is not None
        catalog_engine = sa.create_engine(authority.catalog_url)
        business_factory = sessionmaker(bind=business_engine, expire_on_commit=False)
        catalog_factory = sessionmaker(bind=catalog_engine, expire_on_commit=False)
        context = authority.context
        writer_locked = Event()
        contender_started = Event()
        release_writer = Event()

        def write() -> uuid.UUID:
            with catalog_factory.begin() as catalog_session:
                catalog_session.info["catalog_mode"] = True
                fresh_context = IdentityService(catalog_session).authorize_execution(
                    session_token=authority.session_token.get_secret_value(),
                    executor=ExecutorIdentity(
                        kind=ExecutorKind.AI_AGENT,
                        executor_name="ai-accounting-core",
                        executor_version="0.1.0",
                    ),
                    request_correlation_id=uuid.uuid4(),
                    expected_org_id=org_id,
                )
                writer_locked.set()
                assert contender_started.wait(timeout=10)
                assert release_writer.wait(timeout=10)
                with business_factory.begin() as business_session:
                    with persist_execution_attribution(
                        business_session,
                        context=fresh_context,
                        tool_name="finance_register_evidence",
                    ) as attribution:
                        business_session.add(
                            Evidence(
                                org_id=org_id,
                                sha256="9" * 64,
                                original_name="lock-order.txt",
                                source="test",
                                size_bytes=1,
                                storage_path="test/lock-order.txt",
                            )
                        )
                        business_session.flush()
                        return attribution.id

        def revoke_or_rotate() -> None:
            assert writer_locked.wait(timeout=10)
            contender_started.set()
            with catalog_engine.begin() as connection:
                connection.execute(
                    sa.text("SELECT id FROM owner_accounts WHERE id = :id FOR UPDATE"),
                    {"id": context.owner_account_id},
                )
                connection.execute(
                    sa.text("SELECT id FROM owner_sessions WHERE id = :id FOR UPDATE"),
                    {"id": context.owner_session_id},
                )
                if contender == "credential_rotation":
                    connection.execute(
                        sa.text(
                            """
                            UPDATE owner_accounts
                               SET password_hash = :hash,
                                   credential_version = credential_version + 1,
                                   password_changed_at = password_changed_at + interval '1 second',
                                   updated_at = updated_at + interval '1 second'
                             WHERE id = :id
                            """
                        ),
                        {
                            "id": context.owner_account_id,
                            "hash": PASSWORD_HASH.replace("B", "C"),
                        },
                    )
                connection.execute(
                    sa.text(
                        """
                        UPDATE owner_sessions SET revoked_at = CURRENT_TIMESTAMP,
                           revoke_reason = :reason WHERE id = :id
                        """
                    ),
                    {
                        "id": context.owner_session_id,
                        "reason": (
                            "credential_changed" if contender == "credential_rotation" else "logout"
                        ),
                    },
                )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                write_future = executor.submit(write)
                contender_future = executor.submit(revoke_or_rotate)
                assert contender_started.wait(timeout=10)
                release_writer.set()
                attribution_id = write_future.result(timeout=20)
                contender_future.result(timeout=20)
            with business_engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT count(*) FROM execution_attributions WHERE id = :id"),
                        {"id": attribution_id},
                    )
                    == 1
                )
            with catalog_engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT revoked_at IS NOT NULL FROM owner_sessions WHERE id = :id"),
                        {"id": context.owner_session_id},
                    )
                    is True
                )
        finally:
            catalog_engine.dispose()


def test_postgres_authenticated_mcp_rejected_posted_and_replay_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with authenticated_business_database("execution_mcp") as data:
        business_engine, org_id, evidence_id, authority = data
        assert authority.catalog_url is not None and authority.session_token is not None
        catalog_engine = sa.create_engine(authority.catalog_url)
        catalog_factory = sessionmaker(bind=catalog_engine, expire_on_commit=False)
        settings = Settings(
            _env_file=None,
            finance_environment="development",
            database_url=authority.catalog_url,
            finance_company_database_url=business_engine.url.render_as_string(hide_password=False),
            finance_migration_database_url=business_engine.url.render_as_string(
                hide_password=False
            ),
        )
        try:
            monkeypatch.setattr(
                mcp_server,
                "SessionLocal",
                mcp_server._ContextAwareSessionFactory(catalog_factory),
            )
            monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
            router = CompanyDatabaseRouter(settings)
            monkeypatch.setattr(router, "engine_for", lambda _registry: business_engine)
            monkeypatch.setattr(mcp_server, "company_router", router)
            credential_store = InMemoryCredentialStore()
            credential_store.save_session_token(authority.session_token)
            mcp_server._set_mcp_credential_store_for_tests(credential_store)

            record_tool = mcp_server.mcp._tool_manager.get_tool("finance_record_event")
            assert record_tool is not None
            rejected = record_tool.fn(
                request=RecordEventRequest.model_validate(
                    {
                        "org_id": org_id,
                        "idempotency_key": "pg-auth-missing-expense-facts",
                        "posting_date": "2026-08-01",
                        "components": [
                            {
                                "key": "expense",
                                "kind": "expense",
                                "business_date": "2026-08-01",
                                "amount_fen": 100,
                            }
                        ],
                    }
                )
            )
            assert rejected["status"] == "needs_information"

            request = GenerateAccountingPeriodRequest(
                org_id=org_id,
                period_month="2026-08",
                idempotency_key="pg-auth-period",
                confirmation_note="owner supplied review facts",
                evidence_references=[evidence_id],
            )
            period_tool = mcp_server.mcp._tool_manager.get_tool(
                "finance_generate_accounting_period"
            )
            assert period_tool is not None
            posted = period_tool.fn(request=request)
            assert posted["status"] == "posted"
            replay = period_tool.fn(request=request)
            assert replay["status"] == "posted"
            assert replay["data"]["idempotent_replay"] is True

            with Session(business_engine) as session:
                before_preview = tuple(
                    session.query(model).count()
                    for model in (BusinessEvent, Voucher, ExecutionAttribution)
                )
            preview_tool = mcp_server.mcp._tool_manager.get_tool("finance_preview_event")
            assert preview_tool is not None
            preview_request = RecordEventRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "pg-auth-preview-event",
                    "posting_date": "2026-08-01",
                    "evidence_references": [evidence_id],
                    "components": [
                        {
                            "key": "expense",
                            "kind": "expense",
                            "business_date": "2026-08-01",
                            "amount_fen": 100,
                            "expense_class": "general_expense",
                            "payment_basis": "supplier_credit",
                            "metadata": {
                                "counterparty": {
                                    "kind": "supplier",
                                    "name": "PostgreSQL 试算供应商",
                                }
                            },
                        }
                    ],
                }
            )
            previewed = preview_tool.fn(request=preview_request)
            assert previewed["status"] == "calculated", previewed
            assert previewed["data"]["reviewed_request"] == preview_request.model_dump(mode="json")
            with Session(business_engine) as session:
                assert (
                    tuple(
                        session.query(model).count()
                        for model in (BusinessEvent, Voucher, ExecutionAttribution)
                    )
                    == before_preview
                )

            with Session(business_engine) as session:
                attributions = (
                    session.query(ExecutionAttribution)
                    .order_by(ExecutionAttribution.created_at, ExecutionAttribution.id)
                    .all()
                )
                assert len(attributions) == 4
                assert [item.tool_name for item in attributions[-3:]] == [
                    "finance_record_event",
                    "finance_generate_accounting_period",
                    "finance_generate_accounting_period",
                ]
                action = session.query(AccountingPeriodAction).one()
                assert action.confirmed_by is None
                assert action.execution_attribution_id == attributions[-2].id
        finally:
            mcp_server._set_mcp_credential_store_for_tests(None)
            catalog_engine.dispose()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_credential_manager_real_routed_stdio_write_attribution(
    tmp_path: Path,
) -> None:
    with authenticated_business_database("execution_stdio") as data:
        engine, org_id, _evidence_id, authority = data
        assert authority.catalog_url is not None and authority.session_token is not None
        target = f"ai-accounting-core/test-stdio-session/{uuid.uuid4()}"
        store = WindowsCredentialStore(target_name=target)
        previous = store.load_session_token()
        storage = (tmp_path / "storage").resolve()
        evidence_dir = storage / "evidence"
        evidence_import_dir = (tmp_path / "incoming" / "evidence").resolve()
        bank_import_dir = (tmp_path / "incoming" / "bank").resolve()
        lock_file = storage / "service.lock"
        for directory in (storage, evidence_dir, evidence_import_dir, bank_import_dir):
            directory.mkdir(parents=True, exist_ok=True)
        lock_file.touch()
        _protect_current_windows_user_only(lock_file)
        store.delete_session_token()

        repository_root = Path(__file__).parents[1]
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        business_url = engine.url.render_as_string(hide_password=False)
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        [
                            str(repository_root / "src"),
                            str(site_packages),
                            str(site_packages / "win32"),
                            str(site_packages / "win32" / "lib"),
                            str(site_packages / "pywin32_system32"),
                            environment.get("PYTHONPATH"),
                        ],
                    )
                ),
                "FINANCE_ENVIRONMENT": "development",
                "DATABASE_URL": authority.catalog_url,
                "FINANCE_COMPANY_DATABASE_URL": business_url,
                "FINANCE_MIGRATION_DATABASE_URL": business_url,
                "FINANCE_STORAGE_DIR": str(storage),
                "FINANCE_SERVICE_LOCK_FILE": str(lock_file),
                "FINANCE_EVIDENCE_DIR": str(evidence_dir),
                "FINANCE_EVIDENCE_IMPORT_DIR": str(evidence_import_dir),
                "FINANCE_BANK_IMPORT_DIR": str(bank_import_dir),
            }
        )
        script = """
import sys
from sqlalchemy import create_engine
from ai_accounting import mcp_server
from ai_accounting.credential_store import WindowsCredentialStore

mcp_server.WindowsCredentialStore = lambda: WindowsCredentialStore(
    target_name=sys.argv[1]
)
mcp_server.assert_runtime_role = lambda _connection: None
business_engine = create_engine(sys.argv[2])
mcp_server.company_router.engine_for = lambda _registry: business_engine
mcp_server.main()
"""

        async def invoke() -> tuple[object, object, object]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", script, target, business_url],
                cwd=repository_root,
                env=environment,
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    before_login = await session.call_tool(
                        "finance_get_profile", {"org_id": str(org_id)}
                    )
                    store.save_session_token(authority.session_token)
                    authenticated = await session.call_tool(
                        "finance_register_evidence",
                        {
                            "request": {
                                "org_id": str(org_id),
                                "source": "real-stdio-test",
                                "content_base64": "eA==",
                                "original_name": "real-stdio.txt",
                            }
                        },
                    )
                    store.delete_session_token()
                    after_logout = await session.call_tool(
                        "finance_get_profile", {"org_id": str(org_id)}
                    )
                    return before_login, authenticated, after_logout

        try:
            before_login, response, after_logout = asyncio.run(invoke())
            assert before_login.isError is False
            assert before_login.structuredContent == {
                "status": "rejected",
                "errors": ["AUTHENTICATION_REQUIRED"],
            }
            assert response.isError is False
            assert response.structuredContent is not None
            assert response.structuredContent["status"] == "registered"
            assert after_logout.isError is False
            assert after_logout.structuredContent == {
                "status": "rejected",
                "errors": ["AUTHENTICATION_REQUIRED"],
            }
            with engine.connect() as connection:
                attribution = connection.execute(
                    sa.text(
                        "SELECT id, tool_name FROM execution_attributions "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).one()
                assert attribution.tool_name == "finance_register_evidence"
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM evidence "
                            "WHERE original_name = 'real-stdio.txt' "
                            "AND execution_attribution_id = :attribution_id"
                        ),
                        {"attribution_id": attribution.id},
                    )
                    == 1
                )
        finally:
            if previous is None:
                store.delete_session_token()
            else:
                store.save_session_token(previous)
