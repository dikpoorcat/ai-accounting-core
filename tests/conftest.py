from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    PreviewBankReconciliationScopeRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.credential_store import WindowsCredentialStore
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorIdentity, ExecutorKind
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import (
    Account,
    ExecutionAttribution,
    Organization,
)

_AUTHENTICATED_STDIO_SCRIPT = """
import sys
from ai_accounting import mcp_server
from ai_accounting.credential_store import WindowsCredentialStore

mcp_server.WindowsCredentialStore = lambda: WindowsCredentialStore(
    target_name=sys.argv[1]
)
mcp_server.main()
"""

type AuthenticatedStdioBankScope = Any


@dataclass(frozen=True)
class AuthenticatedOwnerAuthority:
    """One authenticated owner session reusable across real test write calls."""

    context: ExecutionContext

    @contextmanager
    def attributed_call(
        self, session: Session, *, tool_name: str
    ) -> Iterator[ExecutionAttribution]:
        with persist_execution_attribution(
            session,
            context=replace(self.context, request_correlation_id=uuid.uuid4()),
            tool_name=tool_name,
        ) as attribution:
            yield attribution


def authenticate_and_confirm_bank_scope(
    session: Session,
    organization: Organization,
    *,
    evidence_id: uuid.UUID,
    accounts: list[dict[str, object]],
    executor_name: str = "postgres-period-test",
) -> AuthenticatedOwnerAuthority:
    """Enter owner mode and persist one genuine attributed scope action.

    Keeping the resulting authority available makes later test writes subject
    to the same owner-mode guard as production, without synthetic database
    rows or trigger bypasses.
    """

    if organization.bank_reconciliation_scope_current_action_id is not None:
        raise AssertionError("test scope helper requires an unconfigured organization")
    password = SecretStr("Postgres-Zero-Scope-2026!")
    login_name = f"pg-zero-scope-{organization.id.hex[:12]}"
    identity = IdentityService(session)
    identity.provision_owner(
        OwnerProvisionRequest(
            org_id=organization.id,
            login_name=login_name,
            password=password,
        )
    )
    # Provisioning and the first attributed business action are separate
    # production calls.  PostgreSQL verifies the owner session from its
    # trigger, so commit that authority before creating the formal scope.
    session.commit()
    login = identity.authenticate(
        OwnerLoginRequest(login_name=login_name, password=password)
    )
    context = identity.authorize_execution(
        session_token=login.session_token.get_secret_value(),
        executor=ExecutorIdentity(
            kind=ExecutorKind.AI_AGENT,
            executor_name=executor_name,
            executor_version="v1",
        ),
        request_correlation_id=uuid.uuid4(),
    )
    request = PreviewBankReconciliationScopeRequest(
        org_id=organization.id,
        action_type="initial_confirmation",
        accounts=accounts,
        confirm_zero_accounts=not accounts,
        explanation="PostgreSQL 测试明确确认银行对账账户范围",
        evidence_references=[evidence_id],
    )
    with persist_execution_attribution(
        session,
        context=context,
        tool_name="finance_confirm_bank_reconciliation_scope",
    ):
        service = BankStatementService(session)
        preview = service.preview_bank_reconciliation_scope(request)
        assert preview.calculation_hash is not None
        confirmed = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                request.model_dump()
                | {
                    "calculation_hash": preview.calculation_hash,
                    "idempotency_key": f"pg-bank-scope-{organization.id}",
                }
            )
        )
        assert confirmed.status == "posted", confirmed
    return AuthenticatedOwnerAuthority(context=context)


def authenticate_and_confirm_zero_bank_scope(
    session: Session,
    organization: Organization,
    *,
    evidence_id: uuid.UUID,
    executor_name: str = "postgres-period-test",
) -> AuthenticatedOwnerAuthority:
    """Confirm the explicit DEC-038 zero-account scope for a test org."""

    return authenticate_and_confirm_bank_scope(
        session,
        organization,
        evidence_id=evidence_id,
        accounts=[],
        executor_name=executor_name,
    )


@pytest.fixture
def authenticated_zero_bank_scope() -> Callable[..., AuthenticatedOwnerAuthority]:
    """Expose the real zero-scope workflow to PostgreSQL invariant tests."""

    return authenticate_and_confirm_zero_bank_scope


