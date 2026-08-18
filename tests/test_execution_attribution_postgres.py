from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
import sqlalchemy as sa
from alembic.config import Config
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

from ai_accounting import mcp_server
from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.credential_store import WindowsCredentialStore
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutorIdentity, ExecutorKind, token_sha256
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import AccountingPeriodAction, Evidence, ExecutionAttribution
from ai_accounting.schemas import EventType, RecordEventRequest
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]

PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)
MCP_TOKEN = "postgres-mcp-owner-session"


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


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _authority(
    connection: sa.Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    org_id, owner_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO organizations (
                id, name, taxpayer_type, filing_cycle, jurisdiction,
                urban_maintenance_rate, accounting_standard, created_at
            ) VALUES (:org, 'execution pg', 'small_scale', 'quarterly', 'CN',
                      0.07, 'small_enterprise', :now)
            """
        ),
        {"org": org_id, "now": now},
    )
    # Period generation in the attribution test needs a pre-owner evidence
    # fact.  Keep it out of the helper's return contract: authority consists
    # of exactly the organization, owner, and session identifiers.
    evidence_id = uuid.uuid5(org_id, "execution-attribution-period-evidence")
    connection.execute(
        sa.text(
            """
            INSERT INTO evidence (
                id, org_id, sha256, original_name, media_type, source,
                size_bytes, storage_path, metadata, created_at
            ) VALUES (
                :id, :org, :sha, 'period-evidence.txt', 'text/plain', 'test',
                1, 'test/period-evidence.txt', '{}'::jsonb, :now
            )
            """
        ),
        {"id": evidence_id, "org": org_id, "sha": "f" * 64, "now": now},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO owner_accounts (
                id, org_id, login_name, login_name_normalized, password_hash,
                password_changed_at, created_at, updated_at
            ) VALUES (:owner, :org, 'owner', 'owner', :hash, :now, :now, :now)
            """
        ),
        {"owner": owner_id, "org": org_id, "hash": PASSWORD_HASH, "now": now},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO owner_sessions (
                id, org_id, owner_account_id, secret_sha256, credential_version,
                created_at, last_seen_at, idle_expires_at, absolute_expires_at
            ) VALUES (:session, :org, :owner, :secret, 1, :now, :now, :idle, :absolute)
            """
        ),
        {
            "session": session_id,
            "org": org_id,
            "owner": owner_id,
            "secret": token_sha256(MCP_TOKEN),
            "now": now,
            "idle": now + timedelta(minutes=30),
            "absolute": now + timedelta(hours=8),
        },
    )
    return org_id, owner_id, session_id


def _insert_attribution(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    attribution_id: uuid.UUID,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_attributions (
                id, org_id, owner_account_id, owner_session_id,
                owner_credential_version, executor_kind, executor_name,
                executor_version, tool_name, request_correlation_id, created_at
            ) VALUES (
                :id, :org, :owner, :session, 1, 'ai_agent',
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


def test_postgres_current_transaction_attribution_and_direct_sql_guards() -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with engine.begin() as connection:
                org_id, owner_id, session_id = _authority(connection)

            attribution_id = uuid.uuid4()
            with engine.begin() as connection:
                _insert_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    attribution_id=attribution_id,
                )
                connection.execute(
                    sa.text(
                        "SELECT set_config('finance.execution_attribution_id', :value, true)"
                    ),
                    {"value": str(attribution_id)},
                )
                first = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="b",
                )
                _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="c",
                )
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "SELECT set_config('finance.execution_attribution_id', :value, true)"
                    ),
                    {"value": str(attribution_id)},
                )
                with pytest.raises(DBAPIError, match="BUSINESS_EXECUTION_ATTRIBUTION_NOT_CURRENT"):
                    with connection.begin_nested():
                        _insert_evidence(
                            connection,
                            org_id=org_id,
                            attribution_id=attribution_id,
                            suffix="d",
                        )
                with pytest.raises(DBAPIError, match="BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED"):
                    with connection.begin_nested():
                        _insert_evidence(
                            connection,
                            org_id=org_id,
                            attribution_id=None,
                            suffix="e",
                        )
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
        finally:
            engine.dispose()


@pytest.mark.parametrize("contender", ["logout", "credential_rotation"])
def test_postgres_attribution_and_revocation_share_owner_then_session_lock_order(
    contender: str,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with engine.begin() as connection:
                org_id, owner_id, session_id = _authority(connection)
            writer_locked = Event()
            contender_started = Event()
            release_writer = Event()

            def write() -> uuid.UUID:
                with factory.begin() as session:
                    context = IdentityService(session).authorize_execution(
                        session_token=MCP_TOKEN,
                        executor=ExecutorIdentity(
                            kind=ExecutorKind.AI_AGENT,
                            executor_name="ai-accounting-core",
                            executor_version="0.1.0",
                        ),
                        request_correlation_id=uuid.uuid4(),
                        expected_org_id=org_id,
                    )
                    with persist_execution_attribution(
                        session,
                        context=context,
                        tool_name="finance_register_evidence",
                    ) as attribution:
                        writer_locked.set()
                        assert contender_started.wait(timeout=10)
                        assert release_writer.wait(timeout=10)
                        session.add(
                            Evidence(
                                org_id=org_id,
                                sha256="9" * 64,
                                original_name="lock-order.txt",
                                source="test",
                                size_bytes=1,
                                storage_path="test/lock-order.txt",
                            )
                        )
                        session.flush()
                        return attribution.id

            def revoke_or_rotate() -> None:
                assert writer_locked.wait(timeout=10)
                contender_started.set()
                with engine.begin() as connection:
                    connection.execute(
                        sa.text("SELECT id FROM owner_accounts WHERE id = :id FOR UPDATE"),
                        {"id": owner_id},
                    )
                    connection.execute(
                        sa.text("SELECT id FROM owner_sessions WHERE id = :id FOR UPDATE"),
                        {"id": session_id},
                    )
                    if contender == "credential_rotation":
                        connection.execute(
                            sa.text(
                                """
                                UPDATE owner_accounts
                                   SET password_hash = :hash,
                                       credential_version = credential_version + 1,
                                       password_changed_at =
                                           password_changed_at + interval '1 second',
                                       updated_at = updated_at + interval '1 second'
                                 WHERE id = :id
                                """
                            ),
                            {
                                "id": owner_id,
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
                            "id": session_id,
                            "reason": (
                                "credential_changed" if contender == "credential_rotation"
                                else "logout"
                            ),
                        },
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                write_future = executor.submit(write)
                contender_future = executor.submit(revoke_or_rotate)
                assert contender_started.wait(timeout=10)
                release_writer.set()
                attribution_id = write_future.result(timeout=20)
                contender_future.result(timeout=20)
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT count(*) FROM execution_attributions WHERE id = :id"),
                    {"id": attribution_id},
                ) == 1
                assert connection.scalar(
                    sa.text("SELECT revoked_at IS NOT NULL FROM owner_sessions WHERE id = :id"),
                    {"id": session_id},
                ) is True
        finally:
            engine.dispose()


def test_postgres_authenticated_mcp_rejected_posted_and_replay_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with engine.begin() as connection:
                org_id, _owner_id, _session_id = _authority(connection)
                evidence_id = uuid.uuid5(
                    org_id, "execution-attribution-period-evidence"
                )
            monkeypatch.setattr(
                mcp_server,
                "SessionLocal",
                mcp_server._ContextAwareSessionFactory(factory),  # type: ignore[attr-defined]
            )
            monkeypatch.setattr(
                mcp_server,
                "get_settings",
                lambda: type("Settings", (), {"finance_environment": "production"})(),
            )
            mcp_server._set_mcp_session_token_for_tests(SecretStr(MCP_TOKEN))

            record_tool = mcp_server.mcp._tool_manager.get_tool("finance_record_event")
            assert record_tool is not None
            rejected = record_tool.fn(
                request=RecordEventRequest.model_construct(
                    org_id=org_id,
                    event_type=EventType.PAYROLL,
                )
            )
            assert rejected["status"] == "rejected"

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

            with factory() as session:
                attributions = session.query(ExecutionAttribution).order_by(
                    ExecutionAttribution.created_at, ExecutionAttribution.id
                ).all()
                assert len(attributions) == 3
                assert [item.tool_name for item in attributions] == [
                    "finance_record_event",
                    "finance_generate_accounting_period",
                    "finance_generate_accounting_period",
                ]
                action = session.query(AccountingPeriodAction).one()
                assert action.confirmed_by is None
                assert action.execution_attribution_id == attributions[1].id
        finally:
            mcp_server._set_mcp_session_token_for_tests(None)
            engine.dispose()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_credential_manager_real_production_stdio_write_attribution(
    tmp_path: Path,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
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
        try:
            with engine.begin() as connection:
                org_id, _owner_id, _session_id = _authority(connection)
            store.save_session_token(SecretStr(MCP_TOKEN))

            repository_root = Path(__file__).parents[1]
            site_packages = Path(sys.prefix) / "Lib" / "site-packages"
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
                    "FINANCE_ENVIRONMENT": "production",
                    "DATABASE_URL": database_url,
                    "FINANCE_MIGRATION_DATABASE_URL": str(
                        sa.engine.make_url(database_url).set(username="migration_role")
                    ),
                    "FINANCE_STORAGE_DIR": str(storage),
                    "FINANCE_SERVICE_LOCK_FILE": str(lock_file),
                    "FINANCE_EVIDENCE_DIR": str(evidence_dir),
                    "FINANCE_EVIDENCE_IMPORT_DIR": str(evidence_import_dir),
                    "FINANCE_BANK_IMPORT_DIR": str(bank_import_dir),
                }
            )
            script = """
import sys
from ai_accounting import mcp_server
from ai_accounting.credential_store import WindowsCredentialStore

mcp_server.WindowsCredentialStore = lambda: WindowsCredentialStore(
    target_name=sys.argv[1]
)
mcp_server.main()
"""

            async def invoke() -> object:
                parameters = StdioServerParameters(
                    command=getattr(sys, "_base_executable", sys.executable),
                    args=["-c", script, target],
                    cwd=repository_root,
                    env=environment,
                )
                async with stdio_client(parameters) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await session.call_tool(
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

            response = asyncio.run(invoke())
            assert response.isError is False
            assert response.structuredContent is not None
            assert response.structuredContent["status"] == "registered"
            with engine.connect() as connection:
                attribution = connection.execute(
                    sa.text(
                        "SELECT id, tool_name FROM execution_attributions "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).one()
                assert attribution.tool_name == "finance_register_evidence"
                assert connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM evidence "
                        "WHERE original_name = 'real-stdio.txt' "
                        "AND execution_attribution_id = :attribution_id"
                    ),
                    {"attribution_id": attribution.id},
                ) == 1
        finally:
            if previous is None:
                store.delete_session_token()
            else:
                store.save_session_token(previous)
            engine.dispose()
