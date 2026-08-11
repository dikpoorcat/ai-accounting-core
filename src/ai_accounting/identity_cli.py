"""No-echo local owner setup, login, recovery, and logout commands."""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from collections.abc import Callable

from pydantic import SecretStr

from .credential_store import WindowsCredentialStore
from .database import SessionLocal
from .identity import IdentityError
from .identity_schemas import (
    OwnerLoginRequest,
    OwnerPasswordChangeRequest,
    OwnerProvisionRequest,
    OwnerRecoveryCodeReplacementRequest,
    OwnerRecoveryResetRequest,
    OwnerSessionRevokeRequest,
)
from .identity_service import IdentityService


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
        else:
            _logout()
    except Exception as exc:
        # Never expose database URLs, passwords, or source exception text from
        # this local boundary.  IdentityError is already a stable safe code.
        code = exc.code if isinstance(exc, IdentityError) else "IDENTITY_LOCAL_COMMAND_FAILED"
        print(code, file=sys.stderr)
        raise SystemExit(1) from None


def _setup(args: argparse.Namespace) -> None:
    password = _new_password()
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