@pytest.fixture
def authenticated_bank_scope() -> Callable[..., AuthenticatedOwnerAuthority]:
    """Expose real non-zero scope confirmation to PostgreSQL tests."""

    return authenticate_and_confirm_bank_scope


@pytest.fixture(autouse=True)
def deterministic_business_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixed-date unit tests stable; boundary tests override this clock."""

    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: date.max)
    monkeypatch.setattr(
        "ai_accounting.accounting_period_service.china_current_date", lambda: date.max
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        with session.begin():
            yield session
    engine.dispose()


@pytest.fixture
def organization(session: Session) -> Organization:
    organization = seed_organization(
        session,
        name="测试服务公司",
        accounting_period_control_enabled=False,
    )
    # Existing module tests intentionally exercise the compatibility path.
    # New accounting-period tests create a fresh organization separately and
    # assert that the product default remains enabled.
    configured_at = datetime.now(UTC)
    bank_account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.code == "1002",
        )
    )
    assert bank_account is not None
    bank_account.requires_bank_reconciliation = True
    bank_account.bank_reconciliation_start_date = date(2020, 1, 1)
    bank_account.bank_reconciliation_configured_at = configured_at
    session.flush()
    # Legacy service-level tests are not scope-workflow tests.  Give their
    # already-loaded organization an explicit confirmed scope without
    # persisting a synthetic formal action.  Scope boundary tests seed their
    # own organization and therefore continue to exercise the real workflow.
    set_committed_value(
        organization,
        "bank_reconciliation_scope_current_action_id",
        uuid.uuid4(),
    )
    set_committed_value(
        organization,
        "bank_reconciliation_scope_confirmed_at",
        configured_at,
    )
    return organization


@pytest.fixture
def authenticated_stdio_bank_scope() -> Iterator[AuthenticatedStdioBankScope]:
    """Create one real attributed scope action and isolated STDIO credential."""

    stores: list[WindowsCredentialStore] = []

    def prepare(
        session: Session,
        organization: Organization,
        evidence_id: uuid.UUID,
        accounts: list[dict[str, object]],
    ) -> list[str]:
        password = SecretStr("Authenticated-STDIO-Scope-2026!")
        login_name = f"stdio-owner-{organization.id.hex[:12]}"
        identity = IdentityService(session)
        identity.provision_owner(
            OwnerProvisionRequest(
                org_id=organization.id,
                login_name=login_name,
                password=password,
            )
        )
        login = identity.authenticate(
            OwnerLoginRequest(login_name=login_name, password=password)
        )
        context = identity.authorize_execution(
            session_token=login.session_token.get_secret_value(),
            executor=ExecutorIdentity(
                kind=ExecutorKind.AI_AGENT,
                executor_name="stdio-scope-test",
                executor_version="v1",
            ),
            request_correlation_id=uuid.uuid4(),
        )
        request = PreviewBankReconciliationScopeRequest(
            org_id=organization.id,
            action_type="initial_confirmation",
            accounts=accounts,
            confirm_zero_accounts=not accounts,
            explanation="STDIO 回归明确确认完整银行账户范围",
            evidence_references=[evidence_id],
        )
        with persist_execution_attribution(
            session,
            context=context,
            tool_name="finance_confirm_bank_reconciliation_scope",
        ):
            service = BankStatementService(session)
            preview = service.preview_bank_reconciliation_scope(request)
            assert preview.calculation_hash is not None
            confirmed = service.confirm_bank_reconciliation_scope(
                ConfirmBankReconciliationScopeRequest.model_validate(
                    request.model_dump()
                    | {
                        "calculation_hash": preview.calculation_hash,
                        "idempotency_key": f"stdio-scope-{organization.id}",
                    }
                )
            )
            assert confirmed.status == "posted", confirmed

        target = f"ai-accounting-core/test-stdio-scope/{uuid.uuid4()}"
        store = WindowsCredentialStore(target_name=target)
        store.save_session_token(login.session_token)
        stores.append(store)
        return ["-c", _AUTHENTICATED_STDIO_SCRIPT, target]

    try:
        yield prepare
    finally:
        for store in stores:
            store.delete_session_token()
