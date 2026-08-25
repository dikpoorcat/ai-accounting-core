from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.credential_store import WindowsCredentialStore
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorIdentity, ExecutorKind
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    Account,
    AccountingPeriod,
    BankTransaction,
    Evidence,
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

_TEST_BANK_AUTHORITY_SESSION_KEY = "test_authenticated_bank_authority"


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


def _ensure_test_accounting_period(
    session: Session,
    organization: Organization,
    authority: AuthenticatedOwnerAuthority,
    booking_date: date,
) -> None:
    desired_month = booking_date.replace(day=1)
    periods = session.scalars(
        select(AccountingPeriod)
        .where(AccountingPeriod.org_id == organization.id)
        .order_by(AccountingPeriod.start_date)
    ).all()
    if not periods:
        raise AssertionError("authenticated bank fixture has no generated period")
    next_month = periods[-1].start_date
    while next_month < desired_month:
        next_month = (
            date(next_month.year + 1, 1, 1)
            if next_month.month == 12
            else date(next_month.year, next_month.month + 1, 1)
        )
        evidence = session.scalar(
            select(Evidence).where(
                Evidence.org_id == organization.id,
                Evidence.original_name == "test-bank-scope.txt",
            )
        )
        assert evidence is not None
        with authority.attributed_call(
            session, tool_name="finance_generate_accounting_period"
        ) as period_attribution:
            generated = AccountingPeriodService(
                session, current_date=date.max
            ).generate_accounting_period(
                GenerateAccountingPeriodRequest(
                    org_id=organization.id,
                    period_month=f"{next_month:%Y-%m}",
                    idempotency_key=f"test-bank-period-{organization.id}-{next_month:%Y-%m}",
                    confirmation_note=f"PostgreSQL 测试生成 {next_month:%Y-%m} 受控银行导入期间",
                    evidence_references=[evidence.id],
                )
            )
            assert generated.status == "posted", generated
        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = period_attribution.id


def prepare_authenticated_bank_account(
    session: Session,
    organization: Organization,
    *,
    booking_date: date = date(2026, 3, 5),
) -> AuthenticatedOwnerAuthority:
    """Prepare one real owner-controlled bank scope and accounting month.

    PostgreSQL payroll invariant tests use this before creating payroll facts.
    It deliberately follows the production scope and period workflows so a
    later statement row can only arrive through a formal import action.
    """

    existing = session.info.get(_TEST_BANK_AUTHORITY_SESSION_KEY)
    if isinstance(existing, AuthenticatedOwnerAuthority):
        if existing.context.org_id != organization.id:
            raise AssertionError("one test session cannot prepare two owner organizations")
        _ensure_test_accounting_period(session, organization, existing, booking_date)
        return existing

    evidence = Evidence(
        org_id=organization.id,
        sha256=uuid.uuid5(organization.id, "test-bank-scope-evidence").hex * 2,
        original_name="test-bank-scope.txt",
        media_type="text/plain",
        source="postgres-test-fixture",
        size_bytes=1,
        storage_path=f"tests/{organization.id}/bank-scope.txt",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()
    authority = authenticate_and_confirm_bank_scope(
        session,
        organization,
        evidence_id=evidence.id,
        accounts=[
            {
                "bank_account_code": "1002",
                "account_name": "银行存款",
                "start_date": booking_date.replace(day=1),
            }
        ],
        executor_name="postgres-bank-fixture",
    )
    with authority.attributed_call(
        session, tool_name="finance_generate_accounting_period"
    ) as attribution:
        generated = AccountingPeriodService(
            session, current_date=date.max
        ).generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=organization.id,
                period_month=f"{booking_date.year:04d}-{booking_date.month:02d}",
                idempotency_key=f"test-bank-period-{organization.id}-{booking_date:%Y-%m}",
                confirmation_note=(
                    f"PostgreSQL 测试生成 {booking_date.year:04d}-{booking_date.month:02d} "
                    "受控银行导入期间"
                ),
                evidence_references=[evidence.id],
            )
        )
        assert generated.status == "posted", generated
        assert generated.period_id is not None
    # Keep subsequent fixture-driven writes in this transaction attributed.
    # Production entry points create a fresh attribution per call; these tests
    # are about payroll invariants, while dedicated attribution tests cover the
    # per-call boundary itself.
    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution.id
    session.info[_TEST_BANK_AUTHORITY_SESSION_KEY] = authority
    return authority


def bind_authenticated_bank_account(
    session: Session, authority: AuthenticatedOwnerAuthority
) -> None:
    """Reuse an already-authenticated test authority in a new Session."""

    session.info[_TEST_BANK_AUTHORITY_SESSION_KEY] = authority


def import_test_bank_transaction(
    session: Session,
    organization: Organization,
    *,
    amount_fen: int,
    key: str,
    booking_date: date = date(2026, 3, 5),
) -> BankTransaction:
    """Import one test row through the production CSV preview/confirm path."""

    authority = session.info.get(_TEST_BANK_AUTHORITY_SESSION_KEY)
    if not isinstance(authority, AuthenticatedOwnerAuthority):
        raise AssertionError(
            "prepare_authenticated_bank_account must run before PostgreSQL bank import"
        )
    if authority.context.org_id != organization.id:
        raise AssertionError("bank fixture authority belongs to another organization")

    _ensure_test_accounting_period(session, organization, authority, booking_date)

    external_id = uuid.uuid5(organization.id, f"test-bank-row:{key}").hex
    file_name = f"{external_id}.csv"
    amount_text = format(Decimal(amount_fen) / Decimal(100), ".2f")
    with TemporaryDirectory(prefix="ai-accounting-bank-test-") as raw_dir:
        import_dir = Path(raw_dir)
        (import_dir / file_name).write_text(
            "date,amount,reference,memo\n"
            f"{booking_date.isoformat()},{amount_text},{external_id},{key}\n",
            encoding="utf-8",
        )
        service = BankStatementService(
            session,
            settings=Settings(finance_bank_import_dir=import_dir),
            current_date=date.max,
        )
        request = PreviewBankStatementFileImportRequest(
            org_id=organization.id,
            bank_account_code="1002",
            source_file_name=file_name,
            file_format="csv",
            column_mapping={
                "booking_date": "date",
                "amount": "amount",
                "external_id": "reference",
                "memo": "memo",
            },
        )
        preview = service.preview_bank_statement_import(request)
        assert preview.status == "calculated", preview.errors
        assert preview.calculation_hash is not None
        with authority.attributed_call(
            session, tool_name="finance_confirm_bank_statement_import"
        ) as attribution:
            imported = service.confirm_bank_statement_import(
                ConfirmBankStatementFileImportRequest.model_validate(
                    request.model_dump()
                    | {
                        "calculation_hash": preview.calculation_hash,
                        "idempotency_key": f"test-bank-import-{external_id}",
                    }
                )
            )
            assert imported.status == "posted", imported.errors
            assert len(imported.data["imported_transaction_ids"]) == 1
            transaction_id = uuid.UUID(imported.data["imported_transaction_ids"][0])
        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution.id
    transaction = session.get(BankTransaction, transaction_id)
    assert transaction is not None
    return transaction


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
        taxpayer_identification_number="91330106MA1234567T",
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
        if session.get_bind().dialect.name == "postgresql":
            # PostgreSQL's transaction timestamp is also the attribution row's
            # created_at.  Start authentication in a fresh transaction so that
            # it cannot predate the newly issued owner session authority.
            session.commit()
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
