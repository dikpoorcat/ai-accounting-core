"""No-echo local owner setup, login, recovery, and logout commands."""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import select

from .accounting_period_schemas import PreviewAccountingPeriodCloseRequest
from .accounting_period_service import AccountingPeriodService
from .credential_store import WindowsCredentialStore
from .database import SessionLocal
from .identity import IdentityError, validate_password_for_login
from .identity_schemas import (
    OwnerLoginRequest,
    OwnerPasswordChangeRequest,
    OwnerProvisionRequest,
    OwnerRecoveryCodeReplacementRequest,
    OwnerRecoveryResetRequest,
    OwnerSessionRevokeRequest,
)
from .identity_service import IdentityService
from .models import AccountingPeriod, AccountingPeriodCloseApproval, OwnerAccount


def main() -> None:
    parser = argparse.ArgumentParser(description="Local single-owner authentication")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="create the one local owner")
    setup.add_argument("--org-id", required=True, type=uuid.UUID)
    setup.add_argument("--login-name", required=True)
    login = commands.add_parser("login", help="authenticate and store an opaque local session")
    login.add_argument("--login-name", required=True)
    recover = commands.add_parser("recover", help="reset password with the one recovery code")
    recover.add_argument("--login-name", required=True)
    commands.add_parser("change-password", help="change password for the local session")
    commands.add_parser("replace-recovery-code", help="replace the one recovery code")
    approve_close = commands.add_parser(
        "approve-close",
        help="reauthenticate and approve one exact accounting-period close preview",
    )
    approve_close.add_argument("--org-id", required=True, type=uuid.UUID)
    approve_close.add_argument("--period-id", required=True, type=uuid.UUID)
    approve_close.add_argument("--calculation-hash", required=True)
    approve_close.add_argument("--login-name", required=True)
    commands.add_parser("logout", help="revoke and remove the local session")
    args = parser.parse_args()

    try:
        if args.command == "setup":
            _setup(args)
        elif args.command == "login":
            _login(args)
        elif args.command == "recover":
            _recover(args)
        elif args.command == "change-password":
            _change_password()
        elif args.command == "replace-recovery-code":
            _replace_recovery_code()
        elif args.command == "approve-close":
            _approve_close(args)
        else:
            _logout()
    except Exception as exc:
        # Never expose database URLs, passwords, or source exception text from
        # this local boundary.  IdentityError is already a stable safe code.
        code = exc.code if isinstance(exc, IdentityError) else "IDENTITY_LOCAL_COMMAND_FAILED"
        print(code, file=sys.stderr)
        raise SystemExit(1) from None


def _setup(args: argparse.Namespace) -> None:
    password = validate_password_for_login(
        password=_new_password(),
        login_name=args.login_name,
    )
    with SessionLocal.begin() as session:
        result = IdentityService(session).provision_owner(
            OwnerProvisionRequest(
                org_id=args.org_id,
                login_name=args.login_name,
                password=SecretStr(password),
            )
        )
    _print_recovery_code(result.recovery_code)


def _login(args: argparse.Namespace) -> None:
    password = _secret_prompt("Password: ")
    store = WindowsCredentialStore()
    previous_token = store.load_session_token()
    try:
        failure: IdentityError | None = None
        with SessionLocal.begin() as session:
            service = IdentityService(session)
            try:
                result = service.authenticate(
                    OwnerLoginRequest(login_name=args.login_name, password=SecretStr(password))
                )
            except IdentityError as exc:
                failure = exc
            else:
                store.save_session_token(result.session_token)
                if previous_token is not None:
                    service.revoke_session(OwnerSessionRevokeRequest(session_token=previous_token))
        if failure is not None:
            raise failure
    except Exception:
        _restore_previous_token(store, previous_token)
        raise
    print("LOGIN_SUCCEEDED")


def _recover(args: argparse.Namespace) -> None:
    recovery_code = _secret_prompt("Recovery code: ")
    password = _new_password()
    result = _run_identity_operation(
        lambda service: service.reset_password_with_recovery(
            OwnerRecoveryResetRequest(
                login_name=args.login_name,
                recovery_code=SecretStr(recovery_code),
                new_password=SecretStr(password),
            )
        )
    )
    WindowsCredentialStore().delete_session_token()
    _print_recovery_code(result.recovery_code)


def _change_password() -> None:
    store = WindowsCredentialStore()
    token = _required_local_token(store)
    current_password = _secret_prompt("Current password: ")
    new_password = _new_password()
    result = _run_identity_operation(
        lambda service: service.change_password(
            OwnerPasswordChangeRequest(
                session_token=token,
                current_password=SecretStr(current_password),
                new_password=SecretStr(new_password),
            )
        )
    )
    store.delete_session_token()
    _print_recovery_code(result.recovery_code)
    print("PASSWORD_CHANGED_LOGIN_REQUIRED")


def _replace_recovery_code() -> None:
    store = WindowsCredentialStore()
    token = _required_local_token(store)
    result = _run_identity_operation(
        lambda service: service.replace_recovery_code(
            OwnerRecoveryCodeReplacementRequest(session_token=token)
        )
    )
    _print_recovery_code(result.recovery_code)


