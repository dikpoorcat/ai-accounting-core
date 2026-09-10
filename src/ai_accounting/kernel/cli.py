"""Standalone local runtime: finance-local --root DIR call COMMAND --input request.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import KernelError
from .service import LocalService


def main():
    parser = argparse.ArgumentParser(description="本地 SQLite 确定性会计内核")
    parser.add_argument("--root", type=Path, default=Path("data/local-kernel"))
    commands = parser.add_subparsers(dest="mode", required=True)
    call = commands.add_parser("call", help="执行类型化业务命令")
    call.add_argument("command")
    call.add_argument("--input", type=Path)
    commands.add_parser("mcp", help="以本地负责人进程身份提供 stdio MCP")
    web = commands.add_parser("serve", help="提供本机只读会计界面")
    web.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.mode == "mcp":
        from .mcp import serve

        serve(args.root)
        return
    service = LocalService(args.root)
    if args.mode == "serve":
        from .http import serve

        serve(service, port=args.port)
        return
    payload = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {}
    try:
        response = service.dispatch(args.command, payload)
    except KernelError as exc:
        response = exc.response()
    except (ValueError, TypeError, KeyError) as exc:
        response = {"status": "rejected", "code": "invalid_command", "message": str(exc)}
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    if isinstance(response, dict) and response.get("status") == "rejected":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
