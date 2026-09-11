"""Standalone local runtime: finance-local --root DIR call COMMAND --input request.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .daemon import ServiceClient, default_root
from .diagnostics import error_response


def main():
    parser = argparse.ArgumentParser(description="本地 SQLite 确定性会计内核")
    parser.add_argument("--root", type=Path, default=default_root())
    commands = parser.add_subparsers(dest="mode", required=True)
    call = commands.add_parser("call", help="执行类型化业务命令")
    call.add_argument("command")
    call.add_argument("--input", type=Path)
    commands.add_parser("mcp", help="通过本地服务提供 stdio MCP")
    commands.add_parser("stop", help="安全停止此资料目录的本地服务")
    daemon = commands.add_parser("daemon", help="运行每个资料根目录唯一的本地服务")
    daemon.add_argument("--port", type=int, default=0)
    web = commands.add_parser("serve", help="启动本地服务并打开会计界面")
    web.add_argument("--port", type=int, default=0)
    security = commands.add_parser("security", help="请求本机安全窗口；密码只在窗口输入")
    security.add_argument(
        "operation", choices=("login", "change_password", "recover", "setup", "close", "status")
    )
    security.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.mode == "mcp":
        from .mcp import serve

        serve(args.root)
        return
    if args.mode == "stop":
        from .daemon import stop_service

        try:
            print(json.dumps(stop_service(args.root), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps(error_response(exc), ensure_ascii=False))
            raise SystemExit(1) from None
        return
    if args.mode == "daemon":
        from .daemon import run

        try:
            run(args.root, port=args.port)
        except Exception as exc:
            print(json.dumps(error_response(exc), ensure_ascii=False))
            raise SystemExit(1) from None
        return
    try:
        payload = (
            json.loads(args.input.read_text(encoding="utf-8"))
            if getattr(args, "input", None)
            else {}
        )
        service = ServiceClient(args.root)
        if args.mode == "security":
            kind = {"setup": "bootstrap_owner", "close": "approve_period_close"}.get(
                args.operation, args.operation
            )
            response = (
                service.security("session_status", {})
                if args.operation == "status"
                else service.security("request", {"kind": kind, **payload})
            )
        elif args.mode == "serve":
            import webbrowser

            result = service.browser_url()
            if "url" in result:
                webbrowser.open(result["url"])
                response = {"status": "opened", "message": "会计界面已打开"}
            else:
                response = service.security("request", {"kind": "login"})
        else:
            response = service.dispatch(args.command, payload)
    except Exception as exc:
        response = error_response(exc)
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    if isinstance(response, dict) and response.get("status") == "rejected":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