def _approve_close(args: argparse.Namespace) -> None:
    if len(args.calculation_hash) != 64 or any(
        character not in "0123456789abcdef" for character in args.calculation_hash
    ):
        raise IdentityError("ACCOUNTING_PERIOD_CALCULATION_HASH_INVALID")
    with SessionLocal() as session:
        period = session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == args.org_id,
                AccountingPeriod.id == args.period_id,
            )
        )
        if period is None or period.status != "open":
            raise IdentityError("ACCOUNTING_PERIOD_NOT_OPEN")
        preview = AccountingPeriodService(session).preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=args.org_id,
                period_id=args.period_id,
                closing_date=period.end_date,
            )
        )
        if (
            preview.status.value != "calculated"
            or preview.calculation_hash != args.calculation_hash
        ):
            raise IdentityError("ACCOUNTING_PERIOD_CALCULATION_STALE")
        period_month = f"{period.calendar_year:04d}-{period.calendar_month:02d}"
    print(f"CLOSE_APPROVAL_PERIOD={period_month}")
    print(f"CLOSE_APPROVAL_HASH={args.calculation_hash}")
    print("请核对以上关账月份和预览哈希，然后输入负责人密码确认。")
    password = _secret_prompt("Password: ")
    store = WindowsCredentialStore()
    previous_token = store.load_session_token()
    new_token: SecretStr | None = None
    approval: AccountingPeriodCloseApproval | None = None
    try:
        with SessionLocal.begin() as session:
            identity = IdentityService(session).authenticate(
                OwnerLoginRequest(
                    login_name=args.login_name,
                    password=SecretStr(password),
                )
            )
            if identity.owner_account_id is None:
                raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
            period = session.scalar(
                select(AccountingPeriod)
                .where(
                    AccountingPeriod.org_id == args.org_id,
                    AccountingPeriod.id == args.period_id,
                )
                .with_for_update()
            )
            if period is None or period.status != "open":
                raise IdentityError("ACCOUNTING_PERIOD_NOT_OPEN")
            preview = AccountingPeriodService(session).preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=args.org_id,
                    period_id=args.period_id,
                    closing_date=period.end_date,
                )
            )
            if (
                preview.status.value != "calculated"
                or preview.calculation_hash != args.calculation_hash
            ):
                raise IdentityError("ACCOUNTING_PERIOD_CALCULATION_STALE")
            account = session.get(OwnerAccount, identity.owner_account_id)
            if account is None or account.org_id != args.org_id:
                raise IdentityError("ORGANIZATION_CONTEXT_MISMATCH")
            now = datetime.now(UTC)
            approval = AccountingPeriodCloseApproval(
                org_id=args.org_id,
                period_id=period.id,
                owner_account_id=identity.owner_account_id,
                owner_session_id=identity.session_id,
                owner_credential_version=account.credential_version,
                calculation_hash=args.calculation_hash,
                confirmation_method="local_password_reauthentication",
                confirmed_at=now,
                expires_at=now + timedelta(minutes=30),
            )
            session.add(approval)
            session.flush()
            new_token = identity.session_token
        assert approval is not None and new_token is not None
        store.save_session_token(new_token)
    except Exception:
        _restore_previous_token(store, previous_token)
        raise
    print("CLOSE_APPROVAL_CREATED")
    print(f"CLOSE_APPROVAL_ID={approval.id}")
    print(f"CLOSE_APPROVAL_PERIOD={period_month}")
    print(f"CLOSE_APPROVAL_EXPIRES_AT={approval.expires_at.isoformat()}")


def _logout() -> None:
    store = WindowsCredentialStore()
    token = store.load_session_token()
    if token is not None:
        with SessionLocal.begin() as session:
            IdentityService(session).revoke_session(OwnerSessionRevokeRequest(session_token=token))
    store.delete_session_token()
    print("LOGOUT_SUCCEEDED")


def _required_local_token(store: WindowsCredentialStore) -> SecretStr:
    token = store.load_session_token()
    if token is None:
        raise IdentityError("IDENTITY_LOCAL_SESSION_REQUIRED")
    return token


def _restore_previous_token(store: WindowsCredentialStore, token: SecretStr | None) -> None:
    if token is None:
        store.delete_session_token()
    else:
        store.save_session_token(token)


def _run_identity_operation[ResultT](
    operation: Callable[[IdentityService], ResultT],
) -> ResultT:
    """Commit expected security-state changes before surfacing a stable failure."""

    failure: IdentityError | None = None
    result: ResultT | None = None
    with SessionLocal.begin() as session:
        try:
            result = operation(IdentityService(session))
        except IdentityError as exc:
            failure = exc
    if failure is not None:
        raise failure
    assert result is not None
    return result


def _new_password() -> str:
    first = _secret_prompt("New password: ")
    second = _secret_prompt("Repeat new password: ")
    if first != second:
        raise IdentityError("IDENTITY_PASSWORD_CONFIRMATION_MISMATCH")
    return first


def _secret_prompt(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise IdentityError("IDENTITY_SECRET_INPUT_UNAVAILABLE") from exc


def _print_recovery_code(code: SecretStr) -> None:
    # This is the owner-facing one-time display.  It is not an application log
    # and callers must write it down before the terminal is closed.
    print("RECOVERY_CODE_WRITE_DOWN_NOW")
    print(code.get_secret_value())


if __name__ == "__main__":
    main()
