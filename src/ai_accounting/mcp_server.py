from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from contextvars import ContextVar
from functools import wraps
from typing import Any

import mcp.server.fastmcp.server as fastmcp_server
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import selectinload

from .accounting_period_schemas import (
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    GetAccountingPeriodsRequest,
    PreviewAccountingPeriodCloseRequest,
)
from .bank_import import BankStatementInputError, import_bank_statement
from .bank_statement_schemas import (
    ConfirmBankReconciliationRequest,
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    ConfirmLateBankEvidenceRequest,
    PreviewBankReconciliationRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
    PreviewLateBankEvidenceRequest,
    QueryBankStatementStateRequest,
)
from .borrowing_schemas import (
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PayBorrowingInterestRequest,
    PreviewBorrowingInterestRequest,
    RepayBorrowingPrincipalRequest,
)
from .config import get_settings
from .credential_store import CredentialStore, WindowsCredentialStore
from .database import SessionLocal
from .evidence import register_evidence
from .execution_attribution import persist_execution_attribution
from .identity import ExecutorIdentity, ExecutorKind, IdentityError
from .identity_service import IdentityService
from .intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    ConfirmIntangibleAssetAmortizationRequest,
    PreviewIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from .labor_remuneration_schemas import (
    ConfirmLaborExternalDeclarationRequest,
    ConfirmLaborRemunerationBatchRequest,
    ConfirmUnifiedPayoutRunRequest,
    EndLaborServicePersonRequest,
    GetLaborRemunerationRequest,
    PayLaborWithholdingTaxRequest,
    PreviewLaborRemunerationBatchRequest,
    PreviewUnifiedPayoutRunRequest,
    RegisterLaborServicePersonRequest,
)
from .models import (
    Account,
    AuditLog,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    LaborRemunerationTaxPolicyVersion,
    OpenItem,
    Organization,
    OwnerAccount,
    PayrollBatch,
    PayrollEventLink,
    TaxRule,
    Voucher,
    event_evidence,
)
from .schemas import (
    DISABLED_EVENT_TYPES,
    EVENT_REQUIREMENTS,
    INTERNAL_EVENT_TYPES,
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationBatchRequest,
    ConfirmFixedAssetDepreciationRequest,
    ConfirmPayrollRequest,
    DisposeFixedAssetRequest,
    EventType,
    ImportBankStatementRequest,
    PreviewFixedAssetDepreciationBatchRequest,
    PreviewFixedAssetDepreciationRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterEvidenceRequest,
    RegisterPayrollOpeningStateRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from .service import FinanceService
from .service_lease import acquire_windows_service_lease
from .windows_backup import WindowsCurrentUserOnlyAclVerifier

logger = logging.getLogger(__name__)

SERVER_MCP_EXECUTOR = ExecutorIdentity(
    kind=ExecutorKind.AI_AGENT,
    executor_name="ai-accounting-core",
    executor_version="0.1.0",
)
_ACTIVE_TOOL_SESSION: ContextVar[Any | None] = ContextVar(
    "finance_mcp_active_tool_session", default=None
)
_MCP_CREDENTIAL_STORE: CredentialStore | None = None


class _BorrowedSessionContext(AbstractContextManager[Any]):
    def __init__(self, session: Any) -> None:
        self._session = session

    def __enter__(self) -> Any:
        return self._session

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None


class _ContextAwareSessionFactory:
    """Route a handler's legacy session block to the authenticated transaction."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        active = _ACTIVE_TOOL_SESSION.get()
        if active is not None:
            return _BorrowedSessionContext(active)
        return self._factory(*args, **kwargs)

    def begin(self) -> Any:
        active = _ACTIVE_TOOL_SESSION.get()
        if active is not None:
            return _BorrowedSessionContext(active)
        return self._factory.begin()


SessionLocal = _ContextAwareSessionFactory(SessionLocal)

# mcp 1.29 ships a generic Settings forward reference that pydantic-settings 2.15
# cannot resolve automatically. Rebuild it with the defining module's namespace
# before FastMCP instantiates Settings, avoiding a noisy warning and future failures.
fastmcp_server.Settings.model_rebuild(_types_namespace=vars(fastmcp_server))

mcp = FastMCP(
    "ai-accounting-core",
    instructions=(
        "这是确定性财务内核，不是自由分录接口。先调用 finance_get_event_schema；"
        "资料不完整时 finance_record_event 会返回 needs_information，必须向用户核实，"
        "禁止猜测业务性质。所有金额使用整数分，日期使用 YYYY-MM-DD。"
    ),
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
REVERSAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


def _set_mcp_credential_store_for_tests(store: CredentialStore | None) -> None:
    """Inject a session-store boundary; production startup never calls this."""

    global _MCP_CREDENTIAL_STORE
    _MCP_CREDENTIAL_STORE = store


def _clear_mcp_credential_store() -> None:
    global _MCP_CREDENTIAL_STORE
    _MCP_CREDENTIAL_STORE = None


def _initialize_mcp_credential_store(*, environment: str) -> None:
    """Open DEC-031 B's stateless store without caching its current token."""

    global _MCP_CREDENTIAL_STORE
    try:
        _MCP_CREDENTIAL_STORE = WindowsCredentialStore()
    except IdentityError:
        if environment == "production":
            raise
        _MCP_CREDENTIAL_STORE = None


def _load_current_session_token() -> SecretStr | None:
    """Read one call-scoped token so local login changes take effect immediately."""

    if _MCP_CREDENTIAL_STORE is None:
        raise IdentityError("IDENTITY_CREDENTIAL_STORE_UNAVAILABLE")
    return _MCP_CREDENTIAL_STORE.load_session_token()


def _tool_org_id(
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> uuid.UUID:
    bound = inspect.signature(function).bind_partial(*args, **kwargs)
    request = bound.arguments.get("request")
    raw = getattr(request, "org_id", None) if request is not None else bound.arguments.get("org_id")
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str):
        raise ValueError("organization id is required")
    return uuid.UUID(raw)


def _rejected_identity(code: str) -> dict[str, Any]:
    return {"status": "rejected", "errors": [code]}


def _begin_mcp_transaction() -> Any:
    begin = getattr(SessionLocal, "begin", None)
    if begin is not None:
        return begin()
    # Narrow compatibility path for existing in-process handler fakes.  The
    # production factory always exposes begin().
    return SessionLocal()


def _secure_registered_data_tools() -> None:
    """Wrap every enterprise-data tool without changing its public schema."""

    for tool in mcp._tool_manager.list_tools():
        if tool.name == "finance_get_event_schema":
            continue
        original = tool.fn
        is_write = not bool(tool.annotations and tool.annotations.readOnlyHint)

        @wraps(original)
        def authenticated(
            *args: Any,
            _original: Callable[..., Any] = original,
            _tool_name: str = tool.name,
            _is_write: bool = is_write,
            **kwargs: Any,
        ) -> dict[str, Any]:
            try:
                requested_org_id = _tool_org_id(_original, args, kwargs)
            except ValueError:
                return _rejected_identity("INVALID_REQUEST")
            environment = get_settings().finance_environment
            session_token: SecretStr | None = None
            if environment != "development":
                try:
                    session_token = _load_current_session_token()
                except IdentityError as exc:
                    return _rejected_identity(exc.code)
                if session_token is None:
                    return _rejected_identity("AUTHENTICATION_REQUIRED")
            with _begin_mcp_transaction() as session:
                owner_exists = session.scalar(select(OwnerAccount.id).limit(1)) is not None
                if not owner_exists and environment == "development":
                    marker = _ACTIVE_TOOL_SESSION.set(session)
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        _ACTIVE_TOOL_SESSION.reset(marker)
                if environment == "development":
                    try:
                        session_token = _load_current_session_token()
                    except IdentityError as exc:
                        return _rejected_identity(exc.code)
                if session_token is None:
                    return _rejected_identity("AUTHENTICATION_REQUIRED")
                correlation_id = uuid.uuid4()
                try:
                    context = IdentityService(session).authorize_execution(
                        session_token=session_token.get_secret_value(),
                        executor=SERVER_MCP_EXECUTOR,
                        request_correlation_id=correlation_id,
                        expected_org_id=requested_org_id,
                    )
                except IdentityError as exc:
                    if exc.code == "ORGANIZATION_CONTEXT_MISMATCH":
                        return _rejected_identity("ORGANIZATION_CONTEXT_MISMATCH")
                    return _rejected_identity("AUTHENTICATION_REQUIRED")
                marker = _ACTIVE_TOOL_SESSION.set(session)
                try:
                    if _is_write:
                        with persist_execution_attribution(
                            session,
                            context=context,
                            tool_name=_tool_name,
                        ):
                            return _original(*args, **kwargs)
                    return _original(*args, **kwargs)
                finally:
                    _ACTIVE_TOOL_SESSION.reset(marker)

        object.__setattr__(tool, "fn", authenticated)


def _database_error_code(exc: SQLAlchemyError) -> str:
    """Classify database exceptions without inspecting their SQL text or parameters."""
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    sqlite_errorcode = getattr(original, "sqlite_errorcode", None)

    if isinstance(exc, IntegrityError):
        if sqlstate == "23505" or sqlite_errorcode in {1555, 2067}:
            return "UNIQUE_CONFLICT"
        return "CONSTRAINT_VIOLATION"
    if isinstance(exc, OperationalError):
        if sqlstate in {"40001", "40P01", "55P03"} or sqlite_errorcode in {5, 6}:
            return "CONCURRENCY_CONFLICT"
        return "DATABASE_UNAVAILABLE"
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return "DATABASE_UNAVAILABLE"
    return "DATABASE_OPERATION_FAILED"


def _invalid(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        errors = [
            {
                "code": "VALIDATION_ERROR",
                "path": ".".join(str(part) for part in item["loc"]),
                "message": "invalid value",
            }
            for item in exc.errors()
        ]
        return {"status": "rejected", "errors": errors}
    if isinstance(exc, SQLAlchemyError):
        error_code = _database_error_code(exc)
        logger.warning("MCP database request failed with %s", error_code)
        return {"status": "rejected", "errors": [error_code]}
    if isinstance(exc, OSError):
        return {"status": "rejected", "errors": ["INPUT_UNAVAILABLE"]}
    if isinstance(exc, ValueError):
        return {"status": "rejected", "errors": ["INVALID_REQUEST"]}
    logger.warning("MCP request failed with %s", type(exc).__name__)
    errors = ["INTERNAL_ERROR"]
    return {"status": "rejected", "errors": errors}


def _database_error_boundary(
    function: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Translate database failures from read-only handlers before FastMCP serializes them."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return function(*args, **kwargs)
        except SQLAlchemyError as exc:
            return _invalid(exc)

    return wrapped


def _make_all_tool_inputs_strict() -> None:
    """Make FastMCP's generated argument models reject undeclared top-level fields.

    FastMCP 1.x creates dynamic argument models with Pydantic's default
    ``extra='ignore'`` configuration.  Apply one registry-wide policy so newly
    registered tools cannot accidentally publish or accept an open envelope.
    """
    for tool in mcp._tool_manager.list_tools():
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
        argument_model.model_rebuild(force=True)
        tool.parameters = argument_model.model_json_schema(by_alias=True)


def _sanitize_tool_errors(*tool_names: str) -> None:
    """Expose only stable error codes at FastMCP's outer tool boundary.

    FastMCP wraps both argument-model failures and handler exceptions in
    ``ToolError`` before serializing them.  Its default formatting includes the
    original exception message, which can include caller input, SQL or a
    connection string.  Preserve strict validation paths, while translating all
    other categories into stable public codes.
    """
    for tool_name in tool_names:
        tool = mcp._tool_manager.get_tool(tool_name)
        if tool is None:  # pragma: no cover - catches registration mistakes at import time.
            raise RuntimeError(f"MCP tool was not registered: {tool_name}")
        original_run = tool.run

        async def sanitized_run(
            arguments: dict[str, Any],
            context: Any = None,
            convert_result: bool = False,
            *,
            _original_run: Callable[..., Any] = original_run,
        ) -> Any:
            try:
                return await _original_run(
                    arguments,
                    context=context,
                    convert_result=convert_result,
                )
            except Exception as exc:
                # ``Tool.run`` wraps all ordinary failures in ToolError with
                # the actual exception as its cause.  If a FastMCP version
                # raises directly, classify that exception as well.
                source = exc.__cause__ if isinstance(exc, ToolError) and exc.__cause__ else exc
                if isinstance(source, ValidationError):
                    paths = sorted(
                        ".".join(str(part) for part in error["loc"]) for error in source.errors()
                    )
                    raise ToolError(
                        "VALIDATION_ERROR: " + ", ".join(paths or ["request"])
                    ) from None
                if isinstance(source, SQLAlchemyError):
                    raise ToolError(_database_error_code(source)) from None
                if isinstance(source, OSError):
                    raise ToolError("INPUT_UNAVAILABLE") from None
                if isinstance(source, ValueError):
                    raise ToolError("INVALID_REQUEST") from None
                logger.warning("MCP tool failed with %s", type(source).__name__)
                raise ToolError("INTERNAL_ERROR") from None

        # Tool is a Pydantic model and deliberately rejects ordinary runtime
        # attributes.  A bound per-tool runner is nevertheless the narrowest
        # hook before FastMCP serializes its otherwise-leaky ToolError.
        object.__setattr__(tool, "run", sanitized_run)


def _fixed_asset_service(session: Any) -> Any:
    """Load the specialized asset workflow only when an asset tool is invoked."""

    from .fixed_asset_service import FixedAssetService

    return FixedAssetService(session)


def _intangible_asset_service(session: Any) -> Any:
    """Load the specialized intangible-asset workflow only when invoked."""

    from .intangible_asset_service import IntangibleAssetService

    return IntangibleAssetService(session)


def _borrowing_service(session: Any) -> Any:
    """Load the specialized borrowing workflow only when invoked."""

    from .borrowing_service import BorrowingService

    return BorrowingService(session)


def _accounting_period_service(session: Any) -> Any:
    """Load period control only when one of its typed tools is invoked."""

    from .accounting_period_service import AccountingPeriodService

    return AccountingPeriodService(session)


def _labor_remuneration_service(session: Any) -> Any:
    """Load the non-employee labor workflow only when invoked."""

    from .labor_remuneration_service import LaborRemunerationService

    return LaborRemunerationService(session)


def _bank_statement_service(session: Any) -> Any:
    """Load formal CSV import/reconciliation workflows only when invoked."""

    from .bank_statement_service import BankStatementService

    return BankStatementService(session)


@mcp.tool(annotations=READ_ONLY)
@_database_error_boundary
def finance_get_profile(org_id: str) -> dict[str, Any]:
    """读取企业、纳税配置、系统科目映射和有效税务规则。"""
    try:
        parsed = uuid.UUID(org_id)
    except ValueError as exc:
        return _invalid(exc)
    with SessionLocal() as session:
        organization = session.get(Organization, parsed)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        accounts = session.scalars(
            select(Account).where(Account.org_id == parsed, Account.active.is_(True))
        ).all()
        rules = session.scalars(
            select(TaxRule).where(TaxRule.jurisdiction == organization.jurisdiction)
        ).all()
        labor_policies = session.scalars(
            select(LaborRemunerationTaxPolicyVersion).order_by(
                LaborRemunerationTaxPolicyVersion.effective_from
            )
        ).all()
        return {
            "status": "ok",
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "taxpayer_type": organization.taxpayer_type,
                "filing_cycle": organization.filing_cycle,
                "jurisdiction": organization.jurisdiction,
                "urban_maintenance_rate": str(organization.urban_maintenance_rate),
                "accounting_standard": organization.accounting_standard,
                "accounting_period_control_enabled": (
                    organization.accounting_period_control_enabled
                ),
                "accounting_period_control_start_date": (
                    organization.accounting_period_control_start_date.isoformat()
                    if organization.accounting_period_control_start_date
                    else None
                ),
            },
            "account_roles": {
                account.system_role: {"code": account.code, "name": account.name}
                for account in accounts
                if account.system_role
            },
            "tax_rules": [
                {
                    "code": rule.code,
                    "version": rule.version,
                    "effective_from": rule.effective_from.isoformat(),
                    "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
                    "source_url": rule.source_url,
                    "parameters": rule.parameters,
                }
                for rule in rules
            ],
            "labor_remuneration_tax_policies": [
                {
                    "code": policy.code,
                    "version": policy.version,
                    "effective_from": policy.effective_from.isoformat(),
                    "effective_to": (
                        policy.effective_to.isoformat() if policy.effective_to else None
                    ),
                    "primary_source_url": policy.primary_source_url,
                    "invoice_withholding_source_url": policy.invoice_withholding_source_url,
                    "legal_filing_source_url": policy.legal_filing_source_url,
                    "parameters": policy.parameters,
                }
                for policy in labor_policies
            ],
        }


@mcp.tool(annotations=READ_ONLY)
def finance_get_event_schema(event_type: str | None = None) -> dict[str, Any]:
    """返回可提交业务事件、停用模块及 finance_record_event 的 JSON Schema。"""
    enabled = [
        item.value
        for item in EventType
        if item not in DISABLED_EVENT_TYPES and item not in INTERNAL_EVENT_TYPES
    ]
    # ``payroll`` remains rejected by the generic event writer.  It is not a
    # disabled product module, though: payroll has a typed, dedicated workflow
    # below.  Report the legacy sentinel as internal so an MCP client does not
    # conclude incorrectly that payroll is unavailable.
    specialized_sentinels = {
        EventType.PAYROLL,
        EventType.INTANGIBLE_ASSET,
        EventType.LOAN_INTEREST,
    }
    disabled = [item.value for item in DISABLED_EVENT_TYPES if item not in specialized_sentinels]
    internal = [item.value for item in INTERNAL_EVENT_TYPES] + [
        item.value for item in specialized_sentinels
    ]
    if event_type and event_type not in {item.value for item in EventType}:
        return {"status": "rejected", "errors": ["UNKNOWN_EVENT_TYPE"]}
    return {
        "status": "ok",
        "requested_event_type": event_type,
        "enabled_event_types": enabled,
        "disabled_event_types": disabled,
        "internal_event_types": internal,
        "module_capabilities": {
            "payroll": {
                "status": "enabled",
                "entry_tools": [
                    "finance_register_employee",
                    "finance_register_employee_profile_version",
                    "finance_register_payroll_policy_version",
                    "finance_register_payroll_opening_state",
                    "finance_preview_payroll",
                    "finance_confirm_payroll",
                    "finance_get_payroll_batch",
                ],
                "generic_event_writer": "not_available",
                "accrual_entry": "finance_confirm_payroll",
            },
            "personal_labor_remuneration": {
                "status": "enabled",
                "entry_tools": [
                    "finance_register_labor_service_person",
                    "finance_end_labor_service_person",
                    "finance_preview_labor_remuneration_batch",
                    "finance_confirm_labor_remuneration_batch",
                    "finance_get_labor_remuneration",
                    "finance_preview_unified_payout_run",
                    "finance_confirm_unified_payout_run",
                    "finance_pay_labor_withholding_tax",
                    "finance_confirm_labor_external_declaration",
                ],
                "generic_event_writer": "not_available",
                "accrual_entry": "finance_confirm_labor_remuneration_batch",
                "mixed_salary_labor_bank_match": "finance_confirm_unified_payout_run",
                "labor_settlement_modes": [
                    "net_after_withholding",
                    "gross_paid_without_withholding",
                ],
                "gross_paid_without_withholding_requires_exception_evidence": True,
            },
            "fixed_asset": {
                "status": "enabled",
                "entry_tools": [
                    "finance_acquire_fixed_asset",
                    "finance_activate_fixed_asset",
                    "finance_preview_fixed_asset_depreciation_batch",
                    "finance_confirm_fixed_asset_depreciation_batch",
                    "finance_dispose_fixed_asset",
                    "finance_get_fixed_asset",
                ],
                "generic_event_writer": "not_available",
                "accrual_entry": "finance_confirm_fixed_asset_depreciation_batch",
            },
            "intangible_asset": {
                "status": "enabled",
                "entry_tools": [
                    "finance_acquire_intangible_asset",
                    "finance_preview_intangible_asset_amortization",
                    "finance_confirm_intangible_asset_amortization",
                    "finance_retire_intangible_asset",
                    "finance_get_intangible_asset",
                ],
                "generic_event_writer": "not_available",
                "accrual_entry": "finance_confirm_intangible_asset_amortization",
            },
            "borrowing": {
                "status": "enabled",
                "entry_tools": [
                    "finance_draw_borrowing",
                    "finance_preview_borrowing_interest",
                    "finance_confirm_borrowing_interest",
                    "finance_pay_borrowing_interest",
                    "finance_repay_borrowing_principal",
                    "finance_get_borrowing",
                ],
                "generic_event_writer": "not_available",
                "accrual_entry": "finance_confirm_borrowing_interest",
            },
            "accounting_period": {
                "status": "enabled",
                "entry_tools": [
                    "finance_generate_accounting_period",
                    "finance_preview_accounting_period_close",
                    "finance_confirm_accounting_period_close",
                    "finance_get_accounting_periods",
                ],
                "generic_event_writer": "controlled_by_period_status",
                "reopen_entry": "not_available",
            },
        },
        # Return the schema actually advertised by FastMCP, including its strict
        # tool envelope, rather than maintaining a second model-only contract.
        "record_event_schema": mcp._tool_manager.get_tool("finance_record_event").parameters,
        "reverse_event_schema": mcp._tool_manager.get_tool("finance_reverse_event").parameters,
        "event_requirements": (
            EVENT_REQUIREMENTS.get(event_type) if event_type else EVENT_REQUIREMENTS
        ),
        "rules": {
            "amount_unit": "fen",
            "currency": "CNY",
            "no_freeform_entries": True,
            "ambiguous_facts": "return needs_information",
        },
    }


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_evidence(request: RegisterEvidenceRequest) -> dict[str, Any]:
    """把本地文件或 base64 内容登记到 SHA-256 内容寻址证据库。"""
    try:
        with SessionLocal.begin() as session:
            evidence = register_evidence(session, request)
            return {
                "status": "registered",
                "evidence_id": str(evidence.id),
                "sha256": evidence.sha256,
                "size_bytes": evidence.size_bytes,
            }
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_import_bank_statement(request: ImportBankStatementRequest) -> dict[str, Any]:
    """按 Agent 提供的列映射导入 CSV/XLSX 银行流水并做稳定去重。"""
    try:
        with SessionLocal.begin() as session:
            return {"status": "ok", **import_bank_statement(session, request)}
    except BankStatementInputError as exc:
        error: dict[str, str] = {"code": exc.code}
        if exc.field is not None:
            error["field"] = exc.field
        return {"status": "rejected", "errors": [error]}
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_bank_statement_import(
    request: PreviewBankStatementFileImportRequest,
) -> dict[str, Any]:
    """只读预检受控导入目录内的一份 CSV 银行流水，不写入正式事实。"""
    try:
        with SessionLocal() as session:
            return (
                _bank_statement_service(session)
                .preview_bank_statement_import(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_bank_statement_import(
    request: ConfirmBankStatementFileImportRequest,
) -> dict[str, Any]:
    """锁后重算预览哈希，幂等确认 CSV 流水及迟到外部证据。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _bank_statement_service(session)
                .confirm_bank_statement_import(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_bank_reconciliation_scope(
    request: PreviewBankReconciliationScopeRequest,
) -> dict[str, Any]:
    """只读预检完整银行账户范围；支持明确零账户及受控新增银行科目。"""
    try:
        with SessionLocal() as session:
            return (
                _bank_statement_service(session)
                .preview_bank_reconciliation_scope(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_bank_reconciliation_scope(
    request: ConfirmBankReconciliationScopeRequest,
) -> dict[str, Any]:
    """锁后重算并幂等确认完整银行账户范围及其证据。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _bank_statement_service(session)
                .confirm_bank_reconciliation_scope(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_late_bank_evidence(
    request: PreviewLateBankEvidenceRequest,
) -> dict[str, Any]:
    """只读验证迟到银行流水的证据补充或既有类型化补记结果。"""
    try:
        with SessionLocal() as session:
            return (
                _bank_statement_service(session)
                .preview_late_bank_evidence(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_late_bank_evidence(
    request: ConfirmLateBankEvidenceRequest,
) -> dict[str, Any]:
    """幂等追加迟到银行事实处理动作，不改写原月结。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _bank_statement_service(session)
                .confirm_late_bank_evidence(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_bank_reconciliation(
    request: PreviewBankReconciliationRequest,
) -> dict[str, Any]:
    """只读计算完整月份、单一银行账户的正式对账快照。"""
    try:
        with SessionLocal() as session:
            return (
                _bank_statement_service(session)
                .preview_bank_reconciliation(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_bank_reconciliation(
    request: ConfirmBankReconciliationRequest,
) -> dict[str, Any]:
    """锁后重算并幂等追加单账户月度对账版本。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _bank_statement_service(session)
                .confirm_bank_reconciliation(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_query_bank_statement_state(
    request: QueryBankStatementStateRequest,
) -> dict[str, Any]:
    """按固定筛选查询银行范围、流水、迟到处理和对账的当前投影。"""
    try:
        with SessionLocal() as session:
            return _bank_statement_service(session).get_bank_statement_activity(request)
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_employee(request: RegisterEmployeeRequest) -> dict[str, Any]:
    """登记非敏感员工主数据；不接受证件号、银行卡或自由会计科目。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).register_employee(request)
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_employee_profile_version(
    request: RegisterEmployeePayrollProfileVersionRequest,
) -> dict[str, Any]:
    """登记员工有效期工资档案版本及明确的缴费基数。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).register_employee_payroll_profile_version(request)
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_payroll_policy_version(
    request: RegisterPayrollPolicyVersionRequest,
) -> dict[str, Any]:
    """登记有官方来源、有效期和参数的工资政策版本。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).register_payroll_policy_version(request)
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_payroll_opening_state(
    request: RegisterPayrollOpeningStateRequest,
) -> dict[str, Any]:
    """登记年中启用时已知的累计个税期初状态，缺失历史不会被当作零。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).register_payroll_opening_state(request)
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_preview_payroll(request: PreviewPayrollRequest) -> dict[str, Any]:
    """按负责人确认的报税工资试算并保存不可变草稿；不计算工资组成。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).preview_payroll(request).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_payroll(request: ConfirmPayrollRequest) -> dict[str, Any]:
    """确认草稿哈希并用固定内部模板入账，绝不接受外部借贷分录。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).confirm_payroll(request).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_payroll_batch(org_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    """读取工资草稿/正式批次、计算哈希、明细、规则轨迹与入账结果。"""
    try:
        with SessionLocal() as session:
            return FinanceService(session).get_payroll_batch(org_id, batch_id)
    except (ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_labor_service_person(
    request: RegisterLaborServicePersonRequest,
) -> dict[str, Any]:
    """登记与 Employee 严格分离的非员工个人劳务人员及有效期证据。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session)
                .register_person(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_end_labor_service_person(
    request: EndLaborServicePersonRequest,
) -> dict[str, Any]:
    """以证据支持的受控动作结束劳务关系，保留未来转员工的身份链。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session).end_person(request).model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_preview_labor_remuneration_batch(
    request: PreviewLaborRemunerationBatchRequest,
) -> dict[str, Any]:
    """按有效政策试算普通居民个人劳务报酬，冻结分项、税额和哈希。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session).preview_batch(request).model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_labor_remuneration_batch(
    request: ConfirmLaborRemunerationBatchRequest,
) -> dict[str, Any]:
    """确认劳务试算哈希，按固定费用/应付模板原子计提。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session).confirm_batch(request).model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_labor_remuneration(request: GetLaborRemunerationRequest) -> dict[str, Any]:
    """读取劳务人员、劳务批次或统一发放批次的不可变事实链。"""
    try:
        with SessionLocal() as session:
            return _labor_remuneration_service(session).get(request).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_preview_unified_payout_run(
    request: PreviewUnifiedPayoutRunRequest,
) -> dict[str, Any]:
    """按逐劳务项显式结算模式试算统一发放，并精确勾稽已导入银行扣款。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session).preview_payout(request).model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_unified_payout_run(
    request: ConfirmUnifiedPayoutRunRequest,
) -> dict[str, Any]:
    """锁后复算混合发放哈希，整批原子过账且银行流水只匹配一次。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session).confirm_payout(request).model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_pay_labor_withholding_tax(
    request: PayLaborWithholdingTaxRequest,
) -> dict[str, Any]:
    """仅核销劳务报酬扣缴来源的个税应付，不冒充工资个税来源。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session)
                .pay_withholding_tax(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_labor_external_declaration(
    request: ConfirmLaborExternalDeclarationRequest,
) -> dict[str, Any]:
    """追加确认外部劳务个税申报证据；不改写原劳务批次。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _labor_remuneration_service(session)
                .confirm_external_declaration(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_acquire_fixed_asset(request: AcquireFixedAssetRequest) -> dict[str, Any]:
    """登记外购资产；已交付可用时可在同一次请求中直接启用。"""
    try:
        with SessionLocal.begin() as session:
            result = _fixed_asset_service(session).acquire_fixed_asset(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_activate_fixed_asset(request: ActivateFixedAssetRequest) -> dict[str, Any]:
    """启用待启用资产并冻结折旧寿命、残值与受益区域。"""
    try:
        with SessionLocal.begin() as session:
            result = _fixed_asset_service(session).activate_fixed_asset(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_fixed_asset_depreciation(
    request: PreviewFixedAssetDepreciationRequest,
) -> dict[str, Any]:
    """读取不可变资产事实，试算单资产单月折旧而不写入草稿。"""
    try:
        with SessionLocal() as session:
            result = _fixed_asset_service(session).preview_fixed_asset_depreciation(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_fixed_asset_depreciation(
    request: ConfirmFixedAssetDepreciationRequest,
) -> dict[str, Any]:
    """以试算哈希确认月折旧；内核复算后才按固定模板入账。"""
    try:
        with SessionLocal.begin() as session:
            result = _fixed_asset_service(session).confirm_fixed_asset_depreciation(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_fixed_asset_depreciation_batch(
    request: PreviewFixedAssetDepreciationBatchRequest,
) -> dict[str, Any]:
    """一次试算当月全部到期资产的逐卡片折旧与一张汇总凭证。"""
    try:
        with SessionLocal() as session:
            result = _fixed_asset_service(session).preview_fixed_asset_depreciation_batch(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_fixed_asset_depreciation_batch(
    request: ConfirmFixedAssetDepreciationBatchRequest,
) -> dict[str, Any]:
    """负责人一次确认整月折旧，保留逐资产明细并只生成一张汇总凭证。"""
    try:
        with SessionLocal.begin() as session:
            result = _fixed_asset_service(session).confirm_fixed_asset_depreciation_batch(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_dispose_fixed_asset(request: DisposeFixedAssetRequest) -> dict[str, Any]:
    """出售或零收入报废已启用资产，并由内核计算清理损益与增值税。"""
    try:
        with SessionLocal.begin() as session:
            result = _fixed_asset_service(session).dispose_fixed_asset(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_fixed_asset(org_id: uuid.UUID, asset_id: uuid.UUID) -> dict[str, Any]:
    """读取资产卡片、冻结规则、折旧、处置、凭证与冲正链。"""
    try:
        with SessionLocal() as session:
            result = _fixed_asset_service(session).get_fixed_asset(org_id, asset_id)
            return result.model_dump(mode="json")
    except (ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_acquire_intangible_asset(
    request: AcquireIntangibleAssetRequest,
) -> dict[str, Any]:
    """登记已可供使用的外购无形资产，并按固定模板入账。"""
    try:
        with SessionLocal.begin() as session:
            result = _intangible_asset_service(session).acquire_intangible_asset(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_intangible_asset_amortization(
    request: PreviewIntangibleAssetAmortizationRequest,
) -> dict[str, Any]:
    """只读试算无形资产下一个自然月摊销及确认哈希。"""
    try:
        with SessionLocal() as session:
            result = _intangible_asset_service(session).preview_intangible_asset_amortization(
                request
            )
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_intangible_asset_amortization(
    request: ConfirmIntangibleAssetAmortizationRequest,
) -> dict[str, Any]:
    """锁内复算并以预览哈希确认一个自然月摊销。"""
    try:
        with SessionLocal.begin() as session:
            result = _intangible_asset_service(session).confirm_intangible_asset_amortization(
                request
            )
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_retire_intangible_asset(
    request: RetireIntangibleAssetRequest,
) -> dict[str, Any]:
    """在自然月末按零收入、零赔偿边界报废单项无形资产。"""
    try:
        with SessionLocal.begin() as session:
            result = _intangible_asset_service(session).retire_intangible_asset(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_intangible_asset(org_id: uuid.UUID, asset_id: uuid.UUID) -> dict[str, Any]:
    """读取无形资产取得、摊销、报废及冲正历史。"""
    try:
        with SessionLocal() as session:
            result = _intangible_asset_service(session).get_intangible_asset(org_id, asset_id)
            return result.model_dump(mode="json")
    except (ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_draw_borrowing(request: DrawBorrowingRequest) -> dict[str, Any]:
    """登记持牌金融机构人民币固定利率借款的一次全额放款。"""
    try:
        with SessionLocal.begin() as session:
            result = _borrowing_service(session).draw_borrowing(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_borrowing_interest(
    request: PreviewBorrowingInterestRequest,
) -> dict[str, Any]:
    """只读试算下一个合同应付息期间的简单利息。"""
    try:
        with SessionLocal() as session:
            result = _borrowing_service(session).preview_borrowing_interest(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_borrowing_interest(
    request: ConfirmBorrowingInterestRequest,
) -> dict[str, Any]:
    """锁内复算并以哈希确认合同应付息日的利息计提。"""
    try:
        with SessionLocal.begin() as session:
            result = _borrowing_service(session).confirm_borrowing_interest(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_pay_borrowing_interest(
    request: PayBorrowingInterestRequest,
) -> dict[str, Any]:
    """按唯一计息事件和精确银行流水支付全部应付利息。"""
    try:
        with SessionLocal.begin() as session:
            result = _borrowing_service(session).pay_borrowing_interest(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_repay_borrowing_principal(
    request: RepayBorrowingPrincipalRequest,
) -> dict[str, Any]:
    """在合同到期日、全部利息已支付后一次归还全部本金。"""
    try:
        with SessionLocal.begin() as session:
            result = _borrowing_service(session).repay_borrowing_principal(request)
            return result.model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_borrowing(org_id: uuid.UUID, borrowing_id: uuid.UUID) -> dict[str, Any]:
    """读取借款合同、计息、付息、还本和冲正历史。"""
    try:
        with SessionLocal() as session:
            result = _borrowing_service(session).get_borrowing(org_id, borrowing_id)
            return result.model_dump(mode="json")
    except (ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
@_database_error_boundary
def finance_query_context(
    org_id: str,
    counterparty_id: str | None = None,
    include_recent_events: bool = True,
    include_unmatched_bank: bool = True,
) -> dict[str, Any]:
    """查询开放往来、近期事件和未匹配银行流水，供 Agent 判断业务性质。"""
    try:
        parsed_org = uuid.UUID(org_id)
        parsed_counterparty = uuid.UUID(counterparty_id) if counterparty_id else None
    except ValueError as exc:
        return _invalid(exc)
    with SessionLocal() as session:
        if session.get(Organization, parsed_org) is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        item_query = select(OpenItem).where(
            OpenItem.org_id == parsed_org, OpenItem.status == "open"
        )
        if parsed_counterparty:
            item_query = item_query.where(OpenItem.counterparty_id == parsed_counterparty)
        items = session.scalars(item_query.order_by(OpenItem.due_date, OpenItem.id)).all()
        result: dict[str, Any] = {
            "status": "ok",
            "open_items": [
                {
                    "id": str(item.id),
                    "counterparty_id": str(item.counterparty_id),
                    "type": item.item_type,
                    "original_amount_fen": item.original_amount_fen,
                    "settled_amount_fen": item.settled_amount_fen,
                    "open_amount_fen": item.original_amount_fen - item.settled_amount_fen,
                    "due_date": item.due_date.isoformat() if item.due_date else None,
                    "source_event_id": str(item.source_event_id),
                    "payable_category": item.payable_category,
                    "payable_agency_code": item.payable_agency_code,
                    "insurance_kind": item.insurance_kind,
                }
                for item in items
            ],
        }
        if include_recent_events:
            events = session.scalars(
                select(BusinessEvent)
                .where(BusinessEvent.org_id == parsed_org)
                .order_by(BusinessEvent.created_at.desc())
                .limit(50)
            ).all()
            result["recent_events"] = [
                {
                    "id": str(event.id),
                    "event_type": event.event_type,
                    "status": event.status,
                    "posting_date": event.posting_date.isoformat(),
                    "description": event.description,
                }
                for event in events
            ]
        if include_unmatched_bank:
            bank = session.scalars(
                select(BankTransaction)
                .where(
                    BankTransaction.org_id == parsed_org,
                    BankTransaction.matched_event_id.is_(None),
                )
                .order_by(BankTransaction.booking_date.desc())
                .limit(100)
            ).all()
            result["unmatched_bank_transactions"] = [
                {
                    "id": str(row.id),
                    "fingerprint": row.fingerprint,
                    "booking_date": row.booking_date.isoformat(),
                    "amount_fen": row.amount_fen,
                    "counterparty_name": row.counterparty_name,
                    "memo": row.memo,
                }
                for row in bank
            ]
        matches = session.scalars(
            select(BankTransactionMatch)
            .where(BankTransactionMatch.org_id == parsed_org)
            .order_by(BankTransactionMatch.created_at, BankTransactionMatch.id)
        ).all()
        result["bank_transaction_match_history"] = [
            {
                "match_id": str(match.id),
                "bank_transaction_id": str(match.bank_transaction_id),
                "event_id": str(match.event_id),
                "current": match.invalidated_by_event_id is None,
                "invalidated_by_event_id": (
                    str(match.invalidated_by_event_id)
                    if match.invalidated_by_event_id is not None
                    else None
                ),
                "invalidated_at": (
                    match.invalidated_at.isoformat() if match.invalidated_at else None
                ),
            }
            for match in matches
        ]
        return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_generate_accounting_period(
    request: GenerateAccountingPeriodRequest,
) -> dict[str, Any]:
    """显式生成一个自然月会计期间；首次月份由调用方确认，后续必须逐月连续。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _accounting_period_service(session)
                .generate_accounting_period(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_preview_accounting_period_close(
    request: PreviewAccountingPeriodCloseRequest,
) -> dict[str, Any]:
    """只读复核自然月；另返回AI必须逐项提示负责人的月末建议清单和关账哈希。"""
    try:
        with SessionLocal() as session:
            return (
                _accounting_period_service(session)
                .preview_accounting_period_close(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_accounting_period_close(
    request: ConfirmAccountingPeriodCloseRequest,
) -> dict[str, Any]:
    """用预览哈希、完整复核声明和证据幂等确认关账；不提供重开入口。"""
    try:
        with SessionLocal.begin() as session:
            return (
                _accounting_period_service(session)
                .confirm_accounting_period_close(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_accounting_periods(
    request: GetAccountingPeriodsRequest,
) -> dict[str, Any]:
    """读取企业已显式生成的自然月期间及其开放或关闭状态。"""
    try:
        with SessionLocal() as session:
            return (
                _accounting_period_service(session)
                .get_accounting_periods(request)
                .model_dump(mode="json")
            )
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_record_event(request: RecordEventRequest) -> dict[str, Any]:
    """提交结构化业务事实；只在资料完整且规则唯一时原子入账。"""
    try:
        if request.event_type is EventType.PAYROLL:
            return {
                "status": "rejected",
                "errors": ["PAYROLL_REQUIRES_SPECIALIZED_WORKFLOW"],
            }
        if request.event_type in {
            EventType.FIXED_ASSET,
            EventType.FIXED_ASSET_ACQUISITION,
            EventType.FIXED_ASSET_ACTIVATION,
            EventType.FIXED_ASSET_DEPRECIATION,
            EventType.FIXED_ASSET_DISPOSAL,
        }:
            return {
                "status": "rejected",
                "errors": ["FIXED_ASSET_REQUIRES_SPECIALIZED_WORKFLOW"],
            }
        if request.event_type in {
            EventType.INTANGIBLE_ASSET,
            EventType.INTANGIBLE_ASSET_ACQUISITION,
            EventType.INTANGIBLE_ASSET_AMORTIZATION,
            EventType.INTANGIBLE_ASSET_RETIREMENT,
        }:
            return {
                "status": "rejected",
                "errors": ["INTANGIBLE_ASSET_REQUIRES_SPECIALIZED_WORKFLOW"],
            }
        if request.event_type in {
            EventType.LOAN_INTEREST,
            EventType.BORROWING_DRAWDOWN,
            EventType.BORROWING_INTEREST_ACCRUAL,
            EventType.BORROWING_INTEREST_PAYMENT,
            EventType.BORROWING_PRINCIPAL_REPAYMENT,
        }:
            return {
                "status": "rejected",
                "errors": ["BORROWING_REQUIRES_SPECIALIZED_WORKFLOW"],
            }
        with SessionLocal.begin() as session:
            return FinanceService(session).record_event(request).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_calculate_tax_period(request: TaxPeriodPreviewRequest) -> dict[str, Any]:
    """只读试算一个完整自然申报期，并返回规范来源与计算哈希。"""
    try:
        with SessionLocal() as session:
            return FinanceService(session).preview_tax_period(request)
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_confirm_tax_period(request: TaxPeriodConfirmRequest) -> dict[str, Any]:
    """用预览哈希和幂等键确认税期；预览陈旧时拒绝写入。"""
    try:
        with SessionLocal.begin() as session:
            return FinanceService(session).confirm_tax_period(request).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=REVERSAL_WRITE)
def finance_reverse_event(request: ReverseEventRequest) -> dict[str, Any]:
    """生成关联冲正凭证；原凭证保持不变。"""
    try:
        with SessionLocal.begin() as session:
            event_type = None
            if hasattr(session, "scalar"):
                event_type = session.scalar(
                    select(BusinessEvent.event_type).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.id == request.event_id,
                    )
                )
            if event_type in {
                "intangible_asset_acquisition",
                "intangible_asset_amortization",
                "intangible_asset_retirement",
            }:
                service = _intangible_asset_service(session)
            elif event_type in {
                "borrowing_drawdown",
                "borrowing_interest_accrual",
                "borrowing_interest_payment",
                "borrowing_principal_repayment",
            }:
                service = _borrowing_service(session)
            elif event_type in {
                "labor_remuneration_accrual",
                "unified_payout_run",
                "labor_withholding_tax_payment",
            }:
                service = _labor_remuneration_service(session)
            else:
                service = _fixed_asset_service(session)
            return service.reverse_event(request).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
@_database_error_boundary
def finance_get_event(org_id: str, event_id: str) -> dict[str, Any]:
    """读取业务事实、凭证、规则轨迹、证据及审计记录。"""
    try:
        parsed_org = uuid.UUID(org_id)
        parsed_event = uuid.UUID(event_id)
    except ValueError as exc:
        return _invalid(exc)
    with SessionLocal() as session:
        event = session.scalar(
            select(BusinessEvent)
            .options(
                selectinload(BusinessEvent.vouchers).selectinload(Voucher.lines),
                selectinload(BusinessEvent.evidence),
            )
            .where(BusinessEvent.id == parsed_event, BusinessEvent.org_id == parsed_org)
        )
        if event is None:
            return {"status": "rejected", "errors": ["EVENT_NOT_FOUND"]}
        logs = session.scalars(
            select(AuditLog).where(AuditLog.event_id == event.id).order_by(AuditLog.created_at)
        ).all()

        # A reversal chain is a relational fact: ``reversed_by_event_id`` is
        # the canonical source and a final reversal's PEL/evidence edges are
        # read from their normalized tables.  Do not reconstruct this graph
        # from ``facts`` or from a human description.
        predecessor: BusinessEvent | None = None
        current = event
        backwards: list[BusinessEvent] = []
        for _ in range(32):
            predecessor = session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == parsed_org,
                    BusinessEvent.reversed_by_event_id == current.id,
                )
            )
            if predecessor is None:
                break
            backwards.append(predecessor)
            current = predecessor
        forwards: list[BusinessEvent] = []
        current = event
        for _ in range(32):
            if current.reversed_by_event_id is None:
                break
            successor = session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == parsed_org,
                    BusinessEvent.id == current.reversed_by_event_id,
                )
            )
            if successor is None:
                break
            forwards.append(successor)
            current = successor
        chain_events = [*reversed(backwards), event, *forwards]
        chain_event_ids = [item.id for item in chain_events]
        reversal_parent_ids = {
            item.reversed_by_event_id: item.id
            for item in chain_events
            if item.reversed_by_event_id is not None
        }

        evidence_rows = session.execute(
            select(
                event_evidence.c.event_id,
                event_evidence.c.evidence_id,
                event_evidence.c.relation_kind,
            )
            .where(
                event_evidence.c.org_id == parsed_org,
                event_evidence.c.event_id.in_(chain_event_ids),
            )
            .order_by(
                event_evidence.c.event_id,
                event_evidence.c.relation_kind,
                event_evidence.c.evidence_id,
            )
        ).all()
        evidence_by_id = {
            item.id: item
            for item in session.scalars(
                select(Evidence).where(
                    Evidence.org_id == parsed_org,
                    Evidence.id.in_([row.evidence_id for row in evidence_rows]),
                )
            ).all()
        }
        payroll_links = session.scalars(
            select(PayrollEventLink)
            .where(
                PayrollEventLink.org_id == parsed_org,
                PayrollEventLink.event_id.in_(chain_event_ids),
            )
            .order_by(
                PayrollEventLink.event_id,
                PayrollEventLink.link_kind,
                PayrollEventLink.source_payment_event_id,
                PayrollEventLink.source_open_item_id,
                PayrollEventLink.id,
            )
        ).all()
        batches_by_id = {
            batch.id: batch
            for batch in session.scalars(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == parsed_org,
                    PayrollBatch.id.in_([link.payroll_batch_id for link in payroll_links]),
                )
            ).all()
        }
        source_items_by_id = {
            item.id: item
            for item in session.scalars(
                select(OpenItem).where(
                    OpenItem.org_id == parsed_org,
                    OpenItem.id.in_(
                        [
                            link.source_open_item_id
                            for link in payroll_links
                            if link.source_open_item_id is not None
                        ]
                    ),
                )
            ).all()
        }
        source_events_by_id = {
            source_event.id: source_event
            for source_event in session.scalars(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == parsed_org,
                    BusinessEvent.id.in_(
                        [
                            link.source_payment_event_id
                            for link in payroll_links
                            if link.source_payment_event_id is not None
                        ]
                    ),
                )
            ).all()
        }

        def event_summary(item: BusinessEvent) -> dict[str, Any]:
            return {
                "id": str(item.id),
                "event_type": item.event_type,
                "status": item.status,
                "business_date": item.business_date.isoformat(),
                "payment_date": item.payment_date.isoformat() if item.payment_date else None,
                "posting_date": item.posting_date.isoformat(),
                "reversal_of_event_id": (
                    str(reversal_parent_ids[item.id]) if item.id in reversal_parent_ids else None
                ),
                "reversed_by_event_id": (
                    str(item.reversed_by_event_id) if item.reversed_by_event_id else None
                ),
            }

        def payroll_link_projection(link: PayrollEventLink) -> dict[str, Any]:
            batch = batches_by_id.get(link.payroll_batch_id)
            source_item = source_items_by_id.get(link.source_open_item_id)
            source_event = source_events_by_id.get(link.source_payment_event_id)
            return {
                "id": str(link.id),
                "event_id": str(link.event_id),
                "link_kind": link.link_kind,
                "payroll_batch_id": str(link.payroll_batch_id),
                "payroll_batch": (
                    {
                        "batch_kind": batch.batch_kind,
                        "payroll_period": batch.payroll_period,
                        "policy_version_id": str(batch.policy_version_id),
                        "reversal_of_batch_id": (
                            str(batch.reversal_of_batch_id) if batch.reversal_of_batch_id else None
                        ),
                    }
                    if batch is not None
                    else None
                ),
                "source_payment_event_id": (
                    str(link.source_payment_event_id) if link.source_payment_event_id else None
                ),
                "source_payment_event": (
                    {
                        "event_type": source_event.event_type,
                        "status": source_event.status,
                        "reversed_by_event_id": (
                            str(source_event.reversed_by_event_id)
                            if source_event.reversed_by_event_id
                            else None
                        ),
                    }
                    if source_event is not None
                    else None
                ),
                "source_open_item_id": (
                    str(link.source_open_item_id) if link.source_open_item_id else None
                ),
                "source_open_item": (
                    {
                        "payable_category": source_item.payable_category,
                        "payable_agency_code": source_item.payable_agency_code,
                        "insurance_kind": source_item.insurance_kind,
                        "source_event_id": str(source_item.source_event_id),
                    }
                    if source_item is not None
                    else None
                ),
            }

        evidence_projection = [
            {
                "event_id": str(row.event_id),
                "id": str(row.evidence_id),
                "relation_kind": row.relation_kind,
                "sha256": evidence_by_id[row.evidence_id].sha256,
                "original_name": evidence_by_id[row.evidence_id].original_name,
                "source": evidence_by_id[row.evidence_id].source,
                "size_bytes": evidence_by_id[row.evidence_id].size_bytes,
            }
            for row in evidence_rows
            if row.evidence_id in evidence_by_id
        ]
        return {
            "status": "ok",
            "event": {
                "id": str(event.id),
                "event_type": event.event_type,
                "event_status": event.status,
                "description": event.description,
                "facts": event.facts,
                "business_date": event.business_date.isoformat(),
                "fulfillment_date": (
                    event.fulfillment_date.isoformat() if event.fulfillment_date else None
                ),
                "invoice_date": event.invoice_date.isoformat() if event.invoice_date else None,
                "payment_date": event.payment_date.isoformat() if event.payment_date else None,
                "tax_obligation_date": (
                    event.tax_obligation_date.isoformat() if event.tax_obligation_date else None
                ),
                "posting_date": event.posting_date.isoformat(),
                "reversed_by_event_id": (
                    str(event.reversed_by_event_id) if event.reversed_by_event_id else None
                ),
                "trace": event.rule_trace,
                "rule_version": event.rule_version,
            },
            "vouchers": [
                {
                    "id": str(voucher.id),
                    "number": voucher.voucher_number,
                    "posting_date": voucher.posting_date.isoformat(),
                    "reversal_of_voucher_id": (
                        str(voucher.reversal_of_voucher_id)
                        if voucher.reversal_of_voucher_id
                        else None
                    ),
                    "lines": [
                        {
                            "line_number": line.line_number,
                            "account_code": line.account.code,
                            "account_name": line.account.name,
                            "debit_fen": line.debit_fen,
                            "credit_fen": line.credit_fen,
                            "counterparty_id": (
                                str(line.counterparty_id) if line.counterparty_id else None
                            ),
                            "memo": line.memo,
                        }
                        for line in voucher.lines
                    ],
                }
                for voucher in event.vouchers
            ],
            "evidence": [
                {
                    "id": str(item.id),
                    "sha256": item.sha256,
                    "original_name": item.original_name,
                    "source": item.source,
                    "size_bytes": item.size_bytes,
                }
                for item in event.evidence
            ],
            "canonical_reversal_chain": {
                "root_event_id": str(chain_events[0].id),
                "terminal_event_id": str(chain_events[-1].id),
                "events": [event_summary(item) for item in chain_events],
                "event_evidence": evidence_projection,
                "payroll_event_links": [payroll_link_projection(link) for link in payroll_links],
            },
            "audit_log": [
                {
                    "action": log.action,
                    "actor": log.actor,
                    "details": log.details,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }


_secure_registered_data_tools()
_make_all_tool_inputs_strict()
_sanitize_tool_errors(
    *(tool.name for tool in mcp._tool_manager.list_tools()),
)


def main() -> None:
    settings = get_settings()
    if settings.finance_environment == "production":
        # The legacy path-based CSV/XLSX writer remains importable for isolated
        # development regression only.  It must not appear in a formal server's
        # tool registry after DEC-020/040 introduced preview-confirm CSV input.
        legacy_bank_tool = mcp._tool_manager._tools.pop("finance_import_bank_statement", None)
        try:
            with acquire_windows_service_lease(
                settings.finance_service_lock_file,
                mode="service",
                access_verifier=WindowsCurrentUserOnlyAclVerifier(),
            ):
                try:
                    _initialize_mcp_credential_store(
                        environment=settings.finance_environment
                    )
                    mcp.run(transport="stdio")
                finally:
                    _clear_mcp_credential_store()
        finally:
            # ``mcp.run`` normally owns the process lifetime.  Restoring only
            # supports in-process tests or an orderly stopped development host;
            # the live production registry never contains this legacy tool.
            if legacy_bank_tool is not None:
                mcp._tool_manager._tools["finance_import_bank_statement"] = legacy_bank_tool
        return
    try:
        _initialize_mcp_credential_store(environment=settings.finance_environment)
        mcp.run(transport="stdio")
    finally:
        _clear_mcp_credential_store()


if __name__ == "__main__":
    main()
