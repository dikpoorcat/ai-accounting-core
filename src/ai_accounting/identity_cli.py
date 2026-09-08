"""Safe CLI launchers for the native owner-security form; no terminal secret input."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import get_args

from .config import get_settings
from .identity import IdentityError
from .owner_login_launcher import OwnerSecurityWindowLauncher
from .owner_security import OwnerSecurityOperations, OwnerSecurityWindowRequest, SecurityKind

_ALIASES = {
    "setup": "bootstrap_owner",
    "login": "login",
    "recover": "recover",
    "change-password": "change_password",
    "replace-recovery-code": "replace_recovery_code",
    "approve-close": "approve_period_close",
    "approve-close-window": "approve_period_close",
}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid argument values, which may be secrets.
        self.exit(2, "IDENTITY_LOCAL_COMMAND_INVALID\n")


def main() -> None:
    parser = SafeArgumentParser(description="Local native owner-security window")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in [*_ALIASES, "security-window"]:
        command = commands.add_parser(name, help="open the native security window")
        if name == "security-window":
            command.add_argument("--kind", choices=get_args(SecurityKind), required=True)
        command.add_argument("--org-id", type=uuid.UUID)
        command.add_argument("--login-name")
        command.add_argument("--period-id", type=uuid.UUID)
        command.add_argument("--calculation-hash")
    status = commands.add_parser("security-window-status")
    status.add_argument("--request-id", type=uuid.UUID, required=True)
    commands.add_parser("logout")
    args = parser.parse_args()
    try:
        operations = OwnerSecurityOperations(settings=get_settings())
        launcher = OwnerSecurityWindowLauncher(operations=operations)
        if args.command == "logout":
            operations.logout()
            print("LOGOUT_SUCCEEDED")
            return
        elif args.command == "security-window-status":
            result = launcher.status(args.request_id)
        else:
            request = OwnerSecurityWindowRequest(
                kind=args.kind if args.command == "security-window" else _ALIASES[args.command],
                org_id=args.org_id,
                login_name=args.login_name,
                period_id=args.period_id,
                calculation_hash=args.calculation_hash,
            )
            result = launcher.request(request)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("status") == "failed":
            raise SystemExit(1)
    except Exception as exc:
        code = exc.code if isinstance(exc, IdentityError) else "IDENTITY_LOCAL_COMMAND_FAILED"
        print(code, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
